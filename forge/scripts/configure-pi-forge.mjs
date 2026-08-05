#!/usr/bin/env node

import { chmodSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { isAbsolute, join, resolve } from "node:path";
import {
	LEGACY_CHAT_SCHEDULING,
	LEGACY_CHAT_SERVICE,
	SLOT_CONTEXT_TOKENS,
	seedConnectedServicesSettings,
} from "../lib/connected-services.mjs";
import { ensureMoshiHook, formatMoshiHookNotice } from "../lib/moshi-hook.mjs";
import { capacityForUrl, readSnapshot } from "../lib/stack-state.mjs";

// Shared limits for the local code and chat variants. Kept here so every
// install and `pi-forge-update` writes the same context and output budgets.
// Both variants are served by one llama-server with two slots, so the window
// is the per-slot size, not the pool: declaring the pool made compaction
// trigger at 196608, well past the point where a request is refused.
//
// `SLOT_CONTEXT_TOKENS` is the fallback, not a fact about the deployment. Where
// the stack publishes a state API, the real per-slot size is read from it below
// — otherwise a backend reconfigured to a different context leaves five numbers
// wrong at once (both providers' contextWindow, the compaction reserve, and
// both services' contextTokens) with nothing to catch it.
const MAX_OUTPUT_TOKENS = 32768;
const COMPACTION_TRIGGER_RATIO = 0.75;
const CONTEXT_BUDGET_SOFT_RATIO = COMPACTION_TRIGGER_RATIO;
const CONTEXT_BUDGET_VERBATIM_RECENT_TOKENS = 20000;
// Matches `MAX_OWNER_FIELD_CHARS` in forge/lib/vault_profile.py and the same
// constant in forge/extensions/vault-context.ts: a name, not a biography.
const MAX_IDENTITY_FIELD_CHARS = 40;
// Matches `INBOX_DIR` in forge/lib/vault_schema.py, where it is a constant
// rather than a schema registry row: the inbox is the one route a vault cannot
// renumber. Stated here so the profile can name an absolute path.
const INBOX_DIR = "00 Inbox";

const [agentDirectoryArgument, profileDirectoryArgument] = process.argv.slice(2);
if (!agentDirectoryArgument || !profileDirectoryArgument) {
	console.error("Usage: configure-pi-forge.mjs <agent-directory> <profile-directory>");
	process.exit(2);
}

const agentDirectory = resolve(agentDirectoryArgument);
const profileDirectory = resolve(profileDirectoryArgument);
const settingsPath = join(agentDirectory, "settings.json");
const modelsPath = join(agentDirectory, "models.json");
const profilePathMarker = join(agentDirectory, ".pi-forge-profile-path");
const sourceAgentsPath = join(profileDirectory, "AGENTS.md");
const installedAgentsPath = join(agentDirectory, "AGENTS.md");
mkdirSync(agentDirectory, { recursive: true });
mkdirSync(join(agentDirectory, "sessions"), { recursive: true });

let settings = {};
try {
	settings = JSON.parse(readFileSync(settingsPath, "utf8"));
} catch (error) {
	if (error?.code !== "ENOENT") {
		throw new Error(`Cannot read ${settingsPath}: ${error.message}`);
	}
}

if (settings === null || Array.isArray(settings) || typeof settings !== "object") {
	throw new Error(`${settingsPath} must contain a JSON object`);
}

let previousProfileDirectory;
try {
	previousProfileDirectory = readFileSync(profilePathMarker, "utf8").trim();
} catch (error) {
	if (error?.code !== "ENOENT") throw error;
}

// Captured before seeding, which fills every field in and would make a value
// this install just defaulted indistinguishable from one the owner chose.
const persistedServices = structuredClone(
	settings.connectedServices && typeof settings.connectedServices === "object" && !Array.isArray(settings.connectedServices)
		? settings.connectedServices
		: {},
);

const packages = Array.isArray(settings.packages) ? settings.packages : [];
const retainedPackages = packages.filter((entry) => {
	if (!previousProfileDirectory) return true;
	if (typeof entry === "string") return resolve(entry) !== previousProfileDirectory;
	return typeof entry?.source !== "string" || resolve(entry.source) !== previousProfileDirectory;
});

const profileInstructions =
	readFileSync(sourceAgentsPath, "utf8") + identityBlock(settings.forgeUser) + vaultBlock(settings.forgeVault);
settings.packages = [profileDirectory, ...retainedPackages];
settings.defaultProvider = "forge-local";
settings.defaultModel = "code";
// Forge is a knowledge-work profile, not a profile for developing pi itself. The
// pointer block to pi's README/docs/examples costs ~300 tokens of launch context on
// every session; the package path is still named in the system prompt on demand.
settings.includePiDocs = false;
const existingCompaction =
	settings.compaction !== null && typeof settings.compaction === "object" && !Array.isArray(settings.compaction)
		? settings.compaction
		: {};
const existingContextBudget =
	settings.contextBudget !== null && typeof settings.contextBudget === "object" && !Array.isArray(settings.contextBudget)
		? settings.contextBudget
		: {};
delete settings.taskModel;
settings.contextBudget = {
	...existingContextBudget,
	enabled: true,
	softRatio: CONTEXT_BUDGET_SOFT_RATIO,
	useTaskModel: false,
	verbatimRecentTokens: CONTEXT_BUDGET_VERBATIM_RECENT_TOKENS,
};
migrateLegacyChatService(settings);
migrateLegacyChatScheduling(settings);
const services = seedConnectedServicesSettings(settings);

// Everything below this point may depend on what the deployment actually
// serves, so the one read happens here. A stack that cannot be reached returns
// null and every value falls back to the built-in constant, which is the path
// every install without this API takes.
const snapshot = await readSnapshot({ settings: services });
const chatCapacity = capacityForUrl(snapshot, services.chat.baseUrl);
const thinkCapacity = capacityForUrl(snapshot, services.think.baseUrl);
adoptServedContext(services.chat, persistedServices.chat, chatCapacity, "connectedServices.chat");
adoptServedContext(services.think, persistedServices.think, thinkCapacity, "connectedServices.think");
clampBackgroundSlot(services.chat, chatCapacity, "connectedServices.chat");
clampBackgroundSlot(services.think, thinkCapacity, "connectedServices.think");

// The agent's own window follows the endpoint it actually talks to, which is
// the thinking provider. Both providers front one backend today, so this is
// normally the same number as the chat side.
const contextWindow = services.think.contextTokens || SLOT_CONTEXT_TOKENS;
const chatContextWindow = services.chat.contextTokens || SLOT_CONTEXT_TOKENS;
settings.compaction = {
	...existingCompaction,
	enabled: true,
	reserveTokens: contextWindow - Math.floor(contextWindow * COMPACTION_TRIGGER_RATIO),
};

let models = {};
try {
	models = JSON.parse(readFileSync(modelsPath, "utf8"));
} catch (error) {
	if (error?.code !== "ENOENT") {
		throw new Error(`Cannot read ${modelsPath}: ${error.message}`);
	}
}
if (models === null || Array.isArray(models) || typeof models !== "object") {
	throw new Error(`${modelsPath} must contain a JSON object`);
}
if (models.providers !== undefined && (models.providers === null || Array.isArray(models.providers) || typeof models.providers !== "object")) {
	throw new Error(`${modelsPath} providers must contain a JSON object`);
}
models.providers = models.providers ?? {};
delete models.providers["forge-task-local"];
models.providers["forge-local"] = {
	baseUrl: "http://llms:8008/v1",
	api: "openai-completions",
	apiKey: "local",
	compat: {
		supportsDeveloperRole: false,
		supportsReasoningEffort: false,
		maxTokensField: "max_tokens",
		// The served Qwen model emits <think>...</think> in its content; parse
		// it as reasoning so raw think tags do not leak into displayed output
		// (and the vault-workflow execute-phase prefill stays invisible).
		thinkingFormat: "qwen",
	},
	models: [
		{
			id: "code",
			name: "Code (Local)",
			reasoning: false,
			input: ["text"],
			cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
			contextWindow,
			maxTokens: MAX_OUTPUT_TOKENS,
		},
	],
};
models.providers["forge-chat-local"] = {
	baseUrl: "http://llms:8004/v1",
	api: "openai-completions",
	apiKey: "local",
	compat: {
		supportsDeveloperRole: false,
		supportsReasoningEffort: false,
		maxTokensField: "max_tokens",
	},
	models: [
		{
			id: "chat",
			name: "Chat (Local, Non-Thinking)",
			reasoning: false,
			input: ["text"],
			cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
			contextWindow: chatContextWindow,
			maxTokens: MAX_OUTPUT_TOKENS,
		},
	],
};
writeFileSync(settingsPath, `${JSON.stringify(settings, undefined, "\t")}\n`, { mode: 0o600 });
writeFileSync(modelsPath, `${JSON.stringify(models, undefined, "\t")}\n`, { mode: 0o600 });
writeFileSync(installedAgentsPath, profileInstructions, { mode: 0o600 });
chmodSync(installedAgentsPath, 0o600);
writeFileSync(profilePathMarker, `${profileDirectory}\n`, { mode: 0o600 });

// Optional host integration, so it runs after the managed configuration is
// written and can never leave that half-done. Moshi is absent on most machines;
// there the whole step is silent.
try {
	const notice = formatMoshiHookNotice(ensureMoshiHook({ agentDir: agentDirectory }));
	for (const line of notice.out) process.stdout.write(`${line}\n`);
	for (const line of notice.err) process.stderr.write(`${line}\n`);
} catch (error) {
	process.stderr.write(`Moshi hook: ${error.message}\n`);
}

/**
 * Point a service's context ceiling at what its backend actually serves.
 *
 * Only a value this install defaulted is replaced. A `contextTokens` that
 * differs from the built-in constant was typed by someone — most likely to aim
 * a service at a smaller backend than this one — and a probe must not quietly
 * undo that. Same rule as `migrateLegacyChatService` below: byte-equal to the
 * old default means "written by an installer", anything else means "chosen".
 *
 * Getting this wrong is not cosmetic in either direction. Too low wastes most
 * of the window on every call; too high means a skill sends a prompt this
 * client believes fits and reads the server's rejection as the model failing.
 */
function adoptServedContext(service, persisted, capacity, label) {
	if (!capacity?.contextTokens || capacity.contextTokens === service.contextTokens) return;
	const chosen = persisted?.contextTokens;
	if (Number.isInteger(chosen) && chosen > 0 && chosen !== SLOT_CONTEXT_TOKENS) {
		process.stderr.write(
			`${label}: the backend serves ${capacity.contextTokens} context tokens per slot but this install sets ` +
				`${chosen}; leaving your value alone.\n`,
		);
		return;
	}
	process.stdout.write(`${label}: read ${capacity.contextTokens} context tokens per slot from the stack (was ${service.contextTokens}).\n`);
	service.contextTokens = capacity.contextTokens;
}

/**
 * Keep the pinned background slot inside the range the backend actually has.
 *
 * Background work pins a slot so bulk calls cannot evict the interactive
 * session's prefix cache. A backend running one slot has no slot 1, so the pin
 * names something that does not exist — and nothing else in forge would ever
 * notice, because the number is only ever sent, never checked.
 */
function clampBackgroundSlot(service, capacity, label) {
	const total = capacity?.totalSlots;
	if (!Number.isInteger(total) || total < 1) return;
	const scheduling = service.scheduling;
	if (!scheduling?.enabled || !Number.isInteger(scheduling.backgroundSlot) || scheduling.backgroundSlot < total) return;
	const clamped = total - 1;
	process.stderr.write(
		`${label}: the backend runs ${total} slot${total === 1 ? "" : "s"}, so background work cannot pin slot ` +
			`${scheduling.backgroundSlot}; using slot ${clamped}.\n`,
	);
	scheduling.backgroundSlot = clamped;
	if (scheduling.interactiveSlot >= total) scheduling.interactiveSlot = clamped;
}

/** One `forgeUser` field, collapsed to a single line, or "" if unusable. */
function identityField(value) {
	if (typeof value !== "string") return "";
	const text = value.replace(/\s+/g, " ").trim();
	return text.length > 0 && text.length <= MAX_IDENTITY_FIELD_CHARS ? text : "";
}

/**
 * Who this install belongs to, as a block appended to the profile instructions.
 *
 * The installed `AGENTS.md` is rewritten from the packaged profile on every
 * install and every `pi-forge-update`, so a name added to it by hand would not
 * survive the next one. `settings.json` is read, merged, and rewritten, so
 * `forgeUser` does — which makes it the one place a name can live and still be
 * there tomorrow. Inside an Obsidian vault the `vault-context` extension says
 * this from the vault's own owner record; this covers everywhere else.
 *
 * An install that declares no `forgeUser` appends nothing, which is the path
 * every install took before this existed.
 */
function identityBlock(forgeUser) {
	if (!forgeUser || typeof forgeUser !== "object" || Array.isArray(forgeUser)) return "";
	const name = identityField(forgeUser.name);
	if (!name) return "";
	const pronouns = identityField(forgeUser.pronouns);
	const who = pronouns ? `${name} (${pronouns})` : name;
	return [
		"",
		"## Who You Are Working With",
		"",
		`You are working with ${who}. Address them by name; do not call them "the user"`,
		"or \"the owner\". Inside an Obsidian vault, that vault's own owner record wins",
		"over this one.",
		"",
	].join("\n");
}

/** `forgeVault` as an absolute path, expanding a leading `~`, or "" if unusable. */
function vaultPath(forgeVault) {
	if (typeof forgeVault !== "string") return "";
	const text = forgeVault.trim();
	if (!text) return "";
	// A bare `~` is left unexpanded on purpose: the home directory is not a vault,
	// and it fails the absolute-path check below with a message naming the fix.
	const expanded = text.startsWith("~/") ? join(homedir(), text.slice(2)) : text;
	// A relative vault path would resolve against whatever directory the installer
	// happened to run from, which is never what the declaration meant.
	return isAbsolute(expanded) ? resolve(expanded) : "";
}

/**
 * Where this install's vault is, as a block appended to the profile instructions.
 *
 * Inside a vault the `vault-context` extension injects the coordinates it
 * detects by walking up for `.obsidian/`. That walk is the whole mechanism, so a
 * session started anywhere else — a code checkout, a downloads folder — has no
 * way to know the vault exists, and asking is the only honest thing left. This
 * covers that case, and lives in `settings.json` for the same reason `forgeUser`
 * does: the installed `AGENTS.md` is rewritten from the packaged profile on
 * every install and every `pi-forge-update`, so a path added to it by hand would
 * not survive the next one.
 *
 * Obsidian's own vault registry is deliberately not consulted. It records every
 * vault ever opened, marks more than one of them `open`, and orders them by last
 * use — on a machine with a scratch vault and a real one, every available signal
 * points at the wrong vault. A declaration is the only reliable answer.
 *
 * An install that declares no `forgeVault` appends nothing, which is the path
 * every install took before this existed.
 */
function vaultBlock(forgeVault) {
	if (forgeVault === undefined) return "";
	const root = vaultPath(forgeVault);
	if (!root) {
		process.stderr.write(`forgeVault must be an absolute path; ignoring ${JSON.stringify(forgeVault)}\n`);
		return "";
	}
	// A path that is not there is worse than no path: it sends every "put this in
	// my vault" request at a directory the write would silently create.
	try {
		if (!statSync(root).isDirectory()) throw new Error("not a directory");
	} catch {
		process.stderr.write(`forgeVault is not a directory; ignoring ${root}\n`);
		return "";
	}
	const inbox = join(root, INBOX_DIR);
	try {
		statSync(inbox);
	} catch {
		process.stderr.write(`forgeVault has no ${INBOX_DIR} yet; it will be created on first use: ${inbox}\n`);
	}
	return [
		"",
		"## Your Vault",
		"",
		`- Vault root: ${root}`,
		`- Inbox: ${inbox}`,
		"",
		"Use those paths when a session outside the vault is asked to put something into",
		"it, and when a vault skill needs `--vault`. Never ask where the vault is, and",
		"never guess a different one. Inside an Obsidian vault, the vault detected there",
		"wins over this one.",
		"",
	].join("\n");
}

/**
 * Bulk skills moved from the thinking backend to its non-thinking sibling.
 * Seeding preserves whatever is already persisted, so an install configured
 * before the split would keep pointing batch work at the thinking server
 * forever. Drop the chat endpoint only when it is byte-equal to the old
 * default — that value was written by an earlier install, not chosen — and
 * leave any customization alone.
 */
function migrateLegacyChatService(target) {
	const services = target.connectedServices;
	if (!services || typeof services !== "object" || Array.isArray(services)) return;
	const chat = services.chat;
	if (!chat || typeof chat !== "object" || Array.isArray(chat)) return;
	if (chat.baseUrl !== LEGACY_CHAT_SERVICE.baseUrl || chat.model !== LEGACY_CHAT_SERVICE.model) return;
	delete chat.baseUrl;
	delete chat.model;
}

/**
 * Bulk work now pins the background slot on the chat endpoint too. Earlier
 * installs seeded it off, because :8004 looked like a separate server and a
 * slot number there looked meaningless; it is the same llama-server as :8008,
 * so leaving it unpinned lets batch work land on slot 0 and evict the
 * interactive session's prefix cache. Drop the block only when it is byte-equal
 * to the old default, so a deliberate opt-out survives.
 */
function migrateLegacyChatScheduling(target) {
	const services = target.connectedServices;
	if (!services || typeof services !== "object" || Array.isArray(services)) return;
	const chat = services.chat;
	if (!chat || typeof chat !== "object" || Array.isArray(chat)) return;
	const scheduling = chat.scheduling;
	if (!scheduling || typeof scheduling !== "object" || Array.isArray(scheduling)) return;
	const keys = Object.keys(LEGACY_CHAT_SCHEDULING);
	if (Object.keys(scheduling).length !== keys.length) return;
	if (keys.some((key) => scheduling[key] !== LEGACY_CHAT_SCHEDULING[key])) return;
	delete chat.scheduling;
}

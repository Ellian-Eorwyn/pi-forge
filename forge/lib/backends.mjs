/**
 * Named backend setups, and the projection that makes one of them live.
 *
 * `backends.json` is the one human-editable file that names whole setups — where
 * embedding, OCR, transcription, the primary model, and the delegation model each
 * run — and which one is active. It is pure data. This module is the projector
 * that reads the active setup and writes its values into the two registries the
 * rest of forge actually reads:
 *
 * - `settings.json` → `connectedServices` (chat/think/delegate/embeddings/
 *   transcription/ocr), consumed by every skill and by `forge_delegate`.
 * - `models.json` → the three local providers, consumed by the interactive agent.
 *
 * Nothing downstream learns a new format: the projector speaks the exact field
 * shapes `configure-pi-forge.mjs` already writes, so a setup switch and a fresh
 * install converge on the same files. The installer seeds this file and applies
 * the active setup as its last step; the `backends.mjs` CLI and the `/backend`
 * command call `applyProfile` directly.
 *
 * Pure Node (fs/os/path only), so it loads under the extension sandbox alongside
 * `connected-services.mjs`.
 */

import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { DEFAULT_CONNECTED_SERVICES, getForgeAgentDir, SLOT_CONTEXT_TOKENS } from "./connected-services.mjs";

// The standard forge port layout in front of the primary llama-server. The three
// interactive providers and the two skill services all front one backend today;
// a setup that points somewhere else overrides `primary.host` (and `primary.ports`
// for a non-standard layout). Kept in sync with configure-pi-forge.mjs, which
// hardcodes the same numbers.
export const PRIMARY_PORTS = Object.freeze({ think: 8003, code: 8008, chat: 8004 });

// Matches COMPACTION_TRIGGER_RATIO in configure-pi-forge.mjs: the interactive
// agent compacts at this fraction of its window, so the reserve is the remainder.
// Recomputed here because a setup can change the interactive context.
const COMPACTION_TRIGGER_RATIO = 0.75;

/**
 * The setups shipped by default. `single` is the safe baseline and the active one:
 * one image-capable model on llms doing everything, delegation off — byte-for-byte
 * the behavior a fresh install had before setups existed, so seeding this file and
 * applying the active setup changes nothing until someone switches. `distributed`
 * is the two-GPU split — embedding and transcription on the local laptop, a vision
 * primary and a vision-free delegation backend on the two `llms` GPUs in parallel.
 * Both are templates: the one file is meant to be edited, and this is only where a
 * fresh install starts. `single` is listed first so it is the fallback when
 * `active` names a setup that no longer exists.
 */
export const DEFAULT_BACKENDS = Object.freeze({
	active: "single",
	profiles: Object.freeze({
		single: Object.freeze({
			// One image-capable model, no delegation, everything on llms. contextTokens
			// is the safe per-slot ceiling of the current primary; raise it to 262144
			// only against a backend actually serving that per slot (one slot, not two),
			// or the agent over-declares its window and the server refuses long prompts.
			description: "One image-capable model at 131k on llms; no delegation; embedding/transcription on llms.",
			primary: Object.freeze({ host: "http://llms", images: true, contextTokens: SLOT_CONTEXT_TOKENS }),
			delegation: Object.freeze({ enabled: false }),
			embedding: Object.freeze({ url: "http://llms:8005/v1/embeddings", model: "embed" }),
			transcription: Object.freeze({ baseUrl: "http://llms:8014", engine: "parakeet-v3" }),
			ocr: Object.freeze({ url: "http://llms:5002/glmocr/parse" }),
		}),
		distributed: Object.freeze({
			description:
				"Embedding + transcription on this laptop; a vision primary and a vision-free delegation backend " +
				"split across the two llms GPUs, running in parallel.",
			primary: Object.freeze({ host: "http://llms", images: true, contextTokens: SLOT_CONTEXT_TOKENS }),
			delegation: Object.freeze({
				enabled: true,
				baseUrl: "http://llms:8104/v1/chat/completions",
				// `chat` is what the secondary's :8104 aggregate serves; the :8104 URL
				// is what makes it the secondary rather than the primary chat on :8004.
				model: "chat",
				images: false,
				contextTokens: SLOT_CONTEXT_TOKENS,
				chatTemplateKwargs: Object.freeze({ enable_thinking: false }),
			}),
			embedding: Object.freeze({ url: "http://laptop:8005/v1/embeddings", model: "embed" }),
			transcription: Object.freeze({ baseUrl: "http://laptop:8014", engine: "parakeet-v3" }),
			ocr: Object.freeze({ url: "http://llms:5002/glmocr/parse" }),
		}),
	}),
});

/** Where the runtime backends.json lives: an explicit dir, else the forge agent dir. */
function resolveAgentDir({ env = process.env, agentDir } = {}) {
	return agentDir ?? getForgeAgentDir(env);
}

export function getBackendsPath(options = {}) {
	return join(resolveAgentDir(options), "backends.json");
}

/** The runtime backends config, or a deep copy of the shipped defaults. */
export function loadBackends(options = {}) {
	const path = getBackendsPath(options);
	if (!existsSync(path)) return structuredClone(DEFAULT_BACKENDS);
	let parsed;
	try {
		parsed = JSON.parse(readFileSync(path, "utf8"));
	} catch (error) {
		throw new Error(`Cannot read ${path}: ${error.message}`);
	}
	if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
		throw new Error(`${path} must contain a JSON object`);
	}
	if (!parsed.profiles || typeof parsed.profiles !== "object" || Array.isArray(parsed.profiles)) {
		throw new Error(`${path} must contain a "profiles" object`);
	}
	return parsed;
}

export function saveBackends(config, options = {}) {
	writeFileSync(getBackendsPath(options), `${JSON.stringify(config, undefined, "\t")}\n`, { mode: 0o600 });
}

/**
 * Write the shipped setups to backends.json when the file does not exist yet, so a
 * fresh install has the switcher on disk to edit. Returns true when it created the
 * file. Deliberately does NOT apply the active setup: the installer's own write of
 * settings.json/models.json already IS the default `single` baseline, and it reads
 * the live per-slot context size to set it — re-applying the profile here would
 * overwrite that probe with the profile's hardcoded number. Switching to another
 * setup, where the profile's values are the point, is an explicit later action.
 */
export function seedBackends(options = {}) {
	const path = getBackendsPath(options);
	if (existsSync(path)) return false;
	saveBackends(structuredClone(DEFAULT_BACKENDS), options);
	return true;
}

/** The active setup's name, defaulting to the first profile when unset/unknown. */
export function activeProfileName(config) {
	const names = Object.keys(config.profiles ?? {});
	if (typeof config.active === "string" && names.includes(config.active)) return config.active;
	return names[0];
}

function requireProfile(config, name) {
	const profile = config.profiles?.[name];
	if (!profile || typeof profile !== "object" || Array.isArray(profile)) {
		const known = Object.keys(config.profiles ?? {}).join(", ") || "(none)";
		throw new Error(`unknown setup ${JSON.stringify(name)}; known setups: ${known}`);
	}
	return profile;
}

function cleanHost(value) {
	if (typeof value !== "string" || !value.trim()) throw new Error("primary.host must be a non-empty URL");
	return value.trim().replace(/\/+$/, "");
}

function positiveInt(value, fallback) {
	return Number.isInteger(value) && value > 0 ? value : fallback;
}

/**
 * Turn one setup into the concrete values the two registries hold. Returns
 * `{ connectedServices, providers, contextWindow }` — a patch, not a full file.
 */
export function projectProfile(profile) {
	const primary = profile.primary;
	if (!primary || typeof primary !== "object" || Array.isArray(primary)) {
		throw new Error("a setup needs a primary object with at least a host");
	}
	const host = cleanHost(primary.host);
	const ports = { ...PRIMARY_PORTS, ...(primary.ports && typeof primary.ports === "object" ? primary.ports : {}) };
	const ctx = positiveInt(primary.contextTokens, SLOT_CONTEXT_TOKENS);
	const input = primary.images === false ? ["text"] : ["text", "image"];
	const models = primary.models && typeof primary.models === "object" ? primary.models : {};
	const chatModel = typeof models.chat === "string" && models.chat.trim() ? models.chat.trim() : "chat";
	const thinkModel = typeof models.think === "string" && models.think.trim() ? models.think.trim() : "code";

	const delegation = profile.delegation && typeof profile.delegation === "object" ? profile.delegation : {};
	const delegateEnabled = delegation.enabled === true;

	const connectedServices = {
		chat: { baseUrl: `${host}:${ports.chat}/v1/chat/completions`, model: chatModel, contextTokens: ctx },
		think: { baseUrl: `${host}:${ports.code}/v1/chat/completions`, model: thinkModel, contextTokens: ctx },
		delegate: projectDelegate(delegation, delegateEnabled),
	};
	const embedding = profile.embedding && typeof profile.embedding === "object" ? profile.embedding : {};
	if (typeof embedding.url === "string" && embedding.url.trim()) {
		connectedServices.embeddings = { url: embedding.url.trim() };
		if (typeof embedding.model === "string" && embedding.model.trim()) {
			connectedServices.embeddings.model = embedding.model.trim();
		}
	}
	const transcription =
		profile.transcription && typeof profile.transcription === "object" ? profile.transcription : {};
	if (typeof transcription.baseUrl === "string" && transcription.baseUrl.trim()) {
		connectedServices.transcription = { baseUrl: transcription.baseUrl.trim() };
		if (typeof transcription.engine === "string" && transcription.engine.trim()) {
			connectedServices.transcription.engine = transcription.engine.trim();
		}
	}
	const ocr = profile.ocr && typeof profile.ocr === "object" ? profile.ocr : {};
	if (typeof ocr.url === "string" && ocr.url.trim()) connectedServices.ocr = { url: ocr.url.trim() };

	const providers = {
		"forge-local-think": { baseUrl: `${host}:${ports.think}/v1`, input, contextWindow: ctx },
		"forge-local-code": { baseUrl: `${host}:${ports.code}/v1`, input, contextWindow: ctx },
		"forge-local-chat": { baseUrl: `${host}:${ports.chat}/v1`, input, contextWindow: ctx },
	};
	return { connectedServices, providers, contextWindow: ctx };
}

/**
 * The delegate service block. Scheduling is always forced off — the secondary is a
 * separate, single-slot backend, so pinning `id_slot: 1` there is an out-of-range
 * error, not a hint (see connected-services.mjs). When the setup disables
 * delegation we still write `enabled: false` so a prior enabled setup is turned
 * back off, and leave the endpoint fields at whatever they were (unused while off).
 */
function projectDelegate(delegation, enabled) {
	const schedulingOff = { ...DEFAULT_CONNECTED_SERVICES.delegate.scheduling, enabled: false };
	if (!enabled) return { enabled: false, scheduling: schedulingOff };
	if (typeof delegation.baseUrl !== "string" || !delegation.baseUrl.trim()) {
		throw new Error("a setup that enables delegation must set delegation.baseUrl");
	}
	const block = {
		enabled: true,
		baseUrl: delegation.baseUrl.trim().replace(/\/+$/, ""),
		scheduling: schedulingOff,
	};
	if (typeof delegation.model === "string" && delegation.model.trim()) block.model = delegation.model.trim();
	block.contextTokens = positiveInt(delegation.contextTokens, SLOT_CONTEXT_TOKENS);
	if (delegation.chatTemplateKwargs && typeof delegation.chatTemplateKwargs === "object") {
		block.chatTemplateKwargs = { ...delegation.chatTemplateKwargs };
	}
	return block;
}

function readJsonObject(path, label) {
	if (!existsSync(path)) return {};
	let parsed;
	try {
		parsed = JSON.parse(readFileSync(path, "utf8"));
	} catch (error) {
		throw new Error(`Cannot read ${label} (${path}): ${error.message}`);
	}
	if (parsed === null || Array.isArray(parsed) || typeof parsed !== "object") {
		throw new Error(`${label} (${path}) must contain a JSON object`);
	}
	return parsed;
}

function writeJsonObject(path, value) {
	writeFileSync(path, `${JSON.stringify(value, undefined, "\t")}\n`, { mode: 0o600 });
}

/** Assign the fields of `patch` onto `target[key]`, creating the sub-object. */
function mergeInto(target, key, patch) {
	const current = target[key] && typeof target[key] === "object" && !Array.isArray(target[key]) ? target[key] : {};
	target[key] = { ...current, ...patch };
}

/**
 * Make one setup live. Reads settings.json and models.json, writes the active (or
 * named) setup's values into them in place — preserving every unrelated key — and
 * saves. `active` is recorded in backends.json when a name is given. Returns a
 * summary for the caller to print.
 *
 * @param {{ env?: NodeJS.ProcessEnv, agentDir?: string, name?: string }} [options]
 */
export function applyProfile({ env = process.env, agentDir, name } = {}) {
	const options = { env, agentDir };
	const config = loadBackends(options);
	const selected = name ?? activeProfileName(config);
	const profile = requireProfile(config, selected);
	const patch = projectProfile(profile);

	const dir = resolveAgentDir(options);
	const settingsPath = join(dir, "settings.json");
	const modelsPath = join(dir, "models.json");
	const settings = readJsonObject(settingsPath, "settings.json");
	const models = readJsonObject(modelsPath, "models.json");

	if (
		!settings.connectedServices ||
		typeof settings.connectedServices !== "object" ||
		Array.isArray(settings.connectedServices)
	) {
		settings.connectedServices = {};
	}
	for (const [service, fields] of Object.entries(patch.connectedServices)) {
		mergeInto(settings.connectedServices, service, fields);
	}

	// Keep the agent's compaction reserve consistent with the window this setup
	// gives it, the same way the installer derives it. Without this a switch to a
	// larger window would compact too early and a smaller one too late.
	const reserveTokens = patch.contextWindow - Math.floor(patch.contextWindow * COMPACTION_TRIGGER_RATIO);
	if (!settings.compaction || typeof settings.compaction !== "object" || Array.isArray(settings.compaction)) {
		settings.compaction = {};
	}
	settings.compaction.enabled = settings.compaction.enabled ?? true;
	settings.compaction.reserveTokens = reserveTokens;

	const missingProviders = [];
	if (models.providers && typeof models.providers === "object" && !Array.isArray(models.providers)) {
		for (const [provider, fields] of Object.entries(patch.providers)) {
			const entry = models.providers[provider];
			if (
				!entry ||
				typeof entry !== "object" ||
				Array.isArray(entry) ||
				!Array.isArray(entry.models) ||
				!entry.models[0]
			) {
				missingProviders.push(provider);
				continue;
			}
			entry.baseUrl = fields.baseUrl;
			entry.models[0].input = [...fields.input];
			entry.models[0].contextWindow = fields.contextWindow;
		}
	} else {
		missingProviders.push(...Object.keys(patch.providers));
	}

	writeJsonObject(settingsPath, settings);
	writeJsonObject(modelsPath, models);

	if (name && config.active !== name) {
		config.active = name;
		saveBackends(config, options);
	} else if (!existsSync(getBackendsPath(options))) {
		// First apply with no runtime file yet: persist the defaults so the file the
		// user edits actually exists on disk.
		saveBackends(config, options);
	}

	return {
		profile: selected,
		description: typeof profile.description === "string" ? profile.description : "",
		delegation: patch.connectedServices.delegate.enabled ? "on" : "off",
		connectedServices: patch.connectedServices,
		missingProviders,
	};
}

/**
 * Set the active setup's delegation on or off without changing anything else, then
 * re-apply. A convenience for the common toggle; edits the active profile in place.
 *
 * @param {{ env?: NodeJS.ProcessEnv, agentDir?: string, enabled?: boolean }} [options]
 */
export function setDelegation({ env = process.env, agentDir, enabled } = {}) {
	const options = { env, agentDir };
	const config = loadBackends(options);
	const name = activeProfileName(config);
	const profile = requireProfile(config, name);
	if (!profile.delegation || typeof profile.delegation !== "object" || Array.isArray(profile.delegation)) {
		profile.delegation = {};
	}
	profile.delegation.enabled = enabled === true;
	if (enabled && (typeof profile.delegation.baseUrl !== "string" || !profile.delegation.baseUrl.trim())) {
		// Nothing to point at: seed the shipped secondary so the toggle is one word.
		const fallback = DEFAULT_BACKENDS.profiles.distributed.delegation;
		profile.delegation.baseUrl = fallback.baseUrl;
		profile.delegation.model = profile.delegation.model ?? fallback.model;
		profile.delegation.contextTokens = profile.delegation.contextTokens ?? fallback.contextTokens;
		profile.delegation.chatTemplateKwargs = profile.delegation.chatTemplateKwargs ?? {
			...fallback.chatTemplateKwargs,
		};
	}
	saveBackends(config, options);
	return applyProfile({ env, agentDir, name });
}

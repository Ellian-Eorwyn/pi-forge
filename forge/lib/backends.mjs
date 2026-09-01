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
import { capacityForUrl, readSnapshot } from "./stack-state.mjs";

// The standard forge port layout in front of the primary llama-server. The three
// interactive providers and the two skill services all front one backend today;
// a setup that points somewhere else overrides `primary.host` (and `primary.ports`
// for a non-standard layout). Kept in sync with configure-pi-forge.mjs, which
// hardcodes the same numbers.
export const PRIMARY_PORTS = Object.freeze({ think: 8003, code: 8008, chat: 8004 });

// The mirror of PRIMARY_PORTS on the second GPU. `chat2` is the non-thinking bulk
// aggregate; `code2` is the thinking configuration the `think2` verify lane and
// the `think` service map to (exactly as primary `think` maps to the code port).
// `think2` (8103) is carried for completeness but not fronted by a forge service.
export const SECONDARY_PORTS = Object.freeze({ chat2: 8104, think2: 8103, code2: 8108 });

// Matches COMPACTION_TRIGGER_RATIO in configure-pi-forge.mjs: the interactive
// agent compacts at this fraction of its window, so the reserve is the remainder.
// Recomputed here because a setup can change the interactive context.
const COMPACTION_TRIGGER_RATIO = 0.75;

/**
 * The setups shipped by default. `single` is the default and the active one: one
 * image-capable model on llms doing everything, with the whole window that model
 * serves and no delegation — the simplest thing that works, and what an install
 * with one backend should be. `distributed` and `distributed-parallel` are the
 * multi-model setups, where a second backend earns its keep as a delegation target,
 * a second bulk lane, and an independent verify lane. All are templates: the one
 * file is meant to be edited, and this is only where a fresh install starts.
 * `single` is listed first so it is the fallback when `active` names a setup that
 * no longer exists.
 *
 * `primary.contextTokens: "auto"` reads the per-slot window from the deployment at
 * apply time rather than hardcoding one, so a setup declares exactly what its
 * backend serves and follows a backend that is later given a bigger one. It falls
 * back to SLOT_CONTEXT_TOKENS when the stack cannot be read.
 */
export const DEFAULT_BACKENDS = Object.freeze({
	active: "single",
	profiles: Object.freeze({
		single: Object.freeze({
			// One image-capable model doing everything on llms, delegation off. The
			// window is whatever that backend serves per slot: declaring more than it
			// serves means the agent sends prompts it believes fit and reads the
			// server's rejection as the model failing, and declaring less wastes the
			// window on every call — so this asks rather than guesses.
			description:
				"One image-capable model on llms with the full window it serves; no delegation; embedding/transcription on llms.",
			primary: Object.freeze({ host: "http://llms", images: true, contextTokens: "auto" }),
			delegation: Object.freeze({ enabled: false }),
			embedding: Object.freeze({ url: "http://llms:8005/v1/embeddings", model: "embed" }),
			// `api` is stated rather than left to the default. Applying a setup merges
			// its fields onto settings.json, so a machine that had been on a setup
			// aimed at an OpenAI-ASR server keeps that `"openai"` unless a later setup
			// names the protocol — and `single` is the one-word revert, so it is
			// precisely the setup that must be able to undo it.
			transcription: Object.freeze({ baseUrl: "http://llms:8014", engine: "parakeet-v3", api: "sidecar" }),
			ocr: Object.freeze({ url: "http://llms:5002/glmocr/parse" }),
		}),
		distributed: Object.freeze({
			description:
				"A vision primary and a vision-free delegation backend split across the two llms GPUs, " +
				"running in parallel; embedding/transcription/OCR on llms.",
			primary: Object.freeze({ host: "http://llms", images: true, contextTokens: "auto" }),
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
			embedding: Object.freeze({ url: "http://llms:8005/v1/embeddings", model: "embed" }),
			transcription: Object.freeze({ baseUrl: "http://llms:8014", engine: "parakeet-v3", api: "sidecar" }),
			ocr: Object.freeze({ url: "http://llms:5002/glmocr/parse" }),
		}),
		"distributed-parallel": Object.freeze({
			// Two full copies of the model, one per GPU, used at the same time. The
			// vision primary on GPU 1 keeps the interactive session and one bulk lane;
			// the vision-free secondary on GPU 2 adds a second bulk lane, an independent
			// verify lane, delegation, and interactive-session compaction — so skill
			// batch work fans across both GPUs while the main session stays responsive.
			description:
				"Two GPUs at once: interactive + a bulk lane on GPU1; a second bulk lane, independent verify, " +
				"compaction, and delegation on GPU2. Skill batch work fans across both.",
			primary: Object.freeze({ host: "http://llms", images: true, contextTokens: "auto" }),
			// The secondary keeps a literal: the probe below reads the primary's chat
			// endpoint only, so "auto" here would silently mean the fallback constant.
			secondary: Object.freeze({
				host: "http://llms",
				images: false,
				contextTokens: SLOT_CONTEXT_TOKENS,
				ports: Object.freeze({ ...SECONDARY_PORTS }),
				chatTemplateKwargs: Object.freeze({ enable_thinking: false }),
			}),
			delegation: Object.freeze({
				enabled: true,
				baseUrl: "http://llms:8104/v1/chat/completions",
				model: "chat",
				images: false,
				contextTokens: SLOT_CONTEXT_TOKENS,
				chatTemplateKwargs: Object.freeze({ enable_thinking: false }),
			}),
			// Fan per-item bulk work across both GPUs' non-thinking lanes.
			bulk: Object.freeze({ lanes: Object.freeze(["chat", "chat2"]) }),
			// Review on the second GPU — an independent instance from the producers.
			verify: Object.freeze({ service: "think2" }),
			// Offload interactive-session compaction to the second GPU's non-thinking lane.
			compaction: Object.freeze({ offload: true, service: "chat2" }),
			embedding: Object.freeze({ url: "http://llms:8005/v1/embeddings", model: "embed" }),
			transcription: Object.freeze({ baseUrl: "http://llms:8014", engine: "parakeet-v3", api: "sidecar" }),
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
 * `{ connectedServices, providers, contextWindow, taskModel, taskProvider }` — a
 * patch, not a full file. `chat2`/`think2`/`bulk`/`verify` and the compaction
 * `taskModel`/`taskProvider` are ALWAYS present (their off-state when the setup
 * declares no secondary), so switching to a one-GPU setup writes every dual-GPU
 * knob back off and no stale flag can point skills at an absent GPU 2.
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

	const secondary = profile.secondary && typeof profile.secondary === "object" ? profile.secondary : null;
	const projectedSecondary = projectSecondary(secondary);

	const connectedServices = {
		chat: { baseUrl: `${host}:${ports.chat}/v1/chat/completions`, model: chatModel, contextTokens: ctx },
		think: { baseUrl: `${host}:${ports.code}/v1/chat/completions`, model: thinkModel, contextTokens: ctx },
		delegate: projectDelegate(delegation, delegateEnabled),
		chat2: projectedSecondary.chat2,
		think2: projectedSecondary.think2,
		bulk: projectBulk(profile.bulk),
		verify: projectVerify(profile.verify),
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
		// `api` selects the wire protocol (sidecar | openai); `model` is the OpenAI
		// model form field. Carried through so a setup can point transcription at an
		// OpenAI-compatible ASR server (mlx-audio) as easily as the sidecar.
		if (typeof transcription.api === "string" && transcription.api.trim()) {
			connectedServices.transcription.api = transcription.api.trim();
		}
		if (typeof transcription.model === "string" && transcription.model.trim()) {
			connectedServices.transcription.model = transcription.model.trim();
		}
	}
	const ocr = profile.ocr && typeof profile.ocr === "object" ? profile.ocr : {};
	if (typeof ocr.url === "string" && ocr.url.trim()) connectedServices.ocr = { url: ocr.url.trim() };

	const providers = {
		"forge-local-think": { baseUrl: `${host}:${ports.think}/v1`, input, contextWindow: ctx },
		"forge-local-code": { baseUrl: `${host}:${ports.code}/v1`, input, contextWindow: ctx },
		"forge-local-chat": { baseUrl: `${host}:${ports.chat}/v1`, input, contextWindow: ctx },
	};
	const { taskModel, taskProvider } = projectCompaction(
		profile.compaction,
		projectedSecondary.host,
		projectedSecondary.ports,
		projectedSecondary.contextTokens,
	);
	return { connectedServices, providers, contextWindow: ctx, taskModel, taskProvider };
}

/**
 * The GPU-2 `chat2`/`think2` service blocks. Always returns both: enabled and
 * pointed at the secondary when the setup declares one, else their off-state
 * (`enabled: false`), mirroring `projectDelegate`. Scheduling is forced off — a
 * separate single-slot server has no shared prefix cache to protect and would
 * reject an out-of-range `id_slot`. `chat2` is the non-thinking bulk lane
 * (`enable_thinking: false`); `think2` is the thinking verify lane on the code
 * port (`chatTemplateKwargs: null`, it is meant to reason).
 */
function projectSecondary(secondary) {
	const schedulingOff = { ...DEFAULT_CONNECTED_SERVICES.chat2.scheduling, enabled: false };
	if (!secondary) {
		return {
			chat2: { enabled: false, scheduling: schedulingOff },
			think2: { enabled: false, scheduling: schedulingOff },
			host: null,
			ports: null,
			contextTokens: SLOT_CONTEXT_TOKENS,
		};
	}
	const host = cleanHost(secondary.host);
	const ports = {
		...SECONDARY_PORTS,
		...(secondary.ports && typeof secondary.ports === "object" ? secondary.ports : {}),
	};
	const contextTokens = positiveInt(secondary.contextTokens, SLOT_CONTEXT_TOKENS);
	const models = secondary.models && typeof secondary.models === "object" ? secondary.models : {};
	const chat2Model = typeof models.chat2 === "string" && models.chat2.trim() ? models.chat2.trim() : "chat";
	const think2Model = typeof models.think2 === "string" && models.think2.trim() ? models.think2.trim() : "code";
	const images = secondary.images === true;
	const templateKwargs =
		secondary.chatTemplateKwargs && typeof secondary.chatTemplateKwargs === "object"
			? { ...secondary.chatTemplateKwargs }
			: { enable_thinking: false };
	return {
		chat2: {
			enabled: true,
			baseUrl: `${host}:${ports.chat2}/v1/chat/completions`,
			model: chat2Model,
			images,
			contextTokens,
			chatTemplateKwargs: templateKwargs,
			scheduling: schedulingOff,
		},
		think2: {
			enabled: true,
			baseUrl: `${host}:${ports.code2}/v1/chat/completions`,
			model: think2Model,
			images,
			contextTokens,
			chatTemplateKwargs: null,
			scheduling: schedulingOff,
		},
		host,
		ports,
		contextTokens,
	};
}

/** The `bulk.lanes` block, collapsing to the single primary `chat` lane when unset. */
function projectBulk(bulk) {
	const lanes =
		bulk && Array.isArray(bulk.lanes)
			? bulk.lanes.filter((name) => typeof name === "string" && name.trim()).map((name) => name.trim())
			: [];
	return { lanes: lanes.length ? lanes : ["chat"] };
}

/** The `verify.service` block, `null` (primary thinking lane) when unset. */
function projectVerify(verify) {
	const service = verify && typeof verify.service === "string" && verify.service.trim() ? verify.service.trim() : null;
	return { service };
}

/**
 * The harness compaction offload. When the setup asks for it AND has a secondary,
 * returns a `taskModel` pointed at the secondary lane and a `taskProvider` for
 * `models.json` whose `qwen-chat-template` compat + `reasoning: true` makes the
 * non-thinking `:8104` return visible content (see openai-completions.ts). Off or
 * without a secondary, returns the off-state so `applyProfile` clears any prior
 * offload and removes the provider.
 */
function projectCompaction(compaction, secondaryHost, secondaryPorts, secondaryCtx) {
	const wantsOffload = Boolean(compaction && compaction.offload === true) && Boolean(secondaryHost && secondaryPorts);
	if (!wantsOffload) return { taskModel: { enabled: false }, taskProvider: null };
	const service = compaction.service === "think2" ? "think2" : "chat2";
	const port = service === "think2" ? secondaryPorts.code2 : secondaryPorts.chat2;
	const model = service === "think2" ? "code" : "chat";
	const baseUrl = `${secondaryHost}:${port}/v1`;
	return {
		taskModel: {
			enabled: true,
			provider: "forge-task-local",
			model,
			baseUrl,
			contextWindow: secondaryCtx,
			thinkingEnabled: false,
			maxConcurrency: 1,
		},
		taskProvider: { id: "forge-task-local", baseUrl, model, input: ["text"], contextWindow: secondaryCtx },
	};
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
		images: delegation.images === true,
		scheduling: schedulingOff,
	};
	if (typeof delegation.model === "string" && delegation.model.trim()) block.model = delegation.model.trim();
	block.contextTokens = positiveInt(delegation.contextTokens, SLOT_CONTEXT_TOKENS);
	if (delegation.chatTemplateKwargs && typeof delegation.chatTemplateKwargs === "object") {
		block.chatTemplateKwargs = { ...delegation.chatTemplateKwargs };
	}
	return block;
}

/**
 * Keep the pinned background slot inside the range the backend actually has.
 *
 * Background work pins a slot so bulk calls cannot evict the interactive
 * session's prefix cache. A backend running one slot has no slot 1, so the pin
 * names something that does not exist — and nothing else in forge would ever
 * notice, because the number is only ever sent, never checked. Shared with
 * `configure-pi-forge.mjs`: an install and a setup switch must land on the same
 * pin, or one would write a slot the other has already ruled out.
 *
 * Mutates `service.scheduling` in place. Returns the slot it moved to, or null
 * when the pin was already in range (or there was nothing to check).
 */
export function clampBackgroundSlot(service, capacity, label) {
	const total = capacity?.totalSlots;
	if (!Number.isInteger(total) || total < 1) return null;
	const scheduling = service?.scheduling;
	if (!scheduling?.enabled || !Number.isInteger(scheduling.backgroundSlot) || scheduling.backgroundSlot < total)
		return null;
	const clamped = total - 1;
	process.stderr.write(
		`${label}: the backend runs ${total} slot${total === 1 ? "" : "s"}, so background work cannot pin slot ` +
			`${scheduling.backgroundSlot}; using slot ${clamped}.\n`,
	);
	scheduling.backgroundSlot = clamped;
	if (scheduling.interactiveSlot >= total) scheduling.interactiveSlot = clamped;
	return clamped;
}

// Descriptions this repo used to ship, superseded because they name a window that
// is no longer fixed. Replaced only when byte-equal, so an edited one survives.
const SUPERSEDED_DESCRIPTIONS = Object.freeze({
	"One image-capable model at 131k on llms; no delegation; embedding/transcription on llms.":
		DEFAULT_BACKENDS.profiles.single.description,
});

/**
 * Upgrade setups still carrying the old built-in literal to `"auto"`.
 *
 * `seedBackends` only writes when the file is absent, so an install made before
 * `"auto"` existed keeps its literal forever and never learns what its backend
 * grew into. Byte-equal to SLOT_CONTEXT_TOKENS means an installer put it there;
 * any other number was typed by someone aiming a setup at a specific backend and
 * is left alone. Same rule, and the same reasoning, as `adoptServedContext` in
 * configure-pi-forge.mjs — and the same rule again for the description, which on
 * those installs states the very number that just stopped being fixed.
 *
 * Mutates `config`. Returns true when something changed, so the caller can save.
 */
function migrateAutoContext(config) {
	let changed = false;
	for (const profile of Object.values(config.profiles ?? {})) {
		if (!profile || typeof profile !== "object" || Array.isArray(profile)) continue;
		const replacement = SUPERSEDED_DESCRIPTIONS[profile.description];
		if (replacement) {
			profile.description = replacement;
			changed = true;
		}
		const primary = profile.primary;
		if (!primary || typeof primary !== "object" || Array.isArray(primary)) continue;
		if (primary.contextTokens !== SLOT_CONTEXT_TOKENS) continue;
		primary.contextTokens = "auto";
		changed = true;
	}
	return changed;
}

/**
 * Embedding and transcription blocks this repo used to ship pointing at a second
 * machine, superseded because both services now run on the llms box alongside
 * everything else. Listed as literals rather than derived, because what matters
 * is byte-equality with what an installer wrote — see `migrateServiceHosts`.
 */
const SUPERSEDED_EMBEDDINGS = Object.freeze([
	Object.freeze({ url: "http://laptop:8005/v1/embeddings", model: "embed" }),
]);
const SUPERSEDED_TRANSCRIPTIONS = Object.freeze([
	Object.freeze({ baseUrl: "http://laptop:8014", engine: "parakeet-v3" }),
	// The mlx-audio form the `distributed` setup shipped with.
	Object.freeze({ baseUrl: "http://laptop:8014", engine: "parakeet-v3", api: "openai", model: "parakeet-v3-en" }),
]);

/** Same fields, same values — key order and JSON spelling aside. */
function sameBlock(value, shipped) {
	if (!value || typeof value !== "object" || Array.isArray(value)) return false;
	const normalize = (object) => JSON.stringify(Object.entries(object).sort(([a], [b]) => a.localeCompare(b)));
	return normalize(value) === normalize(shipped);
}

/**
 * Move setups still naming the old second machine onto the llms endpoints.
 *
 * `seedBackends` only writes when the file is absent, so an install made while the
 * `distributed*` setups pointed embedding and transcription at a laptop keeps that
 * host forever: updating the shipped defaults alone reaches a fresh install and
 * nothing else. Byte-equal to a block this repo shipped means an installer put it
 * there; any other host was typed by someone aiming a setup at a machine of their
 * own and is left alone — the same rule, and the same reasoning, as
 * SUPERSEDED_DESCRIPTIONS above.
 *
 * Mutates `config`. Returns true when something changed, so the caller can save.
 */
function migrateServiceHosts(config) {
	let changed = false;
	for (const profile of Object.values(config.profiles ?? {})) {
		if (!profile || typeof profile !== "object" || Array.isArray(profile)) continue;
		if (SUPERSEDED_EMBEDDINGS.some((shipped) => sameBlock(profile.embedding, shipped))) {
			profile.embedding = { ...DEFAULT_BACKENDS.profiles.single.embedding };
			changed = true;
		}
		if (SUPERSEDED_TRANSCRIPTIONS.some((shipped) => sameBlock(profile.transcription, shipped))) {
			profile.transcription = { ...DEFAULT_BACKENDS.profiles.single.transcription };
			changed = true;
		}
	}
	return changed;
}

/** Does this setup want its window read from the deployment rather than declared? */
function wantsAutoContext(profile) {
	return profile?.primary?.contextTokens === "auto";
}

/**
 * What the primary's chat endpoint actually serves, or null.
 *
 * The URL is taken from a throwaway projection rather than re-derived here, so
 * the endpoint probed is by construction the endpoint written. Strictly optional
 * in both directions: an unreachable stack returns null from `readSnapshot`, and
 * a URL the snapshot cannot place returns null from `capacityForUrl`.
 */
async function probePrimaryCapacity(profile, env, connectedServices) {
	const chatUrl = projectProfile(profile).connectedServices.chat.baseUrl;
	const snapshot = await readSnapshot({ env, settings: connectedServices });
	return capacityForUrl(snapshot, chatUrl);
}

/**
 * Replace an `"auto"` window with the one the deployment reports.
 *
 * When the read fails, the window already in settings.json is kept if there is
 * one, and only a machine that has never had a number falls through to
 * SLOT_CONTEXT_TOKENS. `"auto"` means "ask the deployment", and an unreachable
 * state API is a failure to ask — not an answer. Overwriting a window a previous
 * apply *did* read with a constant half its size is silent and costs half the
 * context on every later call, and the reasons a probe fails (the API gated
 * behind a token it was not given, a host that is briefly down, a network it
 * cannot see) have nothing to do with what the backend serves. Verified the
 * expensive way 2026-09-01: the state API had been put behind a bearer token,
 * every apply then quietly rewrote a genuine 262144 down to 131072.
 *
 * Returns a copy: the shipped profiles are frozen.
 */
function resolveAutoContext(profile, capacity, known) {
	if (!wantsAutoContext(profile)) return profile;
	const served = capacity?.contextTokens;
	const resolved =
		Number.isInteger(served) && served > 0 ? served : Number.isInteger(known) && known > 0 ? known : null;
	if (resolved === null) return profile;
	return { ...profile, primary: { ...profile.primary, contextTokens: resolved } };
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
 * The `models.json` provider entry for the compaction offload, from a
 * `projectCompaction` `taskProvider` spec. `reasoning: true` +
 * `thinkingFormat: "qwen-chat-template"` is what makes the harness send
 * `enable_thinking: false` to the non-thinking secondary (it passes no reasoning
 * effort because `taskModel.thinkingEnabled` is false), so the lane returns
 * visible content the summarizer can parse. The model id matches
 * `taskModel.model`, which is how the registry resolves the provider.
 */
function taskLocalProvider(spec) {
	return {
		baseUrl: spec.baseUrl,
		api: "openai-completions",
		apiKey: "local",
		compat: {
			supportsDeveloperRole: false,
			supportsReasoningEffort: false,
			thinkingFormat: "qwen-chat-template",
			maxTokensField: "max_tokens",
		},
		models: [
			{
				id: spec.model,
				name: "Task (GPU2 compaction, non-thinking)",
				reasoning: true,
				input: [...spec.input],
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
				contextWindow: spec.contextWindow,
				maxTokens: 4096,
			},
		],
	};
}

/**
 * Make one setup live. Reads settings.json and models.json, writes the active (or
 * named) setup's values into them in place — preserving every unrelated key — and
 * saves. `active` is recorded in backends.json when a name is given. Returns a
 * summary for the caller to print.
 *
 * Async because it reads the deployment once first, the same way the installer
 * does: an `"auto"` window and the background-slot clamp both come from that one
 * read, and a stack that cannot be reached leaves both at what the setup already
 * says. Every caller already sits in an async context.
 *
 * @param {{ env?: NodeJS.ProcessEnv, agentDir?: string, name?: string }} [options]
 */
export async function applyProfile({ env = process.env, agentDir, name } = {}) {
	const options = { env, agentDir };
	const config = loadBackends(options);
	// Both run before anything is projected: an install predating "auto" adopts it
	// here rather than staying pinned to a number its backend may have outgrown, and
	// one predating the move of embedding/transcription onto llms follows it here
	// rather than keeping a host that is no longer serving them.
	const migrated = [migrateAutoContext(config), migrateServiceHosts(config)].some(Boolean);
	const selected = name ?? activeProfileName(config);
	const profile = requireProfile(config, selected);

	const dir = resolveAgentDir(options);
	const settingsPath = join(dir, "settings.json");
	const modelsPath = join(dir, "models.json");
	const settings = readJsonObject(settingsPath, "settings.json");
	const models = readJsonObject(modelsPath, "models.json");

	const capacity = await probePrimaryCapacity(profile, env, settings.connectedServices);
	// Read before the merge below writes the resolved window back into this object,
	// which would otherwise make "was there a previous reading?" always true.
	const knownContext = settings.connectedServices?.chat?.contextTokens;
	const patch = projectProfile(resolveAutoContext(profile, capacity, knownContext));

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

	// A pin carried over from a multi-slot backend names a slot this one does not
	// have. Done after the merge so it corrects what is about to be written, and
	// only against a slot count actually read — an unreachable stack changes nothing.
	for (const service of ["chat", "think"]) {
		clampBackgroundSlot(settings.connectedServices[service], capacity, `connectedServices.${service}`);
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

	// Compaction offload to the second GPU. `settings.taskModel` is the harness
	// summarizer config and `contextBudget.useTaskModel` gates it; both are written
	// every apply, and the task model is removed when off, so switching to a
	// one-GPU setup turns offload back off with no residue (matching the installer,
	// which deletes taskModel and sets useTaskModel:false on a plain `single`).
	if (patch.taskModel.enabled === true) {
		settings.taskModel = { ...patch.taskModel };
	} else {
		delete settings.taskModel;
	}
	if (!settings.contextBudget || typeof settings.contextBudget !== "object" || Array.isArray(settings.contextBudget)) {
		settings.contextBudget = {};
	}
	settings.contextBudget.useTaskModel = patch.taskModel.enabled === true;

	// The offload provider lives in models.json so the harness's model registry can
	// find it. Created/refreshed when offload is on, removed when off — so a revert
	// leaves no `forge-task-local` pointing at an absent GPU. This is the one place
	// applyProfile may CREATE a provider; the three interactive ones stay update-only.
	if (models.providers && typeof models.providers === "object" && !Array.isArray(models.providers)) {
		if (patch.taskProvider) {
			models.providers[patch.taskProvider.id] = taskLocalProvider(patch.taskProvider);
		} else {
			delete models.providers["forge-task-local"];
		}
	}

	writeJsonObject(settingsPath, settings);
	writeJsonObject(modelsPath, models);

	if ((name && config.active !== name) || migrated) {
		if (name) config.active = name;
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
		contextWindow: patch.contextWindow,
		// True when the number above was read from the deployment rather than
		// declared by the setup, so a caller can say which it is.
		contextProbed: wantsAutoContext(profile) && Number.isInteger(capacity?.contextTokens),
		// True when the setup asked the deployment, the deployment could not be
		// reached, and the window already on disk was kept rather than replaced by
		// the fallback constant. Distinct from `contextProbed` because a caller that
		// reports "declared by the setup" for this case is telling the user the
		// number came from a file they can edit, when in fact it is a stale reading
		// nothing has confirmed since — which is exactly when they want to know the
		// state API is unreachable.
		contextKept:
			wantsAutoContext(profile) && !Number.isInteger(capacity?.contextTokens) && Number.isInteger(knownContext),
	};
}

/**
 * Set the active setup's delegation on or off without changing anything else, then
 * re-apply. A convenience for the common toggle; edits the active profile in place.
 *
 * @param {{ env?: NodeJS.ProcessEnv, agentDir?: string, enabled?: boolean }} [options]
 */
export async function setDelegation({ env = process.env, agentDir, enabled } = {}) {
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
	return await applyProfile({ env, agentDir, name });
}

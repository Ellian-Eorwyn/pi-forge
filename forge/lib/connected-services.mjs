import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

// What one request can actually use. The deployment runs a single llama-server
// (`chat-backend-dense`) with `--ctx-size 262144 --parallel 2`, and llama.cpp
// divides the context evenly across slots, so a slot — not the pool — is the
// real ceiling. Confirmed against the live stack 2026-07-28 by oversending: the
// server answers HTTP 400 `exceed_context_size_error` with `"n_ctx": 131072`.
// It rejects at tokenization, in a few seconds, without prefilling.
export const SLOT_CONTEXT_TOKENS = 131072;

// The local model providers written by configure-pi-forge. Extensions also use
// these names to choose the connected service whose slot policy applies, so the
// installer and request hooks must share one registry rather than duplicate
// string literals that can drift during a provider rename.
export const LOCAL_MODEL_PROVIDERS = Object.freeze({
	think: "forge-local-think",
	code: "forge-local-code",
	chat: "forge-local-chat",
});

const LOCAL_PROVIDER_SERVICES = Object.freeze({
	[LOCAL_MODEL_PROVIDERS.think]: "think",
	[LOCAL_MODEL_PROVIDERS.code]: "think",
	[LOCAL_MODEL_PROVIDERS.chat]: "chat",
});

/** The connected inference service whose scheduling applies to a local provider. */
export function serviceNameForLocalProvider(provider) {
	if (typeof provider !== "string" || !Object.hasOwn(LOCAL_PROVIDER_SERVICES, provider)) return null;
	return LOCAL_PROVIDER_SERVICES[provider];
}

export const DEFAULT_CONNECTED_SERVICES = Object.freeze({
	searxng: Object.freeze({
		enabled: true,
		baseUrl: "http://llms/searxng",
	}),
	playwright: Object.freeze({
		enabled: true,
		wsEndpoint: "ws://llms/playwright",
	}),
	// Read-only state API for the inference deployment: which weights each port
	// is serving, how much context a slot has, and why a service is down. Purely
	// additive — nothing here is required to run, and every reader degrades to
	// the built-in constants when it is absent, which is what happens on any
	// install that is not this deployment. See `stack-state.mjs`.
	stackState: Object.freeze({
		enabled: true,
		baseUrl: "http://llms:8078",
	}),
	// Bulk per-file work: the non-thinking configuration of the same weights.
	// Batch skills spend no hidden reasoning tokens here. Scheduling is on
	// because this endpoint is not a separate server — see the note on `think`.
	//
	// `contextTokens` and `chatTemplateKwargs` exist so a service can be pointed
	// somewhere that is not this deployment. The first is the ceiling the
	// preflight enforces: left at the slot default, a skill aimed at a smaller
	// backend sends a prompt this client believes fits and reads the server's
	// rejection as the model failing the task. The second is forwarded verbatim
	// as `chat_template_kwargs`; a backend running `--reasoning-format deepseek`
	// answers into `reasoning_content` and returns empty `content`, so nothing
	// arrives to parse until it is sent `{"enable_thinking": false}`.
	chat: Object.freeze({
		enabled: true,
		baseUrl: "http://llms:8004/v1/chat/completions",
		model: "chat",
		// The primary is served with an mmproj loaded, so this lane accepts images.
		// The bulk fan-out and the routing image guard read this: an image-bearing
		// item must never land on an `images:false` lane, where the transform layer
		// (downgradeUnsupportedImages) would silently drop it.
		images: true,
		contextTokens: SLOT_CONTEXT_TOKENS,
		chatTemplateKwargs: null,
		reasoningEffort: null,
		scheduling: Object.freeze({
			enabled: true,
			interactiveSlot: 0,
			backgroundSlot: 1,
			idleGraceMs: 2000,
			yieldMs: 1000,
			backgroundOutputTokens: 4096,
		}),
	}),
	// Judgment and verification: the thinking configuration, also the interactive
	// agent's own server. `chat` and `think` are two request-shaping profiles in
	// front of one llama-server, so both address the same slots and both carry a
	// scheduling block: background work pins backgroundSlot and leaves the
	// interactive session's prefix cache on interactiveSlot untouched. Leaving
	// either service unpinned lets bulk work land on slot 0 and evict it.
	think: Object.freeze({
		enabled: true,
		baseUrl: "http://llms:8008/v1/chat/completions",
		model: "code",
		images: true,
		contextTokens: SLOT_CONTEXT_TOKENS,
		chatTemplateKwargs: null,
		reasoningEffort: null,
		scheduling: Object.freeze({
			enabled: true,
			interactiveSlot: 0,
			backgroundSlot: 1,
			idleGraceMs: 2000,
			yieldMs: 1000,
			backgroundOutputTokens: 4096,
		}),
	}),
	// A genuinely smaller model, for the stages measured to be *better* on one:
	// faithful cleanup of diarized speech and yes/no pair judgment, where the
	// answer is close to a copy of the input. It is not a cheaper `chat` — across
	// the real prompt mix it is only ~12% faster per call, and slower on long
	// prompts, because it generates half again as many tokens per item.
	//
	// Off by default. Unlike `chat` and `think`, which are two profiles in front
	// of one llama-server, this is a separate backend behind a router at
	// MODEL_ROUTER_MAX=1 shared with embed/ocr/rank, so a stage that alternates
	// with embeddings pays a model swap each time. An install that has not
	// deliberately configured it should never silently start paying that.
	task: Object.freeze({
		enabled: false,
		baseUrl: "http://llms:8007/v1/chat/completions",
		model: "task",
		images: false,
		// Half the chat slot. Recorded as 32,768 for months after the backend
		// moved, which quietly under-budgeted every task-tier prompt.
		contextTokens: 65538,
		// Without this the backend answers into `reasoning_content` and returns
		// empty `content`. `reasoning_budget: 0` and `/no_think` do nothing.
		chatTemplateKwargs: Object.freeze({ enable_thinking: false }),
		reasoningEffort: null,
		scheduling: Object.freeze({
			enabled: true,
			interactiveSlot: 0,
			backgroundSlot: 1,
			idleGraceMs: 2000,
			yieldMs: 1000,
			backgroundOutputTokens: 4096,
		}),
	}),
	// A second, genuinely separate chat backend that `forge_delegate` offloads to.
	// Off by default: a stock install has only the one primary, so delegation
	// falls back to `chat` (same weights, background slot) and nothing here is used
	// until a setup enables it. When it *is* enabled it points at a second
	// llama-server on another GPU, so the delegated investigation runs in parallel
	// with the interactive session rather than sharing its slots.
	//
	// `scheduling.enabled` is false on purpose, and it is the one field that must
	// stay false: this backend does not share a prefix cache with the interactive
	// slot-0 session (it is a different server), so there is nothing to protect by
	// pinning, and the secondary runs a single slot — sending `id_slot: 1` at a
	// one-slot server is an out-of-range error, not a hint. With scheduling off the
	// delegate call sends no `id_slot` and the server assigns its own slot.
	// `chatTemplateKwargs` mirrors `task`: the secondary reasons into
	// `reasoning_content` and returns empty `content` unless told not to think.
	delegate: Object.freeze({
		enabled: false,
		baseUrl: "http://llms:8104/v1/chat/completions",
		// The model id the secondary's non-thinking aggregate actually serves (its
		// `/v1/models` reports `chat`, same name the primary uses — the URL, not the
		// name, is what selects the secondary backend). `chat-custom2` is only the
		// stack's internal alias for the weights and is not a servable id here.
		model: "chat",
		// The secondary GPU runs without an mmproj (vision off to save VRAM).
		images: false,
		contextTokens: SLOT_CONTEXT_TOKENS,
		chatTemplateKwargs: Object.freeze({ enable_thinking: false }),
		reasoningEffort: null,
		scheduling: Object.freeze({
			enabled: false,
			interactiveSlot: 0,
			backgroundSlot: 0,
			idleGraceMs: 2000,
			yieldMs: 1000,
			backgroundOutputTokens: 4096,
		}),
	}),
	// GPU-2 bulk lane. The second full copy of the model on the other GPU, served
	// non-thinking on :8104 — the same endpoint `delegate` uses, but exposed as a
	// first-class bulk service so `bulk.lanes` can fan per-item batch work across
	// both GPUs at once. Off by default; a two-GPU setup (`distributed-parallel`)
	// enables it. `scheduling.enabled:false` for the same reason as `delegate`: a
	// separate single-slot server has no shared prefix cache to protect, and
	// sending `id_slot:1` there is out of range. `images:false` — no mmproj — so
	// the fan-out and routing guards keep image-bearing work off it.
	chat2: Object.freeze({
		enabled: false,
		baseUrl: "http://llms:8104/v1/chat/completions",
		model: "chat",
		images: false,
		contextTokens: SLOT_CONTEXT_TOKENS,
		chatTemplateKwargs: Object.freeze({ enable_thinking: false }),
		reasoningEffort: null,
		scheduling: Object.freeze({
			enabled: false,
			interactiveSlot: 0,
			backgroundSlot: 0,
			idleGraceMs: 2000,
			yieldMs: 1000,
			backgroundOutputTokens: 4096,
		}),
	}),
	// GPU-2 verify lane. The secondary's thinking configuration on :8108 (its code
	// port), the mirror of primary `think`→:8008. Pointing skill verification here
	// makes the reviewer a genuinely independent instance from the bulk producers
	// (a different server, not the same weights reviewing their own work) and lets
	// verify overlap with bulk instead of serializing on one GPU. `chatTemplateKwargs`
	// is null: unlike `chat2` this lane is meant to reason, and :8108 returns its
	// reasoning in visible content the way :8008 does. Off by default; scheduling off
	// (separate single-slot server). `images:false`.
	think2: Object.freeze({
		enabled: false,
		baseUrl: "http://llms:8108/v1/chat/completions",
		model: "code",
		images: false,
		contextTokens: SLOT_CONTEXT_TOKENS,
		chatTemplateKwargs: null,
		reasoningEffort: null,
		scheduling: Object.freeze({
			enabled: false,
			interactiveSlot: 0,
			backgroundSlot: 0,
			idleGraceMs: 2000,
			yieldMs: 1000,
			backgroundOutputTokens: 4096,
		}),
	}),
	// Which lanes a batch skill fans per-item bulk work across, by service name.
	// Default is the single primary `chat` lane — byte-for-byte the pre-fan-out
	// behavior. `distributed-parallel` sets `["chat","chat2"]` to use both GPUs.
	// Names that do not resolve to a real inference service are dropped on seed.
	bulk: Object.freeze({ lanes: Object.freeze(["chat"]) }),
	// Which service skill verification/review runs on. `null` means the primary
	// thinking lane (`resolveThinkOrChat`), the pre-existing behavior;
	// `distributed-parallel` sets `"think2"` to review on the second GPU.
	verify: Object.freeze({ service: null }),
	embeddings: Object.freeze({
		enabled: true,
		url: "http://llms:8005/v1/embeddings",
		model: "embed",
	}),
	// Document OCR: a separate GLM-OCR HTTP service, not the model router. Carried
	// here so one config file can point it somewhere, but env
	// (`FORGE_GLMOCR_URL`/`FORGE_OCR_URL`) and the `--glmocr-url` flag still win in
	// `document-ingest`, and the local OCRmyPDF path ignores it entirely.
	ocr: Object.freeze({
		enabled: true,
		url: "http://llms:5002/glmocr/parse",
	}),
	// Speech to text. Not an inference service: no chat completion, no context
	// window, and no slot to pin, so it carries none of that shape.
	//
	// Its weights are not resident. `/engines` reports `yield_mode: "asr"` and
	// `idle_unload_seconds: 300` — the ASR model yields VRAM to the router that
	// serves embed/ocr/rank/task, and unloads itself when idle. `resident: null`
	// is therefore the normal reading rather than a fault, and the first call
	// after a quiet period pays a ~25s model load before decoding begins. That
	// is what the timeout is for: the decode itself runs at ~210x realtime, so a
	// 70-minute recording is about 20 seconds of actual work.
	transcription: Object.freeze({
		enabled: true,
		baseUrl: "http://llms:8014",
		// Named rather than inherited. The service's own default was
		// `faster-whisper` until 2026-08-09 and is `parakeet-v3` now, and the two
		// normalize text differently — Whisper writes "July 21st, 1969" where
		// Parakeet has written "July twenty first, nineteen sixty nine" on the
		// same audio. Following the server's current preference would change how
		// dates reach a note with nothing here recording that anything changed.
		engine: "parakeet-v3",
		// The service's TRANSCRIPT_API_TOKEN is empty today, so no header is sent.
		// It is a supported setting there and may be turned on; carrying the field
		// now costs nothing and saves an outage later.
		token: "",
		timeoutSeconds: 900,
	}),
	// Optional credentials for search and reference providers, keyed by provider
	// id. Empty by default and it stays that way for anyone who never sets one:
	// every provider in the no-key tier works without this block, and a provider
	// that needs a key and has none is skipped by the router with a logged
	// reason rather than failing the run.
	//
	// Only free-tier keys belong here. Nothing in forge requires a paid plan.
	apiKeys: Object.freeze({}),
});

// The pre-split defaults. A persisted chat service byte-equal to these was
// written by an older install rather than chosen, so configure-pi-forge may
// migrate it onto the non-thinking backend.
export const LEGACY_CHAT_SERVICE = Object.freeze({
	baseUrl: "http://llms:8008/v1/chat/completions",
	model: "code",
});

// Chat scheduling as earlier installs seeded it, when :8004 was believed to be
// a server of its own and pinning a slot there looked pointless. A persisted
// block byte-equal to this was written rather than chosen, so it can be
// upgraded; anything else is a deliberate setting and stays.
export const LEGACY_CHAT_SCHEDULING = Object.freeze({
	enabled: false,
	interactiveSlot: 0,
	backgroundSlot: 1,
	idleGraceMs: 2000,
	yieldMs: 1000,
	backgroundOutputTokens: 4096,
});

export function getForgeAgentDir(env = process.env) {
	const home = env.PI_FORGE_HOME || join(homedir(), ".pi-forge");
	return env.PI_CODING_AGENT_DIR || env.PI_FORGE_AGENT_DIR || join(home, "agent");
}

export function loadForgeSettings(env = process.env) {
	const settingsPath = join(getForgeAgentDir(env), "settings.json");
	if (!existsSync(settingsPath)) return {};
	const value = JSON.parse(readFileSync(settingsPath, "utf8"));
	if (!value || typeof value !== "object" || Array.isArray(value)) return {};
	return value;
}

export function seedConnectedServicesSettings(settings) {
	const current =
		settings.connectedServices &&
		typeof settings.connectedServices === "object" &&
		!Array.isArray(settings.connectedServices)
			? settings.connectedServices
			: {};
	const searxng =
		current.searxng && typeof current.searxng === "object" && !Array.isArray(current.searxng) ? current.searxng : {};
	const playwright =
		current.playwright && typeof current.playwright === "object" && !Array.isArray(current.playwright)
			? current.playwright
			: {};
	const stackState =
		current.stackState && typeof current.stackState === "object" && !Array.isArray(current.stackState)
			? current.stackState
			: {};
	const chat = current.chat && typeof current.chat === "object" && !Array.isArray(current.chat) ? current.chat : {};
	const think =
		current.think && typeof current.think === "object" && !Array.isArray(current.think) ? current.think : {};
	const task = current.task && typeof current.task === "object" && !Array.isArray(current.task) ? current.task : {};
	const delegate =
		current.delegate && typeof current.delegate === "object" && !Array.isArray(current.delegate)
			? current.delegate
			: {};
	const chat2 =
		current.chat2 && typeof current.chat2 === "object" && !Array.isArray(current.chat2) ? current.chat2 : {};
	const think2 =
		current.think2 && typeof current.think2 === "object" && !Array.isArray(current.think2) ? current.think2 : {};
	const embeddings =
		current.embeddings && typeof current.embeddings === "object" && !Array.isArray(current.embeddings)
			? current.embeddings
			: {};
	const ocr = current.ocr && typeof current.ocr === "object" && !Array.isArray(current.ocr) ? current.ocr : {};
	const transcription =
		current.transcription && typeof current.transcription === "object" && !Array.isArray(current.transcription)
			? current.transcription
			: {};
	settings.connectedServices = {
		...current,
		searxng: {
			enabled: searxng.enabled ?? DEFAULT_CONNECTED_SERVICES.searxng.enabled,
			baseUrl: normalizeHttpBaseUrl(searxng.baseUrl) ?? DEFAULT_CONNECTED_SERVICES.searxng.baseUrl,
		},
		playwright: {
			enabled: playwright.enabled ?? DEFAULT_CONNECTED_SERVICES.playwright.enabled,
			wsEndpoint: normalizeWsEndpoint(playwright.wsEndpoint) ?? DEFAULT_CONNECTED_SERVICES.playwright.wsEndpoint,
		},
		stackState: {
			enabled: stackState.enabled ?? DEFAULT_CONNECTED_SERVICES.stackState.enabled,
			baseUrl: normalizeHttpBaseUrl(stackState.baseUrl) ?? DEFAULT_CONNECTED_SERVICES.stackState.baseUrl,
		},
		chat: seedInferenceService(chat, DEFAULT_CONNECTED_SERVICES.chat),
		think: seedInferenceService(think, DEFAULT_CONNECTED_SERVICES.think),
		task: seedInferenceService(task, DEFAULT_CONNECTED_SERVICES.task),
		delegate: seedInferenceService(delegate, DEFAULT_CONNECTED_SERVICES.delegate),
		chat2: seedInferenceService(chat2, DEFAULT_CONNECTED_SERVICES.chat2),
		think2: seedInferenceService(think2, DEFAULT_CONNECTED_SERVICES.think2),
		embeddings: {
			enabled: embeddings.enabled ?? DEFAULT_CONNECTED_SERVICES.embeddings.enabled,
			url: normalizeHttpBaseUrl(embeddings.url) ?? DEFAULT_CONNECTED_SERVICES.embeddings.url,
			model: normalizeServiceName(embeddings.model) ?? DEFAULT_CONNECTED_SERVICES.embeddings.model,
		},
		ocr: {
			enabled: ocr.enabled ?? DEFAULT_CONNECTED_SERVICES.ocr.enabled,
			url: normalizeHttpBaseUrl(ocr.url) ?? DEFAULT_CONNECTED_SERVICES.ocr.url,
		},
		transcription: {
			enabled: transcription.enabled ?? DEFAULT_CONNECTED_SERVICES.transcription.enabled,
			baseUrl: normalizeHttpBaseUrl(transcription.baseUrl) ?? DEFAULT_CONNECTED_SERVICES.transcription.baseUrl,
			engine: normalizeServiceName(transcription.engine) ?? DEFAULT_CONNECTED_SERVICES.transcription.engine,
			// Deliberately not `normalizeServiceName(...) ?? default`: the default
			// is the empty string, and a token cleared on purpose must stay cleared
			// rather than fall through to anything.
			token: normalizeServiceName(transcription.token) ?? DEFAULT_CONNECTED_SERVICES.transcription.token,
			timeoutSeconds: normalizePositiveInteger(
				transcription.timeoutSeconds,
				DEFAULT_CONNECTED_SERVICES.transcription.timeoutSeconds,
			),
		},
		bulk: normalizeBulk(current.bulk),
		verify: normalizeVerify(current.verify),
		routing: normalizeRouting(current.routing),
		apiKeys: normalizeApiKeys(current.apiKeys),
	};
	return settings.connectedServices;
}

/**
 * Per-stage service overrides: `{ "<stage label>": "chat" | "think" | "task" }`.
 *
 * An entry naming a service that does not exist is dropped rather than kept.
 * A typo here would otherwise route a stage into nothing, and the failure would
 * surface as that stage silently not running rather than as a bad setting.
 */
function normalizeRouting(current) {
	if (!current || typeof current !== "object" || Array.isArray(current)) return {};
	const routing = {};
	for (const [stage, service] of Object.entries(current)) {
		const name = normalizeServiceName(service);
		if (name && ROUTABLE_SERVICES.has(name)) routing[stage] = name;
	}
	return routing;
}

const ROUTABLE_SERVICES = new Set(["chat", "think", "task"]);

// Every named inference service. `bulk.lanes` and `verify.service` validate
// against this so a typo disables the feature rather than routing into nothing.
// `chat2`/`think2` are deliberately absent from ROUTABLE_SERVICES above: they are
// fan-out lanes and a verify role, not per-stage routing targets.
const INFERENCE_SERVICE_NAMES = new Set(["chat", "think", "task", "delegate", "chat2", "think2"]);

/**
 * The bulk fan-out lane list: `{ lanes: ["chat", ...] }`. Names that do not name a
 * real inference service are dropped, duplicates collapse, and an empty result
 * falls back to the single primary `chat` lane — so a typo disables fan-out
 * rather than fanning batch work onto nothing.
 */
function normalizeBulk(current) {
	const raw = current && typeof current === "object" && !Array.isArray(current) ? current.lanes : undefined;
	const lanes = [];
	if (Array.isArray(raw)) {
		for (const value of raw) {
			const name = normalizeServiceName(value);
			if (name && INFERENCE_SERVICE_NAMES.has(name) && !lanes.includes(name)) lanes.push(name);
		}
	}
	return { lanes: lanes.length ? lanes : ["chat"] };
}

/**
 * The verify lane selector: `{ service: "think2" | null }`. A value that is not a
 * real inference service normalizes to `null`, which means "review on the primary
 * thinking lane" (`resolveThinkOrChat`) — the pre-existing behavior.
 */
function normalizeVerify(current) {
	const raw = current && typeof current === "object" && !Array.isArray(current) ? current.service : undefined;
	const name = normalizeServiceName(raw);
	return { service: name && INFERENCE_SERVICE_NAMES.has(name) ? name : null };
}

/**
 * Keep only non-empty string values. A key persisted as null, a number, or the
 * empty string is a half-finished edit, and treating it as configured would make
 * a provider send `Authorization: Bearer undefined` and read the resulting 401
 * as the provider refusing us.
 */
function normalizeApiKeys(current) {
	if (!current || typeof current !== "object" || Array.isArray(current)) return {};
	const keys = {};
	for (const [provider, value] of Object.entries(current)) {
		const normalized = normalizeServiceName(value);
		if (normalized) keys[provider] = normalized;
	}
	return keys;
}

/**
 * The env var that overrides a provider's persisted key. `semantic-scholar`
 * becomes FORGE_API_KEY_SEMANTIC_SCHOLAR.
 */
export function apiKeyEnvName(provider) {
	return `FORGE_API_KEY_${String(provider)
		.toUpperCase()
		.replace(/[^A-Z0-9]+/g, "_")}`;
}

function seedInferenceService(current, defaults) {
	const scheduling =
		current.scheduling && typeof current.scheduling === "object" && !Array.isArray(current.scheduling)
			? current.scheduling
			: {};
	return {
		enabled: current.enabled ?? defaults.enabled,
		baseUrl: normalizeHttpBaseUrl(current.baseUrl) ?? defaults.baseUrl,
		model: normalizeServiceName(current.model) ?? defaults.model,
		images: typeof current.images === "boolean" ? current.images : defaults.images,
		contextTokens: normalizePositiveInteger(current.contextTokens, defaults.contextTokens),
		chatTemplateKwargs: normalizeTemplateKwargs(current.chatTemplateKwargs) ?? defaults.chatTemplateKwargs,
		reasoningEffort: normalizeServiceName(current.reasoningEffort) ?? defaults.reasoningEffort,
		scheduling: {
			enabled: scheduling.enabled ?? defaults.scheduling.enabled,
			interactiveSlot: normalizeNonnegativeInteger(scheduling.interactiveSlot, defaults.scheduling.interactiveSlot),
			backgroundSlot: normalizeNonnegativeInteger(scheduling.backgroundSlot, defaults.scheduling.backgroundSlot),
			idleGraceMs: normalizeNonnegativeInteger(scheduling.idleGraceMs, defaults.scheduling.idleGraceMs),
			yieldMs: normalizeNonnegativeInteger(scheduling.yieldMs, defaults.scheduling.yieldMs),
			backgroundOutputTokens: normalizePositiveInteger(
				scheduling.backgroundOutputTokens,
				defaults.scheduling.backgroundOutputTokens,
			),
		},
	};
}

export function resolveConnectedServices(options = {}) {
	const env = options.env ?? process.env;
	const settings = options.settings ?? loadForgeSettings(env);
	const seeded = seedConnectedServicesSettings({ connectedServices: settings.connectedServices });
	const envSearxng = normalizeHttpBaseUrl(env.FORGE_SEARXNG_URL);
	const envPlaywright = normalizeWsEndpoint(env.FORGE_PLAYWRIGHT_WS_ENDPOINT);
	const envChat = normalizeHttpBaseUrl(env.FORGE_BASE_CHAT_URL || env.FORGE_CHAT_URL);
	const envChatModel = normalizeServiceName(env.FORGE_BASE_MODEL);
	const envThink = normalizeHttpBaseUrl(env.FORGE_THINK_URL);
	const envThinkModel = normalizeServiceName(env.FORGE_THINK_MODEL);
	const envTask = normalizeHttpBaseUrl(env.FORGE_TASK_URL);
	const envTaskModel = normalizeServiceName(env.FORGE_TASK_MODEL);
	const envEmbeddings = normalizeHttpBaseUrl(env.FORGE_EMBEDDINGS_URL);
	const envEmbeddingsModel = normalizeServiceName(env.FORGE_EMBEDDINGS_MODEL);
	const envTranscription = normalizeHttpBaseUrl(env.FORGE_TRANSCRIPTION_URL);
	const envTranscriptionEngine = normalizeServiceName(env.FORGE_TRANSCRIPTION_ENGINE);
	const envTranscriptionToken = normalizeServiceName(env.FORGE_TRANSCRIPTION_TOKEN);
	const transcriptionEnvPresent = Object.hasOwn(env, "FORGE_TRANSCRIPTION_URL");
	const searxngEnvPresent = Object.hasOwn(env, "FORGE_SEARXNG_URL");
	const envStackState = normalizeHttpBaseUrl(env.FORGE_STACK_STATE_URL);
	const playwrightEnvPresent = Object.hasOwn(env, "FORGE_PLAYWRIGHT_WS_ENDPOINT");
	const stackStateEnvPresent = Object.hasOwn(env, "FORGE_STACK_STATE_URL");
	const chatEnvPresent = Object.hasOwn(env, "FORGE_BASE_CHAT_URL") || Object.hasOwn(env, "FORGE_CHAT_URL");
	const thinkEnvPresent = Object.hasOwn(env, "FORGE_THINK_URL");
	const taskEnvPresent = Object.hasOwn(env, "FORGE_TASK_URL");
	const embeddingsEnvPresent = Object.hasOwn(env, "FORGE_EMBEDDINGS_URL");
	const explicitSearxng = normalizeHttpBaseUrl(options.searxngUrl);
	const explicitPlaywright = normalizeWsEndpoint(options.playwrightWsEndpoint);
	const explicitStackState = normalizeHttpBaseUrl(options.stackStateUrl);
	const explicitChat = normalizeHttpBaseUrl(options.chatUrl);
	const explicitChatModel = normalizeServiceName(options.chatModel);
	const explicitThink = normalizeHttpBaseUrl(options.thinkUrl);
	const explicitThinkModel = normalizeServiceName(options.thinkModel);
	const explicitTask = normalizeHttpBaseUrl(options.taskUrl);
	const explicitTaskModel = normalizeServiceName(options.taskModel);
	const explicitEmbeddings = normalizeHttpBaseUrl(options.embeddingsUrl);
	const explicitEmbeddingsModel = normalizeServiceName(options.embeddingsModel);
	const explicitTranscription = normalizeHttpBaseUrl(options.transcriptionUrl);
	const explicitTranscriptionEngine = normalizeServiceName(options.transcriptionEngine);
	const envChatContext = normalizePositiveInteger(parseInteger(env.FORGE_BASE_CHAT_CONTEXT_TOKENS), undefined);
	const envThinkContext = normalizePositiveInteger(parseInteger(env.FORGE_THINK_CONTEXT_TOKENS), undefined);
	const envChatTemplate = normalizeTemplateKwargs(env.FORGE_BASE_CHAT_TEMPLATE_KWARGS);
	const envThinkTemplate = normalizeTemplateKwargs(env.FORGE_THINK_TEMPLATE_KWARGS);
	const envTaskContext = normalizePositiveInteger(parseInteger(env.FORGE_TASK_CONTEXT_TOKENS), undefined);
	const envTaskTemplate = normalizeTemplateKwargs(env.FORGE_TASK_TEMPLATE_KWARGS);
	const explicitChatContext = normalizePositiveInteger(parseInteger(options.chatContextTokens), undefined);
	const explicitThinkContext = normalizePositiveInteger(parseInteger(options.thinkContextTokens), undefined);
	const explicitChatTemplate = normalizeTemplateKwargs(options.chatTemplateKwargs);
	const explicitThinkTemplate = normalizeTemplateKwargs(options.thinkTemplateKwargs);
	const explicitTaskContext = normalizePositiveInteger(parseInteger(options.taskContextTokens), undefined);
	const explicitTaskTemplate = normalizeTemplateKwargs(options.taskTemplateKwargs);
	const envChatEffort = normalizeServiceName(env.FORGE_BASE_CHAT_REASONING_EFFORT);
	const envThinkEffort = normalizeServiceName(env.FORGE_THINK_REASONING_EFFORT);
	const envTaskEffort = normalizeServiceName(env.FORGE_TASK_REASONING_EFFORT);
	const explicitChatEffort = normalizeServiceName(options.chatReasoningEffort);
	const explicitThinkEffort = normalizeServiceName(options.thinkReasoningEffort);
	const explicitTaskEffort = normalizeServiceName(options.taskReasoningEffort);
	// The delegate tier reuses `task`'s override shape (url/model/context/template/
	// effort), keyed under FORGE_DELEGATE_*. It is off unless persisted or env
	// enables it, so an env override that only sets, say, the model still leaves
	// the tier disabled — the URL or the persisted `enabled: true` is what turns it
	// on, matching how `chat`/`task` treat a bare URL as enabling.
	const envDelegate = normalizeHttpBaseUrl(env.FORGE_DELEGATE_URL);
	const envDelegateModel = normalizeServiceName(env.FORGE_DELEGATE_MODEL);
	const envDelegateContext = normalizePositiveInteger(parseInteger(env.FORGE_DELEGATE_CONTEXT_TOKENS), undefined);
	const envDelegateTemplate = normalizeTemplateKwargs(env.FORGE_DELEGATE_TEMPLATE_KWARGS);
	const envDelegateEffort = normalizeServiceName(env.FORGE_DELEGATE_REASONING_EFFORT);
	const delegateEnvPresent = Object.hasOwn(env, "FORGE_DELEGATE_URL");
	const explicitDelegate = normalizeHttpBaseUrl(options.delegateUrl);
	const explicitDelegateModel = normalizeServiceName(options.delegateModel);
	const explicitDelegateContext = normalizePositiveInteger(parseInteger(options.delegateContextTokens), undefined);
	const explicitDelegateTemplate = normalizeTemplateKwargs(options.delegateTemplateKwargs);
	const explicitDelegateEffort = normalizeServiceName(options.delegateReasoningEffort);
	// The GPU-2 bulk (`chat2`) and verify (`think2`) lanes reuse `delegate`'s override
	// shape, keyed under FORGE_CHAT2_* / FORGE_THINK2_*. Both are off unless persisted
	// or a URL enables them, matching every other tier: a bare URL turns the lane on.
	const envChat2 = normalizeHttpBaseUrl(env.FORGE_CHAT2_URL);
	const envChat2Model = normalizeServiceName(env.FORGE_CHAT2_MODEL);
	const envChat2Context = normalizePositiveInteger(parseInteger(env.FORGE_CHAT2_CONTEXT_TOKENS), undefined);
	const envChat2Template = normalizeTemplateKwargs(env.FORGE_CHAT2_TEMPLATE_KWARGS);
	const envChat2Effort = normalizeServiceName(env.FORGE_CHAT2_REASONING_EFFORT);
	const chat2EnvPresent = Object.hasOwn(env, "FORGE_CHAT2_URL");
	const explicitChat2 = normalizeHttpBaseUrl(options.chat2Url);
	const explicitChat2Model = normalizeServiceName(options.chat2Model);
	const explicitChat2Context = normalizePositiveInteger(parseInteger(options.chat2ContextTokens), undefined);
	const explicitChat2Template = normalizeTemplateKwargs(options.chat2TemplateKwargs);
	const explicitChat2Effort = normalizeServiceName(options.chat2ReasoningEffort);
	const envThink2 = normalizeHttpBaseUrl(env.FORGE_THINK2_URL);
	const envThink2Model = normalizeServiceName(env.FORGE_THINK2_MODEL);
	const envThink2Context = normalizePositiveInteger(parseInteger(env.FORGE_THINK2_CONTEXT_TOKENS), undefined);
	const envThink2Template = normalizeTemplateKwargs(env.FORGE_THINK2_TEMPLATE_KWARGS);
	const envThink2Effort = normalizeServiceName(env.FORGE_THINK2_REASONING_EFFORT);
	const think2EnvPresent = Object.hasOwn(env, "FORGE_THINK2_URL");
	const explicitThink2 = normalizeHttpBaseUrl(options.think2Url);
	const explicitThink2Model = normalizeServiceName(options.think2Model);
	const explicitThink2Context = normalizePositiveInteger(parseInteger(options.think2ContextTokens), undefined);
	const explicitThink2Template = normalizeTemplateKwargs(options.think2TemplateKwargs);
	const explicitThink2Effort = normalizeServiceName(options.think2ReasoningEffort);
	// OCR takes the same env name document-ingest already honors, so setting it in
	// one place reaches both the settings resolver and the skill's own default chain.
	const envOcr = normalizeHttpBaseUrl(env.FORGE_GLMOCR_URL || env.FORGE_OCR_URL);
	const ocrEnvPresent = Object.hasOwn(env, "FORGE_GLMOCR_URL") || Object.hasOwn(env, "FORGE_OCR_URL");
	const explicitOcr = normalizeHttpBaseUrl(options.ocrUrl);
	return {
		searxng: {
			enabled: explicitSearxng ? true : searxngEnvPresent ? Boolean(envSearxng) : seeded.searxng.enabled,
			baseUrl: explicitSearxng ?? envSearxng ?? seeded.searxng.baseUrl,
		},
		playwright: {
			enabled: explicitPlaywright ? true : playwrightEnvPresent ? Boolean(envPlaywright) : seeded.playwright.enabled,
			wsEndpoint: explicitPlaywright ?? envPlaywright ?? seeded.playwright.wsEndpoint,
		},
		stackState: {
			enabled: explicitStackState ? true : stackStateEnvPresent ? Boolean(envStackState) : seeded.stackState.enabled,
			baseUrl: explicitStackState ?? envStackState ?? seeded.stackState.baseUrl,
		},
		chat: {
			enabled: explicitChat ? true : chatEnvPresent ? Boolean(envChat) : seeded.chat.enabled,
			baseUrl: explicitChat ?? envChat ?? seeded.chat.baseUrl,
			model: explicitChatModel ?? envChatModel ?? seeded.chat.model,
			images: seeded.chat.images,
			contextTokens: explicitChatContext ?? envChatContext ?? seeded.chat.contextTokens,
			chatTemplateKwargs: explicitChatTemplate ?? envChatTemplate ?? seeded.chat.chatTemplateKwargs,
			reasoningEffort: explicitChatEffort ?? envChatEffort ?? seeded.chat.reasoningEffort,
			scheduling: seeded.chat.scheduling,
		},
		think: {
			enabled: explicitThink ? true : thinkEnvPresent ? Boolean(envThink) : seeded.think.enabled,
			baseUrl: explicitThink ?? envThink ?? seeded.think.baseUrl,
			model: explicitThinkModel ?? envThinkModel ?? seeded.think.model,
			images: seeded.think.images,
			contextTokens: explicitThinkContext ?? envThinkContext ?? seeded.think.contextTokens,
			chatTemplateKwargs: explicitThinkTemplate ?? envThinkTemplate ?? seeded.think.chatTemplateKwargs,
			reasoningEffort: explicitThinkEffort ?? envThinkEffort ?? seeded.think.reasoningEffort,
			scheduling: seeded.think.scheduling,
		},
		task: {
			enabled: explicitTask ? true : taskEnvPresent ? Boolean(envTask) : seeded.task.enabled,
			baseUrl: explicitTask ?? envTask ?? seeded.task.baseUrl,
			model: explicitTaskModel ?? envTaskModel ?? seeded.task.model,
			images: seeded.task.images,
			contextTokens: explicitTaskContext ?? envTaskContext ?? seeded.task.contextTokens,
			chatTemplateKwargs: explicitTaskTemplate ?? envTaskTemplate ?? seeded.task.chatTemplateKwargs,
			reasoningEffort: explicitTaskEffort ?? envTaskEffort ?? seeded.task.reasoningEffort,
			scheduling: seeded.task.scheduling,
		},
		delegate: {
			enabled: explicitDelegate ? true : delegateEnvPresent ? Boolean(envDelegate) : seeded.delegate.enabled,
			baseUrl: explicitDelegate ?? envDelegate ?? seeded.delegate.baseUrl,
			model: explicitDelegateModel ?? envDelegateModel ?? seeded.delegate.model,
			images: seeded.delegate.images,
			contextTokens: explicitDelegateContext ?? envDelegateContext ?? seeded.delegate.contextTokens,
			chatTemplateKwargs: explicitDelegateTemplate ?? envDelegateTemplate ?? seeded.delegate.chatTemplateKwargs,
			reasoningEffort: explicitDelegateEffort ?? envDelegateEffort ?? seeded.delegate.reasoningEffort,
			scheduling: seeded.delegate.scheduling,
		},
		chat2: {
			enabled: explicitChat2 ? true : chat2EnvPresent ? Boolean(envChat2) : seeded.chat2.enabled,
			baseUrl: explicitChat2 ?? envChat2 ?? seeded.chat2.baseUrl,
			model: explicitChat2Model ?? envChat2Model ?? seeded.chat2.model,
			images: seeded.chat2.images,
			contextTokens: explicitChat2Context ?? envChat2Context ?? seeded.chat2.contextTokens,
			chatTemplateKwargs: explicitChat2Template ?? envChat2Template ?? seeded.chat2.chatTemplateKwargs,
			reasoningEffort: explicitChat2Effort ?? envChat2Effort ?? seeded.chat2.reasoningEffort,
			scheduling: seeded.chat2.scheduling,
		},
		think2: {
			enabled: explicitThink2 ? true : think2EnvPresent ? Boolean(envThink2) : seeded.think2.enabled,
			baseUrl: explicitThink2 ?? envThink2 ?? seeded.think2.baseUrl,
			model: explicitThink2Model ?? envThink2Model ?? seeded.think2.model,
			images: seeded.think2.images,
			contextTokens: explicitThink2Context ?? envThink2Context ?? seeded.think2.contextTokens,
			chatTemplateKwargs: explicitThink2Template ?? envThink2Template ?? seeded.think2.chatTemplateKwargs,
			reasoningEffort: explicitThink2Effort ?? envThink2Effort ?? seeded.think2.reasoningEffort,
			scheduling: seeded.think2.scheduling,
		},
		bulk: { lanes: [...seeded.bulk.lanes] },
		verify: { service: seeded.verify.service },
		embeddings: {
			enabled: explicitEmbeddings ? true : embeddingsEnvPresent ? Boolean(envEmbeddings) : seeded.embeddings.enabled,
			url: explicitEmbeddings ?? envEmbeddings ?? seeded.embeddings.url,
			model: explicitEmbeddingsModel ?? envEmbeddingsModel ?? seeded.embeddings.model,
		},
		ocr: {
			enabled: explicitOcr ? true : ocrEnvPresent ? Boolean(envOcr) : seeded.ocr.enabled,
			url: explicitOcr ?? envOcr ?? seeded.ocr.url,
		},
		transcription: {
			enabled: explicitTranscription
				? true
				: transcriptionEnvPresent
					? Boolean(envTranscription)
					: seeded.transcription.enabled,
			baseUrl: explicitTranscription ?? envTranscription ?? seeded.transcription.baseUrl,
			engine: explicitTranscriptionEngine ?? envTranscriptionEngine ?? seeded.transcription.engine,
			token: envTranscriptionToken ?? seeded.transcription.token,
			timeoutSeconds: seeded.transcription.timeoutSeconds,
		},
		routing: { ...seeded.routing, ...(options.routing ?? {}) },
		apiKeys: resolveApiKeys(seeded.apiKeys, env, options.apiKeys),
	};
}

/**
 * Same precedence as every other service: explicit option, then environment,
 * then persisted settings. Env vars are also merged in for providers that have
 * no persisted entry at all, so `FORGE_API_KEY_OPENALEX=... forge ...` works on
 * a machine whose settings.json has never been touched.
 */
function resolveApiKeys(seeded, env, explicit) {
	const keys = { ...normalizeApiKeys(seeded) };
	const prefix = "FORGE_API_KEY_";
	for (const [name, value] of Object.entries(env)) {
		if (!name.startsWith(prefix)) continue;
		const normalized = normalizeServiceName(value);
		const provider = name.slice(prefix.length).toLowerCase().replace(/_/g, "-");
		// An env var set to the empty string turns a persisted key off for this
		// process, matching how FORGE_SEARXNG_URL="" disables search.
		if (normalized) keys[provider] = normalized;
		else delete keys[provider];
	}
	for (const [provider, value] of Object.entries(normalizeApiKeys(explicit))) keys[provider] = value;
	return keys;
}

/**
 * The key for one provider, or null. Callers treat null as "this provider is
 * not available right now", never as an error.
 */
export function resolveApiKey(provider, options = {}) {
	const services = options.services ?? resolveConnectedServices(options);
	return services.apiKeys?.[provider] ?? null;
}

/**
 * The service to use for judgment and verification. Falls back to the batch
 * chat service when no thinking backend is configured, so a single-endpoint
 * install still verifies (just without the thinking/non-thinking split) rather
 * than skipping the quality net.
 */
export function resolveThinkOrChat(services) {
	return services.think?.enabled ? services.think : services.chat;
}

/**
 * The small tier, falling back to `chat` when it is not configured — which is
 * the default. The fallback direction matches `resolveThinkOrChat`: an
 * unconfigured tier degrades *toward* the 27B, never away from it. A stage is
 * routed here because a small model was measured to do it better, but "better"
 * was measured against `chat`, so `chat` is always an acceptable answer.
 */
export function resolveTaskOrChat(services) {
	return services.task?.enabled ? services.task : services.chat;
}

/**
 * The delegation target, falling back to `chat` when no secondary is configured
 * — which is the default. Enabled means a second backend exists on another GPU,
 * so `forge_delegate` runs there in parallel with the interactive session; off
 * means the investigation runs on the primary `chat` weights against the
 * background slot, exactly as it did before a secondary was a possibility. The
 * fallback is what makes the tool always available regardless of the setup.
 */
export function resolveDelegateOrChat(services) {
	return services.delegate?.enabled ? services.delegate : services.chat;
}

/**
 * The raw service block skill verification/review runs on. Chain: the lane named
 * by `verify.service` (normally `think2` on the second GPU) → the primary thinking
 * lane → `chat`. A `distributed-parallel` setup reviews on an independent GPU-2
 * instance; every other setup degrades toward the always-present thinking lane,
 * exactly as `resolveThinkOrChat` does, so verification is never skipped for lack
 * of a secondary. Returns a raw block (like `resolveThinkOrChat`); the caller in
 * `forge-llm.mjs` shapes it and labels any degrade.
 */
export function resolveVerifyOrThinkOrChat(services) {
	const wanted = services.verify?.service;
	if (wanted && services[wanted]?.enabled) return services[wanted];
	return resolveThinkOrChat(services);
}

/**
 * The ordered list of resolved services a batch skill fans per-item bulk work
 * across, from `bulk.lanes`. Disabled lanes are dropped; when `carriesImage` is
 * true every `images:false` lane is dropped too (an image must never land on a
 * text-only GPU-2 lane, where the transform layer would silently drop it). The
 * result always keeps at least the primary `chat` lane, so a misconfiguration
 * degrades to single-lane rather than to nothing.
 */
export function resolveBulkLanes(services, { carriesImage = false } = {}) {
	const names = services.bulk?.lanes?.length ? services.bulk.lanes : ["chat"];
	const lanes = [];
	for (const name of names) {
		const service = services[name];
		if (!service?.enabled) continue;
		if (carriesImage && service.images === false) continue;
		lanes.push(service);
	}
	if (!lanes.length && services.chat?.enabled && !(carriesImage && services.chat.images === false)) {
		lanes.push(services.chat);
	}
	return lanes;
}

function normalizeHttpBaseUrl(value) {
	if (typeof value !== "string") return undefined;
	const trimmed = value.trim();
	if (!trimmed) return undefined;
	return trimmed.replace(/\/+$/, "");
}

function normalizeWsEndpoint(value) {
	if (typeof value !== "string") return undefined;
	const trimmed = value.trim();
	if (!trimmed) return undefined;
	return trimmed.replace(/\/+$/, "");
}

function normalizeServiceName(value) {
	if (typeof value !== "string") return undefined;
	const trimmed = value.trim();
	return trimmed || undefined;
}

function normalizeNonnegativeInteger(value, fallback) {
	return Number.isInteger(value) && value >= 0 ? value : fallback;
}

function normalizePositiveInteger(value, fallback) {
	return Number.isInteger(value) && value > 0 ? value : fallback;
}

/** A settings value or env var that should be a count of tokens. */
function parseInteger(value) {
	if (Number.isInteger(value)) return value;
	if (typeof value !== "string" || !value.trim()) return undefined;
	const parsed = Number.parseInt(value.trim(), 10);
	return Number.isNaN(parsed) ? undefined : parsed;
}

/**
 * A `chat_template_kwargs` object, from a settings mapping or a JSON string in
 * an env var. Anything else — a bare string, an array, malformed JSON, or `{}` —
 * is treated as unset, because forwarding it would make the backend reject the
 * whole request rather than ignore one bad field.
 */
function normalizeTemplateKwargs(value) {
	if (value && typeof value === "object" && !Array.isArray(value)) {
		return Object.keys(value).length ? { ...value } : undefined;
	}
	if (typeof value !== "string" || !value.trim()) return undefined;
	let parsed;
	try {
		parsed = JSON.parse(value);
	} catch {
		return undefined;
	}
	if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return undefined;
	return Object.keys(parsed).length ? parsed : undefined;
}

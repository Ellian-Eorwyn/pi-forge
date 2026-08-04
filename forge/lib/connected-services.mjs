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

export const DEFAULT_CONNECTED_SERVICES = Object.freeze({
	searxng: Object.freeze({
		enabled: true,
		baseUrl: "http://llms/searxng",
	}),
	playwright: Object.freeze({
		enabled: true,
		wsEndpoint: "ws://llms/playwright",
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
		contextTokens: SLOT_CONTEXT_TOKENS,
		chatTemplateKwargs: null,
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
		contextTokens: SLOT_CONTEXT_TOKENS,
		chatTemplateKwargs: null,
		scheduling: Object.freeze({
			enabled: true,
			interactiveSlot: 0,
			backgroundSlot: 1,
			idleGraceMs: 2000,
			yieldMs: 1000,
			backgroundOutputTokens: 4096,
		}),
	}),
	embeddings: Object.freeze({
		enabled: true,
		url: "http://llms:8005/v1/embeddings",
		model: "embed",
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
		settings.connectedServices && typeof settings.connectedServices === "object" && !Array.isArray(settings.connectedServices)
			? settings.connectedServices
			: {};
	const searxng = current.searxng && typeof current.searxng === "object" && !Array.isArray(current.searxng) ? current.searxng : {};
	const playwright =
		current.playwright && typeof current.playwright === "object" && !Array.isArray(current.playwright)
			? current.playwright
			: {};
	const chat = current.chat && typeof current.chat === "object" && !Array.isArray(current.chat) ? current.chat : {};
	const think = current.think && typeof current.think === "object" && !Array.isArray(current.think) ? current.think : {};
	const embeddings =
		current.embeddings && typeof current.embeddings === "object" && !Array.isArray(current.embeddings)
			? current.embeddings
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
		chat: seedInferenceService(chat, DEFAULT_CONNECTED_SERVICES.chat),
		think: seedInferenceService(think, DEFAULT_CONNECTED_SERVICES.think),
		embeddings: {
			enabled: embeddings.enabled ?? DEFAULT_CONNECTED_SERVICES.embeddings.enabled,
			url: normalizeHttpBaseUrl(embeddings.url) ?? DEFAULT_CONNECTED_SERVICES.embeddings.url,
			model: normalizeServiceName(embeddings.model) ?? DEFAULT_CONNECTED_SERVICES.embeddings.model,
		},
		apiKeys: normalizeApiKeys(current.apiKeys),
	};
	return settings.connectedServices;
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
	return `FORGE_API_KEY_${String(provider).toUpperCase().replace(/[^A-Z0-9]+/g, "_")}`;
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
		contextTokens: normalizePositiveInteger(current.contextTokens, defaults.contextTokens),
		chatTemplateKwargs: normalizeTemplateKwargs(current.chatTemplateKwargs) ?? defaults.chatTemplateKwargs,
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
	const envEmbeddings = normalizeHttpBaseUrl(env.FORGE_EMBEDDINGS_URL);
	const envEmbeddingsModel = normalizeServiceName(env.FORGE_EMBEDDINGS_MODEL);
	const searxngEnvPresent = Object.hasOwn(env, "FORGE_SEARXNG_URL");
	const playwrightEnvPresent = Object.hasOwn(env, "FORGE_PLAYWRIGHT_WS_ENDPOINT");
	const chatEnvPresent = Object.hasOwn(env, "FORGE_BASE_CHAT_URL") || Object.hasOwn(env, "FORGE_CHAT_URL");
	const thinkEnvPresent = Object.hasOwn(env, "FORGE_THINK_URL");
	const embeddingsEnvPresent = Object.hasOwn(env, "FORGE_EMBEDDINGS_URL");
	const explicitSearxng = normalizeHttpBaseUrl(options.searxngUrl);
	const explicitPlaywright = normalizeWsEndpoint(options.playwrightWsEndpoint);
	const explicitChat = normalizeHttpBaseUrl(options.chatUrl);
	const explicitChatModel = normalizeServiceName(options.chatModel);
	const explicitThink = normalizeHttpBaseUrl(options.thinkUrl);
	const explicitThinkModel = normalizeServiceName(options.thinkModel);
	const explicitEmbeddings = normalizeHttpBaseUrl(options.embeddingsUrl);
	const explicitEmbeddingsModel = normalizeServiceName(options.embeddingsModel);
	const envChatContext = normalizePositiveInteger(parseInteger(env.FORGE_BASE_CHAT_CONTEXT_TOKENS), undefined);
	const envThinkContext = normalizePositiveInteger(parseInteger(env.FORGE_THINK_CONTEXT_TOKENS), undefined);
	const envChatTemplate = normalizeTemplateKwargs(env.FORGE_BASE_CHAT_TEMPLATE_KWARGS);
	const envThinkTemplate = normalizeTemplateKwargs(env.FORGE_THINK_TEMPLATE_KWARGS);
	const explicitChatContext = normalizePositiveInteger(parseInteger(options.chatContextTokens), undefined);
	const explicitThinkContext = normalizePositiveInteger(parseInteger(options.thinkContextTokens), undefined);
	const explicitChatTemplate = normalizeTemplateKwargs(options.chatTemplateKwargs);
	const explicitThinkTemplate = normalizeTemplateKwargs(options.thinkTemplateKwargs);
	return {
		searxng: {
			enabled: explicitSearxng ? true : searxngEnvPresent ? Boolean(envSearxng) : seeded.searxng.enabled,
			baseUrl: explicitSearxng ?? envSearxng ?? seeded.searxng.baseUrl,
		},
		playwright: {
			enabled: explicitPlaywright ? true : playwrightEnvPresent ? Boolean(envPlaywright) : seeded.playwright.enabled,
			wsEndpoint: explicitPlaywright ?? envPlaywright ?? seeded.playwright.wsEndpoint,
		},
		chat: {
			enabled: explicitChat ? true : chatEnvPresent ? Boolean(envChat) : seeded.chat.enabled,
			baseUrl: explicitChat ?? envChat ?? seeded.chat.baseUrl,
			model: explicitChatModel ?? envChatModel ?? seeded.chat.model,
			contextTokens: explicitChatContext ?? envChatContext ?? seeded.chat.contextTokens,
			chatTemplateKwargs: explicitChatTemplate ?? envChatTemplate ?? seeded.chat.chatTemplateKwargs,
			scheduling: seeded.chat.scheduling,
		},
		think: {
			enabled: explicitThink ? true : thinkEnvPresent ? Boolean(envThink) : seeded.think.enabled,
			baseUrl: explicitThink ?? envThink ?? seeded.think.baseUrl,
			model: explicitThinkModel ?? envThinkModel ?? seeded.think.model,
			contextTokens: explicitThinkContext ?? envThinkContext ?? seeded.think.contextTokens,
			chatTemplateKwargs: explicitThinkTemplate ?? envThinkTemplate ?? seeded.think.chatTemplateKwargs,
			scheduling: seeded.think.scheduling,
		},
		embeddings: {
			enabled: explicitEmbeddings ? true : embeddingsEnvPresent ? Boolean(envEmbeddings) : seeded.embeddings.enabled,
			url: explicitEmbeddings ?? envEmbeddings ?? seeded.embeddings.url,
			model: explicitEmbeddingsModel ?? envEmbeddingsModel ?? seeded.embeddings.model,
		},
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

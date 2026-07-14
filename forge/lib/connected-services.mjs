import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export const DEFAULT_CONNECTED_SERVICES = Object.freeze({
	searxng: Object.freeze({
		enabled: true,
		baseUrl: "http://llms/searxng",
	}),
	playwright: Object.freeze({
		enabled: true,
		wsEndpoint: "ws://llms/playwright",
	}),
	chat: Object.freeze({
		enabled: true,
		baseUrl: "http://llms:8008/v1/chat/completions",
		model: "code",
	}),
	embeddings: Object.freeze({
		enabled: true,
		url: "http://llms:8005/v1/embeddings",
		model: "embed",
	}),
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
		chat: {
			enabled: chat.enabled ?? DEFAULT_CONNECTED_SERVICES.chat.enabled,
			baseUrl: normalizeHttpBaseUrl(chat.baseUrl) ?? DEFAULT_CONNECTED_SERVICES.chat.baseUrl,
			model: normalizeServiceName(chat.model) ?? DEFAULT_CONNECTED_SERVICES.chat.model,
		},
		embeddings: {
			enabled: embeddings.enabled ?? DEFAULT_CONNECTED_SERVICES.embeddings.enabled,
			url: normalizeHttpBaseUrl(embeddings.url) ?? DEFAULT_CONNECTED_SERVICES.embeddings.url,
			model: normalizeServiceName(embeddings.model) ?? DEFAULT_CONNECTED_SERVICES.embeddings.model,
		},
	};
	return settings.connectedServices;
}

export function resolveConnectedServices(options = {}) {
	const env = options.env ?? process.env;
	const settings = options.settings ?? loadForgeSettings(env);
	const seeded = seedConnectedServicesSettings({ connectedServices: settings.connectedServices });
	const envSearxng = normalizeHttpBaseUrl(env.FORGE_SEARXNG_URL);
	const envPlaywright = normalizeWsEndpoint(env.FORGE_PLAYWRIGHT_WS_ENDPOINT);
	const envChat = normalizeHttpBaseUrl(env.FORGE_BASE_CHAT_URL || env.FORGE_CHAT_URL);
	const envChatModel = normalizeServiceName(env.FORGE_BASE_MODEL);
	const envEmbeddings = normalizeHttpBaseUrl(env.FORGE_EMBEDDINGS_URL);
	const envEmbeddingsModel = normalizeServiceName(env.FORGE_EMBEDDINGS_MODEL);
	const searxngEnvPresent = Object.hasOwn(env, "FORGE_SEARXNG_URL");
	const playwrightEnvPresent = Object.hasOwn(env, "FORGE_PLAYWRIGHT_WS_ENDPOINT");
	const chatEnvPresent = Object.hasOwn(env, "FORGE_BASE_CHAT_URL") || Object.hasOwn(env, "FORGE_CHAT_URL");
	const embeddingsEnvPresent = Object.hasOwn(env, "FORGE_EMBEDDINGS_URL");
	const explicitSearxng = normalizeHttpBaseUrl(options.searxngUrl);
	const explicitPlaywright = normalizeWsEndpoint(options.playwrightWsEndpoint);
	const explicitChat = normalizeHttpBaseUrl(options.chatUrl);
	const explicitChatModel = normalizeServiceName(options.chatModel);
	const explicitEmbeddings = normalizeHttpBaseUrl(options.embeddingsUrl);
	const explicitEmbeddingsModel = normalizeServiceName(options.embeddingsModel);
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
		},
		embeddings: {
			enabled: explicitEmbeddings ? true : embeddingsEnvPresent ? Boolean(envEmbeddings) : seeded.embeddings.enabled,
			url: explicitEmbeddings ?? envEmbeddings ?? seeded.embeddings.url,
			model: explicitEmbeddingsModel ?? envEmbeddingsModel ?? seeded.embeddings.model,
		},
	};
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

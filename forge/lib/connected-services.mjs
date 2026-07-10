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
	};
	return settings.connectedServices;
}

export function resolveConnectedServices(options = {}) {
	const env = options.env ?? process.env;
	const settings = options.settings ?? loadForgeSettings(env);
	const seeded = seedConnectedServicesSettings({ connectedServices: settings.connectedServices });
	const envSearxng = normalizeHttpBaseUrl(env.FORGE_SEARXNG_URL);
	const envPlaywright = normalizeWsEndpoint(env.FORGE_PLAYWRIGHT_WS_ENDPOINT);
	const searxngEnvPresent = Object.hasOwn(env, "FORGE_SEARXNG_URL");
	const playwrightEnvPresent = Object.hasOwn(env, "FORGE_PLAYWRIGHT_WS_ENDPOINT");
	const explicitSearxng = normalizeHttpBaseUrl(options.searxngUrl);
	const explicitPlaywright = normalizeWsEndpoint(options.playwrightWsEndpoint);
	return {
		searxng: {
			enabled: explicitSearxng ? true : searxngEnvPresent ? Boolean(envSearxng) : seeded.searxng.enabled,
			baseUrl: explicitSearxng ?? envSearxng ?? seeded.searxng.baseUrl,
		},
		playwright: {
			enabled: explicitPlaywright ? true : playwrightEnvPresent ? Boolean(envPlaywright) : seeded.playwright.enabled,
			wsEndpoint: explicitPlaywright ?? envPlaywright ?? seeded.playwright.wsEndpoint,
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

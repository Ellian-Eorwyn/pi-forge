#!/usr/bin/env node

import { chmodSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { seedConnectedServicesSettings } from "../lib/connected-services.mjs";

// Local model limits for the forge-local "code" model. Kept here so every
// install and `pi-forge-update` writes the same context and output budgets.
const CONTEXT_WINDOW = 262144;
const MAX_OUTPUT_TOKENS = 32768;
const COMPACTION_TRIGGER_RATIO = 0.75;
const COMPACTION_RESERVE_TOKENS = CONTEXT_WINDOW - Math.floor(CONTEXT_WINDOW * COMPACTION_TRIGGER_RATIO);
const CONTEXT_BUDGET_SOFT_RATIO = COMPACTION_TRIGGER_RATIO;
const CONTEXT_BUDGET_VERBATIM_RECENT_TOKENS = 20000;

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

const packages = Array.isArray(settings.packages) ? settings.packages : [];
const retainedPackages = packages.filter((entry) => {
	if (!previousProfileDirectory) return true;
	if (typeof entry === "string") return resolve(entry) !== previousProfileDirectory;
	return typeof entry?.source !== "string" || resolve(entry.source) !== previousProfileDirectory;
});

const profileInstructions = readFileSync(sourceAgentsPath, "utf8");
settings.packages = [profileDirectory, ...retainedPackages];
settings.defaultProvider = "forge-local";
settings.defaultModel = "code";
const existingCompaction =
	settings.compaction !== null && typeof settings.compaction === "object" && !Array.isArray(settings.compaction)
		? settings.compaction
		: {};
const existingContextBudget =
	settings.contextBudget !== null && typeof settings.contextBudget === "object" && !Array.isArray(settings.contextBudget)
		? settings.contextBudget
		: {};
settings.compaction = {
	...existingCompaction,
	enabled: true,
	reserveTokens: COMPACTION_RESERVE_TOKENS,
};
delete settings.taskModel;
settings.contextBudget = {
	...existingContextBudget,
	enabled: true,
	softRatio: CONTEXT_BUDGET_SOFT_RATIO,
	useTaskModel: false,
	verbatimRecentTokens: CONTEXT_BUDGET_VERBATIM_RECENT_TOKENS,
};
seedConnectedServicesSettings(settings);

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
				contextWindow: CONTEXT_WINDOW,
				maxTokens: MAX_OUTPUT_TOKENS,
			},
		],
};
writeFileSync(settingsPath, `${JSON.stringify(settings, undefined, "\t")}\n`, { mode: 0o600 });
writeFileSync(modelsPath, `${JSON.stringify(models, undefined, "\t")}\n`, { mode: 0o600 });
writeFileSync(installedAgentsPath, profileInstructions, { mode: 0o600 });
chmodSync(installedAgentsPath, 0o600);
writeFileSync(profilePathMarker, `${profileDirectory}\n`, { mode: 0o600 });

#!/usr/bin/env node

import { chmodSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

const [agentDirectoryArgument, profileDirectoryArgument] = process.argv.slice(2);
if (!agentDirectoryArgument || !profileDirectoryArgument) {
	console.error("Usage: configure-pi-forge.mjs <agent-directory> <profile-directory>");
	process.exit(2);
}

const agentDirectory = resolve(agentDirectoryArgument);
const profileDirectory = resolve(profileDirectoryArgument);
const settingsPath = join(agentDirectory, "settings.json");
const profilePathMarker = join(agentDirectory, ".pi-forge-profile-path");
const sourceAgentsPath = join(profileDirectory, "AGENTS.md");
const installedAgentsPath = join(agentDirectory, "AGENTS.md");
mkdirSync(agentDirectory, { recursive: true });

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
writeFileSync(settingsPath, `${JSON.stringify(settings, undefined, "\t")}\n`, { mode: 0o600 });
writeFileSync(installedAgentsPath, profileInstructions, { mode: 0o600 });
chmodSync(installedAgentsPath, 0o600);
writeFileSync(profilePathMarker, `${profileDirectory}\n`, { mode: 0o600 });

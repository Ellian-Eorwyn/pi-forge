#!/usr/bin/env node

/**
 * Switch and inspect backend setups from the terminal.
 *
 *   backends.mjs list                 — the setups and which is active
 *   backends.mjs show                 — the endpoints the active setup resolves to
 *   backends.mjs use <name>           — make <name> the active setup and apply it
 *   backends.mjs apply                — re-apply the active setup
 *   backends.mjs delegation on|off    — toggle delegation on the active setup
 *
 * All of it is the `backends.mjs` library; this is the CLI face. The `/backend`
 * slash command drives the same functions in-process.
 */

import { activeProfileName, applyProfile, loadBackends, projectProfile, setDelegation } from "../lib/backends.mjs";
import { resolveConnectedServices } from "../lib/connected-services.mjs";

function fail(message) {
	process.stderr.write(`${message}\n`);
	process.exit(1);
}

function printSummary(result) {
	process.stdout.write(`Active setup: ${result.profile}\n`);
	if (result.description) process.stdout.write(`  ${result.description}\n`);
	const cs = result.connectedServices;
	process.stdout.write(
		`  primary chat  : ${cs.chat.baseUrl} (model ${cs.chat.model}, ${cs.chat.contextTokens} ctx)\n`,
	);
	process.stdout.write(`  primary think : ${cs.think.baseUrl} (model ${cs.think.model})\n`);
	process.stdout.write(
		result.delegation === "on"
			? `  delegation    : on → ${cs.delegate.baseUrl} (model ${cs.delegate.model ?? "?"})\n`
			: "  delegation    : off (falls back to primary chat)\n",
	);
	if (cs.embeddings?.url) process.stdout.write(`  embedding     : ${cs.embeddings.url}\n`);
	if (cs.transcription?.baseUrl) process.stdout.write(`  transcription : ${cs.transcription.baseUrl}\n`);
	if (cs.ocr?.url) process.stdout.write(`  ocr           : ${cs.ocr.url}\n`);
	if (result.missingProviders?.length) {
		process.stderr.write(
			`  note: models.json has no provider ${result.missingProviders.join(", ")} to update — ` +
				"run the installer once so the interactive agent tracks this setup too.\n",
		);
	}
}

function cmdList() {
	const config = loadBackends();
	const active = activeProfileName(config);
	for (const [name, profile] of Object.entries(config.profiles ?? {})) {
		const mark = name === active ? "*" : " ";
		const description = typeof profile.description === "string" ? profile.description : "";
		process.stdout.write(`${mark} ${name}\n`);
		if (description) process.stdout.write(`    ${description}\n`);
	}
}

function cmdShow() {
	const config = loadBackends();
	const active = activeProfileName(config);
	// What a skill or the delegate would actually resolve right now — the settings
	// on disk, not just the setup, so an env override or hand edit is visible too.
	const services = resolveConnectedServices({});
	process.stdout.write(`Active setup: ${active}\n`);
	process.stdout.write(`  chat          : ${services.chat.baseUrl} (${services.chat.contextTokens} ctx)\n`);
	process.stdout.write(`  think         : ${services.think.baseUrl}\n`);
	process.stdout.write(
		services.delegate.enabled
			? `  delegate      : ${services.delegate.baseUrl} (model ${services.delegate.model})\n`
			: "  delegate      : off (forge_delegate runs on primary chat)\n",
	);
	process.stdout.write(`  embeddings    : ${services.embeddings.url} (model ${services.embeddings.model})\n`);
	process.stdout.write(
		`  transcription : ${services.transcription.baseUrl} (engine ${services.transcription.engine})\n`,
	);
	process.stdout.write(`  ocr           : ${services.ocr.url}\n`);
}

function main() {
	const [command, argument] = process.argv.slice(2);
	switch (command) {
		case undefined:
		case "list":
			cmdList();
			return;
		case "show":
			cmdShow();
			return;
		case "use": {
			if (!argument) fail("usage: backends.mjs use <setup-name>");
			// Validate the projection before touching disk, so a malformed setup fails
			// with its own message rather than half-writing the registries.
			const config = loadBackends();
			if (!config.profiles?.[argument]) {
				fail(`unknown setup ${JSON.stringify(argument)}; known: ${Object.keys(config.profiles ?? {}).join(", ")}`);
			}
			projectProfile(config.profiles[argument]);
			printSummary(applyProfile({ name: argument }));
			return;
		}
		case "apply":
			printSummary(applyProfile({}));
			return;
		case "delegation": {
			if (argument !== "on" && argument !== "off") fail("usage: backends.mjs delegation on|off");
			printSummary(setDelegation({ enabled: argument === "on" }));
			return;
		}
		default:
			fail(`unknown command: ${command}\nusage: backends.mjs [list|show|use <name>|apply|delegation on|off]`);
	}
}

try {
	main();
} catch (error) {
	fail(error instanceof Error ? error.message : String(error));
}

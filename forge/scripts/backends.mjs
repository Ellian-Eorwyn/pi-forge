#!/usr/bin/env node

/**
 * Switch and inspect backend setups from the terminal.
 *
 *   backends.mjs list                 — the setups and which is active
 *   backends.mjs show                 — the endpoints the active setup resolves to
 *   backends.mjs use <name>           — make <name> the active setup and apply it
 *   backends.mjs single               — failsafe: revert to the single-model setup
 *   backends.mjs parallel             — switch to the two-GPU distributed-parallel setup
 *   backends.mjs apply                — re-apply the active setup
 *   backends.mjs delegation on|off    — toggle delegation on the active setup
 *
 * All of it is the `backends.mjs` library; this is the CLI face. The `/backend`,
 * `/single`, and `/parallel` slash commands drive the same functions in-process.
 */

import {
	activeProfileName,
	applyProfile,
	DEFAULT_BACKENDS,
	loadBackends,
	projectProfile,
	saveBackends,
	setDelegation,
} from "../lib/backends.mjs";
import { loadForgeSettings, resolveConnectedServices } from "../lib/connected-services.mjs";

/** Seed a shipped setup into backends.json if the runtime file predates it. */
function ensureShippedProfile(name) {
	const config = loadBackends();
	if (config.profiles?.[name]) return;
	const template = DEFAULT_BACKENDS.profiles[name];
	if (!template) return;
	config.profiles = { ...(config.profiles ?? {}), [name]: template };
	saveBackends(config);
}

function fail(message) {
	process.stderr.write(`${message}\n`);
	process.exit(1);
}

function printSummary(result) {
	process.stdout.write(`Active setup: ${result.profile}\n`);
	if (result.description) process.stdout.write(`  ${result.description}\n`);
	const cs = result.connectedServices;
	const ctxSource = result.contextProbed
		? "read from the stack"
		: result.contextKept
			? "kept from the last read — the stack state API is unreachable"
			: "declared by the setup";
	process.stdout.write(
		`  primary chat  : ${cs.chat.baseUrl} (model ${cs.chat.model}, ${cs.chat.contextTokens} ctx, ${ctxSource})\n`,
	);
	process.stdout.write(`  primary think : ${cs.think.baseUrl} (model ${cs.think.model})\n`);
	if (cs.bulk?.lanes) {
		process.stdout.write(
			`  bulk lanes    : ${cs.bulk.lanes.join(", ")}${cs.bulk.lanes.length > 1 ? " (fan-out across GPUs)" : ""}\n`,
		);
	}
	if (cs.verify) {
		process.stdout.write(
			cs.verify.service ? `  verify        : ${cs.verify.service}\n` : "  verify        : primary think\n",
		);
	}
	if (cs.chat2?.enabled) process.stdout.write(`  chat2         : ${cs.chat2.baseUrl} (model ${cs.chat2.model})\n`);
	if (cs.think2?.enabled) process.stdout.write(`  think2        : ${cs.think2.baseUrl} (model ${cs.think2.model})\n`);
	process.stdout.write(
		result.delegation === "on"
			? `  delegation    : on → ${cs.delegate.baseUrl} (model ${cs.delegate.model ?? "?"})\n`
			: "  delegation    : off (forge_delegate is not registered — `multi` to enable)\n",
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
	const settings = loadForgeSettings();
	const taskModel =
		settings.taskModel && typeof settings.taskModel === "object" && !Array.isArray(settings.taskModel)
			? settings.taskModel
			: {};
	const contextBudget =
		settings.contextBudget && typeof settings.contextBudget === "object" && !Array.isArray(settings.contextBudget)
			? settings.contextBudget
			: {};
	process.stdout.write(`Active setup: ${active}\n`);
	process.stdout.write(`  chat          : ${services.chat.baseUrl} (${services.chat.contextTokens} ctx)\n`);
	process.stdout.write(`  think         : ${services.think.baseUrl}\n`);
	process.stdout.write(
		`  bulk lanes    : ${services.bulk.lanes.join(", ")}${services.bulk.lanes.length > 1 ? " (fan-out across GPUs)" : ""}\n`,
	);
	process.stdout.write(
		services.verify.service && services[services.verify.service]?.enabled
			? `  verify        : ${services.verify.service} → ${services[services.verify.service].baseUrl}\n`
			: `  verify        : primary think (${services.think.baseUrl})\n`,
	);
	if (services.chat2.enabled) process.stdout.write(`  chat2         : ${services.chat2.baseUrl}\n`);
	if (services.think2.enabled) process.stdout.write(`  think2        : ${services.think2.baseUrl}\n`);
	process.stdout.write(
		services.delegate.enabled
			? `  delegate      : ${services.delegate.baseUrl} (model ${services.delegate.model})\n`
			: "  delegate      : off (forge_delegate is not registered — `multi` to enable)\n",
	);
	process.stdout.write(
		taskModel.enabled && contextBudget.useTaskModel
			? `  compaction    : offload → ${taskModel.baseUrl}\n`
			: "  compaction    : on primary (no offload)\n",
	);
	process.stdout.write(`  embeddings    : ${services.embeddings.url} (model ${services.embeddings.model})\n`);
	process.stdout.write(
		`  transcription : ${services.transcription.baseUrl} (engine ${services.transcription.engine})\n`,
	);
	process.stdout.write(`  ocr           : ${services.ocr.url}\n`);
}

async function main() {
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
			printSummary(await applyProfile({ name: argument }));
			return;
		}
		case "single":
			// The default, and the one-word revert; `single` always exists as shipped.
			printSummary(await applyProfile({ name: "single" }));
			return;
		case "multi":
		case "parallel":
			ensureShippedProfile("distributed-parallel");
			printSummary(await applyProfile({ name: "distributed-parallel" }));
			return;
		case "apply":
			printSummary(await applyProfile({}));
			return;
		case "delegation": {
			if (argument !== "on" && argument !== "off") fail("usage: backends.mjs delegation on|off");
			printSummary(await setDelegation({ enabled: argument === "on" }));
			return;
		}
		default:
			fail(
				`unknown command: ${command}\n` +
					"usage: backends.mjs [list|show|use <name>|single|multi|apply|delegation on|off]",
			);
	}
}

try {
	await main();
} catch (error) {
	fail(error instanceof Error ? error.message : String(error));
}

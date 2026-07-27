import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { ADVANCED_FLAGS, buildAdvancedArgs } from "../forge/extensions/web-research.ts";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const cliPath = join(repositoryRoot, "forge", "skills", "web-research", "scripts", "web-research.mjs");

/** The CLI's own option table: `"--flag": { key: "name", value: true|false, ... }`. */
function cliOptions(): Map<string, { key: string; takesValue: boolean }> {
	const source = readFileSync(cliPath, "utf8");
	const options = new Map<string, { key: string; takesValue: boolean }>();
	for (const match of source.matchAll(/"(--[a-z0-9-]+)":\s*\{\s*key:\s*"([A-Za-z0-9]+)",\s*value:\s*(true|false)/g)) {
		options.set(match[1], { key: match[2], takesValue: match[3] === "true" });
	}
	return options;
}

test("advanced options map to the flags the CLI actually accepts", () => {
	const options = cliOptions();
	assert.ok(options.size > 20, "failed to parse the CLI option table");
	for (const [name, { flag, boolean }] of Object.entries(ADVANCED_FLAGS)) {
		const option = options.get(flag);
		assert.ok(option, `${name} maps to ${flag}, which the CLI does not accept`);
		assert.equal(
			option.takesValue,
			!boolean,
			`${name} is declared ${boolean ? "boolean" : "valued"} but the CLI expects the opposite for ${flag}`,
		);
	}
});

test("valued and boolean options render as the CLI expects", () => {
	assert.deepEqual(buildAdvancedArgs({ evidenceBatchChars: 12000 }), ["--evidence-batch-chars", "12000"]);
	assert.deepEqual(buildAdvancedArgs({ playwrightConcurrency: 4 }), ["--playwright-concurrency", "4"]);
	assert.deepEqual(buildAdvancedArgs({ cacheDir: "/tmp/cache" }), ["--cache-dir", "/tmp/cache"]);
	// The one key whose flag is not its kebab-case spelling.
	assert.deepEqual(buildAdvancedArgs({ playwrightWsEndpoint: "ws://x" }), ["--playwright-ws", "ws://x"]);
	assert.deepEqual(buildAdvancedArgs({ noEmbeddings: true }), ["--no-embeddings"]);
	assert.deepEqual(buildAdvancedArgs({ noEmbeddings: false }), []);
	assert.deepEqual(buildAdvancedArgs(undefined), []);
	assert.deepEqual(buildAdvancedArgs({ cacheDir: undefined }), []);
});

test("unknown and mistyped options fail loudly instead of being dropped", () => {
	assert.throws(() => buildAdvancedArgs({ notAnOption: 1 }), /Unknown advanced option "notAnOption"/);
	assert.throws(() => buildAdvancedArgs({ noEmbeddings: "yes" }), /takes a boolean/);
	assert.throws(() => buildAdvancedArgs({ cacheDir: { nested: true } }), /takes a string or number/);
});

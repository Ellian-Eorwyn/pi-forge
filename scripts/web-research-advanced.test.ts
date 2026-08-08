import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { ADVANCED_FLAGS, buildAdvancedArgs, defaultOutputDirectory } from "../forge/extensions/web-research.ts";

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

const VAULT_SCHEMA = `# Vault Schema

## Domains

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| \`meta\` | \`99\` | \`Meta\` | Notes about the knowledge system itself. |

## Subdomains

### meta

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| \`workflows\` | \`6\` | \`Workflows\` | Capture, automation, and maintenance workflows. |
`;

test("a run started inside a vault lands in the workflows folder, not at the vault root", () => {
	const root = mkdtempSync(join(tmpdir(), "web-research-vault-"));
	try {
		mkdirSync(join(root, ".obsidian"), { recursive: true });
		mkdirSync(join(root, "99 Meta", "99.02 Schemas"), { recursive: true });
		writeFileSync(join(root, "99 Meta", "99.02 Schemas", "0.00 Vault Schema.md"), VAULT_SCHEMA);

		const directory = defaultOutputDirectory(root, "search", "a query");
		assert.equal(dirname(directory), join(root, "99 Meta", "99.06 Workflows", "Web Research"));
		assert.match(directory, /[/\\]search-a-query-[0-9a-f]{8}$/);
		// A second run with the same seed must not adopt the first run's directory.
		mkdirSync(directory, { recursive: true });
		assert.equal(defaultOutputDirectory(root, "search", "a query"), `${directory}-2`);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("outside a vault the run directory stays under forge-output", () => {
	const root = mkdtempSync(join(tmpdir(), "web-research-plain-"));
	try {
		const directory = defaultOutputDirectory(root, "search", "a query");
		assert.equal(dirname(directory), join(root, "forge-output", "web-research"));
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

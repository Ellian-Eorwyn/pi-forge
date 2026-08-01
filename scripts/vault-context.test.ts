import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import vaultContextExtension, {
	inspectVault,
	resolveWorkflowRoot,
	vaultContextMessage,
} from "../forge/extensions/vault-context.ts";

type Handler = (...args: unknown[]) => unknown;

const SCHEMA_WITH_WIKI = `# Vault Schema

## Domains

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| \`personal\` | \`1\` | \`Personal\` | Personal material. |
| \`wiki\` | \`9\` | \`Wiki\` | Cross-cutting entity notes. |
`;

const SCHEMA_WITHOUT_WIKI = SCHEMA_WITH_WIKI.replace(/^\| `wiki`.*$/m, "");

/** A schema carrying the `meta` domain and its `workflows` subdomain, as the real vault does. */
function schemaWithMeta(metaNumber = 99, workflowsNumber = 6): string {
	return `# Vault Schema

## Domains

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| \`personal\` | \`1\` | \`Personal\` | Personal material. |
| \`wiki\` | \`9\` | \`Wiki\` | Cross-cutting entity notes. |
| \`meta\` | \`${metaNumber}\` | \`Meta\` | Notes about the knowledge system itself. |

## Subdomains

### personal

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| \`workflows\` | \`3\` | \`Decoy\` | A same-named row under another domain. |

### meta

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| \`schemas\` | \`2\` | \`Schemas\` | Controlled vocabularies. |
| \`workflows\` | \`${workflowsNumber}\` | \`Workflows\` | Capture, automation, and maintenance workflows. |

## Project registry
`;
}

const REGISTER_WITH_OWNER = `# Personal Context

## Owner

| Field | Value |
| --- | --- |
| \`name\` | \`Ellie\` |
| \`pronouns\` | \`they/them\` |

## Cards

| Card | Tier | Scope | Applies | Triggers | Notes |
| --- | --- | --- | --- | --- | --- |
| \`[[Mental Health]]\` | \`when-relevant\` | \`owner-authored\` | \`personal\` | \`name\` | Private. |
`;

function makeVault(
	options: { schema?: string; schemaAt?: string; notes?: string[]; indexed?: number; register?: string } = {},
) {
	const root = mkdtempSync(join(tmpdir(), "vault-context-"));
	mkdirSync(join(root, ".obsidian"), { recursive: true });
	if (options.schema !== undefined) {
		const relative = options.schemaAt ?? join("99 Meta", "99.02 Schemas", "0.00 Vault Schema.md");
		const full = join(root, relative);
		mkdirSync(join(full, ".."), { recursive: true });
		writeFileSync(full, options.schema);
	}
	if (options.register !== undefined) {
		const full = join(root, "99 Meta", "99.02 Schemas", "0.03 Personal Context.md");
		mkdirSync(join(full, ".."), { recursive: true });
		writeFileSync(full, options.register);
	}
	for (const note of options.notes ?? []) {
		const full = join(root, note);
		mkdirSync(join(full, ".."), { recursive: true });
		writeFileSync(full, "# Note\n");
	}
	if (options.indexed !== undefined) {
		const cache = join(root, ".vault-connections", "cache");
		mkdirSync(cache, { recursive: true });
		const rows: Record<string, number> = {};
		for (let index = 0; index < options.indexed; index += 1) rows[`hash-${index}`] = index;
		writeFileSync(join(cache, "vectors.json"), JSON.stringify({ version: 1, model: "stub", dims: 8, rows }));
	}
	return root;
}

function harness(cwd: string) {
	const commands = new Map<string, Handler>();
	const events = new Map<string, Handler>();
	const status: (string | undefined)[] = [];
	const notices: string[] = [];

	const pi = {
		registerCommand(name: string, options: { handler: Handler }) {
			commands.set(name, options.handler);
		},
		on(event: string, handler: Handler) {
			events.set(event, handler);
		},
	};
	const ctx = {
		cwd,
		ui: {
			setStatus(_key: string, value: string | undefined) {
				status.push(value);
			},
			notify(message: string) {
				notices.push(message);
			},
			theme: { fg: (_color: string, text: string) => text },
		},
	};

	vaultContextExtension(pi as never);
	return {
		ctx,
		status,
		notices,
		async sessionStart() {
			await events.get("session_start")?.({ type: "session_start" }, ctx);
		},
		async beforeAgentStart() {
			return (await events.get("before_agent_start")?.({ type: "before_agent_start" }, ctx)) as
				| { message?: { content: string; display: boolean; customType: string } }
				| undefined;
		},
		async compact() {
			await events.get("session_compact")?.({ type: "session_compact" }, ctx);
		},
		async vaultCommand() {
			await commands.get("vault")?.("", ctx);
		},
	};
}

test("inspectVault reports vault coordinates from anywhere inside the vault", () => {
	const root = makeVault({
		schema: SCHEMA_WITH_WIKI,
		notes: ["01 Personal/A.md", "01 Personal/nested/B.md", "02 Craft/C.md"],
		indexed: 3,
	});
	try {
		const info = inspectVault(join(root, "01 Personal", "nested"));
		assert.ok(info);
		assert.equal(info.root, root);
		// 3 notes plus the schema note itself
		assert.equal(info.noteCount, 4);
		assert.equal(info.schemaNote, join("99 Meta", "99.02 Schemas", "0.00 Vault Schema.md"));
		assert.equal(info.wikiDomain, true);
		assert.equal(info.indexedNotes, 3);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("inspectVault returns undefined outside a vault", () => {
	const plain = mkdtempSync(join(tmpdir(), "not-a-vault-"));
	try {
		assert.equal(inspectVault(plain), undefined);
	} finally {
		rmSync(plain, { recursive: true, force: true });
	}
});

test("inspectVault ignores skill state directories when counting notes", () => {
	const root = makeVault({ notes: ["A.md", ".vault-connections/runs/x/report.md", ".vault-organizer/runs/y/report.md"] });
	try {
		const info = inspectVault(root);
		assert.ok(info);
		assert.equal(info.noteCount, 1);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("inspectVault finds a schema note outside its canonical path", () => {
	const root = makeVault({ schema: SCHEMA_WITH_WIKI, schemaAt: join("Meta", "0.00 Vault Schema.md") });
	try {
		assert.equal(inspectVault(root)?.schemaNote, join("Meta", "0.00 Vault Schema.md"));
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("the injected message names the schema, the index state, and which skill to load", () => {
	const root = makeVault({ schema: SCHEMA_WITH_WIKI, notes: ["A.md"], indexed: 12 });
	try {
		const info = inspectVault(root);
		assert.ok(info);
		const message = vaultContextMessage(info);
		assert.match(message, /OBSIDIAN VAULT DETECTED/);
		assert.match(message, /0\.00 Vault Schema\.md/);
		assert.match(message, /12 notes embedded/);
		assert.match(message, /skills\/vault-connections\/SKILL\.md/);
		assert.match(message, /skills\/vault-organizer\/SKILL\.md/);
		assert.doesNotMatch(message, /wiki` domain in the schema/);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("the injected message flags a missing schema, a missing index, and a missing wiki domain", () => {
	const root = makeVault({ schema: SCHEMA_WITHOUT_WIKI, notes: ["A.md"] });
	try {
		const withoutWiki = vaultContextMessage(inspectVault(root) as NonNullable<ReturnType<typeof inspectVault>>);
		assert.match(withoutWiki, /index: not built yet/);
		assert.match(withoutWiki, /No `wiki` domain in the schema/);
		assert.match(withoutWiki, /vault-organizer has never run here/);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}

	const bare = makeVault({ notes: ["A.md"] });
	try {
		const withoutSchema = vaultContextMessage(inspectVault(bare) as NonNullable<ReturnType<typeof inspectVault>>);
		assert.match(withoutSchema, /Schema note: NOT FOUND/);
	} finally {
		rmSync(bare, { recursive: true, force: true });
	}
});

test("a declared owner is read and the session is told to use their name", () => {
	const root = makeVault({ schema: SCHEMA_WITH_WIKI, register: REGISTER_WITH_OWNER, notes: ["A.md"] });
	try {
		const info = inspectVault(root);
		assert.deepEqual(info?.owner, { name: "Ellie", pronouns: "they/them" });
		const message = vaultContextMessage(info as NonNullable<typeof info>);
		assert.match(message, /You are working with Ellie \(they\/them\)\./);
		assert.match(message, /Address them by name/);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("pronouns are optional and the name alone is enough", () => {
	const register = REGISTER_WITH_OWNER.replace(/^\| `pronouns`.*$\n/m, "");
	const root = makeVault({ schema: SCHEMA_WITH_WIKI, register, notes: ["A.md"] });
	try {
		assert.deepEqual(inspectVault(root)?.owner, { name: "Ellie" });
		const message = vaultContextMessage(inspectVault(root) as NonNullable<ReturnType<typeof inspectVault>>);
		assert.match(message, /You are working with Ellie\. Address them by name/);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("a vault with no register, or a register with no owner row, says nothing about a name", () => {
	const bare = makeVault({ schema: SCHEMA_WITH_WIKI, notes: ["A.md"] });
	try {
		assert.equal(inspectVault(bare)?.owner, undefined);
		const message = vaultContextMessage(inspectVault(bare) as NonNullable<ReturnType<typeof inspectVault>>);
		assert.doesNotMatch(message, /You are working with/);
	} finally {
		rmSync(bare, { recursive: true, force: true });
	}

	const register = REGISTER_WITH_OWNER.replace(/^\| `name`.*$\n/m, "");
	const unnamed = makeVault({ schema: SCHEMA_WITH_WIKI, register, notes: ["A.md"] });
	try {
		assert.equal(inspectVault(unnamed)?.owner, undefined);
	} finally {
		rmSync(unnamed, { recursive: true, force: true });
	}
});

test("only the Owner section is read, never the private cards below it", () => {
	const root = makeVault({ schema: SCHEMA_WITH_WIKI, register: REGISTER_WITH_OWNER, notes: ["A.md"] });
	try {
		const message = vaultContextMessage(inspectVault(root) as NonNullable<ReturnType<typeof inspectVault>>);
		// The card table has a `name` trigger and a private card title; neither is ours to inject.
		assert.doesNotMatch(message, /Mental Health/);
		assert.doesNotMatch(message, /when-relevant/);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("a paragraph pasted into the name row is refused rather than used as a salutation", () => {
	const register = REGISTER_WITH_OWNER.replace("`Ellie`", "x".repeat(41));
	const root = makeVault({ schema: SCHEMA_WITH_WIKI, register, notes: ["A.md"] });
	try {
		assert.equal(inspectVault(root)?.owner, undefined);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("the workflow root is the schema-compiled Meta/Workflows folder for a mapped skill", () => {
	const root = makeVault({ schema: schemaWithMeta(), notes: ["A.md"] });
	try {
		const resolved = resolveWorkflowRoot(join(root, "01 Personal"), "web-research");
		assert.equal(resolved, join(root, "99 Meta", "99.06 Workflows", "Web Research"));
		assert.ok(existsSync(resolved));
		// The decoy `workflows` row under `### personal` must not win.
		assert.doesNotMatch(resolved, /Decoy/);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("renumbering the meta domain or the workflows subdomain moves the workflow root", () => {
	const root = makeVault({ schema: schemaWithMeta(8, 12), notes: ["A.md"] });
	try {
		assert.equal(
			resolveWorkflowRoot(root, "literature-extraction"),
			join(root, "08 Meta", "8.12 Workflows", "Literature Extractions"),
		);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("run artifacts in a marked workspace are left out of the note count", () => {
	const root = makeVault({ schema: schemaWithMeta(), notes: ["01 Personal/A.md"] });
	try {
		assert.equal(inspectVault(root)?.noteCount, 2); // the note plus the schema note
		const workspace = resolveWorkflowRoot(root, "web-research");
		mkdirSync(join(workspace, "run-a"), { recursive: true });
		writeFileSync(join(workspace, "run-a", "research_report.md"), "# Report\n");
		assert.equal(inspectVault(root)?.noteCount, 2);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("the workflow root carries a marker that keeps the organizer and the index out", () => {
	const root = makeVault({ schema: schemaWithMeta(), notes: ["A.md"] });
	try {
		const resolved = resolveWorkflowRoot(root, "web-research");
		const marker = join(resolved, ".forge-workspace");
		assert.ok(existsSync(marker));
		const contents = readFileSync(marker, "utf8");
		// Resolving again must not rewrite a marker a user may have edited.
		writeFileSync(marker, `${contents}edited\n`);
		assert.equal(resolveWorkflowRoot(root, "web-research"), resolved);
		assert.match(readFileSync(marker, "utf8"), /edited/);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("a vault whose schema declares no workflows subdomain falls back to an existing folder", () => {
	const root = makeVault({ schema: SCHEMA_WITH_WIKI, notes: ["A.md"] });
	try {
		mkdirSync(join(root, "99 Meta", "99.06 Workflows"), { recursive: true });
		assert.equal(
			resolveWorkflowRoot(root, "web-research"),
			join(root, "99 Meta", "99.06 Workflows", "Web Research"),
		);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("with no schema and no workflows folder the vault keeps today's forge-output path", () => {
	const root = makeVault({ notes: ["A.md"] });
	try {
		assert.equal(resolveWorkflowRoot(root, "web-research"), join(root, "forge-output", "web-research"));
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("outside a vault, and for an unmapped skill inside one, the root stays forge-output", () => {
	const plain = mkdtempSync(join(tmpdir(), "vault-context-plain-"));
	try {
		assert.equal(resolveWorkflowRoot(plain, "web-research"), join(plain, "forge-output", "web-research"));
	} finally {
		rmSync(plain, { recursive: true, force: true });
	}

	const root = makeVault({ schema: schemaWithMeta(), notes: ["A.md"] });
	try {
		assert.equal(resolveWorkflowRoot(root, "vault-organizer"), join(root, "forge-output", "vault-organizer"));
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("the injected message names the resolved workflow root, or says none resolved", () => {
	const root = makeVault({ schema: schemaWithMeta(), notes: ["A.md"] });
	try {
		const message = vaultContextMessage(inspectVault(root) as NonNullable<ReturnType<typeof inspectVault>>);
		assert.match(message, /Workflow root: /);
		assert.ok(message.includes(join(root, "99 Meta", "99.06 Workflows")));
		assert.match(message, /\.forge-workspace/);
		assert.match(message, /import-run/);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}

	const bare = makeVault({ schema: SCHEMA_WITH_WIKI, notes: ["A.md"] });
	try {
		const message = vaultContextMessage(inspectVault(bare) as NonNullable<ReturnType<typeof inspectVault>>);
		assert.doesNotMatch(message, /Workflow root: /);
		assert.match(message, /No workflows folder resolved/);
	} finally {
		rmSync(bare, { recursive: true, force: true });
	}
});

test("context is injected once per session and hidden from the transcript", async () => {
	const root = makeVault({ schema: SCHEMA_WITH_WIKI, notes: ["A.md"], indexed: 1 });
	try {
		const session = harness(root);
		await session.sessionStart();
		const first = await session.beforeAgentStart();
		assert.ok(first?.message);
		assert.equal(first.message.display, false);
		assert.equal(first.message.customType, "vault-context");
		assert.equal(await session.beforeAgentStart(), undefined);
		assert.equal(await session.beforeAgentStart(), undefined);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("compaction re-arms the injection so the vault facts survive it", async () => {
	const root = makeVault({ schema: SCHEMA_WITH_WIKI, notes: ["A.md"], indexed: 1 });
	try {
		const session = harness(root);
		await session.sessionStart();
		assert.ok((await session.beforeAgentStart())?.message);
		assert.equal(await session.beforeAgentStart(), undefined);
		await session.compact();
		assert.ok((await session.beforeAgentStart())?.message);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("outside a vault the extension injects nothing and sets no status", async () => {
	const plain = mkdtempSync(join(tmpdir(), "not-a-vault-"));
	try {
		const session = harness(plain);
		await session.sessionStart();
		assert.equal(await session.beforeAgentStart(), undefined);
		assert.equal(await session.beforeAgentStart(), undefined);
		assert.ok(session.status.every((value) => value === undefined));
		// One scan on session_start; turns outside a vault must not re-walk the tree.
		assert.equal(session.status.length, 1);
		await session.vaultCommand();
		assert.match(session.notices.join("\n"), /No Obsidian vault found/);
	} finally {
		rmSync(plain, { recursive: true, force: true });
	}
});

/**
 * A fake `obsidian` binary and registry, plus the env pointing at them.
 *
 * Detection here must never spawn anything — naming an unopened vault opens its
 * window — so the shim binary exists only to be found on PATH, and the test
 * asserts it is never executed.
 */
function withObsidian(
	root: string,
	options: { register?: boolean; cli?: boolean; linkUpdates?: "always" | "off" | "unset"; twin?: boolean } = {},
) {
	const home = mkdtempSync(join(tmpdir(), "obsidian-cli-"));
	const bin = join(home, "bin");
	mkdirSync(bin, { recursive: true });
	// Executable that would fail loudly if anything ever ran it.
	writeFileSync(join(bin, "obsidian"), "#!/bin/sh\nexit 99\n", { mode: 0o755 });

	const config = join(home, "config");
	mkdirSync(config, { recursive: true });
	const vaults: Record<string, { path: string }> = {};
	if (options.register !== false) vaults.a = { path: root };
	if (options.twin) {
		const twin = join(home, "twin", root.split("/").pop() as string);
		mkdirSync(twin, { recursive: true });
		vaults.b = { path: twin };
	}
	const registry: Record<string, unknown> = { vaults };
	if (options.cli !== undefined) registry.cli = options.cli;
	writeFileSync(join(config, "obsidian.json"), JSON.stringify(registry));

	const linkUpdates = options.linkUpdates ?? "always";
	writeFileSync(
		join(root, ".obsidian", "app.json"),
		JSON.stringify(linkUpdates === "unset" ? {} : { alwaysUpdateLinks: linkUpdates === "always" }),
	);

	const saved = { PATH: process.env.PATH, dir: process.env.FORGE_OBSIDIAN_CONFIG_DIR };
	process.env.PATH = `${bin}:${process.env.PATH ?? ""}`;
	process.env.FORGE_OBSIDIAN_CONFIG_DIR = config;
	return () => {
		process.env.PATH = saved.PATH;
		if (saved.dir === undefined) delete process.env.FORGE_OBSIDIAN_CONFIG_DIR;
		else process.env.FORGE_OBSIDIAN_CONFIG_DIR = saved.dir;
		rmSync(home, { recursive: true, force: true });
	};
}

test("an available Obsidian CLI is announced with its vault name and a warning", () => {
	const root = makeVault({ schema: SCHEMA_WITH_WIKI, notes: ["A.md"] });
	const restore = withObsidian(root);
	try {
		const vault = inspectVault(root);
		assert.ok(vault);
		assert.equal(vault.obsidianCli?.vaultName, root.split("/").pop());
		assert.equal(vault.obsidianCli?.linkUpdates, "always");
		const message = vaultContextMessage(vault);
		assert.match(message, /Obsidian CLI: available/);
		assert.match(message, /never required/);
		assert.match(message, /Do not run mutating `obsidian` subcommands yourself/);
	} finally {
		restore();
		rmSync(root, { recursive: true, force: true });
	}
});

test("no Obsidian CLI costs no tokens at all", () => {
	const root = makeVault({ schema: SCHEMA_WITH_WIKI, notes: ["A.md"] });
	const saved = process.env.FORGE_OBSIDIAN_CLI;
	process.env.FORGE_OBSIDIAN_CLI = "off";
	try {
		const vault = inspectVault(root);
		assert.ok(vault);
		assert.equal(vault.obsidianCli, undefined);
		assert.doesNotMatch(vaultContextMessage(vault), /Obsidian CLI/);
	} finally {
		if (saved === undefined) delete process.env.FORGE_OBSIDIAN_CLI;
		else process.env.FORGE_OBSIDIAN_CLI = saved;
		rmSync(root, { recursive: true, force: true });
	}
});

test("the near-miss cases each get exactly one actionable line", () => {
	const cases: [Parameters<typeof withObsidian>[1], RegExp][] = [
		[{ linkUpdates: "unset" }, /Automatically update internal links/],
		[{ register: false }, /not registered with it under a unique name/],
		[{ twin: true }, /not registered with it under a unique name/],
		[{ cli: false }, /command line interface is turned off/],
	];
	for (const [options, expected] of cases) {
		const root = makeVault({ schema: SCHEMA_WITH_WIKI, notes: ["A.md"] });
		const restore = withObsidian(root, options);
		try {
			const vault = inspectVault(root);
			assert.ok(vault);
			const message = vaultContextMessage(vault);
			assert.match(message, expected);
			assert.doesNotMatch(message, /Obsidian CLI: available/);
		} finally {
			restore();
			rmSync(root, { recursive: true, force: true });
		}
	}
});

test("the status line shows the vault name and /vault reports a summary", async () => {
	const root = makeVault({ schema: SCHEMA_WITH_WIKI, notes: ["A.md", "B.md"], indexed: 2 });
	try {
		const session = harness(root);
		await session.sessionStart();
		assert.match(session.status.at(-1) as string, /🗂 /);
		await session.vaultCommand();
		const notice = session.notices.join("\n");
		assert.match(notice, /3 notes/);
		assert.match(notice, /schema ok/);
		assert.match(notice, /2 indexed/);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

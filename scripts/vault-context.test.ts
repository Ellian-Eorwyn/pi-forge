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

function makeVault(options: { schema?: string; schemaAt?: string; notes?: string[]; indexed?: number } = {}) {
	const root = mkdtempSync(join(tmpdir(), "vault-context-"));
	mkdirSync(join(root, ".obsidian"), { recursive: true });
	if (options.schema !== undefined) {
		const relative = options.schemaAt ?? join("99 Meta", "99.02 Schemas", "0.00 Vault Schema.md");
		const full = join(root, relative);
		mkdirSync(join(full, ".."), { recursive: true });
		writeFileSync(full, options.schema);
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

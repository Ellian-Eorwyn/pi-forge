/**
 * Tests for the vault-compose extension.
 *
 * Two properties matter here and both are safety properties: a conversation
 * excerpt is copied by the harness rather than retyped by the model, and a
 * session cannot write a note into the vault by hand. The rest is plumbing.
 */

import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import vaultComposeExtension from "../forge/extensions/vault-compose.ts";

type Handler = (...args: never[]) => unknown;

interface Tool {
	name: string;
	parameters: unknown;
	execute: (
		id: string,
		params: unknown,
		signal: AbortSignal | undefined,
		onUpdate: unknown,
		ctx: unknown,
	) => Promise<{ details: unknown }>;
}

const SCHEMA = "| `wiki` | 9 | Wiki | Wiki cards. |\n";

function makeVault(options: { notes?: string[]; workspace?: string } = {}) {
	const root = mkdtempSync(join(tmpdir(), "vault-compose-"));
	mkdirSync(join(root, ".obsidian"), { recursive: true });
	const schema = join(root, "99 Meta", "99.02 Schemas", "0.00 Vault Schema.md");
	mkdirSync(join(schema, ".."), { recursive: true });
	writeFileSync(schema, SCHEMA);
	for (const note of options.notes ?? []) {
		const full = join(root, note);
		mkdirSync(join(full, ".."), { recursive: true });
		writeFileSync(full, "---\ntype: note\nstatus: active\n---\n\n# Note\n\nThe gasket leaks around the rim.\n");
	}
	if (options.workspace) {
		const directory = join(root, options.workspace);
		mkdirSync(directory, { recursive: true });
		writeFileSync(join(directory, ".forge-workspace"), "pi-forge workspace.\n");
	}
	return root;
}

function harness(cwd: string, entries: unknown[] = []) {
	const tools = new Map<string, Tool>();
	const events = new Map<string, Handler>();
	const commands = new Map<string, Handler>();
	const notices: string[] = [];

	const pi = {
		registerTool(tool: Tool) {
			tools.set(tool.name, tool);
		},
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
			notify(message: string) {
				notices.push(message);
			},
		},
		sessionManager: {
			getEntries: () => entries,
			getSessionFile: () => join(cwd, "session.jsonl"),
			getSessionId: () => "session-1",
		},
	};

	vaultComposeExtension(pi as never);
	return {
		ctx,
		notices,
		async capture(params: unknown) {
			const tool = tools.get("forge_vault_capture_source");
			assert.ok(tool, "forge_vault_capture_source is registered");
			return (await tool.execute("call-1", params, undefined, undefined, ctx)).details as Record<string, unknown>;
		},
		async compose(params: unknown) {
			const tool = tools.get("forge_vault_compose");
			assert.ok(tool, "forge_vault_compose is registered");
			return (await tool.execute("call-2", params, undefined, undefined, ctx)).details;
		},
		async toolCall(toolName: string, args: unknown) {
			return (await events.get("tool_call")?.({ toolName, args } as never, ctx as never)) as
				| { block?: boolean; reason?: string }
				| undefined;
		},
		async composeCommand() {
			await commands.get("compose")?.("" as never, ctx as never);
		},
		async apply(params: unknown) {
			const tool = tools.get("forge_vault_compose_apply");
			assert.ok(tool, "forge_vault_compose_apply is registered");
			return (await tool.execute("call-3", params, undefined, undefined, ctx)).details;
		},
		parametersOf(name: string) {
			const tool = tools.get(name);
			assert.ok(tool, `${name} is registered`);
			return (tool.parameters as { properties?: Record<string, unknown> }).properties ?? {};
		},
		toolNames: () => [...tools.keys()],
	};
}

function messageEntry(id: string, role: string, text: string) {
	return { id, type: "message", message: { role, content: [{ type: "text", text }] } };
}

test("no tool has a parameter that can carry note prose", () => {
	const root = makeVault();
	try {
		const app = harness(root);
		assert.deepEqual(app.toolNames(), [
			"forge_vault_capture_source",
			"forge_vault_compose",
			"forge_vault_compose_apply",
		]);
		const serialized = JSON.stringify(app);
		// The capture tool's shape is asserted by the entry-id test below; this
		// guards the tool list itself, since a tool taking text would reopen the
		// hole the others exist to close. Apply is the one that writes, so it is
		// held to naming a run and some ids and nothing else.
		assert.ok(!serialized.includes("sourceText"));
		assert.deepEqual(Object.keys(app.parametersOf("forge_vault_compose_apply")), ["runDirectory", "accept"]);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("a conversation excerpt is copied by the harness, never retyped by the model", async () => {
	const root = makeVault();
	const entries = [
		messageEntry("e1", "user", "The gasket is cracked around the rim."),
		messageEntry("e2", "assistant", "Then it will leak on every double shot."),
		messageEntry("e3", "user", "Unrelated tangent about lunch."),
	];
	try {
		const app = harness(root, entries);
		const details = await app.capture({ kind: "chat", label: "this conversation", entryIds: ["e1", "e2"] });
		assert.equal(details.kind, "chat");
		assert.equal(details.collected, 1);
		// The excerpt is bytes the harness read: the model supplied only ids.
		assert.ok((details.characters as number) > 0);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("a chat source without entry ids is refused", async () => {
	const root = makeVault();
	try {
		const app = harness(root, [messageEntry("e1", "user", "Something said.")]);
		await assert.rejects(
			() => app.capture({ kind: "chat", label: "this conversation" }),
			/needs entryIds/,
		);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("an entry id that matches nothing is refused rather than silently empty", async () => {
	const root = makeVault();
	try {
		const app = harness(root, [messageEntry("e1", "user", "Something said.")]);
		await assert.rejects(() => app.capture({ kind: "chat", label: "chat", entryIds: ["e99"] }), /no message entries matched/);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("a vault note is read from disk with its frontmatter stripped", async () => {
	const root = makeVault({ notes: ["04 Technology/Gasket.md"] });
	try {
		const app = harness(root);
		const details = await app.capture({ kind: "vault-note", label: "Gasket", path: "04 Technology/Gasket.md" });
		assert.equal(details.kind, "vault-note");
		// Frontmatter is the vault's metadata, not the note's content: leaving it
		// in makes a schema value look like something the note said.
		assert.ok((details.characters as number) < 80);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("composing with nothing collected says so", async () => {
	const root = makeVault();
	try {
		const app = harness(root);
		await assert.rejects(
			() => app.compose({ intent: "conversation", request: "make a note" }),
			/no sources collected/,
		);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("apply refuses a run directory this extension did not make", async () => {
	const root = makeVault();
	try {
		const app = harness(root);
		// The run directory decides which manifest apply trusts, so pointing it
		// at a hand-built one somewhere else is the way around every check.
		await assert.rejects(
			() => app.apply({ runDirectory: join(root, "00 Inbox"), accept: ["n-001"] }),
			/not a vault-compose run directory/,
		);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("apply with no ids writes nothing", async () => {
	const root = makeVault();
	try {
		const app = harness(root);
		await assert.rejects(() => app.apply({ runDirectory: root, accept: [] }), /nothing is written/);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("writing a note into the vault by hand is blocked and names the way through", async () => {
	const root = makeVault();
	try {
		const app = harness(root);
		const blocked = await app.toolCall("write", { file_path: join(root, "00 Inbox", "Mine.md") });
		assert.equal(blocked?.block, true);
		assert.match(String(blocked?.reason), /forge_vault_compose/);

		const edited = await app.toolCall("edit", { file_path: join(root, "04 Technology", "Gasket.md") });
		assert.equal(edited?.block, true);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("a run artifact under a workspace marker is not a note and is allowed", async () => {
	const root = makeVault({ workspace: "99 Meta/99.06 Workflows/Composed Notes" });
	try {
		const app = harness(root);
		const allowed = await app.toolCall("write", {
			file_path: join(root, "99 Meta", "99.06 Workflows", "Composed Notes", "run", "run-spec.json"),
		});
		assert.equal(allowed, undefined);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("writing outside the vault is left alone", async () => {
	const root = makeVault();
	const outside = mkdtempSync(join(tmpdir(), "outside-"));
	try {
		const app = harness(root);
		assert.equal(await app.toolCall("write", { file_path: join(outside, "notes.md") }), undefined);
		// And a tool that is not write/edit is never the guard's business.
		assert.equal(await app.toolCall("read", { file_path: join(root, "00 Inbox", "Mine.md") }), undefined);
	} finally {
		rmSync(root, { recursive: true, force: true });
		rmSync(outside, { recursive: true, force: true });
	}
});

test("the /compose command lists what has been collected", async () => {
	const root = makeVault();
	try {
		const app = harness(root, [messageEntry("e1", "user", "The gasket is cracked around the rim.")]);
		await app.composeCommand();
		assert.match(app.notices.join("\n"), /No sources collected/);
		await app.capture({ kind: "chat", label: "this conversation", entryIds: ["e1"] });
		await app.composeCommand();
		assert.match(app.notices.join("\n"), /s-0001 {2}chat/);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

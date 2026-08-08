import assert from "node:assert/strict";
import { test } from "node:test";
import vaultWorkflowExtension from "../forge/extensions/vault-workflow.ts";

const TOOL_NAMES = ["read", "bash", "edit", "write", "multiedit", "grep", "glob", "find", "ls", "questionnaire"];

type Handler = (...args: unknown[]) => unknown;

function harness(options: { knownModels?: string[] } = {}) {
	const commands = new Map<string, Handler>();
	const events = new Map<string, Handler>();
	let activeTools: string[] = [];
	const entries: { type: string; customType?: string; data?: unknown }[] = [];
	const known = new Set(options.knownModels ?? ["forge-local/code", "forge-chat-local/chat"]);
	const modelChanges: string[] = [];

	const pi = {
		registerCommand(name: string, options: { handler: Handler }) {
			commands.set(name, options.handler);
		},
		on(event: string, handler: Handler) {
			events.set(event, handler);
		},
		getAllTools() {
			return TOOL_NAMES.map((name) => ({ name }));
		},
		setActiveTools(names: string[]) {
			activeTools = names;
		},
		appendEntry(customType: string, data: unknown) {
			entries.push({ type: "custom", customType, data });
		},
		async setModel(model: { provider: string; id: string }) {
			ctx.model = { provider: model.provider, id: model.id };
			modelChanges.push(`${model.provider}/${model.id}`);
			return true;
		},
	};

	const ctx = {
		model: { provider: "forge-local", id: "code" } as { provider: string; id: string },
		modelRegistry: {
			find(provider: string, id: string) {
				return known.has(`${provider}/${id}`) ? { provider, id } : undefined;
			},
		},
		ui: {
			setStatus() {},
			notify() {},
			theme: { fg: (_color: string, text: string) => text },
		},
		sessionManager: { getEntries: () => entries },
	};

	vaultWorkflowExtension(pi as never);
	return {
		commands,
		events,
		ctx,
		entries,
		modelChanges,
		model: () => `${ctx.model.provider}/${ctx.model.id}`,
		activeTools: () => activeTools,
		async run(command: string, args = "") {
			const handler = commands.get(command);
			assert.ok(handler, `command ${command} registered`);
			await handler(args, ctx);
		},
	};
}

test("registers the workflow commands", () => {
	const h = harness();
	for (const command of ["plan", "execute", "verify", "workflow"]) {
		assert.ok(h.commands.has(command), `has /${command}`);
	}
});

test("plan and verify phases are read-only; execute unlocks write tools", async () => {
	const h = harness();
	await h.run("plan");
	assert.deepEqual(h.activeTools().sort(), ["bash", "find", "glob", "grep", "ls", "questionnaire", "read"]);
	assert.ok(!h.activeTools().includes("edit"));
	assert.ok(!h.activeTools().includes("write"));

	await h.run("execute");
	assert.ok(h.activeTools().includes("edit"));
	assert.ok(h.activeTools().includes("write"));
	assert.ok(h.activeTools().includes("multiedit"));

	await h.run("verify");
	assert.ok(!h.activeTools().includes("edit"));
});

test("execute switches to the non-thinking model and restores it afterwards", async () => {
	const h = harness();
	await h.run("plan");
	assert.equal(h.model(), "forge-local/code", "planning stays on the thinking model");

	await h.run("execute");
	assert.equal(h.model(), "forge-chat-local/chat");

	await h.run("verify");
	assert.equal(h.model(), "forge-local/code", "verification thinks again");
	assert.deepEqual(h.modelChanges, ["forge-chat-local/chat", "forge-local/code"]);
});

test("a model the user picked during execute is not overwritten on the way out", async () => {
	const h = harness({ knownModels: ["forge-local/code", "forge-chat-local/chat", "anthropic/opus"] });
	await h.run("execute");
	assert.equal(h.model(), "forge-chat-local/chat");

	h.ctx.model = { provider: "anthropic", id: "opus" };
	await h.run("verify");
	assert.equal(h.model(), "anthropic/opus");
});

test("execute still works when the non-thinking provider is not configured", async () => {
	const h = harness({ knownModels: ["forge-local/code"] });
	await h.run("execute");
	assert.equal(h.model(), "forge-local/code");
	assert.deepEqual(h.modelChanges, []);

	await h.run("verify");
	assert.equal(h.model(), "forge-local/code");
});

test("the prefill hook is gone", () => {
	const h = harness();
	assert.equal(h.events.get("before_provider_request"), undefined);
});

test("each phase injects its own system prompt", async () => {
	const h = harness();
	// The harness stores handlers as `(...args: unknown[]) => unknown`, so the
	// injected prompt needs its shape naming here to be read at all. `?? ""` keeps
	// a handler that returned nothing failing the match rather than throwing.
	const before = h.events.get("before_agent_start") as
		| (() => Promise<{ message: { content: string } } | undefined>)
		| undefined;
	assert.ok(before);

	await h.run("plan");
	assert.match((await before())?.message.content ?? "", /PLAN PHASE/);
	await h.run("execute");
	const exec = (await before())?.message.content ?? "";
	assert.match(exec, /EXECUTE PHASE/);
	assert.doesNotMatch(exec, /--think-prefill/, "the prefill workaround is no longer prescribed");
	assert.match(exec, /WAIT for an explicit/);
	await h.run("verify");
	assert.match((await before())?.message.content ?? "", /VERIFY PHASE/);

	await h.run("workflow", "off");
	assert.equal(await before(), undefined, "no prompt when off");
});

test("read-only phases block mutating bash but allow reads", async () => {
	const h = harness();
	const toolCall = h.events.get("tool_call");
	assert.ok(toolCall);

	await h.run("plan");
	const blocked = (await toolCall({ toolName: "bash", input: { command: "rm -rf notes" } })) as { block?: boolean };
	assert.equal(blocked.block, true);
	const applyBlocked = (await toolCall({
		toolName: "bash",
		input: { command: "python3 vault-organizer.py vault --vault . --apply" },
	})) as { block?: boolean };
	assert.equal(applyBlocked.block, true);
	assert.equal(await toolCall({ toolName: "bash", input: { command: "grep -r type ." } }), undefined);
	assert.equal(
		await toolCall({ toolName: "bash", input: { command: "python3 vault-organizer.py doctor --vault ." } }),
		undefined,
	);

	await h.run("execute");
	assert.equal(
		await toolCall({ toolName: "bash", input: { command: "rm -rf notes" } }),
		undefined,
		"execute allows it",
	);
});

test("read-only phases block mutating obsidian subcommands and allow queries", async () => {
	const h = harness();
	const toolCall = h.events.get("tool_call");
	assert.ok(toolCall);
	await h.run("plan");

	const blocked = async (command: string) => {
		const result = (await toolCall({ toolName: "bash", input: { command } })) as { block?: boolean } | undefined;
		return result?.block === true;
	};

	// None of these contain a token the generic regex catches.
	for (const command of [
		"obsidian rename path=Notes/A.md name=B",
		"obsidian move path=Notes/A.md to=Archive",
		"obsidian delete path=Notes/A.md",
		'obsidian property:set path=Notes/A.md name=status value="done"',
		'obsidian eval code="app.vault.delete(f)"',
		"obsidian history:restore path=Notes/A.md version=2",
		"obsidian create name=Scratch",
		"obsidian command id=editor:toggle-bold",
		"obsidian daily",
		"obsidian task toggle",
		"obsidian plugin:uninstall id=dataview",
		"/usr/local/bin/obsidian rename path=A.md name=B",
		"sudo obsidian delete path=A.md",
		"obsidian backlinks path=A.md | obsidian delete path=B.md",
		'obsidian vault="My Vault" rename path=A.md name=B',
		"obsidian", // the interactive TUI
		"obsidian vault=Loom", // no subcommand at all
	]) {
		assert.equal(await blocked(command), true, `blocks: ${command}`);
	}

	for (const command of [
		"obsidian backlinks path=Notes/A.md format=json",
		"obsidian vault=Loom properties format=json counts",
		'obsidian vault="My Vault" search query="schema" format=json',
		"obsidian --copy unresolved format=json verbose",
		"obsidian read path=Notes/A.md | grep type",
		"/usr/local/bin/obsidian version",
		"obsidian base:query path=Dash.base format=paths",
		"grep -rn obsidian .", // "obsidian" as an argument, not a command
		"ls -la .obsidian/",
	]) {
		assert.equal(await blocked(command), false, `allows: ${command}`);
	}

	await h.run("execute");
	assert.equal(
		await toolCall({ toolName: "bash", input: { command: "obsidian rename path=A.md name=B" } }),
		undefined,
		"execute allows it",
	);
});

test("phase persists and restores on session_start", async () => {
	const h = harness();
	await h.run("execute");
	const persisted = h.entries.filter((entry) => entry.customType === "vault-workflow").pop();
	assert.deepEqual(persisted?.data, { phase: "execute", previousModel: { provider: "forge-local", id: "code" } });

	// fresh instance, same session entries -> restores execute + its tools + model
	const h2 = harness();
	for (const entry of h.entries) h2.entries.push(entry);
	const sessionStart = h2.events.get("session_start");
	assert.ok(sessionStart);
	await sessionStart({}, h2.ctx);
	assert.ok(h2.activeTools().includes("write"), "restored execute tool set");
	assert.equal(h2.model(), "forge-chat-local/chat", "resumed mid-execute stays non-thinking");
});

test("a session that crashed mid-execute does not strand the user on the non-thinking model", async () => {
	const h = harness();
	// Phase moved on, but the crash left the session model behind.
	h.entries.push({
		type: "custom",
		customType: "vault-workflow",
		data: { phase: "verify", previousModel: { provider: "forge-local", id: "code" } },
	});
	h.ctx.model = { provider: "forge-chat-local", id: "chat" };
	const sessionStart = h.events.get("session_start");
	assert.ok(sessionStart);
	await sessionStart({}, h.ctx);
	assert.equal(h.model(), "forge-local/code");
});

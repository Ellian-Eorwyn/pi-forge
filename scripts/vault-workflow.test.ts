import assert from "node:assert/strict";
import { test } from "node:test";
import vaultWorkflowExtension from "../forge/extensions/vault-workflow.ts";

const TOOL_NAMES = ["read", "bash", "edit", "write", "multiedit", "grep", "glob", "find", "ls", "questionnaire"];

type Handler = (...args: unknown[]) => unknown;

function harness() {
	const commands = new Map<string, Handler>();
	const events = new Map<string, Handler>();
	let activeTools: string[] = [];
	const entries: { type: string; customType?: string; data?: unknown }[] = [];

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
	};

	const ctx = {
		model: { provider: "forge-local" },
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

test("execute phase prefills a closed think block for forge-local only", async () => {
	const h = harness();
	const hook = h.events.get("before_provider_request");
	assert.ok(hook);
	const payload = { messages: [{ role: "user", content: "do it" }] };

	await h.run("plan");
	assert.equal(hook({ payload }, h.ctx), undefined, "no prefill while planning");

	await h.run("execute");
	const result = hook({ payload }, h.ctx) as { messages: { role: string; content: string }[] };
	assert.equal(result.messages.length, 2);
	assert.equal(result.messages[1].role, "assistant");
	assert.match(result.messages[1].content, /<think>\s*<\/think>/);
	// original payload not mutated
	assert.equal(payload.messages.length, 1);

	// wrong provider -> untouched
	assert.equal(hook({ payload }, { model: { provider: "anthropic" } }), undefined);
});

test("each phase injects its own system prompt", async () => {
	const h = harness();
	const before = h.events.get("before_agent_start");
	assert.ok(before);

	await h.run("plan");
	assert.match((await before()).message.content, /PLAN PHASE/);
	await h.run("execute");
	const exec = (await before()).message.content;
	assert.match(exec, /EXECUTE PHASE/);
	assert.match(exec, /--think-prefill/);
	assert.match(exec, /WAIT for an explicit/);
	await h.run("verify");
	assert.match((await before()).message.content, /VERIFY PHASE/);

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
	assert.equal(await toolCall({ toolName: "bash", input: { command: "python3 vault-organizer.py doctor --vault ." } }), undefined);

	await h.run("execute");
	assert.equal(await toolCall({ toolName: "bash", input: { command: "rm -rf notes" } }), undefined, "execute allows it");
});

test("phase persists and restores on session_start", async () => {
	const h = harness();
	await h.run("execute");
	const persisted = h.entries.filter((entry) => entry.customType === "vault-workflow").pop();
	assert.deepEqual(persisted?.data, { phase: "execute" });

	// fresh instance, same session entries -> restores execute + its tools
	const h2 = harness();
	for (const entry of h.entries) h2.entries.push(entry);
	const sessionStart = h2.events.get("session_start");
	assert.ok(sessionStart);
	await sessionStart({}, h2.ctx);
	assert.ok(h2.activeTools().includes("write"), "restored execute tool set");
});

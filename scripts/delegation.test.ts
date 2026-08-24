import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { createReadOnlyTools } from "@earendil-works/pi-coding-agent";
import delegationExtension, { toOpenAITools } from "../forge/extensions/delegation.ts";

// Importing delegation.ts also asserts it loads with only sandbox-safe imports
// (coding-agent + ../lib/forge-llm.mjs) — the regression that broke the extension
// loader was an import of the non-allowlisted @earendil-works/pi-ai/base.

test("read-only tools convert to OpenAI function-tool definitions", () => {
	const tools = createReadOnlyTools(process.cwd());
	const oa = toOpenAITools(tools);

	assert.equal(oa.length, tools.length);
	assert.ok(oa.length > 0);
	// The read-only set is read/grep/find/ls — never a mutating tool.
	const names = oa.map((t) => t.function.name).sort();
	assert.deepEqual(names, ["find", "grep", "ls", "read"]);
	for (const entry of oa) {
		assert.equal(entry.type, "function");
		assert.equal(typeof entry.function.name, "string");
		assert.equal(typeof entry.function.description, "string");
		assert.ok(entry.function.parameters, "each tool carries a JSON-schema parameters object");
	}
});

/** Run the extension against a capture stub and report what it registered. */
function registeredTools(): string[] {
	const names: string[] = [];
	const noop = (): void => {};
	const stub = new Proxy(
		{},
		{
			get: (_target, property) =>
				property === "registerTool" ? (definition: { name: string }) => names.push(definition.name) : noop,
		},
	);
	delegationExtension(stub as ExtensionAPI);
	return names;
}

/** A temp agent dir whose settings declare delegation on or off. */
function agentDirWithDelegation(enabled: boolean): string {
	const dir = mkdtempSync(join(tmpdir(), "forge-delegation-"));
	const settings = {
		connectedServices: {
			delegate: { enabled, baseUrl: "http://llms:8104/v1/chat/completions", model: "chat" },
		},
	};
	writeFileSync(join(dir, "settings.json"), `${JSON.stringify(settings)}\n`);
	return dir;
}

// On a single-backend setup the delegate would fall back to the primary chat —
// the interactive session's own weights and, at one slot, its own prefix cache.
// Not registering the tool is what keeps that from being reachable at all, and
// saves its schema from every session's context.
test("forge_delegate is registered only when a delegation backend is configured", () => {
	const agentDir = process.env.PI_FORGE_AGENT_DIR;
	const toolFlag = process.env.FORGE_DELEGATE_TOOL;
	try {
		process.env.FORGE_DELEGATE_TOOL = "";

		process.env.PI_FORGE_AGENT_DIR = agentDirWithDelegation(false);
		assert.deepEqual(registeredTools(), []);

		process.env.PI_FORGE_AGENT_DIR = agentDirWithDelegation(true);
		assert.deepEqual(registeredTools(), ["forge_delegate"]);
	} finally {
		if (agentDir === undefined) delete process.env.PI_FORGE_AGENT_DIR;
		else process.env.PI_FORGE_AGENT_DIR = agentDir;
		if (toolFlag === undefined) delete process.env.FORGE_DELEGATE_TOOL;
		else process.env.FORGE_DELEGATE_TOOL = toolFlag;
	}
});

// The override is both the user's escape hatch and how the skill-report generator
// keeps FORGE_SKILLS.md the same on every machine.
test("FORGE_DELEGATE_TOOL overrides the setup in both directions", () => {
	const agentDir = process.env.PI_FORGE_AGENT_DIR;
	const toolFlag = process.env.FORGE_DELEGATE_TOOL;
	try {
		process.env.PI_FORGE_AGENT_DIR = agentDirWithDelegation(false);
		process.env.FORGE_DELEGATE_TOOL = "on";
		assert.deepEqual(registeredTools(), ["forge_delegate"]);

		process.env.PI_FORGE_AGENT_DIR = agentDirWithDelegation(true);
		process.env.FORGE_DELEGATE_TOOL = "off";
		assert.deepEqual(registeredTools(), []);
	} finally {
		if (agentDir === undefined) delete process.env.PI_FORGE_AGENT_DIR;
		else process.env.PI_FORGE_AGENT_DIR = agentDir;
		if (toolFlag === undefined) delete process.env.FORGE_DELEGATE_TOOL;
		else process.env.FORGE_DELEGATE_TOOL = toolFlag;
	}
});

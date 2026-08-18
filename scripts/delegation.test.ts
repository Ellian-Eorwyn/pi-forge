import assert from "node:assert/strict";
import { test } from "node:test";
import { createReadOnlyTools } from "@earendil-works/pi-coding-agent";
import { toOpenAITools } from "../forge/extensions/delegation.ts";

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

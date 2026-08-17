import assert from "node:assert/strict";
import { test } from "node:test";
import { DELEGATE_SLOT, pinBackgroundSlot } from "../forge/extensions/delegation.ts";

// The delegate shares one llama-server with the interactive session. Its requests
// MUST pin the background slot: a request on slot 0 evicts the interactive
// session's prefix cache. These tests guard that invariant.

test("the delegate always pins the background slot and enables its own prefix cache", () => {
	assert.equal(DELEGATE_SLOT, 1);
	assert.deepEqual(pinBackgroundSlot({ model: "chat", messages: [] }), {
		model: "chat",
		messages: [],
		id_slot: 1,
		cache_prompt: true,
	});
});

test("the delegate can never leave a payload on the interactive slot 0", () => {
	// A payload that somehow already carried slot 0 is corrected to slot 1, never trusted.
	const pinned = pinBackgroundSlot({ id_slot: 0, cache_prompt: false }) as Record<string, unknown>;
	assert.equal(pinned.id_slot, DELEGATE_SLOT);
	assert.notEqual(pinned.id_slot, 0);
	assert.equal(pinned.cache_prompt, true);
});

test("non-object payloads are left unchanged so a malformed body is not silently rewritten", () => {
	assert.equal(pinBackgroundSlot(undefined), undefined);
	assert.equal(pinBackgroundSlot(null), undefined);
	assert.equal(pinBackgroundSlot("body"), undefined);
	assert.equal(pinBackgroundSlot([1, 2, 3]), undefined);
});

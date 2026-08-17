import assert from "node:assert/strict";
import { test } from "node:test";
import { addInteractiveSlot, schedulingForProvider } from "../forge/extensions/inference-scheduling.ts";
import { LOCAL_MODEL_PROVIDERS, serviceNameForLocalProvider } from "../forge/lib/connected-services.mjs";

const interactive = { enabled: true, interactiveSlot: 0 };
const services = {
	chat: { scheduling: interactive },
	think: { scheduling: interactive },
};

test("every configured local provider selects interactive slot scheduling", () => {
	const expectedServices: Record<string, "chat" | "think"> = {
		[LOCAL_MODEL_PROVIDERS.think]: "think",
		[LOCAL_MODEL_PROVIDERS.code]: "think",
		[LOCAL_MODEL_PROVIDERS.chat]: "chat",
	};

	for (const [provider, service] of Object.entries(expectedServices)) {
		assert.equal(serviceNameForLocalProvider(provider), service);
		const scheduling = schedulingForProvider(provider, services);
		assert.ok(scheduling);
		assert.deepEqual(scheduling, interactive);
		assert.deepEqual(addInteractiveSlot({ model: provider }, scheduling), {
			model: provider,
			id_slot: 0,
			cache_prompt: true,
		});
	}
});

test("unknown and disabled providers do not acquire slot affinity", () => {
	assert.equal(serviceNameForLocalProvider("forge-local"), null);
	assert.equal(schedulingForProvider("forge-local", services), undefined);
	assert.equal(
		schedulingForProvider(LOCAL_MODEL_PROVIDERS.think, {
			think: { scheduling: { enabled: false, interactiveSlot: 0 } },
		}),
		undefined,
	);
});

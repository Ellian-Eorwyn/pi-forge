import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const libraryRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const {
	projectProfile,
	applyProfile,
	setDelegation,
	clampBackgroundSlot,
	DEFAULT_BACKENDS,
	activeProfileName,
	loadBackends,
} = await import(join(libraryRoot, "backends.mjs"));
const { SLOT_CONTEXT_TOKENS } = await import(join(libraryRoot, "connected-services.mjs"));
const { clearStackStateCache } = await import(join(libraryRoot, "stack-state.mjs"));
const { seedConnectedServicesSettings, resolveConnectedServices } = await import(
	join(libraryRoot, "connected-services.mjs")
);

/** A temp agent dir seeded the way the installer leaves it: full settings + the three providers. */
function seedAgentDir() {
	const dir = mkdtempSync(join(tmpdir(), "forge-backends-"));
	const settings = {
		defaultProvider: "forge-local-think",
		defaultModel: "think",
		compaction: { enabled: true, reserveTokens: 1 },
	};
	seedConnectedServicesSettings(settings);
	writeFileSync(join(dir, "settings.json"), `${JSON.stringify(settings)}\n`);
	const provider = (id, port) => ({
		baseUrl: `http://llms:${port}/v1`,
		api: "openai-completions",
		apiKey: "local",
		compat: {},
		models: [{ id, name: id, reasoning: false, input: ["text", "image"], contextWindow: 131072, maxTokens: 32768 }],
	});
	const models = {
		providers: {
			"forge-local-think": provider("think", 8003),
			"forge-local-code": provider("code", 8008),
			"forge-local-chat": provider("chat", 8004),
		},
	};
	writeFileSync(join(dir, "models.json"), `${JSON.stringify(models)}\n`);
	return dir;
}

function readJson(dir, name) {
	return JSON.parse(readFileSync(join(dir, name), "utf8"));
}

// applyProfile reads the deployment once before projecting. Tests must never do
// that for real: without this kill switch each apply would spend its full 3s
// timeout dialling a stack API that may or may not be on this machine's network,
// and the result would depend on what it found. `"auto"` then falls back to
// SLOT_CONTEXT_TOKENS, which is the path every install without the API takes.
const OFFLINE = { PI_FORGE_SKIP_STACK_DISCOVERY: "1" };

test("projectProfile maps the distributed setup onto both registries", async () => {
	const patch = projectProfile(DEFAULT_BACKENDS.profiles.distributed);
	assert.equal(patch.connectedServices.chat.baseUrl, "http://llms:8004/v1/chat/completions");
	assert.equal(patch.connectedServices.think.baseUrl, "http://llms:8008/v1/chat/completions");
	assert.equal(patch.connectedServices.delegate.enabled, true);
	assert.equal(patch.connectedServices.delegate.baseUrl, "http://llms:8104/v1/chat/completions");
	assert.equal(patch.connectedServices.delegate.model, "chat");
	assert.equal(patch.connectedServices.delegate.scheduling.enabled, false);
	assert.equal(patch.connectedServices.embeddings.url, "http://laptop:8005/v1/embeddings");
	assert.equal(patch.connectedServices.transcription.baseUrl, "http://laptop:8014");
	assert.equal(patch.connectedServices.transcription.api, "openai");
	assert.equal(patch.connectedServices.transcription.model, "parakeet-v3-en");
	assert.equal(patch.connectedServices.ocr.url, "http://llms:5002/glmocr/parse");
	assert.deepEqual(patch.providers["forge-local-think"].input, ["text", "image"]);
	assert.equal(patch.providers["forge-local-think"].baseUrl, "http://llms:8003/v1");
});

test("transcription api/model default, resolve, and survive seeding", async () => {
	// Default is the sidecar protocol.
	const base = resolveConnectedServices({ env: {}, settings: {} });
	assert.equal(base.transcription.api, "sidecar");

	// env and settings both reach it; an unknown protocol falls back to sidecar.
	const fromSettings = resolveConnectedServices({
		env: {},
		settings: { connectedServices: { transcription: { api: "openai", model: "parakeet-v3-en" } } },
	});
	assert.equal(fromSettings.transcription.api, "openai");
	assert.equal(fromSettings.transcription.model, "parakeet-v3-en");
	const fromEnv = resolveConnectedServices({ env: { FORGE_TRANSCRIPTION_API: "openai" }, settings: {} });
	assert.equal(fromEnv.transcription.api, "openai");
	const bogus = resolveConnectedServices({
		env: {},
		settings: { connectedServices: { transcription: { api: "grpc" } } },
	});
	assert.equal(bogus.transcription.api, "sidecar");

	// Seeding (which the installer runs) must not drop api/model — otherwise a
	// re-install would silently revert an openai setup to the sidecar route.
	const settings = { connectedServices: { transcription: { api: "openai", model: "parakeet-v3-en" } } };
	seedConnectedServicesSettings(settings);
	assert.equal(settings.connectedServices.transcription.api, "openai");
	assert.equal(settings.connectedServices.transcription.model, "parakeet-v3-en");
});

test("a vision-free primary projects a text-only input array", async () => {
	const patch = projectProfile({ primary: { host: "http://x", images: false, contextTokens: 131072 } });
	for (const provider of Object.values(patch.providers)) assert.deepEqual(provider.input, ["text"]);
});

test("disabled delegation projects enabled:false and never demands a baseUrl", async () => {
	const patch = projectProfile(DEFAULT_BACKENDS.profiles.single);
	assert.equal(patch.connectedServices.delegate.enabled, false);
	assert.equal(patch.connectedServices.delegate.scheduling.enabled, false);
});

test("enabling delegation without a baseUrl is a clear error", async () => {
	assert.throws(
		() => projectProfile({ primary: { host: "http://x" }, delegation: { enabled: true } }),
		/delegation\.baseUrl/,
	);
});

test("a setup without a primary host is a clear error", async () => {
	assert.throws(() => projectProfile({ primary: {} }), /host/);
});

test("applyProfile writes settings and models and records the active setup", async () => {
	const dir = seedAgentDir();
	const result = await applyProfile({ env: OFFLINE, agentDir: dir, name: "distributed" });
	assert.equal(result.profile, "distributed");
	assert.equal(result.delegation, "on");

	const services = readJson(dir, "settings.json").connectedServices;
	assert.equal(services.delegate.enabled, true);
	assert.equal(services.delegate.baseUrl, "http://llms:8104/v1/chat/completions");
	assert.equal(services.embeddings.url, "http://laptop:8005/v1/embeddings");
	assert.equal(services.transcription.baseUrl, "http://laptop:8014");
	assert.equal(services.ocr.url, "http://llms:5002/glmocr/parse");
	// Scheduling on chat is untouched (its slot pinning must survive a switch).
	assert.equal(services.chat.scheduling.enabled, true);

	const providers = readJson(dir, "models.json").providers;
	assert.equal(providers["forge-local-think"].models[0].contextWindow, 131072);

	// The active setup is persisted so the next `apply` and the interactive
	// status both see it.
	assert.equal(activeProfileName(loadBackends({ env: {}, agentDir: dir })), "distributed");
});

test("switching to single turns delegation off but leaves the endpoint to reuse", async () => {
	const dir = seedAgentDir();
	await applyProfile({ env: OFFLINE, agentDir: dir, name: "distributed" });
	await applyProfile({ env: OFFLINE, agentDir: dir, name: "single" });
	const delegate = readJson(dir, "settings.json").connectedServices.delegate;
	assert.equal(delegate.enabled, false);
	// The endpoint from the previous setup is preserved rather than erased, so a
	// later `/backend on` has something to re-enable.
	assert.equal(delegate.baseUrl, "http://llms:8104/v1/chat/completions");
});

test("setDelegation on/off toggles the active setup and re-applies", async () => {
	const dir = seedAgentDir();
	await applyProfile({ env: OFFLINE, agentDir: dir, name: "single" });
	const on = await setDelegation({ env: OFFLINE, agentDir: dir, enabled: true });
	assert.equal(on.delegation, "on");
	assert.equal(readJson(dir, "settings.json").connectedServices.delegate.enabled, true);
	const off = await setDelegation({ env: OFFLINE, agentDir: dir, enabled: false });
	assert.equal(off.delegation, "off");
	assert.equal(readJson(dir, "settings.json").connectedServices.delegate.enabled, false);
});

test("projectProfile maps distributed-parallel onto both GPUs and the offload", async () => {
	const patch = projectProfile(DEFAULT_BACKENDS.profiles["distributed-parallel"]);
	// GPU-2 lanes: bulk chat2 on :8104 (non-thinking), verify think2 on the code2 port :8108.
	assert.equal(patch.connectedServices.chat2.enabled, true);
	assert.equal(patch.connectedServices.chat2.baseUrl, "http://llms:8104/v1/chat/completions");
	assert.equal(patch.connectedServices.chat2.images, false);
	assert.deepEqual(patch.connectedServices.chat2.chatTemplateKwargs, { enable_thinking: false });
	assert.equal(patch.connectedServices.chat2.scheduling.enabled, false);
	assert.equal(patch.connectedServices.think2.enabled, true);
	assert.equal(patch.connectedServices.think2.baseUrl, "http://llms:8108/v1/chat/completions");
	assert.equal(patch.connectedServices.think2.model, "code");
	assert.equal(patch.connectedServices.think2.chatTemplateKwargs, null);
	assert.equal(patch.connectedServices.think2.scheduling.enabled, false);
	assert.deepEqual(patch.connectedServices.bulk.lanes, ["chat", "chat2"]);
	assert.equal(patch.connectedServices.verify.service, "think2");
	assert.equal(patch.connectedServices.delegate.enabled, true);
	// Compaction offload to the second GPU's non-thinking lane.
	assert.equal(patch.taskModel.enabled, true);
	assert.equal(patch.taskModel.baseUrl, "http://llms:8104/v1");
	assert.equal(patch.taskModel.thinkingEnabled, false);
	assert.equal(patch.taskProvider.id, "forge-task-local");
	assert.equal(patch.taskProvider.model, "chat");
	assert.deepEqual(patch.taskProvider.input, ["text"]);
});

test("a setup without a secondary projects chat2/think2/bulk/verify in their off-state", async () => {
	const patch = projectProfile(DEFAULT_BACKENDS.profiles.single);
	assert.equal(patch.connectedServices.chat2.enabled, false);
	assert.equal(patch.connectedServices.think2.enabled, false);
	assert.deepEqual(patch.connectedServices.bulk.lanes, ["chat"]);
	assert.equal(patch.connectedServices.verify.service, null);
	assert.equal(patch.taskModel.enabled, false);
	assert.equal(patch.taskProvider, null);
});

test("applyProfile distributed-parallel enables offload and creates the task provider", async () => {
	const dir = seedAgentDir();
	await applyProfile({ env: OFFLINE, agentDir: dir, name: "distributed-parallel" });
	const settings = readJson(dir, "settings.json");
	assert.equal(settings.connectedServices.chat2.enabled, true);
	assert.equal(settings.connectedServices.think2.enabled, true);
	assert.deepEqual(settings.connectedServices.bulk.lanes, ["chat", "chat2"]);
	assert.equal(settings.connectedServices.verify.service, "think2");
	assert.equal(settings.taskModel.enabled, true);
	assert.equal(settings.contextBudget.useTaskModel, true);
	const provider = readJson(dir, "models.json").providers["forge-task-local"];
	assert.ok(provider, "forge-task-local provider is created");
	assert.equal(provider.baseUrl, "http://llms:8104/v1");
	assert.equal(provider.compat.thinkingFormat, "qwen-chat-template");
	assert.equal(provider.models[0].id, "chat");
	assert.deepEqual(provider.models[0].input, ["text"]);
});

test("reverting distributed-parallel to single leaves no dual-GPU residue", async () => {
	const dir = seedAgentDir();
	await applyProfile({ env: OFFLINE, agentDir: dir, name: "distributed-parallel" });
	await applyProfile({ env: OFFLINE, agentDir: dir, name: "single" });
	const settings = readJson(dir, "settings.json");
	assert.equal(settings.connectedServices.chat2.enabled, false);
	assert.equal(settings.connectedServices.think2.enabled, false);
	assert.deepEqual(settings.connectedServices.bulk.lanes, ["chat"]);
	assert.equal(settings.connectedServices.verify.service, null);
	assert.equal(settings.connectedServices.delegate.enabled, false);
	assert.equal(settings.contextBudget.useTaskModel, false);
	assert.equal(Object.hasOwn(settings, "taskModel"), false, "settings.taskModel is removed");
	const providers = readJson(dir, "models.json").providers;
	assert.equal(Object.hasOwn(providers, "forge-task-local"), false, "the offload provider is removed");
	// The three interactive providers are untouched.
	assert.deepEqual(Object.keys(providers).sort(), ["forge-local-chat", "forge-local-code", "forge-local-think"]);
});

test("a switch to a larger window recomputes the compaction reserve", async () => {
	const dir = seedAgentDir();
	// A one-off 262k profile: reserve must become 25% of the window, not stay at 1.
	writeFileSync(
		join(dir, "backends.json"),
		JSON.stringify({
			active: "big",
			profiles: {
				big: {
					primary: { host: "http://llms", images: true, contextTokens: 262144 },
					delegation: { enabled: false },
				},
			},
		}),
	);
	await applyProfile({ env: OFFLINE, agentDir: dir });
	assert.equal(readJson(dir, "settings.json").compaction.reserveTokens, 262144 - Math.floor(262144 * 0.75));
});

test('an unreachable stack leaves an "auto" window at the fallback constant', async () => {
	const dir = seedAgentDir();
	const result = await applyProfile({ env: OFFLINE, agentDir: dir, name: "single" });

	// `single` ships `contextTokens: "auto"`. With no stack to ask, the projection
	// has to land on the constant rather than on NaN or the literal string.
	assert.equal(DEFAULT_BACKENDS.profiles.single.primary.contextTokens, "auto");
	assert.equal(result.contextWindow, SLOT_CONTEXT_TOKENS);
	assert.equal(result.contextProbed, false);
	const settings = readJson(dir, "settings.json");
	assert.equal(settings.connectedServices.chat.contextTokens, SLOT_CONTEXT_TOKENS);
	assert.equal(
		readJson(dir, "models.json").providers["forge-local-chat"].models[0].contextWindow,
		SLOT_CONTEXT_TOKENS,
	);
});

test('an "auto" window adopts what the deployment serves, and clamps the slot pin', async () => {
	// A real HTTP stub rather than a mocked module: the probe's value is that it
	// speaks to the actual API, so the test that proves it should too.
	const { createServer } = await import("node:http");
	const snapshot = {
		api_version: "1.0",
		backends: [
			{
				name: "chat-primary",
				base_url: "http://127.0.0.1:8004",
				active: true,
				props: { n_ctx_per_slot: 262144, total_slots: 1, n_ctx_total: 262144 },
			},
		],
		services: [],
		config: {},
	};
	const server = createServer((request, response) => {
		if (!request.url?.endsWith("/api/v1/snapshot")) {
			response.writeHead(404).end();
			return;
		}
		response.writeHead(200, { "content-type": "application/json" });
		response.end(JSON.stringify(snapshot));
	});
	await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
	const baseUrl = `http://127.0.0.1:${server.address().port}`;

	try {
		clearStackStateCache();
		const dir = seedAgentDir();
		// Point the probe at the stub, and leave behind the slot-1 pin a two-slot
		// backend would have justified — the setup being applied has one slot.
		const settings = readJson(dir, "settings.json");
		settings.connectedServices.stackState = { enabled: true, baseUrl };
		settings.connectedServices.chat.scheduling = {
			...settings.connectedServices.chat.scheduling,
			enabled: true,
			interactiveSlot: 0,
			backgroundSlot: 1,
		};
		writeFileSync(join(dir, "settings.json"), `${JSON.stringify(settings)}\n`);

		const result = await applyProfile({ env: {}, agentDir: dir, name: "single" });

		assert.equal(result.contextWindow, 262144);
		assert.equal(result.contextProbed, true);
		const written = readJson(dir, "settings.json");
		assert.equal(written.connectedServices.chat.contextTokens, 262144);
		assert.equal(written.connectedServices.think.contextTokens, 262144);
		assert.equal(readJson(dir, "models.json").providers["forge-local-chat"].models[0].contextWindow, 262144);
		// The reserve follows the window, or the agent would compact far too late.
		assert.equal(written.compaction.reserveTokens, 262144 - Math.floor(262144 * 0.75));
		// One slot means there is no slot 1 to pin background work to.
		assert.equal(written.connectedServices.chat.scheduling.backgroundSlot, 0);
	} finally {
		clearStackStateCache();
		await new Promise((resolve) => server.close(resolve));
	}
});

test("clampBackgroundSlot only moves a pin the backend cannot honour", () => {
	const twoSlots = { totalSlots: 2 };
	const inRange = { scheduling: { enabled: true, interactiveSlot: 0, backgroundSlot: 1 } };
	assert.equal(clampBackgroundSlot(inRange, twoSlots, "chat"), null);
	assert.equal(inRange.scheduling.backgroundSlot, 1);

	const outOfRange = { scheduling: { enabled: true, interactiveSlot: 0, backgroundSlot: 1 } };
	assert.equal(clampBackgroundSlot(outOfRange, { totalSlots: 1 }, "chat"), 0);
	assert.equal(outOfRange.scheduling.backgroundSlot, 0);

	// Nothing read, nothing scheduled, and no service at all are all no-ops rather
	// than errors: the probe is optional and a setup may not carry scheduling.
	const unknown = { scheduling: { enabled: true, interactiveSlot: 0, backgroundSlot: 1 } };
	assert.equal(clampBackgroundSlot(unknown, null, "chat"), null);
	assert.equal(unknown.scheduling.backgroundSlot, 1);
	assert.equal(
		clampBackgroundSlot({ scheduling: { enabled: false, backgroundSlot: 1 } }, { totalSlots: 1 }, "chat"),
		null,
	);
	assert.equal(clampBackgroundSlot(undefined, { totalSlots: 1 }, "chat"), null);
});

test('an install predating "auto" is migrated, but a hand-chosen window is not', async () => {
	const dir = seedAgentDir();
	// A backends.json as written before "auto" existed: every primary carries the
	// built-in literal, except one someone pointed at a smaller backend on purpose.
	const config = structuredClone(DEFAULT_BACKENDS);
	config.profiles.single.primary.contextTokens = SLOT_CONTEXT_TOKENS;
	config.profiles.distributed.primary.contextTokens = SLOT_CONTEXT_TOKENS;
	config.profiles["distributed-parallel"].primary.contextTokens = 40960;
	config.profiles.single.description =
		"One image-capable model at 131k on llms; no delegation; embedding/transcription on llms.";
	config.profiles.distributed.description = "my own words";
	writeFileSync(join(dir, "backends.json"), `${JSON.stringify(config)}\n`);

	await applyProfile({ env: OFFLINE, agentDir: dir, name: "single" });

	const saved = readJson(dir, "backends.json");
	assert.equal(saved.profiles.single.primary.contextTokens, "auto");
	// The shipped description named the old fixed window, so it goes with it.
	assert.equal(saved.profiles.single.description, DEFAULT_BACKENDS.profiles.single.description);
	assert.equal(saved.profiles.distributed.primary.contextTokens, "auto");
	// Not the default value, so it was chosen; leave it exactly as it was.
	assert.equal(saved.profiles["distributed-parallel"].primary.contextTokens, 40960);
	// A description someone wrote is not the repo's to rewrite.
	assert.equal(saved.profiles.distributed.description, "my own words");
	assert.equal(saved.active, "single");
});

import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const libraryRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const { projectProfile, applyProfile, setDelegation, DEFAULT_BACKENDS, activeProfileName, loadBackends } = await import(
	join(libraryRoot, "backends.mjs")
);
const { seedConnectedServicesSettings } = await import(join(libraryRoot, "connected-services.mjs"));

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

test("projectProfile maps the distributed setup onto both registries", () => {
	const patch = projectProfile(DEFAULT_BACKENDS.profiles.distributed);
	assert.equal(patch.connectedServices.chat.baseUrl, "http://llms:8004/v1/chat/completions");
	assert.equal(patch.connectedServices.think.baseUrl, "http://llms:8008/v1/chat/completions");
	assert.equal(patch.connectedServices.delegate.enabled, true);
	assert.equal(patch.connectedServices.delegate.baseUrl, "http://llms:8104/v1/chat/completions");
	assert.equal(patch.connectedServices.delegate.model, "chat");
	assert.equal(patch.connectedServices.delegate.scheduling.enabled, false);
	assert.equal(patch.connectedServices.embeddings.url, "http://laptop:8005/v1/embeddings");
	assert.equal(patch.connectedServices.transcription.baseUrl, "http://laptop:8014");
	assert.equal(patch.connectedServices.ocr.url, "http://llms:5002/glmocr/parse");
	assert.deepEqual(patch.providers["forge-local-think"].input, ["text", "image"]);
	assert.equal(patch.providers["forge-local-think"].baseUrl, "http://llms:8003/v1");
});

test("a vision-free primary projects a text-only input array", () => {
	const patch = projectProfile({ primary: { host: "http://x", images: false, contextTokens: 131072 } });
	for (const provider of Object.values(patch.providers)) assert.deepEqual(provider.input, ["text"]);
});

test("disabled delegation projects enabled:false and never demands a baseUrl", () => {
	const patch = projectProfile(DEFAULT_BACKENDS.profiles.single);
	assert.equal(patch.connectedServices.delegate.enabled, false);
	assert.equal(patch.connectedServices.delegate.scheduling.enabled, false);
});

test("enabling delegation without a baseUrl is a clear error", () => {
	assert.throws(
		() => projectProfile({ primary: { host: "http://x" }, delegation: { enabled: true } }),
		/delegation\.baseUrl/,
	);
});

test("a setup without a primary host is a clear error", () => {
	assert.throws(() => projectProfile({ primary: {} }), /host/);
});

test("applyProfile writes settings and models and records the active setup", () => {
	const dir = seedAgentDir();
	const result = applyProfile({ env: {}, agentDir: dir, name: "distributed" });
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

test("switching to single turns delegation off but leaves the endpoint to reuse", () => {
	const dir = seedAgentDir();
	applyProfile({ env: {}, agentDir: dir, name: "distributed" });
	applyProfile({ env: {}, agentDir: dir, name: "single" });
	const delegate = readJson(dir, "settings.json").connectedServices.delegate;
	assert.equal(delegate.enabled, false);
	// The endpoint from the previous setup is preserved rather than erased, so a
	// later `/backend on` has something to re-enable.
	assert.equal(delegate.baseUrl, "http://llms:8104/v1/chat/completions");
});

test("setDelegation on/off toggles the active setup and re-applies", () => {
	const dir = seedAgentDir();
	applyProfile({ env: {}, agentDir: dir, name: "single" });
	const on = setDelegation({ env: {}, agentDir: dir, enabled: true });
	assert.equal(on.delegation, "on");
	assert.equal(readJson(dir, "settings.json").connectedServices.delegate.enabled, true);
	const off = setDelegation({ env: {}, agentDir: dir, enabled: false });
	assert.equal(off.delegation, "off");
	assert.equal(readJson(dir, "settings.json").connectedServices.delegate.enabled, false);
});

test("a switch to a larger window recomputes the compaction reserve", () => {
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
	applyProfile({ env: {}, agentDir: dir });
	assert.equal(readJson(dir, "settings.json").compaction.reserveTokens, 262144 - Math.floor(262144 * 0.75));
});

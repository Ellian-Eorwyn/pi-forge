import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createServer } from "node:http";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

// `stack-state.mjs` is the counterpart of `stack_state.py` and the two must
// agree: `configure-pi-forge` writes settings from this one while the eval suite
// and the Python skills read them through the other. The assertions here
// deliberately mirror `test_stack_state.py` against the same fixture, so a
// change to one module that is not made to the other fails a test rather than
// producing an install that writes one number and a run that enforces another.
const libraryRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const {
	backendForUrl,
	capacityForUrl,
	clearStackStateCache,
	explainUnreachable,
	healthAlerts,
	healthWarnings,
	identityForUrl,
	readSnapshot,
	resolveStackState,
	stackHealth,
} = await import(join(libraryRoot, "stack-state.mjs"));

const SNAPSHOT = JSON.parse(readFileSync(join(libraryRoot, "tests", "fixtures", "stack-snapshot.json"), "utf8"));
const CHAT_URL = "http://llms:8004/v1/chat/completions";
const THINK_URL = "http://llms:8008/v1/chat/completions";
const EMBED_URL = "http://llms:8005/v1/embeddings";
const TASK_URL = "http://llms:8007/v1";
const BACKEND_URL = "http://llms:8010/v1";
const QWEN = "/mnt/LLMs/llamacpp/llm-stack-git/models/Qwen3.6-27B-Q6_K.gguf";

/** A stub state API that can misbehave in each way a real one might. */
async function withStub({ payload = SNAPSHOT, status = 200, body = null }, run) {
	const requests = [];
	const server = createServer((req, res) => {
		requests.push({ path: req.url, auth: req.headers.authorization ?? null });
		if (status !== 200) {
			res.statusCode = status;
			return res.end();
		}
		res.setHeader("Content-Type", "application/json");
		if (body !== null) return res.end(body);
		if (req.url.endsWith("/health"))
			return res.end(JSON.stringify({ ok: true, api_version: payload.api_version ?? "1.0" }));
		res.end(JSON.stringify(payload));
	});
	await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
	clearStackStateCache();
	try {
		await run({ url: `http://127.0.0.1:${server.address().port}`, requests });
	} finally {
		clearStackStateCache();
		await new Promise((resolve) => server.close(resolve));
	}
}

/** A clean environment, so a developer's own settings cannot leak in. */
const envFor = (url, extra = {}) => ({ FORGE_STACK_STATE_URL: url, ...extra });
const mutated = (mutate) => {
	const copy = structuredClone(SNAPSHOT);
	mutate(copy);
	return copy;
};

test("proxy ports resolve through the config port map", () => {
	// :8004 and :8008 appear in no base_url and no probe target. Only the
	// config["Ports"] block connects them to the backend they front.
	for (const [url, role] of [
		[CHAT_URL, "NOTHINK_PORT"],
		[THINK_URL, "CODE_PORT"],
	]) {
		const located = backendForUrl(SNAPSHOT, url);
		assert.equal(located.backend.name, "chat-primary", url);
		assert.equal(located.role, role);
	}
});

test("backend ports resolve without the port map", () => {
	const stripped = { ...SNAPSHOT, config: {} };
	for (const [url, name] of [
		[EMBED_URL, "embed"],
		[TASK_URL, "task"],
		[BACKEND_URL, "chat-primary"],
	]) {
		assert.equal(backendForUrl(stripped, url)?.backend?.name, name, url);
	}
});

test("an unknown port resolves to nothing", () => {
	assert.equal(backendForUrl(SNAPSHOT, "http://llms:9999/v1"), null);
});

test("a router-held backend finds its service row by name", () => {
	// These backends have `unit: null` because the router loads them on demand,
	// so a lookup keyed only on `unit` would lose the reason worth reporting.
	const located = backendForUrl(SNAPSHOT, EMBED_URL);
	assert.equal(located.backend.unit, null);
	assert.equal(located.unitService.name, "embed");
});

test("a malformed snapshot is not an error", () => {
	for (const bad of [null, undefined, {}, { backends: "nonsense" }, { backends: [] }]) {
		assert.equal(backendForUrl(bad, CHAT_URL), null);
		assert.equal(capacityForUrl(bad, CHAT_URL), null);
		assert.equal(identityForUrl(bad, CHAT_URL), null);
		assert.equal(explainUnreachable(bad, CHAT_URL), null);
	}
});

test("capacity is per slot, not the pool", () => {
	const capacity = capacityForUrl(SNAPSHOT, CHAT_URL);
	assert.equal(capacity.contextTokens, 131072);
	assert.equal(capacity.contextTotal, 262144);
	assert.equal(capacity.totalSlots, 2);
});

test("capacity follows a reconfigured backend", () => {
	const moved = mutated((snapshot) => {
		for (const backend of snapshot.backends) {
			if (backend.name === "chat-primary") {
				backend.props.n_ctx_per_slot = 255998;
				backend.props.total_slots = 1;
			}
		}
	});
	assert.equal(capacityForUrl(moved, CHAT_URL).contextTokens, 255998);
	assert.equal(capacityForUrl(moved, CHAT_URL).totalSlots, 1);
});

test("an inactive backend reports no capacity", () => {
	assert.equal(capacityForUrl(SNAPSHOT, EMBED_URL), null);
});

test("identity names the weights and the build", () => {
	const identity = identityForUrl(SNAPSHOT, CHAT_URL);
	assert.equal(identity.modelPath, QWEN);
	assert.equal(identity.quant, "Q6_K");
	assert.equal(identity.buildInfo, "b10083-846e991ec");
	assert.equal(identity.unit, "chat-backend-dense");
});

test("both proxy profiles name the same weights", () => {
	assert.deepEqual(identityForUrl(SNAPSHOT, CHAT_URL), identityForUrl(SNAPSHOT, THINK_URL));
});

test("a stopped proxy is named with its reason", () => {
	const why = explainUnreachable(
		mutated((snapshot) => {
			for (const row of snapshot.services) {
				if (row.name === "chat-proxy") {
					row.state = "stopped";
					row.reason = "stopped on purpose";
				}
			}
		}),
		CHAT_URL,
	);
	assert.match(why, /port 8004/);
	assert.match(why, /stopped on purpose/);
});

test("a live proxy with no live backend says so", () => {
	// The worst case to diagnose without this: the connection is accepted and
	// every request fails, which reads as the model misbehaving.
	const why = explainUnreachable(
		mutated((snapshot) => {
			for (const row of snapshot.services) {
				if (row.name === "chat-proxy") {
					row.upstreams = [
						{ any_of: ["chat-backend-dense"], ok: false, states: { "chat-backend-dense": "stopped" } },
					];
				}
			}
		}),
		CHAT_URL,
	);
	assert.match(why, /no live backend/);
	assert.match(why, /chat-backend-dense is stopped/);
});

test("a router-held model says it loads on demand", () => {
	const why = explainUnreachable(SNAPSHOT, EMBED_URL);
	assert.match(why, /model router/);
	assert.match(why, /not resident/);
});

test("a loading model says to retry", () => {
	// Observed live: probing :8005 cold moved the router to `loading` and the
	// call timed out. That is the one state where the same call again in a
	// moment is the right response, so it must not read as a fault.
	const why = explainUnreachable(
		mutated((snapshot) => {
			for (const row of snapshot.router.models) if (row.id === "embed") row.state = "loading";
		}),
		EMBED_URL,
	);
	assert.match(why, /loading/);
	assert.match(why, /retrying shortly/);
});

test("the reranker is matched despite its router spelling", () => {
	// The backend list calls it `rerank`; the router calls it `rank`.
	assert.match(explainUnreachable(SNAPSHOT, "http://llms:8006/v1"), /model router/);
});

test("a sleeping backend is named", () => {
	const why = explainUnreachable(
		mutated((snapshot) => {
			for (const backend of snapshot.backends) if (backend.name === "chat-primary") backend.props.is_sleeping = true;
		}),
		CHAT_URL,
	);
	assert.match(why, /sleeping/);
});

test("a healthy stack explains nothing", () => {
	// The endpoint may still be failing for a reason the stack cannot see.
	assert.equal(explainUnreachable(SNAPSHOT, CHAT_URL), null);
});

test("warnings are passed through as the stack wrote them", () => {
	assert.ok(healthWarnings(SNAPSHOT).some((text) => text.toLowerCase().includes("swap")));
});

test("info alerts are excluded", () => {
	// `api_unauthenticated` is an info-level notice about the API itself.
	assert.ok(!healthAlerts(SNAPSHOT).some((row) => row.code === "api_unauthenticated"));
	assert.ok(SNAPSHOT.alerts.some((row) => row.code === "api_unauthenticated"));
});

test("a healthy stub is read", async () => {
	await withStub({}, async ({ url }) => {
		assert.equal((await readSnapshot({ env: envFor(url) })).api_version, "1.0");
		clearStackStateCache();
		assert.equal(await stackHealth({ env: envFor(url) }), true);
	});
});

test("a bearer token is sent when configured", async () => {
	await withStub({}, async ({ url, requests }) => {
		await readSnapshot({ env: envFor(url, { FORGE_STACK_STATE_TOKEN: "hunter2" }) });
		assert.equal(requests[0].auth, "Bearer hunter2");
	});
});

test("a server error reads as absent", async () => {
	await withStub({ status: 500 }, async ({ url }) => {
		assert.equal(await readSnapshot({ env: envFor(url) }), null);
	});
});

test("malformed json reads as absent", async () => {
	await withStub({ body: "{not json" }, async ({ url }) => {
		assert.equal(await readSnapshot({ env: envFor(url) }), null);
	});
});

test("a future major version is refused", async () => {
	// A wrong reading is worse than no reading: a bogus n_ctx_per_slot would be
	// written into settings as though it had been measured.
	await withStub({ payload: { ...SNAPSHOT, api_version: "2.0" } }, async ({ url }) => {
		assert.equal(await readSnapshot({ env: envFor(url) }), null);
	});
});

test("a later minor version is still read", async () => {
	await withStub({ payload: { ...SNAPSHOT, api_version: "1.7" } }, async ({ url }) => {
		assert.notEqual(await readSnapshot({ env: envFor(url) }), null);
	});
});

test("an unreachable host reads as absent", async () => {
	clearStackStateCache();
	assert.equal(await readSnapshot({ env: envFor("http://127.0.0.1:1"), timeoutMs: 1000 }), null);
	clearStackStateCache();
});

test("the snapshot is cached", async () => {
	await withStub({}, async ({ url, requests }) => {
		for (let i = 0; i < 3; i += 1) await readSnapshot({ env: envFor(url) });
		assert.equal(requests.length, 1);
	});
});

test("failure is cached too", async () => {
	// A doctor pass over three services against a stack that is down should wait
	// one timeout, not three.
	await withStub({ status: 500 }, async ({ url, requests }) => {
		for (let i = 0; i < 3; i += 1) await readSnapshot({ env: envFor(url) });
		assert.equal(requests.length, 1);
	});
});

test("the skip switch disables every read", async () => {
	await withStub({}, async ({ url, requests }) => {
		const env = envFor(url, { PI_FORGE_SKIP_STACK_DISCOVERY: "1" });
		assert.equal(await readSnapshot({ env }), null);
		assert.equal(await stackHealth({ env }), false);
		assert.equal(requests.length, 0);
	});
});

test("configuration precedence matches the python module", () => {
	assert.equal(resolveStackState({ env: { FORGE_STACK_STATE_URL: "" } }).enabled, false);
	assert.equal(
		resolveStackState({ env: {}, settings: { stackState: { baseUrl: "http://box:9000/" } } }).baseUrl,
		"http://box:9000",
	);
	assert.equal(
		resolveStackState({
			env: { FORGE_STACK_STATE_URL: "http://env:1" },
			settings: { stackState: { baseUrl: "http://settings:2" } },
		}).baseUrl,
		"http://env:1",
	);
	assert.equal(resolveStackState({ env: {}, settings: { stackState: { enabled: false } } }).enabled, false);
	assert.equal(
		resolveStackState({ env: {}, settings: { apiKeys: { "stack-state": "from-settings" } } }).token,
		"from-settings",
	);
	assert.equal(resolveStackState({ env: {} }).baseUrl, "http://llms:8078");
});

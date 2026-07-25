import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const libraryRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const { call, callJsonWithRetry, ChatError, extractJsonContent, hiddenTokenCount, parseJsonContent, PreemptedError, resolveService, resolveThinkService, serviceDoctor, activeInteractiveLeases, LEASE_STALE_MS } = await import(join(libraryRoot, "forge-llm.mjs"));
const { buildPackets, escalate, summarize, verifyPackets, VerificationError, VERDICT_FLAG } = await import(join(libraryRoot, "forge-verify.mjs"));

function withWorkspace(callback) {
	const workspace = mkdtempSync(join(tmpdir(), "forge-llm-mjs-"));
	try {
		return callback(workspace);
	} finally {
		rmSync(workspace, { recursive: true, force: true });
	}
}

/** A stub completions endpoint. `reply(payload, count)` returns the content string. */
function startStub(reply) {
	const requests = [];
	const server = createServer((request, response) => {
		let body = "";
		request.setEncoding("utf8");
		request.on("data", (chunk) => (body += chunk));
		request.on("end", () => {
			if (request.url.endsWith("/models")) {
				response.writeHead(200, { "Content-Type": "application/json" });
				response.end(JSON.stringify({ data: [{ id: "chat" }] }));
				return;
			}
			const payload = JSON.parse(body);
			requests.push(payload);
			const outcome = reply(payload, requests.length);
			if (outcome?.status && outcome.status >= 400) {
				response.writeHead(outcome.status, { "Content-Type": "text/plain" });
				response.end("upstream failure");
				return;
			}
			response.writeHead(200, { "Content-Type": "application/json" });
			response.end(
				JSON.stringify({
					choices: [{ message: { content: outcome.content }, finish_reason: outcome.finishReason ?? "stop" }],
					usage: { prompt_tokens: 12 },
					timings: { predicted_n: outcome.predicted ?? 4 },
				}),
			);
		});
	});
	return new Promise((resolve) => {
		server.listen(0, "127.0.0.1", () => {
			resolve({
				url: `http://127.0.0.1:${server.address().port}/v1/chat/completions`,
				requests,
				close: () => new Promise((done) => server.close(done)),
			});
		});
	});
}

const service = (url, name = "chat") => ({ name, enabled: true, url, model: "chat", scheduling: { enabled: false, interactiveSlot: 0, backgroundSlot: 1, idleGraceMs: 0 } });

test("a stray think block and code fence are stripped before parsing", () => {
	assert.equal(extractJsonContent('<think>weighing it up</think>\n```json\n{"a":1}\n```'), '{"a":1}');
	assert.deepEqual(parseJsonContent('<think>x</think>{"a":1}'), { a: 1 });
	// Prose around the payload is tolerated, as it is on the Python side.
	assert.deepEqual(parseJsonContent('Here you go: [{"b":2}] hope that helps'), [{ b: 2 }]);
});

test("hidden tokens are what the visible content cannot account for", () => {
	// The only evidence a backend reasoned: llama.cpp strips the block and
	// reports no reasoning_content.
	assert.equal(hiddenTokenCount(2, "ready"), 0);
	assert.equal(hiddenTokenCount(410, "ready"), 408);
	assert.equal(hiddenTokenCount(null, "ready"), null);
});

test("a truncated JSON response says so rather than reporting a parse error", async () => {
	const stub = await startStub(() => ({ content: '{"items": [{"a"', finishReason: "length" }));
	try {
		await assert.rejects(
			() => callJsonWithRetry(service(stub.url), [{ role: "user", content: "go" }], { attempts: 1 }),
			(error) => error instanceof ChatError && /truncated/.test(error.message),
		);
	} finally {
		await stub.close();
	}
});

test("a transient failure is retried and a permanent one is not", async () => {
	const flaky = await startStub((_payload, count) => (count === 1 ? { status: 503 } : { content: '{"ok":true}' }));
	try {
		const { value } = await callJsonWithRetry(service(flaky.url), [{ role: "user", content: "go" }]);
		assert.deepEqual(value, { ok: true });
		assert.equal(flaky.requests.length, 2);
	} finally {
		await flaky.close();
	}

	const broken = await startStub(() => ({ status: 400 }));
	try {
		await assert.rejects(() => callJsonWithRetry(service(broken.url), [{ role: "user", content: "go" }]));
		assert.equal(broken.requests.length, 1, "a 400 is not retried");
	} finally {
		await broken.close();
	}
});

test("doctor reports a bulk endpoint that is quietly reasoning", async () => {
	const thinking = await startStub(() => ({ content: "ready", predicted: 410 }));
	try {
		const report = await serviceDoctor(service(thinking.url), { expectNonThinking: true });
		assert.equal(report.reachable, true);
		assert.equal(report.thinking, true);
		// Nothing in the response body reveals this; only the token count does.
		assert.match(report.warning, /hidden reasoning tokens/);
	} finally {
		await thinking.close();
	}

	const quiet = await startStub(() => ({ content: "ready", predicted: 2 }));
	try {
		const report = await serviceDoctor(service(quiet.url), { expectNonThinking: true });
		assert.equal(report.thinking, false);
		assert.equal(report.warning, undefined);
	} finally {
		await quiet.close();
	}
});

test("doctor reports a model name the endpoint does not serve", async () => {
	const stub = await startStub(() => ({ content: "ready", predicted: 2 }));
	try {
		const report = await serviceDoctor({ ...service(stub.url), model: "code" }, {});
		assert.equal(report.modelMismatch, true);
		assert.match(report.warning, /is not served here/);
	} finally {
		await stub.close();
	}
});

test("resolution honors an explicit url over the environment", () => {
	const previous = process.env.FORGE_BASE_CHAT_URL;
	process.env.FORGE_BASE_CHAT_URL = "http://127.0.0.1:1/v1/chat/completions";
	try {
		const explicit = resolveService("chat", { chatUrl: "http://127.0.0.1:2/v1" });
		assert.equal(explicit.url, "http://127.0.0.1:2/v1/chat/completions");
		assert.equal(resolveService("chat", {}).url, "http://127.0.0.1:1/v1/chat/completions");
	} finally {
		if (previous === undefined) delete process.env.FORGE_BASE_CHAT_URL;
		else process.env.FORGE_BASE_CHAT_URL = previous;
	}
});

test("think falls back to chat when no thinking backend is configured", () => {
	const previous = process.env.FORGE_THINK_URL;
	process.env.FORGE_THINK_URL = "";
	try {
		const think = resolveThinkService({});
		// Losing the split still verifies; it just verifies on the bulk endpoint.
		assert.equal(think.name, "think");
		assert.equal(think.fallback, "chat");
	} finally {
		if (previous === undefined) delete process.env.FORGE_THINK_URL;
		else process.env.FORGE_THINK_URL = previous;
	}
});

test("leases written by the Python client are honored by this one, and the reverse", () => {
	withWorkspace((workspace) => {
		const agentDirectory = join(workspace, "agent");
		const previous = process.env.PI_FORGE_AGENT_DIR;
		process.env.PI_FORGE_AGENT_DIR = agentDirectory;
		try {
			const leases = join(agentDirectory, "inference-leases");
			mkdirSync(leases, { recursive: true });

			// Written the way forge_llm.py writes an interactive lease.
			writeFileSync(join(leases, "interactive-1.json"), `${JSON.stringify({ pid: 1, kind: "interactive", slot: 0, updatedAtMs: Date.now() })}\n`);
			assert.equal(activeInteractiveLeases().length, 1, "a Python-written lease must block a JavaScript worker");

			// Absent `kind` means interactive on both sides.
			writeFileSync(join(leases, "interactive-2.json"), `${JSON.stringify({ pid: 2, slot: 0, updatedAtMs: Date.now() })}\n`);
			assert.equal(activeInteractiveLeases().length, 2, "a lease without kind defaults to interactive");

			// A background lease is not a claim on the interactive slot.
			writeFileSync(join(leases, "background-3.json"), `${JSON.stringify({ pid: 3, kind: "background", slot: 1, updatedAtMs: Date.now() })}\n`);
			assert.equal(activeInteractiveLeases().length, 2);

			// Stale leases expire at the same boundary in both runtimes.
			writeFileSync(join(leases, "interactive-4.json"), `${JSON.stringify({ pid: 4, kind: "interactive", slot: 0, updatedAtMs: Date.now() - LEASE_STALE_MS - 1000 })}\n`);
			assert.equal(activeInteractiveLeases().length, 2, "a stale lease is not a claim");

			// And the Python client must agree about what this one wrote.
			const python = process.env.PI_FORGE_TEST_PYTHON || "python3";
			const script = `
import sys, json
sys.path.insert(0, ${JSON.stringify(libraryRoot)})
import forge_llm
print(json.dumps({"active": len(forge_llm.active_interactive_leases()), "stale": forge_llm.LEASE_STALE_MS}))
`;
			const output = JSON.parse(execFileSync(python, ["-c", script], { encoding: "utf8", env: { ...process.env, PI_FORGE_AGENT_DIR: agentDirectory } }));
			assert.equal(output.active, 2, "the Python client must see the same active leases");
			assert.equal(output.stale, LEASE_STALE_MS, "both runtimes must expire leases at the same boundary");
		} finally {
			if (previous === undefined) delete process.env.PI_FORGE_AGENT_DIR;
			else process.env.PI_FORGE_AGENT_DIR = previous;
		}
	});
});

test("an unwritable agent directory costs scheduling, not the call", async () => {
	const stub = await startStub(() => ({ content: '{"ok":true}' }));
	const previous = process.env.PI_FORGE_AGENT_DIR;
	// A path under /dev/null cannot be created.
	process.env.PI_FORGE_AGENT_DIR = "/dev/null/agent";
	try {
		const scheduled = { ...service(stub.url, "think"), scheduling: { enabled: true, interactiveSlot: 0, backgroundSlot: 1, idleGraceMs: 0 } };
		const { record } = await call(scheduled, [{ role: "user", content: "go" }], { background: true });
		assert.equal(record.mode, "foreground", "the work still runs, it just cannot announce itself");
	} finally {
		await stub.close();
		if (previous === undefined) delete process.env.PI_FORGE_AGENT_DIR;
		else process.env.PI_FORGE_AGENT_DIR = previous;
	}
});

test("packets are bounded by count and by serialized size", () => {
	const many = Array.from({ length: 45 }, (_value, index) => ({ id: `n${index}`, text: "x" }));
	assert.deepEqual(buildPackets(many, 20).map((packet) => packet.length), [20, 20, 5]);
	const large = Array.from({ length: 4 }, (_value, index) => ({ id: `n${index}`, text: "x".repeat(10_000) }));
	assert.deepEqual(buildPackets(large, 20, 24_000).map((packet) => packet.length), [2, 2]);
});

test("verification requires a verdict for every id and a reason on every flag", async () => {
	const items = [{ id: "a" }, { id: "b" }];
	const short = await startStub(() => ({ content: JSON.stringify({ verdicts: [{ id: "a", verdict: "ok" }] }) }));
	try {
		await assert.rejects(
			() => verifyPackets(service(short.url, "think"), "Check.", items, { background: false }),
			(error) => error instanceof VerificationError,
		);
		assert.equal(short.requests.length, 2, "one corrective retry showing the model what it broke");
	} finally {
		await short.close();
	}

	const unreasoned = await startStub(() => ({ content: JSON.stringify({ verdicts: [{ id: "a", verdict: "flag" }, { id: "b", verdict: "ok" }] }) }));
	try {
		await assert.rejects(() => verifyPackets(service(unreasoned.url, "think"), "Check.", items, { background: false }));
	} finally {
		await unreasoned.close();
	}
});

test("an unreachable verifier raises rather than passing work", async () => {
	await assert.rejects(
		() => verifyPackets(service("http://127.0.0.1:9/v1/chat/completions", "think"), "Check.", [{ id: "a" }], { background: false, timeoutMs: 1000 }),
		(error) => error instanceof VerificationError,
	);
});

test("verdicts resume from the journal instead of being re-reviewed", async () => {
	await withWorkspace(async (workspace) => {
		const journalPath = join(workspace, "verified.jsonl");
		const stub = await startStub(() => ({ content: JSON.stringify({ verdicts: [{ id: "a", verdict: "ok" }] }) }));
		try {
			await verifyPackets(service(stub.url, "think"), "Check.", [{ id: "a" }], { background: false, journalPath });
			await verifyPackets(service(stub.url, "think"), "Check.", [{ id: "a" }], { background: false, journalPath });
			assert.equal(stub.requests.length, 1, "a resumed run reviews nothing twice");
		} finally {
			await stub.close();
		}
	});
});

test("an item already escalated is not redone on resume", async () => {
	await withWorkspace(async (workspace) => {
		const journalPath = join(workspace, "verified.jsonl");
		const calls = [];
		const redo = async (item) => {
			calls.push(item.id);
			return { fixed: true };
		};
		const first = await escalate([[{ id: "a" }, "wrong"]], redo, { journalPath });
		const second = await escalate([[{ id: "a" }, "wrong"]], redo, { journalPath });
		assert.deepEqual(calls, ["a"]);
		assert.equal(first.a.resumed, undefined);
		assert.equal(second.a.resumed, true);
		assert.equal(second.a.value, undefined, "the caller must not re-commit a resumed outcome");
	});
});

test("a failed escalation becomes a human review item", async () => {
	const results = await escalate([[{ id: "a" }, "wrong"]], async () => {
		throw new Error("model unavailable");
	});
	assert.equal(results.a.ok, false);
	assert.match(results.a.detail, /model unavailable/);
	const summary = summarize({ a: { verdict: VERDICT_FLAG, reason: "wrong" } }, results);
	assert.equal(summary.needsReview, 1);
	assert.equal(summary.escalated, 0);
});

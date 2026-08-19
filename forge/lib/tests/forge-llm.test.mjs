import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

// `call` and `serviceDoctor` describe the backend behind an endpoint by reading
// the deployment's state API. That read is optional everywhere, but a test suite
// must not depend on whether a particular host is up: on a developer machine it
// is, so these assertions would quietly vary with whatever it happens to be
// serving, and in CI it is not, so every call would spend its timeout. The
// behaviour itself is covered against a stub in `stack-state.test.mjs`.
process.env.PI_FORGE_SKIP_STACK_DISCOVERY = "1";

const libraryRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const {
	call,
	callWithTools,
	callJsonWithRetry,
	ChatError,
	ContextBudgetError,
	doctorWarnings,
	estimatePromptTokens,
	extractJsonContent,
	hiddenTokenCount,
	imageContentPart,
	imageMessage,
	parseJsonContent,
	resetStackConditions,
	resolveBulkLaneServices,
	resolveDelegateService,
	resolveService,
	resolveTaskService,
	resolveThinkService,
	resolveVerifyService,
	dispatchBulk,
	serviceDoctor,
	activeInteractiveLeases,
	LEASE_STALE_MS,
} = await import(join(libraryRoot, "forge-llm.mjs"));
const { clearStackStateCache } = await import(join(libraryRoot, "stack-state.mjs"));
const { SLOT_CONTEXT_TOKENS } = await import(join(libraryRoot, "connected-services.mjs"));
const { buildPackets, escalate, summarize, verifyPackets, VerificationError, VERDICT_FLAG } = await import(
	join(libraryRoot, "forge-verify.mjs")
);

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
		request.on("data", (chunk) => {
			body += chunk;
		});
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

const service = (url, name = "chat") => ({
	name,
	enabled: true,
	url,
	model: "chat",
	scheduling: { enabled: false, interactiveSlot: 0, backgroundSlot: 1, idleGraceMs: 0 },
});

test("a stray think block and code fence are stripped before parsing", () => {
	assert.equal(extractJsonContent('<think>weighing it up</think>\n```json\n{"a":1}\n```'), '{"a":1}');
	assert.deepEqual(parseJsonContent('<think>x</think>{"a":1}'), { a: 1 });
	// Prose around the payload is tolerated, as it is on the Python side.
	assert.deepEqual(parseJsonContent('Here you go: [{"b":2}] hope that helps'), [{ b: 2 }]);
});

test("a prompt that cannot fit a slot is refused before the request is sent", async () => {
	const stub = await startStub(() => ({ content: "{}" }));
	try {
		const oversized = "x".repeat(SLOT_CONTEXT_TOKENS * 4);
		await assert.rejects(
			() => call(service(stub.url), [{ role: "user", content: oversized }]),
			(error) => {
				assert.ok(error instanceof ContextBudgetError);
				// Callers that only know about ChatError still catch it.
				assert.ok(error instanceof ChatError);
				// The ceiling and the service it belongs to, because a service
				// can now carry a smaller one than the default slot.
				assert.match(error.message, new RegExp(`${SLOT_CONTEXT_TOKENS}-token limit on service "chat"`));
				return true;
			},
		);
		// Refused before a socket was opened, not after a long prefill.
		assert.equal(stub.requests.length, 0);
	} finally {
		await stub.close();
	}
});

test("a service can carry a smaller ceiling than the default slot", async () => {
	const stub = await startStub(() => ({ content: "{}" }));
	try {
		// Comfortably inside a 131072-token slot, well over a 4096-token one.
		const messages = [{ role: "user", content: "x".repeat(40_000) }];
		await call(service(stub.url), messages);
		assert.equal(stub.requests.length, 1);

		const smaller = { ...service(stub.url), contextTokens: 4096 };
		await assert.rejects(
			() => call(smaller, messages),
			(error) => {
				assert.ok(error instanceof ContextBudgetError);
				assert.match(error.message, /4096-token limit on service "chat"/);
				return true;
			},
		);
		// Refused before a socket was opened, so the count has not moved.
		assert.equal(stub.requests.length, 1);
	} finally {
		await stub.close();
	}
});

test("chat template kwargs are forwarded verbatim, and omitted when unset", async () => {
	const stub = await startStub(() => ({ content: "{}" }));
	try {
		await call({ ...service(stub.url), chatTemplateKwargs: { enable_thinking: false } }, [
			{ role: "user", content: "hi" },
		]);
		assert.deepEqual(stub.requests[0].chat_template_kwargs, { enable_thinking: false });
		// A backend that does not understand the field must not be sent it.
		await call(service(stub.url), [{ role: "user", content: "hi" }]);
		assert.ok(!("chat_template_kwargs" in stub.requests[1]));
	} finally {
		await stub.close();
	}
});

test("reasoning effort is forwarded, a per-call value wins, and it is omitted when unset", async () => {
	const stub = await startStub(() => ({ content: "{}" }));
	try {
		await call({ ...service(stub.url), reasoningEffort: "medium" }, [{ role: "user", content: "hi" }]);
		assert.equal(stub.requests[0].reasoning_effort, "medium");
		// A per-call value forces the level for one item, as escalation does.
		await call({ ...service(stub.url), reasoningEffort: "medium" }, [{ role: "user", content: "hi" }], {
			reasoningEffort: "xhigh",
		});
		assert.equal(stub.requests[1].reasoning_effort, "xhigh");
		await call(service(stub.url), [{ role: "user", content: "hi" }]);
		assert.ok(!("reasoning_effort" in stub.requests[2]));
	} finally {
		await stub.close();
	}
});

test("the doctor names an endpoint that answers with no visible content", async () => {
	// Measured against the task backend: with `--reasoning-format deepseek` the
	// reply arrives in `reasoning_content` and `content` is empty, so every
	// JSON-expecting skill fails on a response the server called a success.
	const stub = await startStub(() => ({ content: "", predicted: 64 }));
	try {
		const report = await serviceDoctor(service(stub.url), { expectNonThinking: true });
		assert.equal(report.reachable, true);
		assert.equal(report.emptyContent, true);
		assert.match(report.warning, /enable_thinking/);
		assert.match(report.detail, /no visible content/);
	} finally {
		await stub.close();
	}
});

test("output tokens count against the slot alongside the prompt", async () => {
	const stub = await startStub(() => ({ content: "{}" }));
	try {
		// Comfortably under the ceiling on its own, over it once max_tokens is reserved.
		const content = "x".repeat(Math.trunc(SLOT_CONTEXT_TOKENS * 3.42) - 1000);
		await assert.rejects(
			() => call(service(stub.url), [{ role: "user", content }], { maxTokens: 4096 }),
			ContextBudgetError,
		);
		assert.equal(stub.requests.length, 0);
		// The same prompt goes through when nothing is reserved for output.
		const result = await call(service(stub.url), [{ role: "user", content }]);
		assert.equal(result.content, "{}");
	} finally {
		await stub.close();
	}
});

test("prompt estimation counts string and structured content", () => {
	assert.equal(estimatePromptTokens([{ role: "user", content: "x".repeat(342) }]), 100);
	assert.equal(estimatePromptTokens([{ role: "user", content: [{ type: "text", text: "x".repeat(342) }] }]), 100);
	assert.equal(estimatePromptTokens([]), 0);
});

test("both runtimes agree on the slot budget and the density it is measured with", () => {
	// A Python skill and a JavaScript one send work to the same slots. If these
	// drift, one of them refuses prompts the other happily sends.
	const python = process.env.PI_FORGE_TEST_PYTHON || "python3";
	const script = `
import sys, json
sys.path.insert(0, ${JSON.stringify(libraryRoot)})
import forge_llm
print(json.dumps({
    "slot": forge_llm.SLOT_CONTEXT_TOKENS,
    "density": forge_llm.PROMPT_CHARACTERS_PER_TOKEN,
    "estimate": forge_llm.estimate_prompt_tokens([{"role": "user", "content": "x" * 342}]),
    "chatScheduled": forge_llm.DEFAULT_SERVICES["chat"]["scheduling"]["enabled"],
}))
`;
	const output = JSON.parse(execFileSync(python, ["-c", script], { encoding: "utf8" }));
	assert.equal(output.slot, SLOT_CONTEXT_TOKENS);
	assert.equal(output.estimate, estimatePromptTokens([{ role: "user", content: "x".repeat(342) }]));
	assert.equal(output.chatScheduled, true, "bulk work must pin its slot in both runtimes");
});

test("background bulk work pins the slot it was assigned", async () => {
	const stub = await startStub(() => ({ content: "{}" }));
	try {
		const scheduled = {
			...service(stub.url),
			scheduling: { enabled: true, interactiveSlot: 0, backgroundSlot: 1, idleGraceMs: 0 },
		};
		await call(scheduled, [{ role: "user", content: "hi" }], { background: true });
		assert.equal(stub.requests[0].id_slot, 1);
	} finally {
		await stub.close();
	}
});

test("callWithTools forwards tools, pins the background slot, and returns a tool-call turn", async () => {
	// The delegate's agentic calls: a tool-call turn has content null and
	// tool_calls set, which `call` rejects but `callWithTools` must return.
	const toolCallMessage = {
		role: "assistant",
		content: null,
		tool_calls: [{ id: "c1", type: "function", function: { name: "grep", arguments: "{}" } }],
	};
	const requests = [];
	const server = createServer((request, response) => {
		let body = "";
		request.setEncoding("utf8");
		request.on("data", (chunk) => {
			body += chunk;
		});
		request.on("end", () => {
			requests.push(JSON.parse(body));
			response.writeHead(200, { "Content-Type": "application/json" });
			response.end(
				JSON.stringify({ choices: [{ message: toolCallMessage, finish_reason: "tool_calls" }], usage: {} }),
			);
		});
	});
	await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
	const url = `http://127.0.0.1:${server.address().port}/v1/chat/completions`;
	const scheduled = {
		name: "chat",
		enabled: true,
		url,
		model: "chat",
		scheduling: { enabled: true, interactiveSlot: 0, backgroundSlot: 1, idleGraceMs: 0 },
	};
	const oaTools = [
		{
			type: "function",
			function: { name: "grep", description: "search", parameters: { type: "object", properties: {} } },
		},
	];
	try {
		const { message, finishReason } = await callWithTools(scheduled, [{ role: "user", content: "hi" }], {
			background: true,
			tools: oaTools,
		});
		assert.equal(requests[0].id_slot, 1, "background tool calls pin slot 1");
		assert.deepEqual(requests[0].tools, oaTools, "the tools array is forwarded verbatim");
		assert.equal(finishReason, "tool_calls");
		assert.equal(message.content, null, "a null-content tool-call turn is returned, not rejected");
		assert.equal(message.tool_calls[0].function.name, "grep");
	} finally {
		await new Promise((done) => server.close(done));
	}
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

test("the task tier is off by default and falls back up to chat", () => {
	// Off because it is a separate backend behind a swapping router, not another
	// profile in front of the one already loaded. An install that never asked for
	// it should not start paying model swaps.
	const task = resolveTaskService({});
	assert.equal(task.name, "task");
	assert.equal(task.fallback, "chat");
	assert.equal(task.url, resolveService("chat", {}).url);
});

test("a configured task tier keeps its own ceiling and template kwargs", () => {
	const task = resolveTaskService({ taskUrl: "http://127.0.0.1:7/v1" });
	assert.equal(task.url, "http://127.0.0.1:7/v1/chat/completions");
	assert.equal(task.fallback, undefined);
	// The two fields that make a non-default backend usable at all: half the
	// chat slot, and the kwarg without which it answers into reasoning_content.
	assert.equal(task.contextTokens, 65538);
	assert.deepEqual(task.chatTemplateKwargs, { enable_thinking: false });
});

test("the delegate tier is off by default and forge_delegate falls back to chat", () => {
	// Empty settings, not the machine's real ones: this asserts the built-in
	// default, which a host that has switched to a delegation-enabled setup would
	// otherwise override (making the fallback the secondary, not chat).
	const delegate = resolveDelegateService({ env: {}, settings: {} });
	assert.equal(delegate.name, "delegate");
	assert.equal(delegate.fallback, "chat");
	assert.equal(delegate.url, resolveService("chat", { env: {}, settings: {} }).url);
	// The fallback runs on the primary's background slot, exactly as delegation
	// did before a secondary was possible.
	assert.equal(delegate.scheduling.enabled, true);
});

test("a configured delegate targets the secondary with slot pinning off", () => {
	const delegate = resolveDelegateService({
		env: {},
		settings: {
			connectedServices: {
				delegate: {
					enabled: true,
					baseUrl: "http://llms:8104/v1/chat/completions",
					model: "chat-custom2",
					chatTemplateKwargs: { enable_thinking: false },
					scheduling: { enabled: false, backgroundSlot: 0 },
				},
			},
		},
	});
	assert.equal(delegate.url, "http://llms:8104/v1/chat/completions");
	assert.equal(delegate.model, "chat-custom2");
	assert.equal(delegate.fallback, undefined);
	// Off on purpose: the secondary is a separate single-slot backend, so no
	// id_slot is sent (see connected-services.mjs).
	assert.equal(delegate.scheduling.enabled, false);
	assert.deepEqual(delegate.chatTemplateKwargs, { enable_thinking: false });
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
			writeFileSync(
				join(leases, "interactive-1.json"),
				`${JSON.stringify({ pid: 1, kind: "interactive", slot: 0, updatedAtMs: Date.now() })}\n`,
			);
			assert.equal(activeInteractiveLeases().length, 1, "a Python-written lease must block a JavaScript worker");

			// Absent `kind` means interactive on both sides.
			writeFileSync(
				join(leases, "interactive-2.json"),
				`${JSON.stringify({ pid: 2, slot: 0, updatedAtMs: Date.now() })}\n`,
			);
			assert.equal(activeInteractiveLeases().length, 2, "a lease without kind defaults to interactive");

			// A background lease is not a claim on the interactive slot.
			writeFileSync(
				join(leases, "background-3.json"),
				`${JSON.stringify({ pid: 3, kind: "background", slot: 1, updatedAtMs: Date.now() })}\n`,
			);
			assert.equal(activeInteractiveLeases().length, 2);

			// Stale leases expire at the same boundary in both runtimes.
			writeFileSync(
				join(leases, "interactive-4.json"),
				`${JSON.stringify({ pid: 4, kind: "interactive", slot: 0, updatedAtMs: Date.now() - LEASE_STALE_MS - 1000 })}\n`,
			);
			assert.equal(activeInteractiveLeases().length, 2, "a stale lease is not a claim");

			// And the Python client must agree about what this one wrote.
			const python = process.env.PI_FORGE_TEST_PYTHON || "python3";
			const script = `
import sys, json
sys.path.insert(0, ${JSON.stringify(libraryRoot)})
import forge_llm
print(json.dumps({"active": len(forge_llm.active_interactive_leases()), "stale": forge_llm.LEASE_STALE_MS}))
`;
			const output = JSON.parse(
				execFileSync(python, ["-c", script], {
					encoding: "utf8",
					env: { ...process.env, PI_FORGE_AGENT_DIR: agentDirectory },
				}),
			);
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
		const scheduled = {
			...service(stub.url, "think"),
			scheduling: { enabled: true, interactiveSlot: 0, backgroundSlot: 1, idleGraceMs: 0 },
		};
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
	assert.deepEqual(
		buildPackets(many, 20).map((packet) => packet.length),
		[20, 20, 5],
	);
	const large = Array.from({ length: 4 }, (_value, index) => ({ id: `n${index}`, text: "x".repeat(10_000) }));
	assert.deepEqual(
		buildPackets(large, 20, 24_000).map((packet) => packet.length),
		[2, 2],
	);
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

	const unreasoned = await startStub(() => ({
		content: JSON.stringify({
			verdicts: [
				{ id: "a", verdict: "flag" },
				{ id: "b", verdict: "ok" },
			],
		}),
	}));
	try {
		await assert.rejects(() =>
			verifyPackets(service(unreasoned.url, "think"), "Check.", items, { background: false }),
		);
	} finally {
		await unreasoned.close();
	}
});

test("an unreachable verifier raises rather than passing work", async () => {
	await assert.rejects(
		() =>
			verifyPackets(service("http://127.0.0.1:9/v1/chat/completions", "think"), "Check.", [{ id: "a" }], {
				background: false,
				timeoutMs: 1000,
			}),
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

/**
 * A state API whose primary backend claims to live on `chatPort`.
 *
 * Rewriting the port is what lets these tests resolve a throwaway stub on
 * 127.0.0.1 to a backend in the captured fixture. Mirrors `FakeStackServer` in
 * `test_forge_llm.py`.
 */
async function startStackStub(chatPort, { contextTokens = 131_072, totalSlots = 2, active = true } = {}) {
	const snapshot = JSON.parse(readFileSync(join(libraryRoot, "tests", "fixtures", "stack-snapshot.json"), "utf8"));
	for (const backend of snapshot.backends) {
		if (backend.name !== "chat-primary") continue;
		backend.base_url = `http://127.0.0.1:${chatPort}`;
		backend.active = active;
		backend.props.n_ctx_per_slot = contextTokens;
		backend.props.total_slots = totalSlots;
	}
	// The fixture's own port map would otherwise claim this port for a different
	// role and defeat the rewrite above.
	snapshot.config = {};
	const server = createServer((_request, response) => {
		response.writeHead(200, { "Content-Type": "application/json" });
		response.end(JSON.stringify(snapshot));
	});
	await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
	clearStackStateCache();
	return {
		env: { FORGE_STACK_STATE_URL: `http://127.0.0.1:${server.address().port}` },
		close: async () => {
			clearStackStateCache();
			await new Promise((done) => server.close(done));
		},
	};
}

const portOfStub = (url) => Number(new URL(url).port);

test("the doctor names the weights behind the endpoint", async () => {
	// The model id proves nothing — llama.cpp answers to whatever name it is sent
	// regardless of what is loaded — so the doctor reports the launched path, its
	// quantization, and the llama.cpp build instead.
	const stub = await startStub(() => ({ content: "ready" }));
	let stack;
	try {
		stack = await startStackStub(portOfStub(stub.url));
		const report = await serviceDoctor(service(stub.url), { env: stack.env });
		assert.equal(report.backend.quant, "Q6_K");
		assert.equal(report.backend.buildInfo, "b10083-846e991ec");
		assert.equal(doctorWarnings(report).length, 0);
	} finally {
		await stack?.close();
		await stub.close();
	}
});

test("the doctor warns when the configured context does not match the slot", async () => {
	const stub = await startStub(() => ({ content: "ready" }));
	let stack;
	try {
		stack = await startStackStub(portOfStub(stub.url), { contextTokens: 65_536 });
		const report = await serviceDoctor(service(stub.url), { env: stack.env });
		assert.equal(report.contextMismatch, true);
		assert.equal(report.servedContextTokens, 65_536);
		assert.ok(doctorWarnings(report).includes(report.contextWarning));
	} finally {
		await stack?.close();
		await stub.close();
	}
});

test("the doctor warns when the pinned slot does not exist", async () => {
	// Nothing else in forge checks this: the slot number is only ever sent, never
	// validated, so a backend moving to `--parallel 1` would leave every
	// background call naming slot 1 forever.
	const stub = await startStub(() => ({ content: "ready" }));
	let stack;
	try {
		stack = await startStackStub(portOfStub(stub.url), { totalSlots: 1 });
		const report = await serviceDoctor(
			{ ...service(stub.url), scheduling: { ...service(stub.url).scheduling, enabled: true } },
			{ env: stack.env },
		);
		assert.match(report.slotWarning, /slot 1/);
		assert.match(report.slotWarning, /1 slot/);
	} finally {
		await stack?.close();
		await stub.close();
	}
});

test("the first record carries the backend and the stack warnings", async () => {
	resetStackConditions();
	const stub = await startStub(() => ({ content: "{}" }));
	let stack;
	try {
		stack = await startStackStub(portOfStub(stub.url));
		const first = await call(service(stub.url), [{ role: "user", content: "hi" }], { env: stack.env });
		const second = await call(service(stub.url), [{ role: "user", content: "hi" }], { env: stack.env });
		assert.ok(first.record.backend.modelPath.endsWith(".gguf"));
		assert.ok(first.record.stackWarnings.some((text) => text.toLowerCase().includes("swap")));
		// A 500-item batch should say which weights it ran against once, not five
		// hundred times.
		assert.equal(second.record.backend, undefined);
		assert.equal(second.record.stackWarnings, undefined);
	} finally {
		resetStackConditions();
		await stack?.close();
		await stub.close();
	}
});

test("reports and records are unchanged without a stack", async () => {
	// The degradation guarantee: every install that is not this deployment.
	resetStackConditions();
	const stub = await startStub(() => ({ content: "{}" }));
	try {
		const report = await serviceDoctor(service(stub.url), { env: { PI_FORGE_SKIP_STACK_DISCOVERY: "1" } });
		const { record } = await call(service(stub.url), [{ role: "user", content: "hi" }], {
			env: { FORGE_STACK_STATE_URL: "http://127.0.0.1:1" },
		});
		for (const key of ["backend", "stackDetail", "contextWarning", "slotWarning"])
			assert.equal(report[key], undefined, key);
		assert.equal(record.backend, undefined);
		assert.equal(record.stackWarnings, undefined);
	} finally {
		resetStackConditions();
		await stub.close();
	}
});

// A 1×1 PNG — enough to exercise magic-byte sniffing and base64 wrapping. Same
// bytes as `_PNG_BYTES` in `test_forge_llm.py`.
const PNG_BYTES = Buffer.from(
	"89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489" +
		"0000000d49444154789c62f80f0400010101000a2d0f1b0000000049454e44ae426082",
	"hex",
);

function withImage(suffix, bytes = PNG_BYTES) {
	const dir = mkdtempSync(join(tmpdir(), "forge-llm-img-"));
	const path = join(dir, `image${suffix}`);
	writeFileSync(path, bytes);
	return { path, cleanup: () => rmSync(dir, { recursive: true, force: true }) };
}

test("imageContentPart wraps bytes as a base64 data URI", () => {
	const { path, cleanup } = withImage(".png");
	try {
		const part = imageContentPart(path);
		assert.equal(part.type, "image_url");
		assert.ok(part.image_url.url.startsWith("data:image/png;base64,"));
	} finally {
		cleanup();
	}
});

test("imageContentPart sniffs the MIME rather than trusting the extension", () => {
	const { path, cleanup } = withImage(".bin");
	try {
		assert.ok(imageContentPart(path).image_url.url.startsWith("data:image/png;base64,"));
	} finally {
		cleanup();
	}
});

test("imageContentPart rejects a non-image", () => {
	const { path, cleanup } = withImage(".txt", Buffer.from("not an image"));
	try {
		assert.throws(() => imageContentPart(path), ChatError);
	} finally {
		cleanup();
	}
});

test("imageMessage carries text then one part per image", () => {
	const { path, cleanup } = withImage(".png");
	try {
		const message = imageMessage("Describe this.", [path, path]);
		assert.equal(message.role, "user");
		assert.deepEqual(message.content[0], { type: "text", text: "Describe this." });
		assert.deepEqual(
			message.content.slice(1).map((part) => part.type),
			["image_url", "image_url"],
		);
	} finally {
		cleanup();
	}
});

test("estimatePromptTokens counts images instead of zero", () => {
	const { path, cleanup } = withImage(".png");
	try {
		const textOnly = estimatePromptTokens([{ role: "user", content: "Describe this." }]);
		const one = estimatePromptTokens([imageMessage("Describe this.", path)]);
		const two = estimatePromptTokens([imageMessage("Describe this.", [path, path])]);
		// IMAGE_TOKENS_ESTIMATE — kept in step with forge_llm.IMAGE_TOKENS_ESTIMATE.
		assert.equal(one - textOnly, 1600);
		assert.equal(two - textOnly, 3200);
	} finally {
		cleanup();
	}
});

// --- dual-GPU lanes: chat2/think2 resolution, verify chain, bulk lanes, fan-out ---

const NONEXISTENT_ENV = { PI_FORGE_AGENT_DIR: "/nonexistent-agent-directory" };
const DUAL_SETTINGS = {
	connectedServices: {
		chat2: {
			enabled: true,
			baseUrl: "http://llms:8104/v1/chat/completions",
			model: "chat",
			images: false,
			chatTemplateKwargs: { enable_thinking: false },
		},
		think2: { enabled: true, baseUrl: "http://llms:8108/v1/chat/completions", model: "code", images: false },
		bulk: { lanes: ["chat", "chat2"] },
		verify: { service: "think2" },
	},
};

test("resolveService reads chat2/think2 with scheduling off and images false", () => {
	const chat2 = resolveService("chat2", { env: NONEXISTENT_ENV, settings: DUAL_SETTINGS });
	assert.equal(chat2.url, "http://llms:8104/v1/chat/completions");
	assert.equal(chat2.images, false);
	assert.equal(chat2.scheduling.enabled, false);
	const think2 = resolveService("think2", { env: NONEXISTENT_ENV, settings: DUAL_SETTINGS });
	assert.equal(think2.url, "http://llms:8108/v1/chat/completions");
	assert.equal(think2.model, "code");
});

test("FORGE_CHAT2_URL / FORGE_THINK2_URL enable and point the GPU-2 lanes", () => {
	const env = {
		...NONEXISTENT_ENV,
		FORGE_CHAT2_URL: "http://gpu2:9104/v1",
		FORGE_THINK2_URL: "http://gpu2:9108/v1",
	};
	assert.equal(resolveService("chat2", { env }).enabled, true);
	assert.equal(resolveService("chat2", { env }).url, "http://gpu2:9104/v1/chat/completions");
	assert.equal(resolveService("think2", { env }).url, "http://gpu2:9108/v1/chat/completions");
});

test("resolveVerifyService prefers the verify lane, else degrades to think", () => {
	const verify = resolveVerifyService({ env: NONEXISTENT_ENV, settings: DUAL_SETTINGS });
	assert.equal(verify.url, "http://llms:8108/v1/chat/completions");
	assert.equal(verify.fallback, undefined);
	// No secondary configured: falls back to the primary thinking lane, no fallback flag.
	const fallback = resolveVerifyService({ env: NONEXISTENT_ENV });
	assert.equal(fallback.url, resolveThinkService({ env: NONEXISTENT_ENV }).url);
	// An explicit endpoint overrides the configured verify lane.
	const pinned = resolveVerifyService({
		env: NONEXISTENT_ENV,
		settings: DUAL_SETTINGS,
		thinkUrl: "http://pinned:1/v1",
	});
	assert.equal(pinned.url, "http://pinned:1/v1/chat/completions");
});

test("resolveBulkLaneServices spans both lanes, and drops text-only lanes for images", () => {
	const lanes = resolveBulkLaneServices({ env: NONEXISTENT_ENV, settings: DUAL_SETTINGS });
	assert.deepEqual(
		lanes.map((lane) => lane.url),
		["http://llms:8004/v1/chat/completions", "http://llms:8104/v1/chat/completions"],
	);
	const visionLanes = resolveBulkLaneServices(
		{ env: NONEXISTENT_ENV, settings: DUAL_SETTINGS },
		{ carriesImage: true },
	);
	assert.deepEqual(
		visionLanes.map((lane) => lane.url),
		["http://llms:8004/v1/chat/completions"],
	);
	// Default (no bulk config): the single primary chat lane.
	const single = resolveBulkLaneServices({ env: NONEXISTENT_ENV });
	assert.deepEqual(
		single.map((lane) => lane.name),
		["chat"],
	);
});

const LANE_A = { name: "chat", url: "http://gpu1/v1/chat/completions", model: "chat", images: true };
const LANE_B = { name: "chat2", url: "http://gpu2/v1/chat/completions", model: "chat", images: false };

test("dispatchBulk fans items across both lanes and journals producedBy", async () => {
	const items = Array.from({ length: 10 }, (_value, index) => ({ id: `d${index}` }));
	const laneCounts = {};
	const { results, producedBy } = await dispatchBulk(
		[LANE_A, LANE_B],
		items,
		async (lane, item) => {
			laneCounts[lane.name] = (laneCounts[lane.name] ?? 0) + 1;
			await new Promise((resolve) => setTimeout(resolve, 3));
			return `ok:${item.id}`;
		},
		{ itemId: (item) => item.id },
	);
	assert.ok(
		results.every((result, index) => result === `ok:d${index}`),
		"every item completes in order",
	);
	assert.ok(laneCounts.chat > 0 && laneCounts.chat2 > 0, "both lanes ran work");
	assert.equal(producedBy.d0.url, "http://gpu1/v1/chat/completions");
});

test("dispatchBulk keeps image items on a vision lane", async () => {
	const items = [
		{ id: "a", img: false },
		{ id: "b", img: true },
		{ id: "c", img: false },
	];
	const ran = {};
	await dispatchBulk(
		[LANE_A, LANE_B],
		items,
		async (lane, item) => {
			ran[item.id] = lane.name;
			await new Promise((resolve) => setTimeout(resolve, 2));
			return 1;
		},
		{ carriesImage: (item) => item.img },
	);
	assert.equal(ran.b, "chat", "the image item ran on the vision lane, never chat2");
});

test("dispatchBulk drains the rest on the surviving lane when one lane fails", async () => {
	const items = Array.from({ length: 6 }, (_value, index) => ({ id: index }));
	const { results } = await dispatchBulk(
		[LANE_A, LANE_B],
		items,
		async (lane) => {
			if (lane.name === "chat2") throw new ChatError("gpu2 down");
			await new Promise((resolve) => setTimeout(resolve, 1));
			return "done";
		},
		{},
	);
	assert.ok(
		results.every((result) => result === "done"),
		"all items complete despite one dead lane",
	);
});

test("dispatchBulk rejects when an image item has no vision lane", async () => {
	await assert.rejects(
		() => dispatchBulk([LANE_B], [{ id: "x", img: true }], async () => 1, { carriesImage: () => true }),
		/no configured bulk lane accepts images/,
	);
});

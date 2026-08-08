import assert from "node:assert/strict";
import { createServer } from "node:http";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const libraryRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const {
	assertFetchableUrl,
	HostLimiter,
	httpJson,
	httpRequest,
	httpText,
	parseRetryAfterMs,
	readCappedBody,
	redactSecrets,
	WEB_RESEARCH_HOST_RULES,
} = await import(join(libraryRoot, "http-fetch.mjs"));
const { isTransientFailure } = await import(join(libraryRoot, "run-state.mjs"));

/** A server whose handler is swapped per test. `requests` records every hit. */
function startServer(handler) {
	const requests = [];
	const server = createServer((request, response) => {
		requests.push({ url: request.url, method: request.method, headers: request.headers });
		handler(request, response, requests.length);
	});
	return new Promise((resolve) => {
		server.listen(0, "127.0.0.1", () => {
			resolve({
				origin: `http://127.0.0.1:${server.address().port}`,
				requests,
				close: () => new Promise((done) => server.close(done)),
			});
		});
	});
}

function json(response, status, payload, headers = {}) {
	response.writeHead(status, { "content-type": "application/json", ...headers });
	response.end(JSON.stringify(payload));
}

// Fixtures live on 127.0.0.1, which is exactly what the guard refuses, so every
// request test opts out explicitly rather than weakening the default.
const allowLoopback = { allow: true };

test("assertFetchableUrl refuses non-http schemes and unparseable URLs", () => {
	assert.throws(() => assertFetchableUrl("ftp://example.com/x"), /only http\/https/);
	assert.throws(() => assertFetchableUrl("not a url"), /invalid URL/);
	assert.equal(assertFetchableUrl("https://example.com/x").hostname, "example.com");
});

test("assertFetchableUrl blocks loopback, metadata and private ranges by default", () => {
	for (const host of [
		"localhost",
		"127.0.0.1",
		"169.254.169.254",
		"metadata.google.internal",
		"10.0.0.5",
		"192.168.1.9",
		"172.20.0.1",
	]) {
		assert.throws(() => assertFetchableUrl(`http://${host}/x`), /refused loopback, private, or metadata host/, host);
	}
});

test("web-research rules keep RFC1918 reachable and preserve the asserted message", () => {
	// Three tests in forge-skills.test.mjs match this exact string.
	assert.throws(
		() => assertFetchableUrl("http://127.0.0.1/x", WEB_RESEARCH_HOST_RULES),
		/refused loopback or metadata host/,
	);
	// The LLM stack and SearXNG live on the LAN; this policy must not refuse them.
	assert.equal(assertFetchableUrl("http://10.0.0.5/x", WEB_RESEARCH_HOST_RULES).hostname, "10.0.0.5");
	assert.equal(assertFetchableUrl("http://llms/searxng", WEB_RESEARCH_HOST_RULES).hostname, "llms");
});

test("both escape hatches open the guard, and only when set exactly", () => {
	const rules = { ...WEB_RESEARCH_HOST_RULES, env: { FORGE_WEB_RESEARCH_ALLOW_UNSAFE: "1" } };
	assert.equal(assertFetchableUrl("http://127.0.0.1/x", rules).hostname, "127.0.0.1");
	const off = { ...WEB_RESEARCH_HOST_RULES, env: { FORGE_WEB_RESEARCH_ALLOW_UNSAFE: "true" } };
	assert.throws(() => assertFetchableUrl("http://127.0.0.1/x", off), /refused/);
	assert.equal(assertFetchableUrl("http://127.0.0.1/x", { allow: true }).hostname, "127.0.0.1");
});

test("guard errors carry codes so tool_contract reports them structurally", () => {
	const codes = [];
	for (const url of ["nope", "ftp://x.example/y", "http://127.0.0.1/z"]) {
		try {
			assertFetchableUrl(url);
		} catch (error) {
			codes.push(error.code);
		}
	}
	assert.deepEqual(codes, ["invalid_url", "unsupported_scheme", "refused_host"]);
});

test("redactSecrets removes credentials but keeps the polite-pool contact", () => {
	assert.equal(redactSecrets("https://x.example/a?api_key=abc&q=cat"), "https://x.example/a?api_key=REDACTED&q=cat");
	assert.equal(redactSecrets("https://x.example/a?api-key=abc"), "https://x.example/a?api-key=REDACTED");
	assert.equal(redactSecrets("https://x.example/a?appid=abc"), "https://x.example/a?appid=REDACTED");
	// A contact email is an identifier several academic APIs require, not a secret.
	assert.equal(redactSecrets("https://x.example/a?mailto=e@x.com"), "https://x.example/a?mailto=e@x.com");
	assert.equal(redactSecrets("not a url"), "not a url");
});

test("parseRetryAfterMs accepts seconds and HTTP-dates, and caps the wait", () => {
	assert.equal(parseRetryAfterMs("2"), 2000);
	assert.equal(parseRetryAfterMs("0"), 0);
	assert.equal(parseRetryAfterMs(null), 0);
	assert.equal(parseRetryAfterMs("garbage"), 0);
	// A day-long Retry-After is honored as a signal but not as a sleep.
	assert.equal(parseRetryAfterMs("86400"), 300_000);
	const now = Date.UTC(2026, 0, 1, 0, 0, 0);
	assert.equal(parseRetryAfterMs(new Date(now + 3000).toUTCString(), now), 3000);
});

test("retries a 503 and returns the eventual success", async () => {
	const server = await startServer((_request, response, count) => {
		if (count < 3) return json(response, 503, { error: "busy" });
		json(response, 200, { ok: true });
	});
	try {
		const { json: payload } = await httpJson(`${server.origin}/x`, { hostRules: allowLoopback, backoffMs: 1 });
		assert.deepEqual(payload, { ok: true });
		assert.equal(server.requests.length, 3);
	} finally {
		await server.close();
	}
});

test("gives up after the attempt budget and reports the status transiently", async () => {
	const server = await startServer((_request, response) => json(response, 503, { error: "busy" }));
	try {
		await assert.rejects(
			httpJson(`${server.origin}/x`, { hostRules: allowLoopback, attempts: 2, backoffMs: 1 }),
			(error) => {
				assert.match(error.message, /HTTP 503/);
				assert.equal(error.transient, true);
				assert.equal(isTransientFailure(error), true);
				return true;
			},
		);
		assert.equal(server.requests.length, 2);
	} finally {
		await server.close();
	}
});

test("does not retry a 404 -- one provider's miss is an answer", async () => {
	const server = await startServer((_request, response) => json(response, 404, { error: "no" }));
	try {
		const response = await httpRequest(`${server.origin}/x`, { hostRules: allowLoopback, backoffMs: 1 });
		assert.equal(response.status, 404);
		assert.equal(server.requests.length, 1);
	} finally {
		await server.close();
	}
});

test("honors Retry-After on 429 instead of the computed backoff", async () => {
	const server = await startServer((_request, response, count) => {
		if (count === 1) return json(response, 429, { error: "slow down" }, { "retry-after": "1" });
		json(response, 200, { ok: true });
	});
	try {
		const startedAt = Date.now();
		const { json: payload } = await httpJson(`${server.origin}/x`, { hostRules: allowLoopback, backoffMs: 1 });
		assert.deepEqual(payload, { ok: true });
		// The computed backoff here is at most 1ms, so anything near a second is
		// proof the header was honored rather than the jittered default.
		assert.ok(Date.now() - startedAt >= 900, `waited ${Date.now() - startedAt}ms`);
	} finally {
		await server.close();
	}
});

test("401 is a decision about us, not a transient -- returned without retry", async () => {
	const server = await startServer((_request, response) => json(response, 401, { error: "nope" }));
	try {
		const response = await httpRequest(`${server.origin}/x`, { hostRules: allowLoopback, backoffMs: 1 });
		assert.equal(response.status, 401);
		assert.equal(server.requests.length, 1);
	} finally {
		await server.close();
	}
});

test("the circuit breaker trips after repeated refusals and stays tripped", async () => {
	const server = await startServer((_request, response) => json(response, 403, { error: "no" }));
	const limiter = new HostLimiter({ breakerThreshold: 3 });
	try {
		for (let attempt = 0; attempt < 3; attempt += 1) {
			const response = await httpRequest(`${server.origin}/x`, { hostRules: allowLoopback, limiter, backoffMs: 1 });
			assert.equal(response.status, 403);
		}
		assert.equal(limiter.isTripped("127.0.0.1"), true);
		await assert.rejects(
			httpRequest(`${server.origin}/x`, { hostRules: allowLoopback, limiter, backoffMs: 1 }),
			/tripped the circuit breaker/,
		);
		// The refused request never left the process.
		assert.equal(server.requests.length, 3);
	} finally {
		await server.close();
	}
});

test("a 429 defers the host but never trips the breaker", async () => {
	// GDELT answers 429 to anyone exceeding one request every five seconds and
	// says so in the body. Counting that as a refusal disabled the provider for
	// the rest of the run, when all it asked for was a pause.
	const server = await startServer((_request, response) => json(response, 429, { error: "slow down" }));
	const limiter = new HostLimiter({ breakerThreshold: 3 });
	try {
		for (let attempt = 0; attempt < 3; attempt += 1) {
			const response = await httpRequest(`${server.origin}/x`, {
				hostRules: allowLoopback,
				limiter,
				attempts: 1,
				backoffMs: 1,
			});
			assert.equal(response.status, 429);
		}
		assert.equal(limiter.isTripped("127.0.0.1"), false, "a rate limit must not disable the provider");
		const after = await httpRequest(`${server.origin}/x`, {
			hostRules: allowLoopback,
			limiter,
			attempts: 1,
			backoffMs: 1,
		});
		assert.equal(after.status, 429);
	} finally {
		await server.close();
	}
});

test("a success resets the refusal count so one blip cannot trip a host", async () => {
	const server = await startServer((_request, response, count) => {
		if (count === 3) return json(response, 200, { ok: true });
		json(response, 403, { error: "no" });
	});
	const limiter = new HostLimiter({ breakerThreshold: 3 });
	try {
		await httpRequest(`${server.origin}/a`, { hostRules: allowLoopback, limiter, backoffMs: 1 });
		await httpRequest(`${server.origin}/b`, { hostRules: allowLoopback, limiter, backoffMs: 1 });
		await httpRequest(`${server.origin}/c`, { hostRules: allowLoopback, limiter, backoffMs: 1 });
		assert.equal(limiter.isTripped("127.0.0.1"), false);
		await httpRequest(`${server.origin}/d`, { hostRules: allowLoopback, limiter, backoffMs: 1 });
		assert.equal(limiter.isTripped("127.0.0.1"), false);
	} finally {
		await server.close();
	}
});

test("the limiter spaces requests to one host", async () => {
	const server = await startServer((_request, response) => json(response, 200, { ok: true }));
	const limiter = new HostLimiter({ spacingMs: 120 });
	try {
		const startedAt = Date.now();
		await httpRequest(`${server.origin}/a`, { hostRules: allowLoopback, limiter });
		await httpRequest(`${server.origin}/b`, { hostRules: allowLoopback, limiter });
		await httpRequest(`${server.origin}/c`, { hostRules: allowLoopback, limiter });
		assert.ok(Date.now() - startedAt >= 240, `spacing collapsed: ${Date.now() - startedAt}ms`);
		assert.ok(limiter.totalWaitMs >= 200);
	} finally {
		await server.close();
	}
});

test("a timeout is marked transient despite the message saying 'timed out'", async () => {
	const server = await startServer((_request, response) => {
		// Never responds; the request must be aborted by the timeout.
		setTimeout(() => json(response, 200, { ok: true }), 5000).unref();
	});
	try {
		await assert.rejects(
			httpRequest(`${server.origin}/slow`, { hostRules: allowLoopback, timeoutMs: 60, attempts: 1 }),
			(error) => {
				assert.match(error.message, /timed out after 60ms/);
				assert.equal(error.code, "ETIMEDOUT");
				// The regression this flag exists for: the string matcher looks for
				// "timeout" and would miss this message on its own.
				assert.equal(error.message.toLowerCase().includes("timeout"), false);
				assert.equal(isTransientFailure(error), true);
				return true;
			},
		);
	} finally {
		await server.close();
	}
});

test("a caller abort is not treated as a transient failure", async () => {
	const server = await startServer((_request, response) => {
		setTimeout(() => json(response, 200, { ok: true }), 5000).unref();
	});
	const controller = new AbortController();
	setTimeout(() => controller.abort(), 40).unref();
	try {
		await assert.rejects(
			httpRequest(`${server.origin}/slow`, { hostRules: allowLoopback, signal: controller.signal, attempts: 3 }),
			(error) => {
				assert.equal(error.code, "aborted");
				assert.notEqual(error.transient, true);
				return true;
			},
		);
		assert.equal(server.requests.length, 1);
	} finally {
		await server.close();
	}
});

test("readCappedBody refuses past the cap rather than truncating silently", async () => {
	const server = await startServer((_request, response) => {
		response.writeHead(200, { "content-type": "application/octet-stream" });
		response.end(Buffer.alloc(4096, 0x41));
	});
	try {
		const under = await readCappedBody(await httpRequest(`${server.origin}/big`, { hostRules: allowLoopback }), 8192);
		assert.equal(under.truncated, false);
		assert.equal(under.buffer.length, 4096);
		const over = await readCappedBody(await httpRequest(`${server.origin}/big`, { hostRules: allowLoopback }), 100);
		assert.equal(over.truncated, true);
	} finally {
		await server.close();
	}
});

test("httpText surfaces the body of a non-ok response for diagnosis", async () => {
	const server = await startServer((_request, response) => {
		response.writeHead(400, { "content-type": "text/plain" });
		response.end("query syntax error near 'AND'");
	});
	try {
		await assert.rejects(httpText(`${server.origin}/x`, { hostRules: allowLoopback, attempts: 1 }), (error) => {
			assert.equal(error.status, 400);
			assert.match(error.body, /query syntax error/);
			// A 400 is our fault, not the network's.
			assert.equal(error.transient, false);
			return true;
		});
	} finally {
		await server.close();
	}
});

test("httpJson reports unparseable JSON as its own failure mode", async () => {
	const server = await startServer((_request, response) => {
		response.writeHead(200, { "content-type": "application/json" });
		response.end("<html>we are down for maintenance</html>");
	});
	try {
		await assert.rejects(httpJson(`${server.origin}/x`, { hostRules: allowLoopback }), (error) => {
			assert.equal(error.code, "invalid_json");
			return true;
		});
	} finally {
		await server.close();
	}
});

test("the guard runs before the socket does", async () => {
	// No server: if the guard did not fire first this would be a connection error.
	await assert.rejects(httpRequest("http://169.254.169.254/latest/meta-data/"), (error) => {
		assert.equal(error.code, "refused_host");
		return true;
	});
});

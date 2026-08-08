import assert from "node:assert/strict";
import { createServer } from "node:http";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const libraryRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const { pingSearxng, searchSearxng, searxngSearchParams } = await import(join(libraryRoot, "searxng.mjs"));
const { isTransientFailure } = await import(join(libraryRoot, "run-state.mjs"));

function startSearxng(handler) {
	const requests = [];
	const server = createServer((request, response) => {
		requests.push(request.url);
		handler(request, response, requests.length);
	});
	return new Promise((resolve) => {
		server.listen(0, "127.0.0.1", () => {
			resolve({
				base: `http://127.0.0.1:${server.address().port}`,
				requests,
				close: () => new Promise((done) => server.close(done)),
			});
		});
	});
}

function json(response, payload, status = 200) {
	response.writeHead(status, { "content-type": "application/json" });
	response.end(JSON.stringify(payload));
}

test("search params carry only what was asked for", () => {
	assert.equal(searxngSearchParams("cats").toString(), "q=cats&format=json");
	const full = searxngSearchParams("cats", {
		categories: "general",
		engines: "duckduckgo",
		language: "en",
		safesearch: 0,
		timeRange: "week",
		pageNo: 2,
	});
	assert.equal(full.get("categories"), "general");
	assert.equal(full.get("time_range"), "week");
	assert.equal(full.get("pageno"), "2");
	// safesearch: 0 is a real setting and must survive a falsy check.
	assert.equal(full.get("safesearch"), "0");
});

test("a normal result set comes back untouched", async () => {
	const server = await startSearxng((_request, response) => {
		json(response, { results: [{ title: "Alpha", url: "https://a.example/1" }], unresponsive_engines: [] });
	});
	try {
		const payload = await searchSearxng(server.base, "alpha");
		assert.equal(payload.results.length, 1);
	} finally {
		await server.close();
	}
});

test("an honest empty result set stays empty rather than raising", async () => {
	const server = await startSearxng((_request, response) => {
		json(response, { results: [], unresponsive_engines: [] });
	});
	try {
		// Every engine answered and none of them had anything. That is an answer.
		const payload = await searchSearxng(server.base, "no such thing anywhere");
		assert.deepEqual(payload.results, []);
	} finally {
		await server.close();
	}
});

test("a throttled instance raises transiently instead of reporting no sources", async () => {
	const server = await startSearxng((_request, response) => {
		// What a rate-limited SearXNG actually sends: HTTP 200, no results, and
		// the engines listed as unresponsive.
		json(response, {
			results: [],
			unresponsive_engines: [
				["google", "timeout"],
				["bing", "CAPTCHA"],
			],
		});
	});
	try {
		await assert.rejects(searchSearxng(server.base, "alpha"), (error) => {
			assert.equal(error.code, "searxng_throttled");
			assert.match(error.message, /every engine was unresponsive \(google, bing\)/);
			// The whole point: this must reach the retry ladder, not be recorded
			// as "this subject has no source".
			assert.equal(error.transient, true);
			assert.equal(isTransientFailure(error), true);
			return true;
		});
	} finally {
		await server.close();
	}
});

test("results alongside unresponsive engines are still results", async () => {
	const server = await startSearxng((_request, response) => {
		json(response, {
			results: [{ title: "Alpha", url: "https://a.example/1" }],
			unresponsive_engines: [["bing", "timeout"]],
		});
	});
	try {
		// A partial outage is not a throttle. One engine failing while others
		// answered is the normal state of a metasearch instance.
		const payload = await searchSearxng(server.base, "alpha");
		assert.equal(payload.results.length, 1);
	} finally {
		await server.close();
	}
});

test("a 5xx is retried and the failure is transient", async () => {
	const server = await startSearxng((_request, response, count) => {
		if (count < 2) return json(response, { error: "busy" }, 503);
		json(response, { results: [{ title: "Alpha", url: "https://a.example/1" }], unresponsive_engines: [] });
	});
	try {
		const payload = await searchSearxng(server.base, "alpha", { attempts: 3 });
		assert.equal(payload.results.length, 1);
		assert.equal(server.requests.length, 2);
	} finally {
		await server.close();
	}
});

test("an unreachable instance reports the original wording", async () => {
	const server = await startSearxng((_request, response) => json(response, { error: "no" }, 500));
	const base = server.base;
	await server.close();
	await assert.rejects(searchSearxng(base, "alpha", { attempts: 1 }), (error) => {
		assert.match(error.message, /^SearXNG request failed: /);
		assert.equal(error.transient, true);
		return true;
	});
});

test("doctor reports reachability without throwing", async () => {
	const server = await startSearxng((_request, response) => json(response, { results: [] }));
	try {
		assert.deepEqual(await pingSearxng("", "agent", 1000), {
			configured: false,
			reachable: false,
			detail: "no SearXNG URL configured",
		});
		const reachable = await pingSearxng(server.base, "agent", 5000);
		assert.equal(reachable.configured, true);
		assert.equal(reachable.reachable, true);
	} finally {
		await server.close();
	}
});

test("doctor reports an unreachable instance as configured but down", async () => {
	const server = await startSearxng((_request, response) => json(response, {}));
	const base = server.base;
	await server.close();
	const result = await pingSearxng(base, "agent", 2000);
	assert.equal(result.configured, true);
	assert.equal(result.reachable, false);
	assert.match(result.detail, /unreachable/);
});

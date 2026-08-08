import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const repositoryRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const { DECLARED_BUDGETS, loadBudget, providerBudgetState, recordProviderSpend, reportedRateLimit } = await import(
	join(repositoryRoot, "forge", "skills", "web-research", "scripts", "provider-budget.mjs")
);

const NOW = new Date("2026-07-31T12:00:00Z");

function withLedger(body) {
	const directory = mkdtempSync(join(tmpdir(), "pi-forge-budget-"));
	try {
		return body(join(directory, "budget.json"));
	} finally {
		rmSync(directory, { recursive: true, force: true });
	}
}

test("a provider with no declared budget costs nothing to record and is never exhausted", () => {
	withLedger((path) => {
		// The common case. Almost every provider publishes a rate, which spacing
		// handles, and must not pay for a ledger it does not use.
		assert.equal(recordProviderSpend("wikipedia", { options: { path, now: NOW } }), null);
		assert.deepEqual(loadBudget({ path }).days, {});
		assert.equal(providerBudgetState("wikipedia", { path, now: NOW }).exhausted, false);
	});
});

test("a counted budget is exhausted on the call that reaches the declared limit", () => {
	withLedger((path) => {
		for (let call = 1; call < DECLARED_BUDGETS.nyt.calls; call += 1)
			recordProviderSpend("nyt", { options: { path, now: NOW } });
		assert.equal(providerBudgetState("nyt", { path, now: NOW }).exhausted, false, "499 of 500 is not exhausted");

		recordProviderSpend("nyt", { options: { path, now: NOW } });
		const state = providerBudgetState("nyt", { path, now: NOW });
		assert.equal(state.exhausted, true);
		assert.match(state.reason, /500\/day budget \(500 calls\)/);

		// Tomorrow is a different bucket.
		assert.equal(providerBudgetState("nyt", { path, now: new Date("2026-08-01T00:30:00Z") }).exhausted, false);
	});
});

test("a reported balance outranks the local count", () => {
	withLedger((path) => {
		// OpenAlex answers every request with what is left. A ledger that
		// disagrees with the service is worse than no ledger, so the count is not
		// consulted once the service has spoken.
		const plenty = new Headers({
			"x-ratelimit-limit-usd": "0.1",
			"x-ratelimit-remaining-usd": "0.05",
			"x-ratelimit-cost-usd": "0.001",
		});
		for (let call = 0; call < 1000; call += 1)
			recordProviderSpend("openalex", { headers: plenty, options: { path, now: NOW } });
		const healthy = providerBudgetState("openalex", { path, now: NOW });
		assert.equal(healthy.exhausted, false, "a thousand calls do not matter if the service says there is money left");
		assert.equal(healthy.entry.calls, 1000);

		const spent = new Headers({
			"x-ratelimit-limit-usd": "0.1",
			"x-ratelimit-remaining-usd": "0.0005",
			"x-ratelimit-cost-usd": "0.001",
		});
		recordProviderSpend("openalex", { headers: spent, options: { path, now: NOW } });
		const state = providerBudgetState("openalex", { path, now: NOW });
		// Exhausted at $0.0005 left because the next search costs $0.001.
		assert.equal(state.exhausted, true);
		assert.match(state.reason, /\$0\.0005 of its daily allowance left/);
	});
});

test("a monthly budget sums the trailing month rather than the day", () => {
	withLedger((path) => {
		for (let day = 1; day <= 20; day += 1) {
			const when = new Date(`2026-07-${String(day).padStart(2, "0")}T09:00:00Z`);
			for (let call = 0; call < 100; call += 1) recordProviderSpend("wolfram", { options: { path, now: when } });
		}
		// 2,000 calls across twenty days is the whole monthly allowance, even
		// though no single day came close to it.
		const state = providerBudgetState("wolfram", { path, now: new Date("2026-07-20T10:00:00Z") });
		assert.equal(state.exhausted, true);
		assert.match(state.reason, /2000\/month/);
	});
});

test("a rate-limit header set is read only when it says something", () => {
	assert.equal(reportedRateLimit(new Headers({})), null);
	assert.equal(reportedRateLimit(new Headers({ "content-type": "application/json" })), null);
	// A header present but unparseable is the same as absent: CORE reports its
	// reset as an absolute timestamp, which is not a number of seconds.
	const core = reportedRateLimit(
		new Headers({ "x-ratelimit-limit": "10", "x-ratelimit-retry-after": "2026-07-31T19:34:03+0000" }),
	);
	assert.equal(core.limit, 10);
	assert.equal(core.remainingUsd, null);
});

test("a corrupt or foreign ledger starts fresh instead of failing a run", () => {
	withLedger((path) => {
		writeFileSync(path, "{not json");
		assert.deepEqual(loadBudget({ path }).days, {});
		writeFileSync(path, JSON.stringify({ schemaVersion: 99, days: { "2026-07-31": { nyt: { calls: 500 } } } }));
		// A ledger from a future schema is discarded rather than misread, so the
		// worst case is counting today from zero -- never refusing every provider.
		assert.deepEqual(loadBudget({ path }).days, {});
		assert.equal(providerBudgetState("nyt", { path, now: NOW }).exhausted, false);
	});
});

test("old days are pruned so the file cannot grow without bound", () => {
	withLedger((path) => {
		recordProviderSpend("nyt", { options: { path, now: new Date("2026-01-01T00:00:00Z") } });
		recordProviderSpend("nyt", { options: { path, now: NOW } });
		const days = Object.keys(loadBudget({ path }).days);
		assert.deepEqual(days, ["2026-07-31"]);
	});
});

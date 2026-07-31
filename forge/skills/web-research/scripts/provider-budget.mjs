// The daily spend ledger.
//
// Most providers publish a *rate* -- one request every five seconds, twelve a
// second -- and a rate is enforced by spacing, which PROVIDER_SPACING_MS and the
// HostLimiter in lib/http-fetch.mjs already do. A handful publish a *budget*
// instead: OpenAlex allows $0.10 of queries a day anonymously and $1.00 with a
// key, NYT 500 requests, Wolfram 2,000 a month. Spacing cannot express that, and
// running into it mid-run is how a research run silently loses a provider.
//
// Two kinds of entry, because two kinds of truth exist:
//
//   - Reported. OpenAlex returns the remaining balance in every response
//     (x-ratelimit-remaining-usd). Store what the service said; no local count
//     can be more accurate, and a ledger that disagrees with the service is
//     worse than no ledger.
//   - Counted. Guardian, NYT and Wolfram publish a number and report nothing
//     back, so the calls are counted here and checked against the declaration.
//
// The distinction matters for a third case that looks like the second and is
// not: CORE reports `x-ratelimit-remaining` against a short rolling *window*,
// not a day. Treating a zero there as "exhausted until midnight" would disable a
// provider that recovers in seconds, so only a provider whose declared budget
// names a `reportedBy` header is read that way. Everything else is a 429, which
// the transport already defers and retries.

import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { atomicWriteJson } from "../../../lib/run-state.mjs";
import { defaultCacheDirectory } from "./acquisition.mjs";

export const BUDGET_SCHEMA_VERSION = 1;

// Long enough to sum a monthly allowance, short enough that the file stays small.
const RETAINED_DAYS = 40;

export function budgetPath(cacheDirectory = defaultCacheDirectory()) {
	return join(cacheDirectory, "budget.json");
}

function today(now = new Date()) {
	return now.toISOString().slice(0, 10);
}

function emptyLedger() {
	return { schemaVersion: BUDGET_SCHEMA_VERSION, days: {} };
}

export function loadBudget(options = {}) {
	const path = options.path ?? budgetPath(options.cacheDirectory);
	if (!existsSync(path)) return emptyLedger();
	try {
		const value = JSON.parse(readFileSync(path, "utf8"));
		if (!value || typeof value !== "object" || Array.isArray(value)) return emptyLedger();
		if (value.schemaVersion !== BUDGET_SCHEMA_VERSION) return emptyLedger();
		return { schemaVersion: BUDGET_SCHEMA_VERSION, days: value.days ?? {} };
	} catch {
		// A corrupt ledger is not worth failing a run over: the worst case of
		// starting fresh is that today's budget is counted from zero again.
		return emptyLedger();
	}
}

function prune(ledger, now) {
	const cutoff = new Date(now.getTime() - RETAINED_DAYS * 86_400_000).toISOString().slice(0, 10);
	for (const day of Object.keys(ledger.days)) {
		if (day < cutoff) delete ledger.days[day];
	}
	return ledger;
}

/**
 * The rate-limit facts a response reported, or null. Numeric fields only --
 * a header that is present but unparseable is the same as absent.
 */
export function reportedRateLimit(headers) {
	if (!headers?.get) return null;
	const number = (name) => {
		const raw = headers.get(name);
		if (raw === null || raw === undefined || String(raw).trim() === "") return null;
		const parsed = Number(raw);
		return Number.isFinite(parsed) ? parsed : null;
	};
	const reported = {
		limit: number("x-ratelimit-limit"),
		remaining: number("x-ratelimit-remaining"),
		limitUsd: number("x-ratelimit-limit-usd"),
		remainingUsd: number("x-ratelimit-remaining-usd"),
		costUsd: number("x-ratelimit-cost-usd"),
		resetSeconds: number("x-ratelimit-reset"),
	};
	return Object.values(reported).some((value) => value !== null) ? reported : null;
}

/**
 * Record one call against a provider. Cheap and silent for a provider with no
 * declared budget, which is almost all of them.
 *
 * Concurrent processes read-modify-write without a lock, so two runs racing can
 * undercount. That is acceptable precisely because the provider that costs money
 * is the one that reports its own remaining balance: the next response corrects
 * the ledger. A counted provider drifting low by a few calls is not worth a lock
 * file on every request.
 */
export function recordProviderSpend(provider, { headers = null, budget = null, options = {} } = {}) {
	const declared = budget ?? DECLARED_BUDGETS[provider];
	if (!declared) return null;
	const now = options.now ?? new Date();
	const path = options.path ?? budgetPath(options.cacheDirectory);
	const ledger = prune(loadBudget({ path }), now);
	const day = today(now);
	const entry = ledger.days[day]?.[provider] ?? { calls: 0 };
	entry.calls += 1;
	const reported = reportedRateLimit(headers);
	if (reported && declared.reportedBy) {
		if (reported.remainingUsd !== null) entry.remainingUsd = reported.remainingUsd;
		if (reported.limitUsd !== null) entry.limitUsd = reported.limitUsd;
		if (reported.costUsd !== null) entry.spentUsd = Number(((entry.spentUsd ?? 0) + reported.costUsd).toFixed(6));
		entry.reportedAt = now.toISOString();
	}
	ledger.days[day] = { ...(ledger.days[day] ?? {}), [provider]: entry };
	atomicWriteJson(path, ledger);
	return entry;
}

/**
 * Whether a provider has budget left. Returns `exhausted: false` for anything
 * with no declared budget, which is the common case and must stay free.
 */
export function providerBudgetState(provider, options = {}) {
	const declared = options.budget ?? DECLARED_BUDGETS[provider];
	if (!declared) return { exhausted: false, reason: null, entry: null };
	const now = options.now ?? new Date();
	const ledger = options.ledger ?? loadBudget({ path: options.path, cacheDirectory: options.cacheDirectory });
	const entry = ledger.days?.[today(now)]?.[provider] ?? null;

	// A reported balance is the service's own answer and outranks any local count.
	if (declared.reportedBy && entry?.remainingUsd !== undefined && entry.remainingUsd !== null) {
		const nextCall = declared.perCallUsd ?? 0;
		if (entry.remainingUsd <= nextCall) {
			return { exhausted: true, reason: `${provider} reported $${entry.remainingUsd} of its daily allowance left`, entry };
		}
		return { exhausted: false, reason: null, entry };
	}

	if (declared.calls) {
		const used = entry?.calls ?? 0;
		if (used >= declared.calls) {
			return { exhausted: true, reason: `${provider} has used its ${declared.calls}/day budget (${used} calls)`, entry };
		}
	}
	if (declared.monthlyCalls) {
		const used = callsInTrailingMonth(ledger, provider, now);
		if (used >= declared.monthlyCalls) {
			return { exhausted: true, reason: `${provider} has used its ${declared.monthlyCalls}/month budget (${used} calls)`, entry };
		}
	}
	return { exhausted: false, reason: null, entry };
}

function callsInTrailingMonth(ledger, provider, now) {
	const cutoff = new Date(now.getTime() - 30 * 86_400_000).toISOString().slice(0, 10);
	let total = 0;
	for (const [day, providers] of Object.entries(ledger.days ?? {})) {
		if (day < cutoff) continue;
		total += providers?.[provider]?.calls ?? 0;
	}
	return total;
}

/**
 * The published allowances, keyed by provider id. Checked against the live
 * services on 2026-07-31; a provider absent from here has no daily budget to
 * blow, only a rate, and the limiter handles that.
 */
export const DECLARED_BUDGETS = {
	// $0.10/day anonymous, $1.00/day with a free key. Every response says how
	// much is left, so the declaration below is only the shape -- what gets
	// enforced is what OpenAlex reported. perCallUsd is the cost of a keyword
	// search, the most expensive call this repo makes.
	openalex: { reportedBy: "x-ratelimit-remaining-usd", usd: 0.1, keyedUsd: 1, perCallUsd: 0.001 },
	nyt: { calls: 500 },
	guardian: { calls: 5000 },
	wolfram: { monthlyCalls: 2000 },
};

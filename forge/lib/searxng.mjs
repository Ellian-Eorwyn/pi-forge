// The SearXNG client, shared by web-research and web-collection.
//
// It lives in lib/ rather than in either skill because both skills search, and
// the client existed twice -- byte-identical request construction, byte-identical
// error handling, and the same bug in both copies.
//
// That bug is the reason this file is worth reading. SearXNG is a metasearch
// scraper, so the rate limits it hits belong to Google and Bing rather than to
// the instance, and it reports them by answering HTTP 200 with an empty result
// list. Both copies treated that as "nothing matched". A throttled search was
// therefore indistinguishable from an honest miss, and a research run would
// quietly return a thinner answer rather than failing. vault-wiki's
// references/canonical-sources.json has documented the symptom since it was
// written; searchSearxng below is the fix.

import { CONFIGURED_ENDPOINT_RULES, HostLimiter, httpJson, httpText } from "./http-fetch.mjs";

export const DEFAULT_SEARCH_TIMEOUT_MS = 20_000;

// Spacing is deliberately modest rather than the 2000ms vault-wiki calibrated
// for its own calls. That delay was tuned against a failure it could not see: a
// throttled instance answered HTTP 200 with zero results, so the only available
// defence was to never provoke it. Now that a throttled response raises
// transiently and backs off, pacing every request pre-emptively buys little and
// costs latency on the majority of calls that were never going to be throttled.
// Enough to avoid bursting; the retry ladder handles the rest.
export const DEFAULT_SPACING_MS = 250;

/**
 * One limiter per process, shared by every search caller so two skills running
 * in one process cannot each spend the full budget against the same instance.
 */
export const searchLimiter = new HostLimiter({ spacingMs: DEFAULT_SPACING_MS });

// The instance is operator-configured and normally on the LAN; see
// CONFIGURED_ENDPOINT_RULES in ./http-fetch.mjs for why that exempts it from the
// SSRF guard while the URLs it returns stay subject to it.
const CONFIGURED_ENDPOINT = CONFIGURED_ENDPOINT_RULES;

export function searxngSearchParams(query, params = {}) {
	const search = new URLSearchParams({ q: query, format: "json" });
	if (params.categories) search.set("categories", params.categories);
	if (params.engines) search.set("engines", params.engines);
	if (params.language) search.set("language", params.language);
	if (params.safesearch !== undefined && params.safesearch !== null)
		search.set("safesearch", String(params.safesearch));
	if (params.timeRange) search.set("time_range", params.timeRange);
	if (params.pageNo) search.set("pageno", String(params.pageNo));
	return search;
}

function transient(message, detail = {}) {
	const error = new Error(message);
	error.transient = true;
	Object.assign(error, detail);
	return error;
}

/**
 * Query SearXNG, raising a *transient* error when the instance reports that its
 * upstream engines did not answer.
 *
 * `unresponsive_engines` is how SearXNG distinguishes "every engine refused"
 * from "this query matched nothing", and reading the first as the second is what
 * made throttling invisible. Raising transiently hands it to the retry ladder in
 * run-state.mjs, which is why the error is marked rather than merely worded:
 * isTransientFailure's string matching would not catch this message.
 */
export async function searchSearxng(base, query, options = {}) {
	const url = `${base}/search?${searxngSearchParams(query, options).toString()}`;
	let payload;
	try {
		({ json: payload } = await httpJson(url, {
			headers: { "user-agent": options.userAgent, accept: "application/json" },
			timeoutMs: options.timeoutMs ?? DEFAULT_SEARCH_TIMEOUT_MS,
			attempts: options.attempts ?? 3,
			hostRules: CONFIGURED_ENDPOINT,
			limiter: options.limiter ?? null,
			spacingMs: options.spacingMs,
		}));
	} catch (error) {
		// Preserve the original wording: callers and one test match on it.
		throw transient(`SearXNG request failed: ${error.message}`, { cause: error });
	}
	const results = Array.isArray(payload?.results) ? payload.results : [];
	const unresponsive = Array.isArray(payload?.unresponsive_engines) ? payload.unresponsive_engines : [];
	if (results.length === 0 && unresponsive.length > 0) {
		const names = unresponsive.map((entry) => (Array.isArray(entry) ? entry[0] : String(entry))).join(", ");
		throw transient(`SearXNG returned no results because every engine was unresponsive (${names})`, {
			code: "searxng_throttled",
			unresponsiveEngines: unresponsive,
		});
	}
	return payload;
}

export async function pingSearxng(base, userAgent, timeoutMs) {
	if (!base) return { configured: false, reachable: false, detail: "no SearXNG URL configured" };
	try {
		const { response } = await httpText(`${base}/search?q=ping&format=json`, {
			headers: { "user-agent": userAgent, accept: "application/json" },
			timeoutMs,
			attempts: 1,
			hostRules: CONFIGURED_ENDPOINT,
		});
		return { configured: true, reachable: response.ok, detail: `${base} responded with HTTP ${response.status}` };
	} catch (error) {
		return { configured: true, reachable: false, detail: `${base} unreachable: ${error.message}` };
	}
}

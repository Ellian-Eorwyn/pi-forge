// The shared outbound HTTP transport. Everything forge fetches from the open
// internet -- search providers, academic providers, page acquisition, PDF
// downloads -- goes through here.
//
// It exists because the same four concerns were written three or four times
// each, slightly differently every time, and one of them was not written at all:
// before this module no HTTP call in the repo retried. `withRetry` in
// forge-llm.mjs covers LLM calls only, and `retryableItem` in run-state.mjs
// re-runs a work item across process invocations rather than re-issuing a
// request. A 503 or a dropped connection simply failed the item.
//
// The four consolidated concerns:
//   - the SSRF guard, previously in acquisition.mjs, web-research.mjs and
//     acquire_pdf.mjs, in two different strengths (see assertFetchableUrl)
//   - per-host spacing, previously in acquisition.mjs (only when a JSON registry
//     declared a rate) and acquire_pdf.mjs (always)
//   - the circuit breaker, previously only in acquire_pdf.mjs
//   - the capped body reader, previously three identical copies

import { Buffer } from "node:buffer";

export const DEFAULT_TIMEOUT_MS = 30_000;
export const DEFAULT_ATTEMPTS = 3;
export const DEFAULT_BACKOFF_MS = 500;
export const MAX_BACKOFF_MS = 10_000;

// A host that answers 429 with `Retry-After: 86400` is telling us to go away for
// a day. Honor the signal, but cap the wait: a run that sleeps for an hour has
// hung as far as anyone watching it is concerned.
export const MAX_RETRY_AFTER_MS = 300_000;

// Three consecutive refusals from one host means that host has decided about us.
// Continuing to ask is what turns a slow run into a blocked address block.
export const DEFAULT_BREAKER_THRESHOLD = 3;

const REFUSAL_STATUSES = new Set([401, 402, 403, 429]);
const RETRYABLE_STATUSES = new Set([408, 425, 429, 500, 502, 503, 504]);

/**
 * Host rules for the web-research family. Blocks loopback and cloud metadata but
 * not RFC1918, and honors FORGE_WEB_RESEARCH_ALLOW_UNSAFE so the test fixtures
 * can serve from 127.0.0.1.
 *
 * This is deliberately weaker than the default. It is preserved verbatim rather
 * than tightened because tightening it is a behavior change that belongs in its
 * own commit, with its own reasoning about which internal hosts a research run
 * is allowed to reach.
 */
export const WEB_RESEARCH_HOST_RULES = Object.freeze({
	blockPrivateRanges: false,
	allowEnv: "FORGE_WEB_RESEARCH_ALLOW_UNSAFE",
});

/**
 * Host rules for an endpoint that came from configuration -- a settings.json
 * entry, an environment variable, or a --flag -- rather than from data.
 *
 * Service endpoints are in the same class as the LLM endpoints: chosen by the
 * operator, frequently on the LAN, and not attacker-influenced. The SSRF guard
 * exists for URLs that come *out* of a search or a document, and acquisition.mjs
 * applies it when those are fetched. Keeping the two apart is load-bearing: one
 * test reaches a loopback SearXNG precisely in order to assert that the loopback
 * URLs it returns are refused.
 */
export const CONFIGURED_ENDPOINT_RULES = Object.freeze({ allow: true });

function isLoopbackOrMetadataHost(host) {
	return (
		host === "localhost" ||
		host.endsWith(".localhost") ||
		host === "::1" ||
		host === "[::1]" ||
		host === "0.0.0.0" ||
		host === "metadata" ||
		host === "metadata.google.internal" ||
		host === "169.254.169.254" ||
		host.startsWith("169.254.") ||
		/^127\./.test(host)
	);
}

function isPrivateRangeHost(host) {
	return /^10\./.test(host) || /^192\.168\./.test(host) || /^172\.(1[6-9]|2\d|3[01])\./.test(host);
}

function fetchError(code, message) {
	const error = new Error(message);
	error.code = code;
	return error;
}

/**
 * Refuse a URL that should never be fetched from data we did not author.
 *
 * Two strengths, because the two original call sites guard different things and
 * both behaviors are load-bearing:
 *
 *   - The default (`blockPrivateRanges: true`) also refuses RFC1918. This is
 *     what literature-library enforces, and its test suite asserts the guard
 *     fires when `allowPrivateHosts` is false.
 *   - `WEB_RESEARCH_HOST_RULES` refuses only loopback and metadata. Three tests
 *     in forge-skills.test.mjs assert its exact message, so both message forms
 *     below are reproduced character for character.
 *
 * The safe strength is the default: new callers get RFC1918 blocking unless they
 * name a weaker rule set explicitly.
 *
 * @param {string} url
 * @param {{blockPrivateRanges?: boolean, allow?: boolean, allowEnv?: string|null, env?: object}} [rules]
 * @returns {URL}
 */
export function assertFetchableUrl(url, rules = {}) {
	const { blockPrivateRanges = true, allow = false, allowEnv = null, env = process.env } = rules;
	let parsed;
	try {
		parsed = new URL(url);
	} catch {
		throw fetchError("invalid_url", `invalid URL: ${url}`);
	}
	if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
		throw fetchError("unsupported_scheme", `unsupported URL scheme (only http/https): ${url}`);
	}
	if (allow) return parsed;
	if (allowEnv && env[allowEnv] === "1") return parsed;
	const host = parsed.hostname.toLowerCase();
	const refused = isLoopbackOrMetadataHost(host) || (blockPrivateRanges && isPrivateRangeHost(host));
	if (refused) {
		// Both strings are asserted by existing tests. Do not reword.
		const detail = blockPrivateRanges ? "loopback, private, or metadata host" : "loopback or metadata host";
		throw fetchError("refused_host", `refused ${detail}: ${host}`);
	}
	return parsed;
}

export function hostForUrl(url) {
	try {
		return new URL(url).hostname.toLowerCase();
	} catch {
		return null;
	}
}

const SECRET_PARAMS = new Set([
	"api_key",
	"apikey",
	"api-key",
	"key",
	"token",
	"access_token",
	"auth",
	"authorization",
	"subscription_key",
	"app_key",
	"appid",
	"secret",
	"password",
]);

/**
 * Strip credential-bearing query parameters from a URL before it is written
 * anywhere durable.
 *
 * providerFetch archives the full request URL into provider_requests.jsonl, and
 * several providers take the key as a query parameter rather than a header
 * (Guardian, NYT, Wolfram, OpenAlex). Without this, enabling the keyed tier
 * would write live credentials into every run directory.
 *
 * A contact email (`mailto`, `email`) is deliberately *not* redacted: it is the
 * polite-pool identifier several academic APIs require, it is not a secret, and
 * seeing it in a log is how you confirm a request was made politely.
 */
export function redactSecrets(url) {
	try {
		const parsed = new URL(url);
		let touched = false;
		for (const name of [...parsed.searchParams.keys()]) {
			if (!SECRET_PARAMS.has(name.toLowerCase())) continue;
			parsed.searchParams.set(name, "REDACTED");
			touched = true;
		}
		return touched ? parsed.toString() : url;
	} catch {
		return url;
	}
}

/**
 * Read a response body, refusing rather than truncating past `maxBytes`.
 *
 * A truncated file that still parses is worse than no file, because nothing
 * downstream can tell it is incomplete -- so the caller is handed `truncated`
 * and is expected to discard, not salvage.
 */
export async function readCappedBody(response, maxBytes) {
	if (!response.body) return { buffer: Buffer.alloc(0), truncated: false };
	const reader = response.body.getReader();
	const chunks = [];
	let total = 0;
	for (;;) {
		const { done, value } = await reader.read();
		if (done) break;
		total += value.length;
		if (total > maxBytes) {
			await reader.cancel();
			return { buffer: Buffer.concat(chunks), truncated: true };
		}
		chunks.push(Buffer.from(value));
	}
	return { buffer: Buffer.concat(chunks), truncated: false };
}

/** `Retry-After` is either a delta in seconds or an HTTP-date. Accept both. */
export function parseRetryAfterMs(headerValue, now = Date.now()) {
	if (!headerValue) return 0;
	const seconds = Number.parseInt(String(headerValue).trim(), 10);
	if (Number.isInteger(seconds) && seconds >= 0) return Math.min(seconds * 1000, MAX_RETRY_AFTER_MS);
	const date = Date.parse(String(headerValue));
	if (Number.isNaN(date)) return 0;
	return Math.min(Math.max(0, date - now), MAX_RETRY_AFTER_MS);
}

const sleep = (milliseconds) => new Promise((done) => setTimeout(done, milliseconds));

/**
 * Per-host spacing, refusal tracking and circuit breaking.
 *
 * An instance rather than module state so a test can exercise a tripped breaker
 * without leaking that host into the next case. Callers that want one budget for
 * a whole run (acquire_pdf processes a batch in a single process, and "this host
 * refused us three times earlier in this run" is the judgement it wants) create
 * one instance at module scope and share it.
 */
export class HostLimiter {
	constructor({ spacingMs = 0, breakerThreshold = DEFAULT_BREAKER_THRESHOLD } = {}) {
		this.defaultSpacingMs = spacingMs;
		this.breakerThreshold = breakerThreshold;
		this.hosts = new Map();
		this.totalWaitMs = 0;
	}

	stateFor(host) {
		let state = this.hosts.get(host);
		if (!state) {
			state = { nextAllowedAt: 0, consecutiveRefusals: 0, tripped: false, requests: 0 };
			this.hosts.set(host, state);
		}
		return state;
	}

	isTripped(host) {
		return this.hosts.get(host)?.tripped === true;
	}

	/** Block until this host may be called again, then claim the slot. */
	async wait(host, spacingMs = this.defaultSpacingMs) {
		const state = this.stateFor(host);
		const now = Date.now();
		const waitMs = Math.max(0, state.nextAllowedAt - now);
		state.nextAllowedAt = Math.max(now, state.nextAllowedAt) + Math.max(0, spacingMs);
		state.requests += 1;
		if (waitMs > 0) {
			this.totalWaitMs += waitMs;
			await sleep(waitMs);
		}
		return waitMs;
	}

	/** Push this host's next allowed time out, e.g. because it sent Retry-After. */
	defer(host, delayMs) {
		if (!(delayMs > 0)) return;
		const state = this.stateFor(host);
		state.nextAllowedAt = Math.max(state.nextAllowedAt, Date.now() + delayMs);
	}

	noteSuccess(host) {
		this.stateFor(host).consecutiveRefusals = 0;
	}

	/** Returns true when this refusal tripped the breaker. */
	noteRefusal(host) {
		const state = this.stateFor(host);
		state.consecutiveRefusals += 1;
		if (state.consecutiveRefusals >= this.breakerThreshold) state.tripped = true;
		return state.tripped;
	}
}

export function isRetryableStatus(status) {
	return RETRYABLE_STATUSES.has(status);
}

export function isRefusalStatus(status) {
	return REFUSAL_STATUSES.has(status);
}

function backoffFor(attempt, baseMs, maxMs) {
	const exponential = Math.min(maxMs, baseMs * 2 ** (attempt - 1));
	// Full jitter. Several providers are hit by the same run in the same tick;
	// without jitter their retries stay in lockstep and re-collide.
	return Math.round(Math.random() * exponential);
}

/**
 * Fetch with a timeout, per-host spacing, bounded retries and a circuit breaker.
 *
 * Returns the `Response` untouched so the caller decides how to read it -- text
 * for a JSON provider, `readCappedBody` for a download. Non-retryable error
 * responses are returned rather than thrown: a 404 from one provider is an
 * answer, and only the caller knows whether it is fatal.
 *
 * Errors thrown from here carry `transient: true` when a retry could plausibly
 * succeed, which `isTransientFailure` in run-state.mjs checks first. That flag
 * exists because the string matching it falls back on does not catch our own
 * timeout message: the matcher looks for "timeout" and the message says "timed
 * out". Marking the error explicitly is what makes a timed-out item retryable
 * across invocations instead of permanently failed.
 */
export async function httpRequest(url, options = {}) {
	const {
		method = "GET",
		headers = {},
		body,
		redirect = "follow",
		timeoutMs = DEFAULT_TIMEOUT_MS,
		attempts = DEFAULT_ATTEMPTS,
		backoffMs = DEFAULT_BACKOFF_MS,
		maxBackoffMs = MAX_BACKOFF_MS,
		limiter = null,
		spacingMs,
		hostRules = {},
		signal = null,
		onRetry = null,
	} = options;

	const parsed = assertFetchableUrl(url, hostRules);
	const host = parsed.hostname.toLowerCase();

	if (limiter?.isTripped(host)) {
		throw fetchError("host_tripped", `host tripped the circuit breaker earlier in this run: ${host}`);
	}

	let lastError = null;
	for (let attempt = 1; attempt <= Math.max(1, attempts); attempt += 1) {
		if (limiter) await limiter.wait(host, spacingMs);

		const controller = new AbortController();
		const onAbort = () => controller.abort();
		if (signal) {
			if (signal.aborted) controller.abort();
			else signal.addEventListener("abort", onAbort, { once: true });
		}
		const timer = setTimeout(() => controller.abort(), timeoutMs);
		let response = null;
		try {
			response = await fetch(url, { method, headers, body, redirect, signal: controller.signal });
		} catch (error) {
			// A caller-supplied abort is a decision, not a transient failure.
			if (signal?.aborted) throw fetchError("aborted", "request aborted by caller");
			lastError =
				error.name === "AbortError"
					? fetchError("ETIMEDOUT", `request timed out after ${timeoutMs}ms`)
					: fetchError(error.code ?? "fetch_failed", `Fetch failed: ${error.message}`);
			lastError.transient = true;
		} finally {
			clearTimeout(timer);
			if (signal) signal.removeEventListener("abort", onAbort);
		}

		if (response) {
			if (response.status === 429) {
				// "Slow down" is not "go away". A 429 defers the host but must not
				// count toward the breaker: GDELT answers 429 to anyone exceeding
				// one request every five seconds, and tripping on that would
				// permanently disable a provider that was only asking us to wait.
				const retryAfterMs = parseRetryAfterMs(response.headers.get("retry-after"));
				const backoff = Math.max(retryAfterMs, backoffFor(attempt, backoffMs, maxBackoffMs), spacingMs ?? 0);
				limiter?.defer(host, backoff);
				if (attempt >= attempts) return response;
				lastError = fetchError("http_429", `${host} returned HTTP 429`);
				lastError.transient = true;
				lastError.retryAfterMs = backoff;
			} else if (isRefusalStatus(response.status)) {
				// 401/402/403 are decisions about us. Repeated, they trip the breaker.
				limiter?.defer(host, parseRetryAfterMs(response.headers.get("retry-after")));
				limiter?.noteRefusal(host);
				return response;
			} else if (isRetryableStatus(response.status) && attempt < attempts) {
				lastError = fetchError(`http_${response.status}`, `${host} returned HTTP ${response.status}`);
				lastError.transient = true;
				lastError.retryAfterMs = parseRetryAfterMs(response.headers.get("retry-after"));
			} else {
				if (limiter) limiter.noteSuccess(host);
				return response;
			}
		}

		if (attempt >= attempts) break;
		const delayMs = Math.max(lastError?.retryAfterMs ?? 0, backoffFor(attempt, backoffMs, maxBackoffMs));
		onRetry?.({ url, host, attempt, delayMs, error: lastError });
		await sleep(delayMs);
	}

	throw lastError ?? fetchError("fetch_failed", `request failed: ${url}`);
}

/**
 * `httpRequest` plus "give me the text, and throw if the status is not ok".
 * The common shape for a metadata provider, where a non-2xx has no useful body.
 */
export async function httpText(url, options = {}) {
	const response = await httpRequest(url, options);
	const text = await response.text();
	if (!response.ok) {
		const host = hostForUrl(url) ?? "host";
		const error = fetchError(`http_${response.status}`, `${host} returned HTTP ${response.status}`);
		error.status = response.status;
		error.transient = isRetryableStatus(response.status);
		error.body = text.slice(0, 2000);
		throw error;
	}
	return { text, response };
}

export async function httpJson(url, options = {}) {
	const { text, response } = await httpText(url, {
		...options,
		headers: { accept: "application/json", ...(options.headers ?? {}) },
	});
	try {
		return { json: JSON.parse(text), text, response };
	} catch (error) {
		throw fetchError("invalid_json", `${hostForUrl(url) ?? "host"} returned unparseable JSON: ${error.message}`);
	}
}

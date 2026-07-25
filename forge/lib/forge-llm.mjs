/**
 * Shared client for the forge chat endpoints, for skills written in JavaScript.
 *
 * This is the `.mjs` counterpart of `forge_llm.py`, and deliberately mirrors it:
 * the two must agree, because they share one lease directory and one set of
 * endpoints. Where the Python module also owns endpoint resolution, this one
 * defers to `connected-services.mjs`, which already layers explicit argument →
 * environment → `connectedServices` → default.
 *
 * One local model is served twice:
 *
 * - `chat`  — every per-item batch call. Spends no hidden reasoning tokens.
 * - `think` — verification, review, and escalation of flagged batch work. Also
 *   the interactive agent's own server, which is why background calls against it
 *   pin a slot and yield to interactive turns.
 *
 * Run all the bulk calls, then all the review calls. Alternating between the two
 * servers swaps the prompt prefix on both and throws away the cache the split
 * exists to exploit.
 */

import { mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { getForgeAgentDir, resolveConnectedServices, resolveThinkOrChat } from "./connected-services.mjs";
import { isTransientFailure } from "./run-state.mjs";

// A thinking backend that was asked not to think can still emit a stray block.
// Strip it defensively everywhere rather than trusting any one server's config.
const THINK_BLOCK = /^\s*<think>[\s\S]*?<\/think>\s*/;
export const DEFAULT_TIMEOUT_MS = 600_000;
export const MAX_TRANSIENT_ATTEMPTS = 3;
// Must match forge_llm.LEASE_STALE_MS. A Python worker and a JavaScript one read
// each other's leases, so a disagreement here means one of them ignores the
// other and both generate at once.
export const LEASE_STALE_MS = 15_000;
// Reasoning is invisible in the response body: llama.cpp strips the think block
// server-side and reports no reasoning_content, so the only evidence is the
// token count. Measured on this deployment, the thinking server spends ~410
// hidden tokens answering a question the non-thinking one answers in 2 — even
// for a one-word reply. Anything well past the visible content is reasoning.
const HIDDEN_TOKEN_MARGIN = 32;
const CHARACTERS_PER_TOKEN = 3.0;

export class ChatError extends Error {
	constructor(message) {
		super(message);
		this.name = "ChatError";
	}
}

export class PreemptedError extends Error {
	constructor(message = "background inference preempted by interactive activity") {
		super(message);
		this.name = "PreemptedError";
	}
}

/** Tokens generated beyond what the visible content can account for. */
export function hiddenTokenCount(generatedTokens, content) {
	if (typeof generatedTokens !== "number" || Number.isNaN(generatedTokens)) return null;
	const visible = String(content ?? "").length / CHARACTERS_PER_TOKEN;
	return Math.max(0, Math.trunc(generatedTokens - visible));
}

/**
 * Accept a bare `/v1` base or a full chat-completions URL.
 *
 * `forge_llm.normalize_base_url` does this on the Python side, and the repo's
 * own provider config stores bases as `http://llms:8004/v1`, so without it the
 * same setting works in Python and 404s here. It lives in this module rather
 * than in `connected-services.mjs` because that one also normalizes the
 * embeddings, searxng, and playwright URLs, which must not gain this suffix.
 */
export function normalizeChatUrl(value) {
	const url = String(value ?? "").trim().replace(/\/+$/, "");
	if (!url || url.endsWith("/chat/completions")) return url;
	if (url.endsWith("/v1")) return `${url}/chat/completions`;
	return url;
}

/**
 * Resolve a named service into the shape the rest of this module uses.
 *
 * `connected-services.mjs` speaks `{enabled, baseUrl, model, scheduling}`;
 * everything here and in `forge_llm.py` speaks `{name, url, model, ...}`.
 */
export function resolveService(name, options = {}) {
	const services = resolveConnectedServices(options);
	const service = name === "think" ? services.think : services.chat;
	return { name, enabled: Boolean(service.enabled), url: normalizeChatUrl(service.baseUrl), model: service.model, scheduling: service.scheduling };
}

/**
 * The service to use for judgment, falling back to `chat` when no thinking
 * backend is configured. A single-endpoint install still verifies its batch
 * work; it just loses the split. Bulk work never falls back the other way,
 * because quietly thinking per item is the cost this module exists to avoid.
 */
export function resolveThinkService(options = {}) {
	const services = resolveConnectedServices(options);
	const chosen = resolveThinkOrChat(services);
	const isFallback = chosen === services.chat;
	return {
		name: "think",
		enabled: Boolean(chosen.enabled),
		url: normalizeChatUrl(chosen.baseUrl),
		model: chosen.model,
		scheduling: chosen.scheduling,
		...(isFallback ? { fallback: "chat" } : {}),
	};
}

/** Strip a stray think block and any code fence, returning JSON text. */
export function extractJsonContent(content) {
	let text = String(content ?? "").replace(THINK_BLOCK, "").trim();
	if (text.startsWith("```")) {
		text = text.replace(/^```[a-zA-Z]*\s*/, "").replace(/\s*```$/, "");
	}
	return text;
}

/** Parse a model response as JSON, tolerating prose around the payload. */
export function parseJsonContent(content) {
	const text = extractJsonContent(content);
	try {
		return JSON.parse(text);
	} catch (error) {
		const candidates = [text.indexOf("{"), text.indexOf("[")].filter((position) => position >= 0);
		const start = candidates.length ? Math.min(...candidates) : -1;
		const end = Math.max(text.lastIndexOf("}"), text.lastIndexOf("]"));
		if (start < 0 || end < start) throw error;
		return JSON.parse(text.slice(start, end + 1));
	}
}

function leaseDirectory() {
	return join(getForgeAgentDir(), "inference-leases");
}

/**
 * Interactive sessions currently generating.
 *
 * The row shape, the directory, the staleness window, and the `interactive`
 * default when `kind` is absent all match `forge_llm.py`. They have to: leases
 * written by one runtime are read by the other.
 */
export function activeInteractiveLeases() {
	let entries;
	try {
		entries = readdirSync(leaseDirectory());
	} catch {
		return [];
	}
	const now = Date.now();
	const active = [];
	for (const entry of entries) {
		if (!entry.endsWith(".json")) continue;
		try {
			const row = JSON.parse(readFileSync(join(leaseDirectory(), entry), "utf8"));
			if ((row.kind ?? "interactive") === "interactive" && now - Number(row.updatedAtMs ?? 0) <= LEASE_STALE_MS) {
				active.push(row);
			}
		} catch {
			// A partially written or malformed lease is not a claim.
		}
	}
	return active;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Block until no interactive session is generating, then observe the grace period. */
export async function waitForInteractiveIdle(scheduling) {
	const started = Date.now();
	const grace = Math.max(0, scheduling?.idleGraceMs ?? 0);
	for (;;) {
		while (activeInteractiveLeases().length) await sleep(200);
		if (!grace) break;
		await sleep(grace);
		if (!activeInteractiveLeases().length) break;
	}
	return Date.now() - started;
}

function writeBackgroundLease(path, slot) {
	writeFileSync(path, `${JSON.stringify({ pid: process.pid, kind: "background", slot, updatedAtMs: Date.now() })}\n`);
}

/**
 * Claim the background slot, retrying if an interactive turn starts during the
 * claim. Returns the lease path, or null if leases cannot be written.
 *
 * An unwritable agent directory costs cooperative scheduling, not the call: the
 * work still runs, it just cannot announce itself.
 */
async function acquireBackgroundLease(scheduling) {
	for (;;) {
		await waitForInteractiveIdle(scheduling);
		let lease;
		try {
			mkdirSync(leaseDirectory(), { recursive: true });
			lease = join(leaseDirectory(), `background-${process.pid}-${Date.now()}.json`);
			writeBackgroundLease(lease, scheduling.backgroundSlot);
		} catch {
			return null;
		}
		if (!activeInteractiveLeases().length) return lease;
		rmSync(lease, { force: true });
	}
}

async function postSimple(url, body, timeoutMs) {
	const controller = new AbortController();
	const timer = setTimeout(() => controller.abort(), timeoutMs);
	try {
		const response = await fetch(url, {
			method: "POST",
			headers: { "Content-Type": "application/json", Authorization: "Bearer local" },
			body,
			signal: controller.signal,
		});
		const text = await response.text();
		if (!response.ok) throw new ChatError(`chat endpoint returned HTTP ${response.status}: ${text.slice(0, 500)}`);
		try {
			return JSON.parse(text);
		} catch (error) {
			throw new ChatError(`chat endpoint returned invalid JSON: ${error.message}`);
		}
	} catch (error) {
		if (error instanceof ChatError) throw error;
		if (error.name === "AbortError") throw new ChatError(`chat endpoint request timed out after ${timeoutMs}ms`);
		throw new ChatError(`chat endpoint request failed: ${error.message}`);
	} finally {
		clearTimeout(timer);
	}
}

/**
 * POST while watching for interactive activity, aborting the moment it appears.
 *
 * Abandoning the response is not enough: the server would keep generating and
 * keep holding the GPU, so the request itself is cancelled.
 */
async function postPreemptible(url, body, timeoutMs, lease, scheduling) {
	const controller = new AbortController();
	let preempted = false;
	const watcher = setInterval(() => {
		writeBackgroundLease(lease, scheduling.backgroundSlot);
		if (activeInteractiveLeases().length) {
			preempted = true;
			controller.abort();
		}
	}, 1000);
	const timer = setTimeout(() => controller.abort(), timeoutMs);
	try {
		const response = await fetch(url, {
			method: "POST",
			headers: { "Content-Type": "application/json", Authorization: "Bearer local" },
			body,
			signal: controller.signal,
		});
		const text = await response.text();
		if (!response.ok) throw new ChatError(`chat endpoint returned HTTP ${response.status}: ${text.slice(0, 500)}`);
		return JSON.parse(text);
	} catch (error) {
		if (preempted) throw new PreemptedError();
		if (error instanceof ChatError) throw error;
		if (error.name === "AbortError") throw new ChatError(`chat endpoint request timed out after ${timeoutMs}ms`);
		throw new ChatError(`chat endpoint request failed: ${error.message}`);
	} finally {
		clearInterval(watcher);
		clearTimeout(timer);
	}
}

/**
 * Post one chat completion and return `{content, record}`.
 *
 * `background: true` claims the service's background slot and yields the GPU to
 * interactive turns, throwing `PreemptedError` if one starts mid-request. Use it
 * for verification against the thinking backend, which is the same server the
 * interactive session is using.
 */
export async function call(service, messages, options = {}) {
	const {
		temperature = 0,
		maxTokens = null,
		responseFormat = null,
		cachePrompt = true,
		background = false,
		session = null,
		timeoutMs = DEFAULT_TIMEOUT_MS,
		task = null,
	} = options;
	const scheduling = service.scheduling ?? {};
	const request = { model: service.model, messages, temperature, stream: false };
	if (session) request.user = session;
	if (responseFormat) request.response_format = responseFormat;
	if (cachePrompt) request.cache_prompt = true;
	if (maxTokens) request.max_tokens = maxTokens;
	let useSlot = background && Boolean(scheduling.enabled);
	if (useSlot) request.id_slot = scheduling.backgroundSlot;
	const body = JSON.stringify(request);

	const lease = useSlot ? await acquireBackgroundLease(scheduling) : null;
	if (useSlot && lease === null) useSlot = false; // no lease held, so not scheduled
	const started = Date.now();
	let payload;
	try {
		payload = lease === null ? await postSimple(service.url, body, timeoutMs) : await postPreemptible(service.url, body, timeoutMs, lease, scheduling);
	} finally {
		if (lease !== null) rmSync(lease, { force: true });
	}

	const usage = payload.usage ?? {};
	const details = usage.prompt_tokens_details ?? {};
	const timings = payload.timings ?? {};
	const choices = payload.choices ?? [];
	const message = choices.length ? (choices[0].message ?? {}) : {};
	const content = message.content;
	if (typeof content !== "string") throw new ChatError("chat response did not contain choices[0].message.content");
	const generated = timings.predicted_n ?? usage.completion_tokens ?? null;
	const hidden = hiddenTokenCount(generated, content);
	const record = {
		at: new Date().toISOString(),
		event: "model_call",
		task,
		service: service.name,
		endpoint: service.url,
		model: service.model,
		mode: useSlot ? "background" : "foreground",
		slot: useSlot ? scheduling.backgroundSlot : null,
		promptTokens: usage.prompt_tokens ?? null,
		cachedTokens: details.cached_tokens ?? timings.cache_n ?? null,
		generatedTokens: generated,
		hiddenTokens: hidden,
		prefillMs: timings.prompt_ms ?? null,
		generationMs: timings.predicted_ms ?? null,
		elapsedMs: Date.now() - started,
		finishReason: choices.length ? (choices[0].finish_reason ?? null) : null,
		reasoned: Boolean(message.reasoning_content || message.reasoning) || (hidden !== null && hidden > HIDDEN_TOKEN_MARGIN),
	};
	return { content, record };
}

/** `call` with the response parsed as JSON. Returns `{value, record}`. */
export async function callJson(service, messages, options = {}) {
	const { content, record } = await call(service, messages, options);
	try {
		return { value: parseJsonContent(content), record };
	} catch (error) {
		if (record.finishReason === "length") {
			throw new ChatError("chat response was truncated before valid JSON (raise maxTokens or split the input)");
		}
		throw new ChatError(`chat response was not valid JSON: ${error.message}`);
	}
}

async function withRetry(attempts, operation) {
	let lastError;
	for (let attempt = 1; attempt <= attempts; attempt += 1) {
		try {
			return await operation();
		} catch (error) {
			if (error instanceof PreemptedError) throw error;
			lastError = error;
			if (attempt < attempts && isTransientFailure(error)) {
				await sleep(Math.min(2000 * attempt, 10_000));
				continue;
			}
			throw error;
		}
	}
	throw lastError;
}

/** `callJson` retrying transient transport failures with backoff. */
export async function callJsonWithRetry(service, messages, options = {}) {
	const { attempts = MAX_TRANSIENT_ATTEMPTS, ...rest } = options;
	return withRetry(attempts, () => callJson(service, messages, rest));
}

/**
 * `call` with retries, returning the content with any stray think block and code
 * fence removed. For the callers whose answer is a word or a sentence rather
 * than a document — they still need the retry and the stripping.
 */
export async function callTextWithRetry(service, messages, options = {}) {
	const { attempts = MAX_TRANSIENT_ATTEMPTS, ...rest } = options;
	const { content, record } = await withRetry(attempts, () => call(service, messages, rest));
	return { text: extractJsonContent(content), record };
}

/** List the model ids a service actually serves, via `GET /v1/models`. */
export async function servedModels(service, timeoutMs = 5000) {
	const root = service.url.replace(/\/chat\/completions$/, "");
	const controller = new AbortController();
	const timer = setTimeout(() => controller.abort(), timeoutMs);
	try {
		const response = await fetch(`${root}/models`, { signal: controller.signal });
		const payload = await response.json();
		return (payload.data ?? []).map((entry) => entry.id).filter(Boolean);
	} catch (error) {
		throw new ChatError(`could not list models at ${root}/models: ${error.message}`);
	} finally {
		clearTimeout(timer);
	}
}

/**
 * Probe a service and report reachability, served model, and — for the batch
 * service — whether it actually answers without thinking.
 *
 * That last one cannot be read off the response body, and it is the single most
 * useful signal here: a bulk endpoint that is quietly reasoning costs a few
 * hundred wasted tokens on every item and nothing else reveals it.
 */
export async function serviceDoctor(service, { expectNonThinking = false, timeoutMs = 30_000 } = {}) {
	const report = { service: service.name, url: service.url, model: service.model, reachable: false };
	if (!service.enabled) {
		report.detail = "disabled in connectedServices";
		return report;
	}
	if (service.fallback) report.fallback = service.fallback;
	try {
		const available = await servedModels(service, Math.min(timeoutMs, 10_000));
		report.servedModels = available;
		if (available.length && !available.includes(service.model)) {
			report.modelMismatch = true;
			report.warning = `configured model ${JSON.stringify(service.model)} is not served here (available: ${available.join(", ")})`;
		}
	} catch (error) {
		report.warning = error.message;
	}

	let content;
	let record;
	try {
		({ content, record } = await call(service, [{ role: "user", content: "Reply with the single word: ready" }], { timeoutMs }));
	} catch (error) {
		report.detail = `${error.name}: ${error.message}`;
		return report;
	}
	report.reachable = true;
	report.elapsedMs = record.elapsedMs;
	report.hiddenTokens = record.hiddenTokens;
	const thought = record.reasoned || THINK_BLOCK.test(content);
	report.thinking = thought;
	if (expectNonThinking && thought) {
		report.warning = `this endpoint is configured for bulk work but spent ~${record.hiddenTokens} hidden reasoning tokens on a one-word reply; every item is paying that`;
	}
	return report;
}

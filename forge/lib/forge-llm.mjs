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
import {
	getForgeAgentDir,
	resolveConnectedServices,
	resolveTaskOrChat,
	resolveThinkOrChat,
	SLOT_CONTEXT_TOKENS,
} from "./connected-services.mjs";
import { isTransientFailure } from "./run-state.mjs";
import { capacityForUrl, explainUnreachable, healthWarnings, identityForUrl, readSnapshot } from "./stack-state.mjs";

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
// Deliberately different from CHARACTERS_PER_TOKEN above. That one deflates the
// visible-content estimate so hidden reasoning stands out; this one is the
// density actually measured on this model and is used to decide whether a
// prompt fits a slot. Must match forge_llm.PROMPT_CHARACTERS_PER_TOKEN.
const PROMPT_CHARACTERS_PER_TOKEN = 3.42;
// What one image contributes to the prompt-fit preflight. There is no character
// count to divide, and the real figure is tokenizer- and tiling-specific, so this
// is a single conservative estimate whose only job is to stop estimatePromptTokens
// from budgeting an image at zero. Must match forge_llm.IMAGE_TOKENS_ESTIMATE.
const IMAGE_TOKENS_ESTIMATE = 1600;
// Extension fallback for the magic-byte sniff; the keys are the formats the vision
// backend and the interactive agent's reader both accept.
const IMAGE_MIME_BY_EXT = {
	png: "image/png",
	jpg: "image/jpeg",
	jpeg: "image/jpeg",
	gif: "image/gif",
	webp: "image/webp",
};

export class ChatError extends Error {
	constructor(message) {
		super(message);
		this.name = "ChatError";
	}
}

/**
 * A prompt could not fit the slot it would have run in.
 *
 * Extends ChatError so existing handlers keep working, while callers that know
 * how to split their input can catch this specifically and do so.
 */
export class ContextBudgetError extends ChatError {
	constructor(message) {
		super(message);
		this.name = "ContextBudgetError";
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
 * Approximate the prompt size of a message list, in tokens.
 *
 * Character density is a run-to-run estimate, not a tokenizer, so this is only
 * accurate enough to catch a prompt that cannot possibly fit.
 */
export function estimatePromptTokens(messages) {
	let characters = 0;
	let imageTokens = 0;
	for (const message of messages ?? []) {
		const content = message?.content;
		if (typeof content === "string") {
			characters += content.length;
		} else if (Array.isArray(content)) {
			for (const part of content) {
				if (typeof part?.text === "string") characters += part.text.length;
				else if (part?.type === "image_url") imageTokens += IMAGE_TOKENS_ESTIMATE;
			}
		} else if (content !== undefined && content !== null) {
			characters += String(content).length;
		}
	}
	return Math.trunc(characters / PROMPT_CHARACTERS_PER_TOKEN) + imageTokens;
}

/**
 * Best-effort image MIME from magic bytes, falling back to the extension.
 *
 * Mirrors `forge_llm._sniff_image_mime`. Sniffing beats trusting the suffix — a
 * screenshot saved as `.img` is still a PNG to the backend — but a truncated or
 * odd file can still name its type, so the extension is the fallback.
 */
function sniffImageMime(buffer, path) {
	const head = buffer.subarray(0, 12).toString("hex");
	if (head.startsWith("89504e470d0a1a0a")) return "image/png";
	if (head.startsWith("ffd8ff")) return "image/jpeg";
	if (head.startsWith("474946383761") || head.startsWith("474946383961")) return "image/gif"; // GIF87a / GIF89a
	if (head.startsWith("52494646") && buffer.subarray(8, 12).toString("latin1") === "WEBP") return "image/webp"; // RIFF….WEBP
	const ext = path.toLowerCase().split(".").pop();
	const guessed = IMAGE_MIME_BY_EXT[ext];
	if (guessed) return guessed;
	throw new ChatError(`${JSON.stringify(path)} is not a supported image (need PNG, JPEG, GIF, or WEBP)`);
}

/**
 * Load an image file into an OpenAI `image_url` content part.
 *
 * Mirrors `forge_llm.image_content_part`. Returns
 * `{ type: "image_url", image_url: { url: "data:<mime>;base64,…" } }`, inlined as a
 * data URI because the inference host cannot fetch from wherever a skill runs.
 * Accepts PNG, JPEG, GIF, WEBP; throws for anything else.
 */
export function imageContentPart(path) {
	const buffer = readFileSync(path);
	const mime = sniffImageMime(buffer, String(path));
	return { type: "image_url", image_url: { url: `data:${mime};base64,${buffer.toString("base64")}` } };
}

/**
 * Build one chat message carrying `prompt` text plus one or more images.
 *
 * Mirrors `forge_llm.image_message`. `images` is a single path or an array of
 * paths; the result is one message whose `content` is a text part followed by an
 * image part per file, ready for the `messages` list `call` takes.
 */
export function imageMessage(prompt, images, { role = "user" } = {}) {
	const list = Array.isArray(images) ? images : [images];
	const content = [{ type: "text", text: prompt }];
	for (const path of list) content.push(imageContentPart(path));
	return { role, content };
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
	const url = String(value ?? "")
		.trim()
		.replace(/\/+$/, "");
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
const INFERENCE_SERVICES = new Set(["chat", "think", "task"]);

export function resolveService(name, options = {}) {
	const services = resolveConnectedServices(options);
	// Only the inference services have this shape. `embeddings` carries `url`
	// rather than `baseUrl`, so falling through to it would produce a service
	// with an undefined endpoint rather than an error.
	const service = INFERENCE_SERVICES.has(name) ? services[name] : services.chat;
	return {
		name,
		enabled: Boolean(service.enabled),
		url: normalizeChatUrl(service.baseUrl),
		model: service.model,
		contextTokens: service.contextTokens,
		chatTemplateKwargs: service.chatTemplateKwargs,
		reasoningEffort: service.reasoningEffort,
		scheduling: service.scheduling,
	};
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
		contextTokens: chosen.contextTokens,
		chatTemplateKwargs: chosen.chatTemplateKwargs,
		reasoningEffort: chosen.reasoningEffort,
		scheduling: chosen.scheduling,
		...(isFallback ? { fallback: "chat" } : {}),
	};
}

/**
 * The small tier, falling back to `chat` when it is not configured — which is
 * the default, so most installs get `chat` here and nothing changes for them.
 */
export function resolveTaskService(options = {}) {
	const services = resolveConnectedServices(options);
	const chosen = resolveTaskOrChat(services);
	const isFallback = chosen === services.chat;
	return {
		name: "task",
		enabled: Boolean(chosen.enabled),
		url: normalizeChatUrl(chosen.baseUrl),
		model: chosen.model,
		contextTokens: chosen.contextTokens,
		chatTemplateKwargs: chosen.chatTemplateKwargs,
		reasoningEffort: chosen.reasoningEffort,
		scheduling: chosen.scheduling,
		...(isFallback ? { fallback: "chat" } : {}),
	};
}

/** Strip a stray think block and any code fence, returning JSON text. */
export function extractJsonContent(content) {
	let text = String(content ?? "")
		.replace(THINK_BLOCK, "")
		.trim();
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
// Which service URLs have already had their backend identity written to a
// record in this process. A 500-item batch should say which weights it ran
// against once, not five hundred times.
const reportedConditions = new Set();

/**
 * Backend identity and stack warnings to attach to this call's record, once.
 *
 * Skills journal every `model_call` record into their run directory, so this is
 * how a run comes to say which weights produced its output and what shape the
 * host was in at the time. A batch that crawled because the inference host was
 * at 97% swap should read that way in its own journal, rather than looking like
 * the model got slow.
 *
 * Returns null on every call after the first for a given endpoint, and on any
 * install where the state API is absent. Mirrors `stack_conditions` in
 * `forge_llm.py`.
 */
export async function stackConditions(service, env = process.env) {
	if (reportedConditions.has(service.url)) return null;
	reportedConditions.add(service.url);
	const snapshot = await readSnapshot({ env });
	if (!snapshot) return null;
	const conditions = {};
	const identity = identityForUrl(snapshot, service.url);
	if (identity) conditions.backend = identity;
	const warnings = healthWarnings(snapshot);
	if (warnings.length) conditions.stackWarnings = warnings;
	return Object.keys(conditions).length ? conditions : null;
}

/** Forget which endpoints have been reported. For tests. */
export function resetStackConditions() {
	reportedConditions.clear();
}

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
		env = process.env,
		reasoningEffort = null,
	} = options;
	const scheduling = service.scheduling ?? {};
	const request = { model: service.model, messages, temperature, stream: false };
	if (session) request.user = session;
	if (responseFormat) request.response_format = responseFormat;
	if (cachePrompt) request.cache_prompt = true;
	if (maxTokens) request.max_tokens = maxTokens;
	if (service.chatTemplateKwargs) request.chat_template_kwargs = service.chatTemplateKwargs;
	// Graded reasoning effort as the top-level OpenAI field. A per-call value wins
	// over the service default, mirroring the Python client, so an escalation can
	// force `xhigh` on one item without re-resolving the service.
	const effort = reasoningEffort ?? service.reasoningEffort;
	if (effort) request.reasoning_effort = effort;
	let useSlot = background && Boolean(scheduling.enabled);
	if (useSlot) request.id_slot = scheduling.backgroundSlot;
	// Refuse a prompt that cannot fit before uploading it and taking a lease.
	// llama.cpp does reject it too, quickly and with the numbers
	// ("exceeds the available context size (131072 tokens)"), but its advice —
	// "try increasing it" — is wrong here: the context is fixed by the
	// deployment, and the knob a skill actually has is how much it sends.
	const contextTokens = service.contextTokens || SLOT_CONTEXT_TOKENS;
	const estimatedPrompt = estimatePromptTokens(messages);
	const reservedOutput = maxTokens || 0;
	if (estimatedPrompt + reservedOutput > contextTokens) {
		throw new ContextBudgetError(
			`prompt is about ${estimatedPrompt} tokens and reserves ${reservedOutput} for output, ` +
				`over the ${contextTokens}-token limit on service ${JSON.stringify(service.name)}. ` +
				`Send less text per call (lower the skill's packet or chunk size), or lower maxTokens.`,
		);
	}
	const body = JSON.stringify(request);

	const lease = useSlot ? await acquireBackgroundLease(scheduling) : null;
	if (useSlot && lease === null) useSlot = false; // no lease held, so not scheduled
	const started = Date.now();
	let payload;
	try {
		payload =
			lease === null
				? await postSimple(service.url, body, timeoutMs)
				: await postPreemptible(service.url, body, timeoutMs, lease, scheduling);
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
		reasoned:
			Boolean(message.reasoning_content || message.reasoning) || (hidden !== null && hidden > HIDDEN_TOKEN_MARGIN),
	};
	Object.assign(record, (await stackConditions(service, env)) ?? {});
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
/**
 * Every warning in a `serviceDoctor` report, in reporting order.
 *
 * The report carries several independent warnings under separate keys, because a
 * single `warning` field means the last writer silently wins. Callers that
 * surface warnings to a person should read this rather than one key.
 *
 * `stackDetail` is deliberately absent: it is already folded into `detail`, and a
 * caller printing both would say the same thing twice.
 */
export function doctorWarnings(report) {
	return ["warning", "contextWarning", "slotWarning"]
		.map((key) => report?.[key])
		.filter((value) => typeof value === "string" && value.trim());
}

/**
 * Add what the stack knows about this endpoint to a doctor report.
 *
 * Three separate findings, each independently useful:
 *
 * - `backend` names the weights, quantization, and llama.cpp build. The model id
 *   in the config cannot do that, because llama.cpp answers to whatever name it
 *   is sent regardless of what is loaded.
 * - `contextMismatch` catches a configured ceiling that no longer matches the
 *   slot. Too high and a skill sends a prompt this client believes fits, then
 *   reads the server's rejection as the model failing the task.
 * - `slotWarning` catches pinning a slot that does not exist. Nothing else checks
 *   this: a deployment moving to `--parallel 1` leaves every background call in
 *   forge naming slot 1 forever.
 *
 * Each finding gets its own key rather than sharing `warning`, which the probe
 * below overwrites. Mirrors `_describe_backend` in `forge_llm.py`.
 */
function describeBackend(report, snapshot, service) {
	if (!snapshot) return;
	const identity = identityForUrl(snapshot, service.url);
	if (identity) report.backend = identity;
	const capacity = capacityForUrl(snapshot, service.url);
	if (!capacity) return;
	report.servedContextTokens = capacity.contextTokens;
	const configured = service.contextTokens || SLOT_CONTEXT_TOKENS;
	if (configured !== capacity.contextTokens) {
		report.contextMismatch = true;
		report.contextWarning =
			`this service is configured for ${configured} context tokens but the backend serves ${capacity.contextTokens} ` +
			`per slot; run the installer to re-read the deployment, or set contextTokens in connectedServices`;
	}
	const scheduling = service.scheduling ?? {};
	const totalSlots = capacity.totalSlots;
	if (
		scheduling.enabled &&
		Number.isInteger(totalSlots) &&
		Number.isInteger(scheduling.backgroundSlot) &&
		scheduling.backgroundSlot >= totalSlots
	) {
		report.slotWarning =
			`background work pins slot ${scheduling.backgroundSlot} but the backend runs ${totalSlots} ` +
			`slot${totalSlots === 1 ? "" : "s"}; every background call is naming a slot that does not exist`;
	}
}

export async function serviceDoctor(
	service,
	{ expectNonThinking = false, timeoutMs = 30_000, env = process.env } = {},
) {
	const report = {
		service: service.name,
		url: service.url,
		model: service.model,
		contextTokens: service.contextTokens || SLOT_CONTEXT_TOKENS,
		chatTemplateKwargs: service.chatTemplateKwargs ?? null,
		reachable: false,
	};
	if (!service.enabled) {
		report.detail = "disabled in connectedServices";
		return report;
	}
	if (service.fallback) report.fallback = service.fallback;
	const snapshot = await readSnapshot({ env });
	describeBackend(report, snapshot, service);
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
		({ content, record } = await call(service, [{ role: "user", content: "Reply with the single word: ready" }], {
			timeoutMs,
		}));
	} catch (error) {
		report.detail = `${error.name}: ${error.message}`;
		// The transport error says the call did not land. The stack says why,
		// which is the difference between "connection refused" and "the primary
		// proxy is up but every backend behind it is stopped".
		const explanation = explainUnreachable(snapshot, service.url);
		if (explanation) {
			report.stackDetail = explanation;
			report.detail = `${report.detail} — ${explanation}`;
		}
		return report;
	}
	report.reachable = true;
	report.elapsedMs = record.elapsedMs;
	report.hiddenTokens = record.hiddenTokens;
	const thought = record.reasoned || THINK_BLOCK.test(content);
	report.thinking = thought;
	// An empty reply that still burned tokens is worth naming outright: the
	// backend reasoned into `reasoning_content`, which this client never reads,
	// so nothing arrives to parse and every JSON-expecting skill fails on a
	// response the server reported as successful.
	if (!content.trim() && (record.generatedTokens || 0) > 0) {
		report.emptyContent = true;
		report.warning =
			`the endpoint generated ~${record.generatedTokens} tokens but returned empty content: it is reasoning ` +
			`into a field this client does not read. Set chatTemplateKwargs to {"enable_thinking": false} for this ` +
			`service (connectedServices, or FORGE_BASE_CHAT_TEMPLATE_KWARGS / FORGE_THINK_TEMPLATE_KWARGS).`;
		report.detail = "reachable, but answers with no visible content";
		return report;
	}
	if (expectNonThinking && thought) {
		report.warning = `this endpoint is configured for bulk work but spent ~${record.hiddenTokens} hidden reasoning tokens on a one-word reply; every item is paying that`;
	}
	return report;
}

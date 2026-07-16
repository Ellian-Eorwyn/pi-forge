import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { basename, dirname, join, resolve } from "node:path";
import { homedir } from "node:os";
import { JSDOM, VirtualConsole } from "jsdom";
import { Readability } from "@mozilla/readability";
import * as playwright from "playwright";
import { resolveConnectedServices } from "../../../lib/connected-services.mjs";
import { atomicWriteFile, atomicWriteJson } from "../../../lib/run-state.mjs";

export const DEFAULT_USER_AGENT = "pi-forge-web-research/1 (+https://github.com/pi-forge)";
export const DEFAULT_TIMEOUT_MS = 30_000;
export const DEFAULT_MAX_BYTES = 20 * 1024 * 1024;

const TRACKING_PARAMS = new Set([
	"fbclid",
	"gclid",
	"igshid",
	"mc_cid",
	"mc_eid",
	"mkt_tok",
	"msclkid",
	"ref",
	"ref_src",
	"spm",
	"utm_campaign",
	"utm_content",
	"utm_medium",
	"utm_source",
	"utm_term",
	"yclid",
]);
const REDIRECT_WRAPPERS = new Map([
	["www.google.com", ["url", "q"]],
	["google.com", ["url", "q"]],
	["duckduckgo.com", ["uddg"]],
	["l.facebook.com", ["u"]],
	["lm.facebook.com", ["u"]],
]);
const TEXT_TYPES = /(?:text\/html|application\/xhtml\+xml|text\/plain|text\/xml|application\/xml|application\/json|application\/ld\+json)/i;
const HTML_TYPES = /(?:text\/html|application\/xhtml\+xml)/i;
const JSON_TYPES = /(?:application\/json|application\/ld\+json|\+json)/i;
const quietJsdomConsole = new VirtualConsole();
quietJsdomConsole.on("jsdomError", () => {});

function nowIso() {
	return new Date().toISOString();
}

function sha256(value) {
	return createHash("sha256").update(value).digest("hex");
}

function writeJson(filePath, value) {
	mkdirSync(dirname(filePath), { recursive: true });
	atomicWriteJson(filePath, value);
}

function writeJsonl(filePath, rows) {
	mkdirSync(dirname(filePath), { recursive: true });
	atomicWriteFile(filePath, rows.map((row) => JSON.stringify(row)).join("\n") + (rows.length > 0 ? "\n" : ""));
}

function relativeArtifact(runDirectory, path) {
	if (!path || !runDirectory) return path ?? null;
	const resolvedRun = resolve(runDirectory);
	const resolvedPath = resolve(path);
	return resolvedPath.startsWith(`${resolvedRun}/`) ? resolvedPath.slice(resolvedRun.length + 1) : resolvedPath;
}

function compactObject(value) {
	const output = {};
	for (const [key, entry] of Object.entries(value)) {
		if (entry === undefined || entry === null) continue;
		if (typeof entry === "string" && !entry.trim()) continue;
		if (Array.isArray(entry) && entry.length === 0) continue;
		output[key] = entry;
	}
	return output;
}

function normalizeWhitespace(value) {
	return String(value ?? "").replace(/\s+/g, " ").trim();
}

function htmlToReadableText(html) {
	return String(html ?? "")
		.replace(/<script[\s\S]*?<\/script>/gi, " ")
		.replace(/<style[\s\S]*?<\/style>/gi, " ")
		.replace(/<(br|\/p|\/div|\/section|\/article|\/main|\/h[1-6]|\/li)>/gi, "\n")
		.replace(/<li[^>]*>/gi, "\n- ")
		.replace(/<[^>]+>/g, " ")
		.replace(/&nbsp;/g, " ")
		.replace(/&amp;/g, "&")
		.replace(/&lt;/g, "<")
		.replace(/&gt;/g, ">")
		.replace(/&quot;/g, '"')
		.replace(/&#39;/g, "'")
		.replace(/[ \t]{2,}/g, " ")
		.replace(/\n[ \t]+/g, "\n")
		.replace(/\n{3,}/g, "\n\n")
		.trim();
}

function looksBinaryLike(text) {
	const sample = String(text ?? "").slice(0, 10_000);
	if (!sample) return false;
	const replacementRatio = (sample.match(/\uFFFD/g) ?? []).length / sample.length;
	const controlRatio = (sample.match(/[\u0000-\u0008\u000B\u000C\u000E-\u001F]/g) ?? []).length / sample.length;
	return replacementRatio > 0.01 || controlRatio > 0.01;
}

function isLoopbackOrMetadataHost(hostname) {
	const host = hostname.toLowerCase();
	if (process.env.FORGE_WEB_RESEARCH_ALLOW_UNSAFE === "1") return false;
	if (host === "localhost" || host.endsWith(".localhost")) return true;
	if (host === "127.0.0.1" || host.startsWith("127.")) return true;
	if (host === "::1" || host === "0.0.0.0") return true;
	if (host === "169.254.169.254" || host.startsWith("169.254.")) return true;
	if (host === "metadata" || host === "metadata.google.internal") return true;
	return false;
}

export function assertFetchableUrl(url) {
	let parsed;
	try {
		parsed = new URL(url);
	} catch {
		throw new Error(`invalid URL: ${url}`);
	}
	if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
		throw new Error(`unsupported URL scheme (only http/https): ${url}`);
	}
	if (isLoopbackOrMetadataHost(parsed.hostname)) {
		throw new Error(`refused loopback or metadata host: ${parsed.hostname}`);
	}
	return parsed;
}

export function normalizeUrl(url) {
	try {
		const wrapper = new URL(url);
		const wrapperParams = REDIRECT_WRAPPERS.get(wrapper.hostname.toLowerCase());
		if (wrapperParams) {
			for (const parameter of wrapperParams) {
				const target = wrapper.searchParams.get(parameter);
				if (target && /^https?:\/\//i.test(target)) return normalizeUrl(target);
			}
		}
		const parsed = new URL(url);
		parsed.hash = "";
		parsed.hostname = parsed.hostname.toLowerCase();
		if (parsed.hostname.startsWith("m.")) parsed.hostname = parsed.hostname.slice(2);
		if (parsed.hostname.startsWith("amp.")) parsed.hostname = parsed.hostname.slice(4);
		if ((parsed.protocol === "http:" && parsed.port === "80") || (parsed.protocol === "https:" && parsed.port === "443")) {
			parsed.port = "";
		}
		for (const key of [...parsed.searchParams.keys()]) {
			if (TRACKING_PARAMS.has(key.toLowerCase()) || key.toLowerCase().startsWith("utm_")) parsed.searchParams.delete(key);
		}
		const sorted = [...parsed.searchParams.entries()].sort(([left], [right]) => left.localeCompare(right));
		parsed.search = "";
		for (const [key, value] of sorted) parsed.searchParams.append(key, value);
		if (parsed.pathname !== "/" && parsed.pathname.endsWith("/")) parsed.pathname = parsed.pathname.slice(0, -1);
		if (/^\/10\.\d{4,9}\//i.test(parsed.pathname) && /(?:^|\.)doi\.org$/i.test(parsed.hostname)) {
			parsed.pathname = parsed.pathname.toLowerCase();
		}
		return parsed.toString();
	} catch {
		return url;
	}
}

export function sourceIdForUrl(url) {
	return `src-${sha256(normalizeUrl(url)).slice(0, 12)}`;
}

export function defaultCacheDirectory() {
	return process.env.FORGE_WEB_RESEARCH_CACHE_DIR || join(homedir(), ".pi-forge", "cache", "web-research");
}

export function modeDefaults(mode = "standard") {
	if (mode === "fast") {
		return { mode, allowBrowser: false, maxConcurrency: 3, perDomainConcurrency: 1, timeoutMs: 12_000, maxBytes: 6 * 1024 * 1024 };
	}
	if (mode === "deep") {
		return { mode, allowBrowser: true, maxConcurrency: 3, perDomainConcurrency: 1, timeoutMs: DEFAULT_TIMEOUT_MS, maxBytes: DEFAULT_MAX_BYTES };
	}
	return { mode: "standard", allowBrowser: true, maxConcurrency: 3, perDomainConcurrency: 1, timeoutMs: DEFAULT_TIMEOUT_MS, maxBytes: DEFAULT_MAX_BYTES };
}

export function readDomainRegistry(registryPath = new URL("../references/domain-strategies.json", import.meta.url)) {
	try {
		const parsed = JSON.parse(readFileSync(registryPath, "utf8"));
		return Array.isArray(parsed.domains) ? parsed.domains : [];
	} catch {
		return [];
	}
}

export function domainRuleForUrl(url, registry = []) {
	let parsed;
	try {
		parsed = new URL(url);
	} catch {
		return null;
	}
	const host = parsed.hostname.toLowerCase();
	return (
		registry.find((rule) => {
			if (rule.domain && host === rule.domain.toLowerCase()) return true;
			return (rule.subdomains ?? []).some((pattern) => host === pattern.toLowerCase() || host.endsWith(`.${pattern.toLowerCase()}`));
		}) ?? null
	);
}

function hostForUrl(url) {
	try {
		return new URL(url).hostname.toLowerCase();
	} catch {
		return "unknown";
	}
}

function createTaskQueue(name, concurrency, schedulerLog) {
	let active = 0;
	const pending = [];
	const runNext = () => {
		while (active < concurrency && pending.length > 0) {
			const task = pending.shift();
			active += 1;
			task.start();
		}
	};
	return {
		name,
		concurrency,
		run(taskName, fn, metadata = {}) {
			const enqueuedAtMs = Date.now();
			const enqueuedAt = nowIso();
			return new Promise((resolveTask, rejectTask) => {
				pending.push({
					start: async () => {
						const startedAtMs = Date.now();
						const startedAt = nowIso();
						try {
							const value = await fn();
							const endedAtMs = Date.now();
							schedulerLog.push({
								queue: name,
								task: taskName,
								status: "success",
								enqueuedAt,
								startedAt,
								endedAt: nowIso(),
								waitMs: startedAtMs - enqueuedAtMs,
								durationMs: endedAtMs - startedAtMs,
								...metadata,
							});
							resolveTask(value);
						} catch (error) {
							const endedAtMs = Date.now();
							schedulerLog.push({
								queue: name,
								task: taskName,
								status: "failed",
								enqueuedAt,
								startedAt,
								endedAt: nowIso(),
								waitMs: startedAtMs - enqueuedAtMs,
								durationMs: endedAtMs - startedAtMs,
								error: error instanceof Error ? error.message : String(error),
								...metadata,
							});
							rejectTask(error);
						} finally {
							active -= 1;
							runNext();
						}
					},
				});
				runNext();
			});
		},
	};
}

export function createAcquisitionContext(options = {}) {
	const defaults = modeDefaults(options.mode);
	const runDirectory = options.runDirectory ? resolve(options.runDirectory) : null;
	if (runDirectory) {
		mkdirSync(join(runDirectory, "archive", "raw"), { recursive: true });
		mkdirSync(join(runDirectory, "archive", "rendered"), { recursive: true });
		mkdirSync(join(runDirectory, "archive", "extracted"), { recursive: true });
		mkdirSync(join(runDirectory, "archive", "chunks"), { recursive: true });
		mkdirSync(join(runDirectory, "discovery_reports"), { recursive: true });
	}
	let cacheDirectory = resolve(options.cacheDir || defaultCacheDirectory());
	const earlyCacheLog = [];
	try {
		mkdirSync(cacheDirectory, { recursive: true });
	} catch (error) {
		if (!runDirectory) throw error;
		const fallback = join(runDirectory, "archive", "cache");
		mkdirSync(fallback, { recursive: true });
		earlyCacheLog.push({
			layer: "cache",
			key: cacheDirectory,
			status: "fallback",
			path: fallback,
			warning: error instanceof Error ? error.message : String(error),
			recordedAt: nowIso(),
		});
		cacheDirectory = fallback;
	}
	const context = {
		runDirectory,
		cacheDirectory,
		forceRefresh: Boolean(options.forceRefresh),
		forceStrategy: options.forceStrategy ?? null,
		mode: defaults.mode,
		allowBrowser: options.allowBrowser ?? defaults.allowBrowser,
		userAgent: options.userAgent ?? DEFAULT_USER_AGENT,
		timeoutMs: options.timeoutMs ?? defaults.timeoutMs,
		maxBytes: options.maxBytes ?? defaults.maxBytes,
		playwrightWsEndpoint: options.playwrightWsEndpoint ?? null,
		maxConcurrency: options.maxConcurrency ?? defaults.maxConcurrency,
		perDomainConcurrency: options.perDomainConcurrency ?? defaults.perDomainConcurrency,
		registry: options.registry ?? readDomainRegistry(),
		normalizedUrls: [],
		strategyDecisions: [],
		acquisitionLog: [],
		extractionLog: [],
		cacheLog: earlyCacheLog,
		schedulerLog: [],
		discoveryReports: [],
		domainNextAllowedAt: new Map(),
		domainQueues: new Map(),
		metrics: {
			searchResultsDiscovered: 0,
			uniqueCanonicalUrls: 0,
			directHttpSuccesses: 0,
			embeddedStructuredDataSuccesses: 0,
			staticExtractionSuccesses: 0,
			internalApiSuccesses: 0,
			playwrightDomFallbacks: 0,
			failedSources: 0,
			cacheHits: 0,
			cacheMisses: 0,
			rawBytesDownloaded: 0,
			extractedCharacters: 0,
			evidenceCharactersSentToModel: 0,
			queueWaitMs: {},
			queueDurationMs: {},
			rateLimitWaitMs: 0,
			startedAt: nowIso(),
			completedAt: null,
		},
		browser: null,
	};
	context.acquisitionQueue = createTaskQueue("acquisition", Math.max(1, context.maxConcurrency), context.schedulerLog);
	context.browserQueue = createTaskQueue("browser", Math.max(1, options.playwrightConcurrency ?? 1), context.schedulerLog);
	return context;
}

export async function closeAcquisitionContext(context) {
	if (context.browser) {
		await context.browser.close();
		context.browser = null;
	}
}

export function writeAcquisitionArtifacts(context) {
	if (!context.runDirectory) return;
	context.metrics.completedAt = nowIso();
	context.metrics.uniqueCanonicalUrls = new Set(context.normalizedUrls.map((record) => record.canonicalUrl)).size;
	context.metrics.queueWaitMs = queueMetric(context.schedulerLog, "waitMs");
	context.metrics.queueDurationMs = queueMetric(context.schedulerLog, "durationMs");
	writeJsonl(join(context.runDirectory, "normalized_urls.jsonl"), context.normalizedUrls);
	writeJsonl(join(context.runDirectory, "strategy_decisions.jsonl"), context.strategyDecisions);
	writeJsonl(join(context.runDirectory, "acquisition_log.jsonl"), context.acquisitionLog);
	writeJsonl(join(context.runDirectory, "extraction_log.jsonl"), context.extractionLog);
	writeJsonl(join(context.runDirectory, "cache_log.jsonl"), context.cacheLog);
	writeJsonl(join(context.runDirectory, "scheduler_log.jsonl"), context.schedulerLog);
	writeJson(join(context.runDirectory, "metrics.json"), context.metrics);
	for (const report of context.discoveryReports) {
		const id = sha256(`${report.domain}:${report.page_url}`).slice(0, 12);
		writeJson(join(context.runDirectory, "discovery_reports", `${report.domain}-${id}.json`), report);
	}
}

function queueMetric(rows, field) {
	const byQueue = {};
	for (const row of rows) {
		if (!row.queue) continue;
		const value = Number.isFinite(row[field]) ? row[field] : 0;
		byQueue[row.queue] = (byQueue[row.queue] ?? 0) + value;
	}
	return byQueue;
}

function extensionForContent(contentType, finalUrl) {
	if (JSON_TYPES.test(contentType)) return "json";
	if (HTML_TYPES.test(contentType)) return "html";
	if (/xml/i.test(contentType)) return "xml";
	if (/text\/plain/i.test(contentType)) return "txt";
	try {
		const extension = basename(new URL(finalUrl).pathname).split(".").pop();
		if (extension && /^[a-z0-9]{1,8}$/i.test(extension)) return extension.toLowerCase();
	} catch {
		// fall through
	}
	return "bin";
}

function cachePath(context, layer, key, extension) {
	return join(context.cacheDirectory, layer, `${sha256(key)}.${extension}`);
}

function sleep(milliseconds) {
	return new Promise((resolveSleep) => setTimeout(resolveSleep, milliseconds));
}

async function waitForDomainRateLimit(url, context, domainRule) {
	const host = hostForUrl(url);
	const perSecond = domainRule?.rate_limit?.requests_per_second;
	if (!perSecond || perSecond <= 0) return;
	const spacingMs = Math.ceil(1000 / perSecond);
	const now = Date.now();
	const nextAllowedAt = context.domainNextAllowedAt.get(host) ?? 0;
	const waitMs = Math.max(0, nextAllowedAt - now);
	context.domainNextAllowedAt.set(host, Math.max(now, nextAllowedAt) + spacingMs);
	if (waitMs > 0) {
		context.metrics.rateLimitWaitMs += waitMs;
		await sleep(waitMs);
	}
}

async function queuedDirectHttpAcquire(url, context, domainRule) {
	const host = hostForUrl(url);
	if (!context.domainQueues.has(host)) {
		context.domainQueues.set(host, createTaskQueue(`domain:${host}`, Math.max(1, context.perDomainConcurrency), context.schedulerLog));
	}
	return context.domainQueues.get(host).run(
		"domain_slot",
		() =>
			context.acquisitionQueue.run(
				"direct_http",
				async () => {
					await waitForDomainRateLimit(url, context, domainRule);
					return directHttpAcquire(url, context);
				},
				{ domain: host, url },
			),
		{ domain: host, url },
	);
}

async function readCappedBody(response, maxBytes) {
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

async function directHttpAcquire(url, context) {
	const startedAt = Date.now();
	const canonicalUrl = normalizeUrl(url);
	const cachedMetaPath = cachePath(context, "raw", canonicalUrl, "json");
	if (!context.forceRefresh && existsSync(cachedMetaPath)) {
		const cached = JSON.parse(readFileSync(cachedMetaPath, "utf8"));
		if (existsSync(cached.cacheBodyPath)) {
			const buffer = readFileSync(cached.cacheBodyPath);
			context.cacheLog.push({ layer: "raw", key: canonicalUrl, status: "hit", path: cached.cacheBodyPath, recordedAt: nowIso() });
			context.metrics.cacheHits += 1;
			return { ...cached, buffer, fromCache: true, durationMs: Date.now() - startedAt };
		}
	}
	context.cacheLog.push({ layer: "raw", key: canonicalUrl, status: "miss", path: cachedMetaPath, recordedAt: nowIso() });
	context.metrics.cacheMisses += 1;
	const controller = new AbortController();
	const timer = setTimeout(() => controller.abort(), context.timeoutMs);
	let response;
	try {
		response = await fetch(url, {
			signal: controller.signal,
			redirect: "follow",
			headers: {
				"user-agent": context.userAgent,
				accept: "text/html,application/xhtml+xml,application/json,application/ld+json,text/plain,application/xml;q=0.8,*/*;q=0.4",
				"accept-encoding": "gzip, deflate, br",
			},
		});
	} catch (error) {
		throw new Error(error.name === "AbortError" ? `request timed out after ${context.timeoutMs}ms` : `Fetch failed: ${error.message}`);
	} finally {
		clearTimeout(timer);
	}
	const contentType = response.headers.get("content-type") ?? "";
	const declared = Number.parseInt(response.headers.get("content-length") || "", 10);
	if (Number.isInteger(declared) && declared > context.maxBytes) throw new Error(`resource exceeds max-bytes (${declared} > ${context.maxBytes})`);
	const { buffer, truncated } = await readCappedBody(response, context.maxBytes);
	if (truncated) throw new Error(`resource exceeded max-bytes (${context.maxBytes}); not saved`);
	const finalUrl = response.url || url;
	const extension = extensionForContent(contentType, finalUrl);
	const hash = sha256(buffer);
	const cacheBodyPath = cachePath(context, "raw", `${finalUrl}:${hash}`, extension);
	mkdirSync(dirname(cacheBodyPath), { recursive: true });
	if (!existsSync(cacheBodyPath)) atomicWriteFile(cacheBodyPath, buffer);
	writeJson(cachedMetaPath, {
		requestedUrl: url,
		finalUrl,
		canonicalUrl,
		statusCode: response.status,
		contentType,
		headers: Object.fromEntries(response.headers.entries()),
		cacheBodyPath,
		rawHash: hash,
		rawBytes: buffer.length,
	});
	return {
		requestedUrl: url,
		finalUrl,
		canonicalUrl,
		statusCode: response.status,
		contentType,
		headers: Object.fromEntries(response.headers.entries()),
		cacheBodyPath,
		rawHash: hash,
		rawBytes: buffer.length,
		buffer,
		fromCache: false,
		durationMs: Date.now() - startedAt,
	};
}

function titleFromDocument(document) {
	return (
		document.querySelector("meta[property='og:title']")?.getAttribute("content") ||
		document.querySelector("meta[name='citation_title']")?.getAttribute("content") ||
		document.querySelector("title")?.textContent ||
		null
	);
}

function extractStructuredDataFromDocument(document) {
	const jsonLd = [];
	const applicationJson = [];
	for (const script of [...document.querySelectorAll("script[type='application/ld+json']")]) {
		try {
			jsonLd.push(JSON.parse(script.textContent || "null"));
		} catch {
			// ignore invalid embedded data
		}
	}
	const nextData = document.querySelector("script#__NEXT_DATA__")?.textContent;
	let parsedNextData = null;
	if (nextData) {
		try {
			parsedNextData = JSON.parse(nextData);
		} catch {
			parsedNextData = null;
		}
	}
	for (const script of [...document.querySelectorAll("script[type='application/json'], script[type='application/graphql-response+json']")]) {
		if (script.id === "__NEXT_DATA__") continue;
		try {
			applicationJson.push({ id: script.id || null, value: JSON.parse(script.textContent || "null") });
		} catch {
			// ignore invalid embedded data
		}
	}
	const meta = {};
	for (const node of [...document.querySelectorAll("meta")]) {
		const key = node.getAttribute("property") || node.getAttribute("name");
		const value = node.getAttribute("content");
		if (!key || !value) continue;
		if (/^(og:|twitter:|citation_|dc\.|article:)/i.test(key)) meta[key] = value;
	}
	const canonical = document.querySelector("link[rel='canonical']")?.getAttribute("href") || null;
	return compactObject({ jsonLd, nextData: parsedNextData, applicationJson, meta, canonical });
}

function readabilityExtract(html, url) {
	let dom = null;
	try {
		dom = new JSDOM(html, { url, virtualConsole: quietJsdomConsole });
		const document = dom.window.document;
		const structured = extractStructuredDataFromDocument(document);
		const title = normalizeWhitespace(titleFromDocument(document));
		const article = new Readability(document).parse();
		const readableText = article?.content ? htmlToReadableText(article.content) : "";
		const fallbackText = htmlToReadableText(html);
		return {
			title: article?.title || title || null,
			text: readableText || normalizeWhitespace(article?.textContent) || fallbackText,
			metadata: compactObject({
				structured,
				excerpt: article?.excerpt ?? null,
				byline: article?.byline ?? null,
				dir: article?.dir ?? null,
				siteName: article?.siteName ?? null,
				lang: article?.lang ?? null,
				publishedTime: article?.publishedTime ?? null,
				length: article?.length ?? null,
			}),
			extractionKind: structured.jsonLd?.length || structured.nextData || structured.applicationJson?.length ? "structured_static" : "static_readability",
		};
	} finally {
		dom?.window.close();
	}
}

function extractJsonText(payload) {
	const text = JSON.stringify(payload, null, 2);
	const title = payload?.title || payload?.name || payload?.headline || null;
	return { title, text, metadata: { structured: { json: payload } }, extractionKind: "structured_json" };
}

function extractDocument(acquired, context) {
	const startedAt = Date.now();
	const contentType = acquired.contentType ?? "";
	const textValue = acquired.buffer.toString("utf8");
	if (looksBinaryLike(textValue)) throw new Error("response looked like binary content");
	let extracted;
	if (JSON_TYPES.test(contentType) || textValue.trimStart().startsWith("{") || textValue.trimStart().startsWith("[")) {
		try {
			extracted = extractJsonText(JSON.parse(textValue));
		} catch {
			extracted = { title: null, text: textValue, metadata: {}, extractionKind: "static_text" };
		}
	} else if (HTML_TYPES.test(contentType) || /<html|<article|<main|<!doctype/i.test(textValue)) {
		extracted = readabilityExtract(textValue, acquired.finalUrl);
	} else if (/text\/plain|text\/xml|application\/xml/i.test(contentType)) {
		extracted = { title: null, text: textValue, metadata: {}, extractionKind: "static_text" };
	} else if (contentType && !TEXT_TYPES.test(contentType)) {
		throw new Error(`unsupported readable content type: ${contentType}`);
	} else {
		extracted = { title: null, text: htmlToReadableText(textValue) || textValue, metadata: {}, extractionKind: "static_text" };
	}
	const text = String(extracted.text ?? "").trim();
	const hash = sha256(text);
	const cacheDocPath = cachePath(context, "extracted", `${acquired.finalUrl}:${hash}`, "json");
	const record = {
		title: extracted.title,
		text,
		charCount: text.length,
		metadata: extracted.metadata ?? {},
		extractionKind: extracted.extractionKind,
		documentHash: hash,
		cacheDocumentPath: cacheDocPath,
		durationMs: Date.now() - startedAt,
	};
	writeJson(cacheDocPath, record);
	return record;
}

function endpointCandidatesFromHtml(acquired) {
	const content = acquired.buffer.toString("utf8");
	if (!HTML_TYPES.test(acquired.contentType ?? "") && !/<html|<!doctype/i.test(content)) return [];
	let dom = null;
	try {
		dom = new JSDOM(content, { url: acquired.finalUrl, virtualConsole: quietJsdomConsole });
		const candidates = [];
		for (const link of [...dom.window.document.querySelectorAll("a[href], link[href], script[src]")]) {
			const raw = link.getAttribute("href") || link.getAttribute("src");
			if (!raw) continue;
			const absolute = new URL(raw, acquired.finalUrl).toString();
			if (/\/(?:api|graphql|json|search|data)\b/i.test(absolute)) candidates.push(absolute);
		}
		return [...new Set(candidates)].slice(0, 4);
	} finally {
		dom?.window.close();
	}
}

async function tryEndpointReplay(acquired, context, domainRule) {
	for (const endpoint of endpointCandidatesFromHtml(acquired)) {
		try {
			const replayed = await queuedDirectHttpAcquire(endpoint, context, domainRule);
			replayed.strategy = "endpoint_replay";
			const extracted = extractDocument(replayed, context);
			const validation = validateAcquisition(replayed, extracted);
			if (!validation.success || !extracted.text || extracted.text.length < 120) continue;
			context.metrics.internalApiSuccesses += 1;
			return { acquired: replayed, extracted, validation };
		} catch {
			// Try the next candidate endpoint.
		}
	}
	return null;
}

function validationFailureType(warnings) {
	if (warnings.some((warning) => /captcha|checkpoint|checking your browser|cloudflare|bot challenge/i.test(warning))) return "blocked_request";
	if (warnings.some((warning) => /login|authentication/i.test(warning))) return "authentication_required";
	if (warnings.some((warning) => /unsupported readable content type/i.test(warning))) return "unsupported_format";
	if (warnings.some((warning) => /HTTP 404/i.test(warning))) return "permanent_not_found";
	if (warnings.some((warning) => /HTTP 429|retry-after|rate limit/i.test(warning))) return "rate_limit";
	if (warnings.some((warning) => /timed out|timeout/i.test(warning))) return "timeout";
	if (warnings.some((warning) => /HTTP [45]\d\d/i.test(warning))) return "http_error";
	return warnings.length ? "extraction_failure" : null;
}

function validateAcquisition(acquired, extracted) {
	const warnings = [];
	if (acquired.statusCode !== null && acquired.statusCode !== undefined && (acquired.statusCode < 200 || acquired.statusCode >= 300)) {
		warnings.push(`HTTP ${acquired.statusCode}`);
	}
	const contentType = acquired.contentType ?? "";
	if (contentType && !TEXT_TYPES.test(contentType)) warnings.push(`unsupported readable content type: ${contentType}`);
	if (!extracted.text || extracted.text.length < 120) warnings.push(`short extracted text (${extracted.text?.length ?? 0} chars)`);
	const combined = `${extracted.title ?? ""} ${extracted.text ?? ""}`.toLowerCase();
	if (/\b(captcha|security checkpoint|checking your browser|just a moment|attention required|cloudflare)\b/.test(combined)) {
		warnings.push("bot challenge or checkpoint indicators detected");
	}
	if (/\b(sign in|log in|login required|authentication required)\b/.test(combined) && (extracted.text?.length ?? 0) < 2_000) {
		warnings.push("login wall indicators detected");
	}
	if (/\b(enable javascript|javascript is required)\b/.test(combined) && (extracted.text?.length ?? 0) < 2_000) {
		warnings.push("javascript-required warning detected");
	}
	let qualityScore = 1;
	for (const warning of warnings) {
		if (/short extracted text/.test(warning)) qualityScore -= 0.35;
		else if (/bot challenge|login wall|javascript-required|unsupported/.test(warning)) qualityScore -= 0.45;
		else qualityScore -= 0.25;
	}
	qualityScore = Math.max(0, Number(qualityScore.toFixed(2)));
	const browserRecoverable = warnings.some((warning) => /javascript-required|bot challenge|login wall/i.test(warning));
	const unsupported = warnings.some((warning) => /unsupported readable content type/i.test(warning));
	const fallbackRecommended = !unsupported && (browserRecoverable || (!qualityScore || qualityScore < 0.6));
	const success = warnings.length === 0 || qualityScore >= 0.6;
	return {
		success,
		strategy: acquired.strategy,
		content_type: contentType,
		raw_bytes: acquired.rawBytes ?? 0,
		extracted_chars: extracted.text?.length ?? 0,
		quality_score: qualityScore,
		warnings,
		fallback_recommended: fallbackRecommended,
		failure_type: success ? null : validationFailureType(warnings),
	};
}

function archiveBuffer(context, acquired) {
	if (!context.runDirectory) return null;
	const extension = extensionForContent(acquired.contentType ?? "", acquired.finalUrl);
	const path = join(context.runDirectory, "archive", "raw", `${acquired.rawHash}.${extension}`);
	if (!existsSync(path)) atomicWriteFile(path, acquired.buffer);
	return relativeArtifact(context.runDirectory, path);
}

function archiveExtracted(context, result) {
	if (!context.runDirectory) return null;
	const path = join(context.runDirectory, "archive", "extracted", `${result.sourceId}.json`);
	writeJson(path, {
		sourceId: result.sourceId,
		title: result.title,
		text: result.text,
		metadata: result.metadata,
		provenance: result.provenance,
		qualityScore: result.validation.quality_score,
	});
	return relativeArtifact(context.runDirectory, path);
}

function strategyDecision(url, strategy, reason, fallbacks, domainRule) {
	return {
		requested_url: url,
		canonical_url: normalizeUrl(url),
		selected_strategy: strategy,
		reason,
		fallbacks,
		domain_rule_used: domainRule?.domain ?? null,
		decided_at: nowIso(),
	};
}

function recordNormalizedUrl(context, requestedUrl, finalUrl) {
	const canonicalUrl = normalizeUrl(finalUrl || requestedUrl);
	context.normalizedUrls.push({
		requestedUrl,
		finalUrl: finalUrl || requestedUrl,
		canonicalUrl,
		normalizedAt: nowIso(),
	});
	return canonicalUrl;
}

async function browser() {
	return playwright.chromium;
}

function playwrightWsEndpoint(explicit) {
	const services = resolveConnectedServices({ playwrightWsEndpoint: explicit });
	return services.playwright.enabled ? services.playwright.wsEndpoint : "";
}

async function getBrowser(context) {
	if (context.browser) return context.browser;
	const endpoint = playwrightWsEndpoint(context.playwrightWsEndpoint);
	if (!endpoint) throw new Error("Playwright rendered browsing is disabled in settings");
	context.browser = await (await browser()).connect(endpoint, { timeout: context.timeoutMs });
	return context.browser;
}

function shouldBlockResource(url, resourceType) {
	if (["image", "media", "font"].includes(resourceType)) return true;
	return /(?:doubleclick|googletagmanager|google-analytics|facebook\.net|analytics|adservice|adsystem|hotjar|segment|mixpanel)/i.test(url);
}

async function extractWithPlaywright(url, context, domainRule) {
	return context.browserQueue.run("playwright_dom", () => extractWithPlaywrightUnqueued(url, context, domainRule), { domain: hostForUrl(url), url });
}

async function extractWithPlaywrightUnqueued(url, context, domainRule) {
	const startedAt = Date.now();
	const activeBrowser = await getBrowser(context);
	const pageWarnings = [];
	const apiResponses = [];
	const contextOptions = { userAgent: context.userAgent };
	const browserContext = await activeBrowser.newContext(contextOptions);
	try {
		await browserContext.route("**/*", async (route) => {
			const request = route.request();
			if (shouldBlockResource(request.url(), request.resourceType())) await route.abort();
			else await route.continue();
		});
		const page = await browserContext.newPage();
		page.on("response", async (response) => {
			const contentType = response.headers()["content-type"] ?? "";
			if (!/(json|graphql)/i.test(contentType)) return;
			const responseUrl = response.url();
			try {
				const body = await response.text();
				if (!body.trim()) return;
				apiResponses.push({
					url: responseUrl,
					method: response.request().method(),
					status: response.status(),
					contentType,
					sizeBytes: Buffer.byteLength(body),
					containsMeaningfulContent: body.length > 50,
				});
			} catch {
				// Ignore response bodies Playwright cannot read.
			}
		});
		const selectors = domainRule?.main_selectors ?? ["article", "main", "[role='main']", ".content", ".post", ".entry", "#content", "#main"];
		const response = await page.goto(url, { waitUntil: "domcontentloaded", timeout: context.timeoutMs });
		const selector = await waitForContentSelector(page, selectors, 8_000);
		let html = await page.content();
		let text = "";
		if (selector) text = normalizeWhitespace(await page.locator(selector).first().textContent());
		if (text.length < 120) text = htmlToReadableText(html);
		const title = (await page.title()) || null;
		const finalUrl = page.url();
		const rawHash = sha256(html);
		let renderedArtifact = null;
		if (context.runDirectory) {
			renderedArtifact = join(context.runDirectory, "archive", "rendered", `${rawHash}.html`);
			atomicWriteFile(renderedArtifact, html);
			renderedArtifact = relativeArtifact(context.runDirectory, renderedArtifact);
		}
		return {
			requestedUrl: url,
			finalUrl,
			canonicalUrl: normalizeUrl(finalUrl),
			statusCode: response?.status() ?? null,
			contentType: "text/html; rendered",
			rawHash,
			rawBytes: Buffer.byteLength(html),
			text,
			title,
			metadata: { structured: readabilityExtract(html, finalUrl).metadata?.structured ?? {}, apiResponses },
			warnings: pageWarnings,
			renderedArtifact,
			durationMs: Date.now() - startedAt,
		};
	} finally {
		await browserContext.close();
	}
}

async function waitForContentSelector(page, selectors, timeoutMs) {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		for (const selector of selectors) {
			try {
				const count = await page.locator(selector).count();
				if (count > 0) {
					const text = await page.locator(selector).first().textContent({ timeout: 500 });
					if (normalizeWhitespace(text).length > 80) return selector;
				}
			} catch {
				// Continue trying selectors.
			}
		}
		await new Promise((resolveWait) => setTimeout(resolveWait, 250));
	}
	return null;
}

export async function acquireUrl(url, context) {
	assertFetchableUrl(url);
	const domainRule = domainRuleForUrl(url, context.registry);
	const allowBrowser = context.allowBrowser && context.forceStrategy !== "direct_http";
	const forced = context.forceStrategy;
	const selectedStrategy = forced || domainRule?.preferred_strategy || "direct_http";
	const decision = strategyDecision(
		url,
		selectedStrategy,
		forced ? "forced by user" : domainRule ? "matched domain strategy" : "default cheapest strategy",
		allowBrowser ? ["playwright_network_discovery", "playwright_dom"] : [],
		domainRule,
	);
	context.strategyDecisions.push(decision);
	const acquisitionStartedAt = nowIso();
	let rawArtifact = null;
	let extractedArtifact = null;
	let acquired = null;
	let extracted = null;
	let validation = null;
	let strategy = selectedStrategy;
	try {
		if (selectedStrategy !== "playwright_dom" && selectedStrategy !== "playwright_network_discovery") {
			acquired = await queuedDirectHttpAcquire(url, context, domainRule);
			acquired.strategy = "direct_http";
			rawArtifact = archiveBuffer(context, acquired);
			extracted = extractDocument(acquired, context);
			validation = validateAcquisition(acquired, extracted);
			if (validation.fallback_recommended && selectedStrategy !== "direct_http_only") {
				const replayed = await tryEndpointReplay(acquired, context, domainRule);
				if (replayed) {
					strategy = "endpoint_replay";
					acquired = replayed.acquired;
					extracted = replayed.extracted;
					validation = replayed.validation;
					rawArtifact = archiveBuffer(context, acquired);
				}
			}
			if (
				validation.fallback_recommended &&
				allowBrowser &&
				context.mode !== "fast" &&
				selectedStrategy !== "direct_http_only"
			) {
				try {
					const rendered = await extractWithPlaywright(url, context, domainRule);
					strategy = "playwright_dom";
					acquired = {
						...rendered,
						strategy,
						buffer: Buffer.from(rendered.text),
						cacheBodyPath: null,
					};
					extracted = {
						title: rendered.title,
						text: rendered.text,
						charCount: rendered.text.length,
						metadata: rendered.metadata,
						extractionKind: "playwright_dom",
						documentHash: sha256(rendered.text),
						cacheDocumentPath: null,
						durationMs: rendered.durationMs,
					};
					validation = validateAcquisition(acquired, extracted);
					rawArtifact = rendered.renderedArtifact;
					context.metrics.playwrightDomFallbacks += 1;
				} catch (error) {
					if (!validation.success) throw error;
					validation = {
						...validation,
						warnings: [...validation.warnings, `browser fallback failed after successful direct extraction: ${error.message}`],
					};
				}
			}
		} else {
			const rendered = await extractWithPlaywright(url, context, domainRule);
			strategy = "playwright_dom";
			acquired = { ...rendered, strategy, buffer: Buffer.from(rendered.text), cacheBodyPath: null };
			extracted = {
				title: rendered.title,
				text: rendered.text,
				charCount: rendered.text.length,
				metadata: rendered.metadata,
				extractionKind: "playwright_dom",
				documentHash: sha256(rendered.text),
				cacheDocumentPath: null,
				durationMs: rendered.durationMs,
			};
			validation = validateAcquisition(acquired, extracted);
			rawArtifact = rendered.renderedArtifact;
			context.metrics.playwrightDomFallbacks += 1;
		}
		const canonicalUrl = recordNormalizedUrl(context, url, acquired.finalUrl);
		if (extracted.extractionKind === "structured_static" || extracted.extractionKind === "structured_json") {
			context.metrics.embeddedStructuredDataSuccesses += 1;
		}
		if (strategy === "direct_http") context.metrics.directHttpSuccesses += 1;
		if (extracted.text) context.metrics.staticExtractionSuccesses += 1;
		context.metrics.rawBytesDownloaded += acquired.rawBytes ?? 0;
		context.metrics.extractedCharacters += extracted.text.length;
		const sourceId = sourceIdForUrl(canonicalUrl);
		const result = {
			sourceId,
			requestedUrl: url,
			finalUrl: acquired.finalUrl,
			canonicalUrl,
			title: extracted.title,
			text: extracted.text,
			charCount: extracted.text.length,
			strategy,
			extractionMethod: strategy === "playwright_dom" ? "playwright" : "http",
			contentType: acquired.contentType,
			httpStatus: acquired.statusCode,
			rawBytes: acquired.rawBytes,
			rawHash: acquired.rawHash,
			documentHash: extracted.documentHash,
			rawArtifact,
			renderedArtifact: strategy === "playwright_dom" ? rawArtifact : null,
			extractedArtifact: null,
			cache: {
				rawPath: acquired.cacheBodyPath ? relativeArtifact(context.runDirectory, acquired.cacheBodyPath) : null,
				documentPath: extracted.cacheDocumentPath ? relativeArtifact(context.runDirectory, extracted.cacheDocumentPath) : null,
				hit: Boolean(acquired.fromCache),
			},
			metadata: extracted.metadata,
			validation,
			warnings: validation.warnings,
			extractedAt: nowIso(),
		};
		extractedArtifact = archiveExtracted(context, result);
		result.extractedArtifact = extractedArtifact;
		context.acquisitionLog.push({
			sourceId,
			requestedUrl: url,
			finalUrl: result.finalUrl,
			canonicalUrl,
			strategy,
			status: validation.success ? "success" : "needs_review",
			httpStatus: result.httpStatus,
			contentType: result.contentType,
			rawArtifact,
			extractedArtifact,
			qualityScore: validation.quality_score,
			warnings: validation.warnings,
			startedAt: acquisitionStartedAt,
			endedAt: nowIso(),
		});
		context.extractionLog.push({
			sourceId,
			strategy,
			extractionKind: extracted.extractionKind,
			title: result.title,
			charCount: result.charCount,
			documentHash: result.documentHash,
			locator: extracted.metadata?.structured?.canonical ?? result.finalUrl,
			recordedAt: nowIso(),
		});
		return result;
	} catch (error) {
		context.metrics.failedSources += 1;
		const message = error instanceof Error ? error.message : String(error);
		context.acquisitionLog.push({
			sourceId: sourceIdForUrl(url),
			requestedUrl: url,
			finalUrl: url,
			canonicalUrl: normalizeUrl(url),
			strategy,
			status: "failed",
			httpStatus: null,
			contentType: null,
			rawArtifact,
			extractedArtifact,
			qualityScore: 0,
			warnings: [message],
			failureType: validationFailureType([message]),
			startedAt: acquisitionStartedAt,
			endedAt: nowIso(),
		});
		throw error;
	}
}

export async function discoverUrl(url, context) {
	assertFetchableUrl(url);
	const domainRule = domainRuleForUrl(url, context.registry);
	const report = {
		domain: new URL(url).hostname.toLowerCase(),
		page_url: url,
		discovered_at: nowIso(),
		candidate_endpoints: [],
		embedded_state: {},
		recommended_strategy: domainRule?.preferred_strategy ?? "direct_http",
		fallback_selector: domainRule?.main_selectors?.[0] ?? "main article",
		warnings: [],
	};
	try {
		const acquired = await queuedDirectHttpAcquire(url, context, domainRule);
		const content = acquired.buffer.toString("utf8");
		if (HTML_TYPES.test(acquired.contentType ?? "") || /<html|<!doctype/i.test(content)) {
			let dom = null;
			try {
				dom = new JSDOM(content, { url: acquired.finalUrl, virtualConsole: quietJsdomConsole });
				report.embedded_state = extractStructuredDataFromDocument(dom.window.document);
				for (const link of [...dom.window.document.querySelectorAll("a[href], link[href], script[src]")]) {
					const raw = link.getAttribute("href") || link.getAttribute("src");
					if (!raw) continue;
					const absolute = new URL(raw, acquired.finalUrl).toString();
					if (/\/(?:api|graphql|json|search|data)\b/i.test(absolute)) {
						report.candidate_endpoints.push({
							url_pattern: new URL(absolute).pathname,
							method: "GET",
							content_type: null,
							contains_primary_content: false,
							requires_cookies: false,
						});
					}
				}
			} finally {
				dom?.window.close();
			}
		}
		if (report.embedded_state?.nextData || report.embedded_state?.jsonLd?.length) report.recommended_strategy = "structured_static";
	} catch (error) {
		report.warnings.push(error instanceof Error ? error.message : String(error));
	}
	if (context.allowBrowser) {
		try {
			await context.browserQueue.run("playwright_network_discovery", async () => {
				const activeBrowser = await getBrowser(context);
				const browserContext = await activeBrowser.newContext({ userAgent: context.userAgent });
				try {
					await browserContext.route("**/*", async (route) => {
						const request = route.request();
						if (shouldBlockResource(request.url(), request.resourceType())) await route.abort();
						else await route.continue();
					});
					const page = await browserContext.newPage();
					page.on("response", async (response) => {
						const contentType = response.headers()["content-type"] ?? "";
						if (!/(json|graphql)/i.test(contentType)) return;
						report.candidate_endpoints.push({
							url_pattern: new URL(response.url()).pathname,
							method: response.request().method(),
							content_type: contentType,
							status: response.status(),
							contains_primary_content: true,
							requires_cookies: false,
						});
					});
					await page.goto(url, { waitUntil: "domcontentloaded", timeout: context.timeoutMs });
					await page.waitForTimeout(1000);
					report.recommended_strategy = report.candidate_endpoints.length > 0 ? "direct_api" : report.recommended_strategy;
				} finally {
					await browserContext.close();
				}
			}, { domain: hostForUrl(url), url });
		} catch (error) {
			report.warnings.push(`Playwright discovery failed: ${error instanceof Error ? error.message : String(error)}`);
		}
	}
	report.candidate_endpoints = [...new Map(report.candidate_endpoints.map((endpoint) => [`${endpoint.method}:${endpoint.url_pattern}`, endpoint])).values()];
	context.discoveryReports.push(report);
	return report;
}

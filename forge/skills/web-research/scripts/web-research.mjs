#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { basename, join, resolve, sep } from "node:path";
import { JSDOM } from "jsdom";
import { Readability } from "@mozilla/readability";

const DEFAULT_USER_AGENT = "pi-forge-web-research/1 (+https://github.com/pi-forge)";
const DEFAULT_TIMEOUT_MS = 30_000;
const DEFAULT_SEARXNG_URL = "http://llms/searxng";
const DEFAULT_LIMIT = 10;
const DEFAULT_READ_COUNT = 5;
const DEFAULT_DEEP_ITERATIONS = 3;
const DEEP_SCHEMA_VERSION = 1;
const DEEP_MANIFEST_COLUMNS = [
	"resource_id",
	"source_url",
	"final_url",
	"access_date",
	"status",
	"http_status",
	"content_type",
	"title",
	"filename",
	"output_path",
	"sha256",
	"byte_size",
	"capture_method",
	"rendered",
	"duplicate_of",
	"error",
];

// --- Utility ---------------------------------------------------------------

function fail(message, exitCode = 1) {
	process.stderr.write(`Error: ${message}\n`);
	process.exit(exitCode);
}

function nowIso() {
	return new Date().toISOString();
}

function sleep(milliseconds) {
	return new Promise((resolveSleep) => setTimeout(resolveSleep, milliseconds));
}

function run(command, args = ["--version"]) {
	const result = spawnSync(command, args, { encoding: "utf8" });
	if (result.error?.code === "ENOENT" || result.error || result.status !== 0) return { available: false, version: null };
	return { available: true, version: result.stdout.trim().split(/\r?\n/, 1)[0] || "available" };
}

function writeJson(filePath, value) {
	writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`);
}

function writeJsonl(filePath, rows) {
	writeFileSync(filePath, rows.map((row) => JSON.stringify(row)).join("\n") + (rows.length > 0 ? "\n" : ""));
}

function readJsonl(filePath) {
	if (!existsSync(filePath)) return [];
	return readFileSync(filePath, "utf8")
		.split(/\r?\n/)
		.map((line) => line.trim())
		.filter(Boolean)
		.map((line) => JSON.parse(line));
}

function sha256(value) {
	return createHash("sha256").update(value).digest("hex");
}

function safeStem(value) {
	const raw = String(value).normalize("NFKC").trim();
	const safe = raw.replace(/[^\p{L}\p{N}._-]+/gu, "-").replace(/^-+|-+$/g, "");
	return safe.slice(0, 80) || "research";
}

function normalizeUrl(url) {
	try {
		const parsed = new URL(url);
		parsed.hash = "";
		parsed.hostname = parsed.hostname.toLowerCase();
		if ((parsed.protocol === "http:" && parsed.port === "80") || (parsed.protocol === "https:" && parsed.port === "443")) {
			parsed.port = "";
		}
		return parsed.toString();
	} catch {
		return url;
	}
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

function assertFetchableUrl(url) {
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

function csvValue(value) {
	const text = value === null || value === undefined ? "" : String(value);
	return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

function writeCsv(filePath, columns, rows) {
	const lines = [columns.join(",")];
	for (const row of rows) lines.push(columns.map((column) => csvValue(row[column])).join(","));
	writeFileSync(filePath, `${lines.join("\n")}\n`);
}

function parseCsv(value) {
	const rows = [];
	let row = [];
	let field = "";
	let quoted = false;
	for (let index = 0; index < value.length; index += 1) {
		const character = value[index];
		if (quoted) {
			if (character === '"' && value[index + 1] === '"') {
				field += '"';
				index += 1;
			} else if (character === '"') quoted = false;
			else field += character;
		} else if (character === '"') quoted = true;
		else if (character === ",") {
			row.push(field);
			field = "";
		} else if (character === "\n") {
			row.push(field.replace(/\r$/, ""));
			rows.push(row);
			row = [];
			field = "";
		} else field += character;
	}
	if (field || row.length > 0) {
		row.push(field);
		rows.push(row);
	}
	return rows;
}

function sourceIdForUrl(url) {
	return `src-${sha256(normalizeUrl(url)).slice(0, 12)}`;
}

function nextId(prefix, index) {
	return `${prefix}-${String(index).padStart(4, "0")}`;
}

function normalizeWhitespace(value) {
	return String(value ?? "").replace(/\s+/g, " ").trim();
}

function includesQuote(text, quote) {
	if (!quote) return true;
	return normalizeWhitespace(text).toLowerCase().includes(normalizeWhitespace(quote).toLowerCase());
}

function readQueryFile(filePath) {
	return readFileSync(resolve(filePath), "utf8")
		.split(/\r?\n/)
		.map((line) => line.trim())
		.filter((line) => line && !line.startsWith("#"));
}

function asArray(value) {
	if (value === undefined || value === null) return [];
	return Array.isArray(value) ? value : [value];
}

function extractJsonFromText(text, fallback) {
	if (!text) return fallback;
	try {
		return JSON.parse(text);
	} catch {
		const objectMatch = text.match(/\{[\s\S]*\}/);
		const arrayMatch = text.match(/\[[\s\S]*\]/);
		const candidate = objectMatch?.[0] ?? arrayMatch?.[0];
		if (!candidate) return fallback;
		try {
			return JSON.parse(candidate);
		} catch {
			return fallback;
		}
	}
}

// --- SearXNG ---------------------------------------------------------------

function searxngBase(explicit) {
	const base = explicit || process.env.FORGE_SEARXNG_URL || DEFAULT_SEARXNG_URL;
	return base.trim().replace(/\/+$/, "");
}

async function pingSearxng(base, userAgent, timeoutMs) {
	if (!base) return { configured: false, reachable: false, detail: "no SearXNG URL configured" };
	const controller = new AbortController();
	const timer = setTimeout(() => controller.abort(), timeoutMs);
	try {
		const response = await fetch(`${base}/search?q=ping&format=json`, {
			signal: controller.signal,
			headers: { "user-agent": userAgent, accept: "application/json" },
		});
		return { configured: true, reachable: response.ok, detail: `${base} responded with HTTP ${response.status}` };
	} catch (error) {
		return { configured: true, reachable: false, detail: `${base} unreachable: ${error.message}` };
	} finally {
		clearTimeout(timer);
	}
}

async function searchSearxng(base, query, options) {
	const params = new URLSearchParams({ q: query, format: "json" });
	if (options.categories) params.set("categories", options.categories);
	if (options.engines) params.set("engines", options.engines);
	if (options.language) params.set("language", options.language);
	if (options.safesearch !== undefined) params.set("safesearch", String(options.safesearch));
	if (options.timeRange) params.set("time_range", options.timeRange);
	if (options.pageNo) params.set("pageno", String(options.pageNo));

	const controller = new AbortController();
	const timer = setTimeout(() => controller.abort(), options.timeoutMs);
	try {
		const response = await fetch(`${base}/search?${params.toString()}`, {
			signal: controller.signal,
			headers: { "user-agent": options.userAgent, accept: "application/json" },
		});
		if (!response.ok) throw new Error(`SearXNG returned HTTP ${response.status}`);
		return await response.json();
	} catch (error) {
		throw new Error(`SearXNG request failed: ${error.message}`);
	} finally {
		clearTimeout(timer);
	}
}

// --- Playwright extraction -------------------------------------------------

async function loadPlaywright() {
	try {
		const module = await import("playwright");
		return module.chromium ? module : null;
	} catch {
		return null;
	}
}

function readabilityMetadata(html, url) {
	let dom = null;
	try {
		dom = new JSDOM(html, { url });
		const document = dom.window.document;
		const article = new Readability(document).parse();
		if (!article) return {};
		return {
			title: article.title ?? null,
			textContent: article.textContent ?? null,
			excerpt: article.excerpt ?? null,
			byline: article.byline ?? null,
			dir: article.dir ?? null,
			siteName: article.siteName ?? null,
			lang: article.lang ?? null,
			publishedTime: article.publishedTime ?? null,
			length: article.length ?? null,
		};
	} catch {
		return {};
	} finally {
		dom?.window.close();
	}
}

async function extractWithPlaywright(playwright, url, timeoutMs, userAgent) {
	const browser = await playwright.chromium.launch({ headless: true });
	let text = "";
	let title = null;
	let finalUrl = url;
	let warnings = [];
	let metadata = {};
	try {
		const context = await browser.newContext({ userAgent });
		const page = await context.newPage();
		await page.goto(url, { waitUntil: "networkidle", timeout: timeoutMs }).catch(async (error) => {
			warnings.push(`networkidle wait failed (${error.message}); retried with domcontentloaded`);
			await page.goto(url, { waitUntil: "domcontentloaded", timeout: timeoutMs });
		});
		finalUrl = page.url();
		title = (await page.title()) || null;
		metadata = readabilityMetadata(await page.content(), finalUrl);

		// Try structured extraction in order of preference
		const selectors = ["article", "main", '[role="main"]', ".content", ".post", ".entry", "#content", "#main"];
		for (const selector of selectors) {
			const element = await page.$(selector);
			if (element) {
				text = (await element.textContent()) || "";
				if (text.trim().length > 100) break;
			}
		}

		// Fallback: extract from body, excluding common noise
		if (text.trim().length < 100) {
			text = await page.evaluate(() => {
				const clone = document.body.cloneNode(true);
				for (const tag of ["script", "style", "nav", "footer", "header", "noscript", "svg"]) {
					for (const el of clone.querySelectorAll(tag)) el.remove();
				}
				for (const attr of ["class"]) {
					for (const el of clone.querySelectorAll(`[${attr}]`)) {
						const val = el.getAttribute(attr) || "";
						if (/[ad-]?(nav|header|footer|sidebar|menu|widget|banner|cookie|popup|modal)/i.test(val)) {
							el.remove();
						}
					}
				}
				return clone.textContent || "";
			});
		}

		text = text.replace(/\n{3,}/g, "\n\n").trim();
	} finally {
		await browser.close();
	}
	return { text: metadata.textContent?.trim() || text, title: metadata.title || title, finalUrl, warnings, metadata };
}

// --- HTTP extraction -------------------------------------------------------

async function extractWithHttp(url, timeoutMs, userAgent) {
	const controller = new AbortController();
	const timer = setTimeout(() => controller.abort(), timeoutMs);
	let response;
	try {
		response = await fetch(url, {
			signal: controller.signal,
			headers: { "user-agent": userAgent, accept: "text/html,application/xhtml+xml" },
		});
	} catch (error) {
		throw new Error(`Fetch failed: ${error.message}`);
	} finally {
		clearTimeout(timer);
	}
	if (!response.ok) throw new Error(`HTTP ${response.status}`);

	const html = await response.text();
	const titleMatch = html.match(/<title[^>]*>([\s\S]*?)<\/title>/i);
	const title = titleMatch ? titleMatch[1].replace(/\s+/g, " ").trim() : null;
	const metadata = readabilityMetadata(html, url);

	// Strip tags and extract text
	const text = html
		.replace(/<script[\s\S]*?<\/script>/gi, " ")
		.replace(/<style[\s\S]*?<\/style>/gi, " ")
		.replace(/<[^>]+>/g, " ")
		.replace(/&nbsp;/g, " ")
		.replace(/&amp;/g, "&")
		.replace(/&lt;/g, "<")
		.replace(/&gt;/g, ">")
		.replace(/&quot;/g, '"')
		.replace(/&#39;/g, "'")
		.replace(/\n{3,}/g, "\n\n")
		.replace(/\s+/g, " ")
		.trim();

	return {
		text: metadata.textContent?.trim() || text,
		title: metadata.title || title,
		finalUrl: url,
		warnings: ["used HTTP extraction (no Playwright)"],
		metadata,
	};
}

async function readPage(url, options) {
	assertFetchableUrl(url);
	const playwright = options.render ? await loadPlaywright() : null;
	let extraction;
	if (playwright && options.render) {
		try {
			extraction = await extractWithPlaywright(playwright, url, options.timeoutMs, options.userAgent);
		} catch (error) {
			extraction = await extractWithHttp(url, options.timeoutMs, options.userAgent);
			extraction.warnings.push(`Playwright extraction failed (${error.message}); fell back to HTTP`);
		}
	} else {
		extraction = await extractWithHttp(url, options.timeoutMs, options.userAgent);
	}
	return {
		url: extraction.finalUrl,
		title: extraction.title,
		text: extraction.text,
		charCount: extraction.text.length,
		extractionMethod: extraction.warnings.some((w) => w.includes("HTTP")) ? "http" : "playwright",
		metadata: extraction.metadata ?? {},
		warnings: extraction.warnings,
		extractedAt: nowIso(),
	};
}

// --- Auto-select SearXNG parameters ----------------------------------------

function autoSelectParams(query) {
	const lower = query.toLowerCase();
	const params = {};

	// Detect query type and suggest categories/engines
	if (/\b(paper|research|study|thesis|journal|doi|scholar)\b/.test(lower)) {
		params.categories = "science,scientific publications";
		params.engines = "google scholar,semantic scholar,arxiv,pubmed";
		params.safesearch = 0;
	} else if (/\b(news|today|recent|breaking|latest)\b/.test(lower)) {
		params.categories = "news";
		params.timeRange = "week";
	} else if (/\b(code|github|repository|npm|pypi|package|api|sdk|library)\b/.test(lower)) {
		params.categories = "it";
		params.engines = "github,stackoverflow,duckduckgo";
	} else if (/\b(define|definition|what is|meaning|etymology)\b/.test(lower)) {
		params.categories = "general,dictionaries";
		params.engines = "wikipedia,duckduckgo";
	}

	// Detect language hints
	if (/\b(en|english)\b/.test(lower)) params.language = "en";
	else if (/\b(de|german|deutsch)\b/.test(lower)) params.language = "de";
	else if (/\b(fr|french|français)\b/.test(lower)) params.language = "fr";
	else if (/\b(es|spanish|español)\b/.test(lower)) params.language = "es";
	else if (/\b(zh|chinese|中文)\b/.test(lower)) params.language = "zh";

	return params;
}

// --- Report generation -----------------------------------------------------

function buildReport(data) {
	const { query, params, results, readings, searchBase } = data;
	const lines = [];
	lines.push("# Research Report");
	lines.push("");
	lines.push(`**Query**: ${query}`);
	lines.push(`**Date**: ${nowIso()}`);
	lines.push(`**Engine**: ${searchBase}`);
	lines.push("");

	if (params && Object.values(params).some((v) => v !== null && v !== undefined)) {
		lines.push("### Search Parameters");
		for (const [key, value] of Object.entries(params)) {
			if (value !== null && value !== undefined) lines.push(`- ${key}: ${value}`);
		}
		lines.push("");
	}

	lines.push("## Search Results");
	lines.push("");
	for (const result of results) {
		const read = readings.find((r) => r.url === result.url);
		const marker = read ? " [read]" : "";
		lines.push(`### ${result.rank}. ${result.title}${marker}`);
		lines.push("");
		lines.push(`- **URL**: ${result.url}`);
		lines.push(`- **Engine**: ${result.engine}`);
		lines.push(`- **Score**: ${result.score}`);
		if (result.content) lines.push(`- **Snippet**: ${result.content.slice(0, 300)}`);
		lines.push("");
		if (read) {
			lines.push("#### Extracted Content");
			lines.push("");
			const excerpt = read.text.slice(0, 3000);
			lines.push("```");
			lines.push(excerpt);
			if (read.text.length > 3000) lines.push(`\n...(truncated, ${read.charCount} total characters)`);
			lines.push("```");
			lines.push("");
			lines.push(`_Extracted via ${read.extractionMethod} at ${read.extractedAt}_`);
			if (read.warnings.length > 0) {
				lines.push(`_Warnings: ${read.warnings.join("; ")}_`);
			}
			lines.push("");
		}
	}

	lines.push("## Sources");
	lines.push("");
	lines.push("| # | Title | URL | Method |");
	lines.push("|---|-------|-----|--------|");
	for (const read of readings) {
		const rank = results.find((r) => r.url === read.url)?.rank || "-";
		lines.push(`| ${rank} | ${read.title || "Untitled"} | ${read.url} | ${read.extractionMethod} |`);
	}
	if (readings.length === 0) lines.push("- No pages were fetched.");
	lines.push("");

	return lines.join("\n");
}

// --- Deep research ----------------------------------------------------------

function deepDefaults(flags) {
	return {
		maxIterations: flags.maxIterations ?? DEFAULT_DEEP_ITERATIONS,
		limit: flags.limit ?? DEFAULT_LIMIT,
		readCount: flags.readCount ?? DEFAULT_READ_COUNT,
		delayMs: flags.delayMs ?? 500,
		timeoutMs: flags.timeoutMs ?? DEFAULT_TIMEOUT_MS,
		userAgent: flags.userAgent ?? DEFAULT_USER_AGENT,
		render: flags.render !== false,
	};
}

function searchParamsForQuery(query, flags, defaults) {
	const autoParams = autoSelectParams(query);
	return {
		userAgent: defaults.userAgent,
		timeoutMs: defaults.timeoutMs,
		categories: flags.categories ?? autoParams.categories,
		engines: flags.engines ?? autoParams.engines,
		language: flags.language ?? autoParams.language,
		safesearch: flags.safesearch ?? autoParams.safesearch,
		timeRange: flags.timeRange ?? autoParams.timeRange,
		pageNo: flags.pageNo ?? autoParams.pageNo,
	};
}

async function callLocalJsonModel(runDirectory, task, prompt, fallback) {
	const startedAt = nowIso();
	const callId = `${task}-${sha256(`${startedAt}\n${prompt}`).slice(0, 12)}`;
	const baseChatUrl = process.env.FORGE_BASE_CHAT_URL || process.env.FORGE_CHAT_URL || "http://llms:8008/v1/chat/completions";
	const model = process.env.FORGE_BASE_MODEL || "code";
	const request = {
		model,
		messages: [
			{
				role: "system",
				content:
					"You are a source-grounded research assistant. Return only valid JSON. Do not invent sources, quotes, or citations.",
			},
			{ role: "user", content: prompt },
		],
		temperature: 0.1,
	};
	const record = { id: callId, task, startedAt, endedAt: null, endpoint: baseChatUrl, model, request, response: null, status: "failed", error: null };
	try {
		const response = await fetch(baseChatUrl, {
			method: "POST",
			headers: { "Content-Type": "application/json", Authorization: "Bearer local" },
			body: JSON.stringify(request),
		});
		record.endedAt = nowIso();
		if (!response.ok) throw new Error(`LLM returned HTTP ${response.status}`);
		const payload = await response.json();
		record.response = payload;
		record.status = "success";
		const text = payload.choices?.[0]?.message?.content ?? "";
		return { value: extractJsonFromText(text, fallback), record };
	} catch (error) {
		record.endedAt = nowIso();
		record.error = error instanceof Error ? error.message : String(error);
		record.response = fallback;
		return { value: fallback, record };
	}
}

function evidencePrompt(source, question) {
	const text = source.text.slice(0, 60_000);
	return `Extract source-backed evidence for this research question.

Question:
${question}

Source:
- source_id: ${source.sourceId}
- title: ${source.title || "Untitled"}
- url: ${source.finalUrl}
- extracted_at: ${source.extractedAt}

Return JSON with this shape:
{
  "evidence": [
    {
      "text": "faithful extracted statement",
      "direct_quote": "short exact quote from source text or null",
      "locator": "heading/section/URL fragment or null",
      "interpretation": "explicit|inferred|unclear",
      "confidence": "high|medium|low",
      "notes": "optional note or null"
    }
  ]
}

Use only the provided source text. If nothing supports the question, return {"evidence":[]}.

Source text:
${text}`;
}

function queryExpansionPrompt(question, queries, evidenceItems, gaps, iteration) {
	const evidenceSummary = evidenceItems
		.slice(-30)
		.map((item) => `- ${item.evidenceId} (${item.sourceId}, ${item.confidence}): ${item.text}`)
		.join("\n");
	const gapSummary = gaps
		.slice(-20)
		.map((gap) => `- ${gap.gapId}: ${gap.text}`)
		.join("\n");
	return `Plan follow-up web searches for iteration ${iteration}.

Research question:
${question}

Queries already tried:
${queries.map((query) => `- ${query}`).join("\n")}

Recent evidence:
${evidenceSummary || "- none yet"}

Known gaps:
${gapSummary || "- none yet"}

Return JSON with this shape:
{
  "queries": ["specific follow-up query", "..."],
  "rationale": "short reason"
}

Return at most 5 queries. Do not repeat existing queries.`;
}

function claimPrompt(question, evidenceItems) {
	const evidence = evidenceItems
		.map(
			(item) =>
				`- evidence_id: ${item.evidenceId}\n  source_id: ${item.sourceId}\n  confidence: ${item.confidence}\n  interpretation: ${item.interpretation}\n  text: ${item.text}\n  quote: ${item.directQuote ?? ""}`,
		)
		.join("\n");
	return `Build a source-backed claim register from the evidence below.

Research question:
${question}

Return JSON with this shape:
{
  "claims": [
    {
      "text": "claim supported by listed evidence",
      "evidence_ids": ["ev-0001"],
      "source_ids": ["src-..."],
      "confidence": "high|medium|low",
      "notes": "agreement, disagreement, limits, or null"
    }
  ],
  "gaps": [
    {
      "text": "missing or under-supported point",
      "reason": "why it remains unresolved",
      "source_ids": ["src-..."]
    }
  ]
}

Rules:
- Every claim must cite at least one evidence_id and one source_id.
- Do not create claims that are not supported by evidence.
- Record disagreement or thin support in notes or gaps.

Evidence:
${evidence || "- no evidence"}`;
}

function sanitizeEvidence(rawEvidence, source, startIndex) {
	const rows = Array.isArray(rawEvidence?.evidence) ? rawEvidence.evidence : Array.isArray(rawEvidence) ? rawEvidence : [];
	const items = [];
	let index = startIndex;
	for (const row of rows) {
		if (typeof row !== "object" || row === null) continue;
		const text = typeof row.text === "string" ? row.text.trim() : "";
		if (!text) continue;
		const directQuote = typeof row.direct_quote === "string" && row.direct_quote.trim() ? row.direct_quote.trim() : null;
		items.push({
			evidenceId: nextId("ev", index++),
			sourceId: source.sourceId,
			text,
			directQuote,
			locator: typeof row.locator === "string" && row.locator.trim() ? row.locator.trim() : null,
			interpretation: ["explicit", "inferred", "unclear"].includes(row.interpretation) ? row.interpretation : "unclear",
			confidence: ["high", "medium", "low"].includes(row.confidence) ? row.confidence : "low",
			notes: typeof row.notes === "string" && row.notes.trim() ? row.notes.trim() : null,
			extractedAt: nowIso(),
		});
	}
	if (items.length === 0 && source.text.trim()) {
		const fallbackText = source.text.trim().slice(0, 500);
		items.push({
			evidenceId: nextId("ev", index++),
			sourceId: source.sourceId,
			text: fallbackText,
			directQuote: fallbackText.slice(0, 180),
			locator: source.finalUrl,
			interpretation: "explicit",
			confidence: "low",
			notes: "Deterministic fallback because no model evidence was returned.",
			extractedAt: nowIso(),
		});
	}
	return items;
}

function sanitizeClaims(rawClaims, evidenceItems) {
	const evidenceById = new Map(evidenceItems.map((item) => [item.evidenceId, item]));
	const claims = [];
	const rawRows = Array.isArray(rawClaims?.claims) ? rawClaims.claims : [];
	let index = 1;
	for (const row of rawRows) {
		if (typeof row !== "object" || row === null) continue;
		const text = typeof row.text === "string" ? row.text.trim() : "";
		const evidenceIds = asArray(row.evidence_ids).filter((id) => typeof id === "string" && evidenceById.has(id));
		const sourceIds = new Set(asArray(row.source_ids).filter((id) => typeof id === "string"));
		for (const evidenceId of evidenceIds) sourceIds.add(evidenceById.get(evidenceId).sourceId);
		if (!text || evidenceIds.length === 0 || sourceIds.size === 0) continue;
		claims.push({
			claimId: nextId("cl", index++),
			text,
			evidenceIds,
			sourceIds: [...sourceIds],
			confidence: ["high", "medium", "low"].includes(row.confidence) ? row.confidence : "low",
			notes: typeof row.notes === "string" && row.notes.trim() ? row.notes.trim() : null,
			createdAt: nowIso(),
		});
	}
	if (claims.length === 0) {
		for (const item of evidenceItems.slice(0, 25)) {
			claims.push({
				claimId: nextId("cl", index++),
				text: item.text,
				evidenceIds: [item.evidenceId],
				sourceIds: [item.sourceId],
				confidence: item.confidence,
				notes: "Deterministic fallback claim copied from evidence.",
				createdAt: nowIso(),
			});
		}
	}
	const gaps = [];
	const rawGaps = Array.isArray(rawClaims?.gaps) ? rawClaims.gaps : [];
	let gapIndex = 1;
	for (const row of rawGaps) {
		if (typeof row !== "object" || row === null) continue;
		const text = typeof row.text === "string" ? row.text.trim() : "";
		if (!text) continue;
		gaps.push({
			gapId: nextId("gap", gapIndex++),
			text,
			reason: typeof row.reason === "string" && row.reason.trim() ? row.reason.trim() : null,
			sourceIds: asArray(row.source_ids).filter((id) => typeof id === "string"),
			createdAt: nowIso(),
		});
	}
	return { claims, gaps };
}

function sourceTextPath(runDirectory, sourceId) {
	return join(runDirectory, "downloads", `${sourceId}.txt`);
}

function writeDeepSource(runDirectory, source) {
	mkdirSync(join(runDirectory, "downloads"), { recursive: true });
	const outputPath = sourceTextPath(runDirectory, source.sourceId);
	writeFileSync(outputPath, source.text, { flag: "wx" });
	const hash = sha256(source.text);
	return {
		filename: basename(outputPath),
		outputPath: `downloads/${basename(outputPath)}`,
		sha256: hash,
		byteSize: Buffer.byteLength(source.text),
		resourceId: `sha256:${hash}`,
	};
}

function deepManifestRows(sources) {
	return sources.map((source) => ({
		resource_id: source.resourceId ?? "",
		source_url: source.sourceUrl,
		final_url: source.finalUrl ?? "",
		access_date: source.accessDate,
		status: source.status,
		http_status: source.httpStatus ?? "",
		content_type: source.contentType ?? "text/plain; charset=utf-8",
		title: source.title ?? "",
		filename: source.filename ?? "",
		output_path: source.outputPath ?? "",
		sha256: source.sha256 ?? "",
		byte_size: source.byteSize ?? "",
		capture_method: source.extractionMethod ?? "deep-research",
		rendered: source.extractionMethod === "playwright",
		duplicate_of: source.duplicateOf ?? "",
		error: (source.warnings ?? []).join("; "),
	}));
}

function writeDeepArtifacts(runDirectory, state) {
	writeJson(join(runDirectory, "research_run.json"), {
		schemaVersion: DEEP_SCHEMA_VERSION,
		question: state.question,
		startedAt: state.startedAt,
		completedAt: nowIso(),
		options: state.options,
		seedQueries: state.seedQueries,
		counts: {
			queries: state.queryLog.length,
			sources: state.sources.length,
			evidence: state.evidenceItems.length,
			claims: state.claims.length,
			gaps: state.gaps.length,
			modelCalls: state.modelCalls.length,
		},
	});
	writeJsonl(join(runDirectory, "query_log.jsonl"), state.queryLog);
	writeJson(join(runDirectory, "source_index.json"), {
		schemaVersion: DEEP_SCHEMA_VERSION,
		sources: state.sources.map(({ text, ...source }) => source),
	});
	writeJsonl(join(runDirectory, "evidence_items.jsonl"), state.evidenceItems);
	writeJsonl(join(runDirectory, "claim_register.jsonl"), state.claims);
	writeJsonl(join(runDirectory, "gap_log.jsonl"), state.gaps);
	writeJsonl(join(runDirectory, "model_calls.jsonl"), state.modelCalls);
	writeCsv(join(runDirectory, "web_manifest.csv"), DEEP_MANIFEST_COLUMNS, deepManifestRows(state.sources));
	writeJson(join(runDirectory, "web_manifest.json"), {
		schemaVersion: 1,
		generatedAt: nowIso(),
		command: "deep",
		options: state.options,
		resources: state.sources.map(({ text, ...source }) => ({
			resourceId: source.resourceId,
			sourceUrl: source.sourceUrl,
			finalUrl: source.finalUrl,
			accessDate: source.accessDate,
			status: source.status,
			httpStatus: source.httpStatus,
			contentType: source.contentType,
			title: source.title,
			filename: source.filename,
			outputPath: source.outputPath,
			sha256: source.sha256,
			byteSize: source.byteSize,
			rendered: source.extractionMethod === "playwright",
			redirectChain: source.redirectChain ?? [],
			warnings: source.warnings ?? [],
			source: "deep-research",
			searchOrigins: source.searchOrigins,
			readability: source.metadata ?? {},
		})),
	});
	writeFileSync(join(runDirectory, "sources.md"), buildSourcesMarkdown(state));
	writeFileSync(join(runDirectory, "deep_research_report.md"), buildDeepReport(state));
}

function buildSourcesMarkdown(state) {
	const lines = ["# Sources", "", "Generated from `source_index.json`. Cite sources by `sourceId`.", ""];
	lines.push("| Source ID | Title | URL | Accessed | Status | SHA-256 |");
	lines.push("|---|---|---|---|---|---|");
	for (const source of state.sources) {
		lines.push(
			`| \`${source.sourceId}\` | ${source.title || "Untitled"} | ${source.finalUrl || source.sourceUrl} | ${source.accessDate} | ${source.status} | ${source.sha256 || ""} |`,
		);
	}
	return `${lines.join("\n")}\n`;
}

function buildDeepReport(state) {
	const sourceById = new Map(state.sources.map((source) => [source.sourceId, source]));
	const evidenceById = new Map(state.evidenceItems.map((item) => [item.evidenceId, item]));
	const lines = ["# Deep Research Report", "", `**Question**: ${state.question}`, `**Generated**: ${nowIso()}`, ""];
	lines.push("## Findings", "");
	if (state.claims.length === 0) lines.push("- No source-backed claims were validated.");
	for (const claim of state.claims) {
		lines.push(`### ${claim.claimId}`);
		lines.push("");
		lines.push(claim.text);
		lines.push("");
		lines.push(`- Confidence: ${claim.confidence}`);
		lines.push(`- Sources: ${claim.sourceIds.map((id) => `\`${id}\``).join(", ")}`);
		lines.push(`- Evidence: ${claim.evidenceIds.map((id) => `\`${id}\``).join(", ")}`);
		if (claim.notes) lines.push(`- Notes: ${claim.notes}`);
		for (const evidenceId of claim.evidenceIds) {
			const evidence = evidenceById.get(evidenceId);
			const source = evidence ? sourceById.get(evidence.sourceId) : null;
			if (!evidence || !source) continue;
			lines.push(`- ${evidenceId} from ${evidence.sourceId}: ${evidence.directQuote ? `"${evidence.directQuote}"` : evidence.text}`);
			lines.push(`  ${source.finalUrl || source.sourceUrl}`);
		}
		lines.push("");
	}
	lines.push("## Gaps and Limits", "");
	if (state.gaps.length === 0) lines.push("- No model-identified gaps were recorded.");
	for (const gap of state.gaps) {
		const sources = gap.sourceIds?.length ? ` Sources: ${gap.sourceIds.map((id) => `\`${id}\``).join(", ")}.` : "";
		lines.push(`- \`${gap.gapId}\` ${gap.text}${gap.reason ? ` Reason: ${gap.reason}.` : ""}${sources}`);
	}
	lines.push("");
	lines.push("## Query Log", "");
	for (const entry of state.queryLog) {
		lines.push(`- Iteration ${entry.iteration}: ${entry.query} (${entry.results.length} results)`);
	}
	lines.push("");
	lines.push("## Source Register", "");
	for (const source of state.sources) {
		lines.push(`- \`${source.sourceId}\` ${source.title || "Untitled"} - ${source.finalUrl || source.sourceUrl}`);
	}
	return `${lines.join("\n")}\n`;
}

function validateDeepRun(runDirectory, options = {}) {
	const errors = [];
	const warnings = [];
	const required = [
		"research_run.json",
		"query_log.jsonl",
		"source_index.json",
		"evidence_items.jsonl",
		"claim_register.jsonl",
		"gap_log.jsonl",
		"model_calls.jsonl",
		"deep_research_report.md",
		"sources.md",
		"web_manifest.csv",
		"web_manifest.json",
	];
	for (const name of required) {
		if (!existsSync(join(runDirectory, name))) errors.push(`${name} is missing`);
	}
	let sourceIndex = { sources: [] };
	let evidenceItems = [];
	let claims = [];
	let report = "";
	try {
		if (existsSync(join(runDirectory, "source_index.json"))) sourceIndex = JSON.parse(readFileSync(join(runDirectory, "source_index.json"), "utf8"));
		if (existsSync(join(runDirectory, "evidence_items.jsonl"))) evidenceItems = readJsonl(join(runDirectory, "evidence_items.jsonl"));
		if (existsSync(join(runDirectory, "claim_register.jsonl"))) claims = readJsonl(join(runDirectory, "claim_register.jsonl"));
		if (existsSync(join(runDirectory, "deep_research_report.md"))) report = readFileSync(join(runDirectory, "deep_research_report.md"), "utf8");
	} catch (error) {
		errors.push(`could not parse deep research artifacts: ${error instanceof Error ? error.message : String(error)}`);
	}
	const sources = Array.isArray(sourceIndex.sources) ? sourceIndex.sources : [];
	const sourceById = new Map(sources.map((source) => [source.sourceId, source]));
	const sourceTexts = new Map();
	for (const source of sources) {
		if (!source.sourceId) errors.push("source is missing sourceId");
		if (!source.sourceUrl) errors.push(`${source.sourceId ?? "unknown source"} is missing sourceUrl`);
		if (source.status === "failed") continue;
		if (!source.outputPath) {
			errors.push(`${source.sourceId} is missing outputPath`);
			continue;
		}
		const outputPath = resolve(runDirectory, source.outputPath);
		if (!outputPath.startsWith(`${resolve(runDirectory)}${sep}`)) {
			errors.push(`${source.sourceId} output path escapes run directory: ${source.outputPath}`);
			continue;
		}
		if (!existsSync(outputPath)) {
			errors.push(`${source.sourceId} output path is missing: ${source.outputPath}`);
			continue;
		}
		const text = readFileSync(outputPath, "utf8");
		sourceTexts.set(source.sourceId, text);
		const hash = sha256(text);
		if (source.sha256 && source.sha256 !== hash) errors.push(`${source.sourceId} SHA-256 does not match archived text`);
		if (source.resourceId && source.resourceId !== `sha256:${hash}`) errors.push(`${source.sourceId} resourceId does not match archived text`);
	}
	const evidenceById = new Map();
	for (const item of evidenceItems) {
		evidenceById.set(item.evidenceId, item);
		if (!item.evidenceId) errors.push("evidence item is missing evidenceId");
		if (!item.sourceId || !sourceById.has(item.sourceId)) errors.push(`${item.evidenceId ?? "unknown evidence"} references missing sourceId`);
		if (!item.text) errors.push(`${item.evidenceId ?? "unknown evidence"} is missing text`);
		if (item.directQuote && !includesQuote(sourceTexts.get(item.sourceId) ?? "", item.directQuote)) {
			errors.push(`${item.evidenceId} direct quote was not found in archived source text`);
		}
	}
	for (const claim of claims) {
		if (!claim.claimId) errors.push("claim is missing claimId");
		if (!claim.text) errors.push(`${claim.claimId ?? "unknown claim"} is missing text`);
		if (!Array.isArray(claim.sourceIds) || claim.sourceIds.length === 0) errors.push(`${claim.claimId} has no sourceIds`);
		if (!Array.isArray(claim.evidenceIds) || claim.evidenceIds.length === 0) errors.push(`${claim.claimId} has no evidenceIds`);
		for (const sourceId of claim.sourceIds ?? []) {
			if (!sourceById.has(sourceId)) errors.push(`${claim.claimId} references missing source ${sourceId}`);
			if (!report.includes(sourceId)) errors.push(`deep_research_report.md does not cite source ${sourceId} for ${claim.claimId}`);
		}
		for (const evidenceId of claim.evidenceIds ?? []) {
			const evidence = evidenceById.get(evidenceId);
			if (!evidence) {
				errors.push(`${claim.claimId} references missing evidence ${evidenceId}`);
				continue;
			}
			if (!claim.sourceIds?.includes(evidence.sourceId)) errors.push(`${claim.claimId} does not include source ${evidence.sourceId} for ${evidenceId}`);
			if (!report.includes(evidenceId)) errors.push(`deep_research_report.md does not cite evidence ${evidenceId} for ${claim.claimId}`);
		}
		if (claim.claimId && !report.includes(claim.claimId)) errors.push(`deep_research_report.md does not cite claim ${claim.claimId}`);
	}
	if (existsSync(join(runDirectory, "web_manifest.csv"))) {
		const rows = parseCsv(readFileSync(join(runDirectory, "web_manifest.csv"), "utf8"));
		const headers = rows.shift() ?? [];
		if (headers.join(",") !== DEEP_MANIFEST_COLUMNS.join(",")) errors.push("web_manifest.csv columns do not match the required contract");
	}
	const result = { valid: errors.length === 0, errors, warnings };
	writeJson(join(runDirectory, "validation_report.json"), result);
	if (options.emit !== false) process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
	if (errors.length > 0 && options.exitOnError) process.exit(1);
	return result;
}

// --- Commands ---------------------------------------------------------------

async function commandDoctor(options) {
	const playwright = await loadPlaywright();
	let chromiumPath = null;
	let chromiumAvailable = false;
	if (playwright) {
		try {
			chromiumPath = playwright.chromium.executablePath();
			chromiumAvailable = chromiumPath !== null && existsSync(chromiumPath);
		} catch {
			chromiumAvailable = false;
		}
	}
	const searxng = await pingSearxng(searxngBase(options.searxng), DEFAULT_USER_AGENT, 5000);
	const tools = {
		fetch: { available: typeof fetch === "function", version: process.version },
		playwright: { available: Boolean(playwright), version: playwright ? "importable" : null },
		chromium: { available: chromiumAvailable, version: chromiumAvailable ? chromiumPath : null },
	};
	const capabilities = {
		search: searxng.configured && searxng.reachable,
		extraction: tools.playwright.available && tools.chromium.available,
		httpFallback: tools.fetch.available,
	};
	const remediation = [];
	if (!searxng.configured) remediation.push("Set FORGE_SEARXNG_URL or --searxng to enable search.");
	else if (!searxng.reachable) remediation.push(`SearXNG unreachable: ${searxng.detail}`);
	if (!tools.playwright.available) remediation.push("Install Playwright for rendered page extraction.");
	if (tools.playwright.available && !tools.chromium.available) {
		remediation.push("Install Chromium: node_modules/.bin/playwright install chromium.");
	}
	const report = { tools, capabilities, searxng, remediation };
	if (options.json) {
		process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
		return;
	}
	for (const [name, info] of Object.entries(tools)) {
		process.stdout.write(`${name}: ${info.available ? info.version || "available" : "missing"}\n`);
	}
	process.stdout.write(`Search: ${capabilities.search ? "available" : `unavailable (${searxng.detail})`}
`);
	process.stdout.write(`Page extraction: ${capabilities.extraction ? "available (Playwright)" : capabilities.httpFallback ? "available (HTTP fallback)" : "unavailable"}
`);
	process.stdout.write(`SearXNG URL: ${searxngBase(options.searxng)}
`);
	for (const item of remediation) process.stdout.write(`Action: ${item}\n`);
}

async function commandSearch(positionals, flags) {
	if (positionals.length === 0) fail("search requires a query");
	if (!flags.output) fail("search requires --output <new-directory>");
	const query = positionals.join(" ");
	const base = searxngBase(flags.searxng);
	if (!base) fail("search requires a SearXNG instance; set FORGE_SEARXNG_URL or pass --searxng <url>");

	const autoParams = autoSelectParams(query);
	const searchParams = {
		userAgent: flags.userAgent ?? DEFAULT_USER_AGENT,
		timeoutMs: flags.timeoutMs ?? DEFAULT_TIMEOUT_MS,
		categories: flags.categories ?? autoParams.categories,
		engines: flags.engines ?? autoParams.engines,
		language: flags.language ?? autoParams.language,
		safesearch: flags.safesearch ?? autoParams.safesearch,
		timeRange: flags.timeRange ?? autoParams.timeRange,
		pageNo: flags.pageNo ?? autoParams.pageNo,
	};

	let payload;
	try {
		payload = await searchSearxng(base, query, searchParams);
	} catch (error) {
		fail(error.message);
	}

	const limit = flags.limit ?? DEFAULT_LIMIT;
	const results = (Array.isArray(payload.results) ? payload.results : [])
		.slice(0, limit)
		.map((result, index) => ({
			rank: index + 1,
			title: result.title ?? null,
			url: result.url ?? null,
			content: result.content ?? null,
			engine: result.engine ?? null,
			score: result.score ?? null,
		}));

	const runDirectory = resolve(flags.output);
	if (existsSync(runDirectory)) fail(`output directory already exists: ${runDirectory}`);
	mkdirSync(runDirectory, { recursive: true });

	const data = {
		query,
		searchBase: base,
		params: searchParams,
		retrievedAt: nowIso(),
		results,
		readings: [],
	};
	writeJson(join(runDirectory, "research_report.json"), data);

	const report = buildReport(data);
	writeFileSync(join(runDirectory, "research_report.md"), report);

	process.stdout.write(
		`${JSON.stringify({ runDirectory, query, results: results.length, params: searchParams }, null, 2)}\n`,
	);
}

async function commandRead(positionals, flags) {
	if (positionals.length === 0) fail("read requires at least one URL");
	if (!flags.output) fail("read requires --output <new-directory>");
	const urls = [...positionals];
	if (flags.inputFile) {
		const list = readFileSync(resolve(flags.inputFile), "utf8")
			.split(/\r?\n/)
			.map((line) => line.trim())
			.filter((line) => line && !line.startsWith("#"));
		urls.push(...list);
	}

	const options = {
		userAgent: flags.userAgent ?? DEFAULT_USER_AGENT,
		timeoutMs: flags.timeoutMs ?? DEFAULT_TIMEOUT_MS,
		render: flags.render !== false, // default to true
		delayMs: flags.delayMs ?? 500,
	};

	const runDirectory = resolve(flags.output);
	if (existsSync(runDirectory)) fail(`output directory already exists: ${runDirectory}`);
	mkdirSync(runDirectory, { recursive: true });

	const readings = [];
	for (const [index, url] of urls.entries()) {
		if (index > 0 && options.delayMs > 0) await sleep(options.delayMs);
		try {
			process.stderr.write(`Reading ${url}...\n`);
			const reading = await readPage(url, options);
			readings.push(reading);
		} catch (error) {
			readings.push({
				url,
				title: null,
				text: "",
				charCount: 0,
				extractionMethod: "failed",
				warnings: [error.message],
				extractedAt: nowIso(),
			});
		}
	}

	const data = {
		query: null,
		searchBase: null,
		params: null,
		retrievedAt: nowIso(),
		results: [],
		readings,
	};
	writeJson(join(runDirectory, "research_report.json"), data);

	const report = buildReport(data);
	writeFileSync(join(runDirectory, "research_report.md"), report);

	const successCount = readings.filter((r) => r.extractionMethod !== "failed").length;
	process.stdout.write(
		`${JSON.stringify({ runDirectory, urls: urls.length, success: successCount, readings: readings.length }, null, 2)}\n`,
	);
}

async function commandResearch(positionals, flags) {
	if (positionals.length === 0) fail("research requires a query");
	if (!flags.output) fail("research requires --output <new-directory>");
	const query = positionals.join(" ");
	const base = searxngBase(flags.searxng);
	if (!base) fail("research requires a SearXNG instance; set FORGE_SEARXNG_URL or pass --searxng <url>");

	const autoParams = autoSelectParams(query);
	const searchParams = {
		userAgent: flags.userAgent ?? DEFAULT_USER_AGENT,
		timeoutMs: flags.timeoutMs ?? DEFAULT_TIMEOUT_MS,
		categories: flags.categories ?? autoParams.categories,
		engines: flags.engines ?? autoParams.engines,
		language: flags.language ?? autoParams.language,
		safesearch: flags.safesearch ?? autoParams.safesearch,
		timeRange: flags.timeRange ?? autoParams.timeRange,
		pageNo: flags.pageNo ?? autoParams.pageNo,
	};

	// Step 1: Search
	let payload;
	try {
		payload = await searchSearxng(base, query, searchParams);
	} catch (error) {
		fail(error.message);
	}

	const limit = flags.limit ?? DEFAULT_LIMIT;
	const results = (Array.isArray(payload.results) ? payload.results : [])
		.slice(0, limit)
		.map((result, index) => ({
			rank: index + 1,
			title: result.title ?? null,
			url: result.url ?? null,
			content: result.content ?? null,
			engine: result.engine ?? null,
			score: result.score ?? null,
		}));

	// Step 2: Read top N results
	const readCount = flags.readCount ?? DEFAULT_READ_COUNT;
	const urlsToRead = results.slice(0, readCount).map((r) => r.url).filter(Boolean);

	const readOptions = {
		userAgent: flags.userAgent ?? DEFAULT_USER_AGENT,
		timeoutMs: flags.timeoutMs ?? DEFAULT_TIMEOUT_MS,
		render: flags.render !== false,
		delayMs: flags.delayMs ?? 500,
	};

	const readings = [];
	for (const [index, url] of urlsToRead.entries()) {
		if (index > 0 && readOptions.delayMs > 0) await sleep(readOptions.delayMs);
		try {
			process.stderr.write(`Reading ${url}...\n`);
			const reading = await readPage(url, readOptions);
			readings.push(reading);
		} catch (error) {
			readings.push({
				url,
				title: null,
				text: "",
				charCount: 0,
				extractionMethod: "failed",
				warnings: [error.message],
				extractedAt: nowIso(),
			});
		}
	}

	// Step 3: Write report
	const runDirectory = resolve(flags.output);
	if (existsSync(runDirectory)) fail(`output directory already exists: ${runDirectory}`);
	mkdirSync(runDirectory, { recursive: true });

	const data = {
		query,
		searchBase: base,
		params: searchParams,
		retrievedAt: nowIso(),
		results,
		readings,
	};
	writeJson(join(runDirectory, "research_report.json"), data);

	const report = buildReport(data);
	writeFileSync(join(runDirectory, "research_report.md"), report);

	const successCount = readings.filter((r) => r.extractionMethod !== "failed").length;
	process.stdout.write(
		`${JSON.stringify({ runDirectory, query, results: results.length, read: readings.length, success: successCount, params: searchParams }, null, 2)}\n`,
	);
}

async function commandDeep(positionals, flags) {
	if (!flags.output) fail("deep requires --output <new-directory>");
	const positionalQuestion = positionals.join(" ").trim();
	const explicitQueries = asArray(flags.query).map((query) => String(query).trim()).filter(Boolean);
	const fileQueries = flags.queryFile ? readQueryFile(flags.queryFile) : [];
	const seedQueries = [...explicitQueries, ...fileQueries];
	if (positionalQuestion) seedQueries.unshift(positionalQuestion);
	const uniqueSeedQueries = [...new Map(seedQueries.map((query) => [query.toLowerCase(), query])).values()];
	if (uniqueSeedQueries.length === 0) fail("deep requires a query, --query, or --query-file");
	const question = flags.question || positionalQuestion || uniqueSeedQueries.join("; ");
	const base = searxngBase(flags.searxng);
	if (!base) fail("deep requires a SearXNG instance; set FORGE_SEARXNG_URL or pass --searxng <url>");
	const runDirectory = resolve(flags.output);
	if (existsSync(runDirectory)) fail(`output directory already exists: ${runDirectory}`);
	mkdirSync(runDirectory, { recursive: true });

	const options = deepDefaults(flags);
	const state = {
		question,
		startedAt: nowIso(),
		options: { ...options, searxng: base },
		seedQueries: uniqueSeedQueries,
		queryLog: [],
		sources: [],
		evidenceItems: [],
		claims: [],
		gaps: [],
		modelCalls: [],
	};
	const seenQueries = new Set();
	const queuedQueries = [...uniqueSeedQueries];
	const seenUrls = new Map();

	for (let iteration = 1; iteration <= options.maxIterations; iteration += 1) {
		const iterationQueries = [];
		while (queuedQueries.length > 0) {
			const query = queuedQueries.shift();
			const key = query.toLowerCase();
			if (seenQueries.has(key)) continue;
			seenQueries.add(key);
			iterationQueries.push(query);
		}
		if (iterationQueries.length === 0) break;

		for (const query of iterationQueries) {
			const searchParams = searchParamsForQuery(query, flags, options);
			let results = [];
			let error = null;
			try {
				const payload = await searchSearxng(base, query, searchParams);
				results = (Array.isArray(payload.results) ? payload.results : [])
					.slice(0, options.limit)
					.map((result, index) => ({
						rank: index + 1,
						title: result.title ?? null,
						url: result.url ?? null,
						content: result.content ?? null,
						engine: result.engine ?? null,
						score: result.score ?? null,
					}));
			} catch (searchError) {
				error = searchError instanceof Error ? searchError.message : String(searchError);
			}
			state.queryLog.push({ iteration, query, params: searchParams, searchedAt: nowIso(), results, error });
			const urlsToRead = results.slice(0, options.readCount).filter((result) => result.url);
			for (const [index, result] of urlsToRead.entries()) {
				if (index > 0 && options.delayMs > 0) await sleep(options.delayMs);
				const normalized = normalizeUrl(result.url);
				if (seenUrls.has(normalized)) {
					const source = seenUrls.get(normalized);
					source.searchOrigins.push({ iteration, query, rank: result.rank, engine: result.engine, score: result.score });
					continue;
				}
				const source = {
					sourceId: sourceIdForUrl(result.url),
					sourceUrl: result.url,
					finalUrl: result.url,
					accessDate: nowIso(),
					status: "failed",
					httpStatus: null,
					contentType: "text/plain; charset=utf-8",
					title: result.title ?? null,
					filename: null,
					outputPath: null,
					sha256: null,
					byteSize: null,
					resourceId: null,
					extractionMethod: "failed",
					extractedAt: null,
					charCount: 0,
					metadata: {},
					searchOrigins: [{ iteration, query, rank: result.rank, engine: result.engine, score: result.score }],
					warnings: [],
					text: "",
				};
				seenUrls.set(normalized, source);
				state.sources.push(source);
				try {
					process.stderr.write(`Deep reading ${result.url}...\n`);
					const reading = await readPage(result.url, options);
					source.finalUrl = reading.url;
					source.title = reading.title || result.title || null;
					source.status = reading.text.trim() ? "success" : "needs_review";
					source.extractionMethod = reading.extractionMethod;
					source.extractedAt = reading.extractedAt;
					source.charCount = reading.charCount;
					source.metadata = reading.metadata ?? {};
					source.warnings = reading.warnings ?? [];
					source.text = reading.text;
					const archived = writeDeepSource(runDirectory, source);
					Object.assign(source, archived);
					const { value, record } = await callLocalJsonModel(runDirectory, "extract-evidence", evidencePrompt(source, question), {
						evidence: [],
					});
					state.modelCalls.push(record);
					state.evidenceItems.push(...sanitizeEvidence(value, source, state.evidenceItems.length + 1));
				} catch (readError) {
					source.warnings.push(readError instanceof Error ? readError.message : String(readError));
				}
			}
		}

		if (iteration < options.maxIterations) {
			const { value, record } = await callLocalJsonModel(
				runDirectory,
				"expand-queries",
				queryExpansionPrompt(question, [...seenQueries], state.evidenceItems, state.gaps, iteration + 1),
				{ queries: [] },
			);
			state.modelCalls.push(record);
			const followUps = Array.isArray(value?.queries) ? value.queries : [];
			for (const query of followUps) {
				if (typeof query !== "string" || !query.trim()) continue;
				const normalized = query.trim().toLowerCase();
				if (!seenQueries.has(normalized)) queuedQueries.push(query.trim());
			}
		}
	}

	const { value: claimValue, record: claimRecord } = await callLocalJsonModel(runDirectory, "register-claims", claimPrompt(question, state.evidenceItems), {
		claims: [],
		gaps: [],
	});
	state.modelCalls.push(claimRecord);
	const { claims, gaps } = sanitizeClaims(claimValue, state.evidenceItems);
	state.claims = claims;
	state.gaps = gaps;
	writeDeepArtifacts(runDirectory, state);
	const validation = validateDeepRun(runDirectory, { emit: false });
	process.stdout.write(
		`${JSON.stringify(
			{
				runDirectory,
				question,
				queries: state.queryLog.length,
				sources: state.sources.length,
				evidence: state.evidenceItems.length,
				claims: state.claims.length,
				gaps: state.gaps.length,
				valid: validation.valid,
				validationErrors: validation.errors,
			},
			null,
			2,
		)}\n`,
	);
	if (!validation.valid) process.exit(1);
}

// --- Argument parsing -------------------------------------------------------

const FLAG_SPECS = {
	"--output": { key: "output", value: true },
	"--input-file": { key: "inputFile", value: true },
	"--query-file": { key: "queryFile", value: true },
	"--query": { key: "query", value: true, repeat: true },
	"--question": { key: "question", value: true },
	"--user-agent": { key: "userAgent", value: true },
	"--searxng": { key: "searxng", value: true },
	"--limit": { key: "limit", value: true, integer: true },
	"--read-count": { key: "readCount", value: true, integer: true },
	"--max-iterations": { key: "maxIterations", value: true, integer: true },
	"--delay-ms": { key: "delayMs", value: true, integer: true },
	"--timeout-ms": { key: "timeoutMs", value: true, integer: true },
	"--categories": { key: "categories", value: true },
	"--engines": { key: "engines", value: true },
	"--language": { key: "language", value: true },
	"--safesearch": { key: "safesearch", value: true, integer: true },
	"--time-range": { key: "timeRange", value: true },
	"--pageno": { key: "pageNo", value: true, integer: true },
	"--render": { key: "render", value: false },
	"--no-render": { key: "noRender", value: false },
	"--json": { key: "json", value: false },
};

function parseArguments(args) {
	const positionals = [];
	const flags = {};
	for (let index = 0; index < args.length; index += 1) {
		const argument = args[index];
		if (argument.startsWith("--")) {
			const spec = FLAG_SPECS[argument];
			if (!spec) fail(`unknown option: ${argument}`);
			if (!spec.value) {
				flags[spec.key] = true;
				continue;
			}
			const raw = args[++index];
			if (raw === undefined) fail(`${argument} requires a value`);
			if (spec.integer) {
				const parsed = Number.parseInt(raw, 10);
				if (!Number.isInteger(parsed) || parsed < 0) fail(`${argument} requires a non-negative integer`);
				if (spec.repeat) flags[spec.key] = [...asArray(flags[spec.key]), parsed];
				else flags[spec.key] = parsed;
			} else {
				if (spec.repeat) flags[spec.key] = [...asArray(flags[spec.key]), raw];
				else flags[spec.key] = raw;
			}
		} else {
			positionals.push(argument);
		}
	}
	// Handle --no-render as render=false
	if (flags.noRender) flags.render = false;
	return { positionals, flags };
}

function usage() {
	process.stdout.write(`Usage:
  web-research.mjs doctor [--json] [--searxng <url>]
  web-research.mjs search <query...> --output <dir> [--searxng <url>] [--limit N]
      [--categories <cats>] [--engines <engines>] [--language <lang>]
      [--safesearch <0|1|2>] [--time-range <day|week|month|year>] [--pageno N]
  web-research.mjs read <url...> --output <dir> [--input-file <path>]
      [--render] [--no-render] [--delay-ms N] [--timeout-ms N]
  web-research.mjs research <query...> --output <dir> [--searxng <url>]
      [--limit N] [--read-count N] [--render] [--no-render] [--delay-ms N]
      [--categories <cats>] [--engines <engines>] [--language <lang>]
      [--safesearch <0|1|2>] [--time-range <day|week|month|year>] [--pageno N]
  web-research.mjs deep <query...> --output <dir> [--question <text>] [--query <query>] [--query-file <path>]
      [--max-iterations N] [--limit N] [--read-count N] [--render] [--no-render]
      [--categories <cats>] [--engines <engines>] [--language <lang>]
      [--safesearch <0|1|2>] [--time-range <day|week|month|year>] [--pageno N]
  web-research.mjs validate <run-directory>
`);
}

async function main() {
	const [command, ...rest] = process.argv.slice(2);
	if (!command || command === "--help" || command === "-h") {
		usage();
		process.exit(command ? 0 : 2);
	}
	const { positionals, flags } = parseArguments(rest);
	if (command === "doctor") await commandDoctor(flags);
	else if (command === "search") await commandSearch(positionals, flags);
	else if (command === "read") await commandRead(positionals, flags);
	else if (command === "research") await commandResearch(positionals, flags);
	else if (command === "deep") await commandDeep(positionals, flags);
	else if (command === "validate") {
		if (positionals.length !== 1) fail("validate requires exactly one run directory");
		validateDeepRun(resolve(positionals[0]), { exitOnError: true });
	}
	else fail(`unknown command: ${command}`, 2);
}

main().catch((error) => fail(error instanceof Error ? error.stack || error.message : String(error)));

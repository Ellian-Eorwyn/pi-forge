#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

const DEFAULT_USER_AGENT = "pi-forge-web-research/1 (+https://github.com/pi-forge)";
const DEFAULT_TIMEOUT_MS = 30_000;
const DEFAULT_SEARXNG_URL = "http://llms/searxng";
const DEFAULT_LIMIT = 10;
const DEFAULT_READ_COUNT = 5;

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

function safeStem(value) {
	const raw = String(value).normalize("NFKC").trim();
	const safe = raw.replace(/[^\p{L}\p{N}._-]+/gu, "-").replace(/^-+|-+$/g, "");
	return safe.slice(0, 80) || "research";
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

async function extractWithPlaywright(playwright, url, timeoutMs, userAgent) {
	const browser = await playwright.chromium.launch({ headless: true });
	let text = "";
	let title = null;
	let finalUrl = url;
	let warnings = [];
	try {
		const context = await browser.newContext({ userAgent });
		const page = await context.newPage();
		await page.goto(url, { waitUntil: "networkidle", timeout: timeoutMs }).catch(async (error) => {
			warnings.push(`networkidle wait failed (${error.message}); retried with domcontentloaded`);
			await page.goto(url, { waitUntil: "domcontentloaded", timeout: timeoutMs });
		});
		finalUrl = page.url();
		title = (await page.title()) || null;

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
	return { text, title, finalUrl, warnings };
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

	return { text, title, finalUrl: url, warnings: ["used HTTP extraction (no Playwright)"] };
}

async function readPage(url, options) {
	const playwright = await loadPlaywright();
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

// --- Argument parsing -------------------------------------------------------

const FLAG_SPECS = {
	"--output": { key: "output", value: true },
	"--input-file": { key: "inputFile", value: true },
	"--user-agent": { key: "userAgent", value: true },
	"--searxng": { key: "searxng", value: true },
	"--limit": { key: "limit", value: true, integer: true },
	"--read-count": { key: "readCount", value: true, integer: true },
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
				flags[spec.key] = parsed;
			} else {
				flags[spec.key] = raw;
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
	else fail(`unknown command: ${command}`, 2);
}

main().catch((error) => fail(error instanceof Error ? error.stack || error.message : String(error)));

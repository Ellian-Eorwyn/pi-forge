#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, lstatSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { basename, extname, join, resolve, sep } from "node:path";

const DEFAULT_USER_AGENT = "pi-forge-web-collection/1 (+https://github.com/pi-forge)";
const DEFAULT_DELAY_MS = 500;
const DEFAULT_TIMEOUT_MS = 30_000;
const DEFAULT_MAX_BYTES = 100 * 1024 * 1024;
const DEFAULT_SEARXNG_URL = "";
const MAX_REDIRECTS = 10;
const REDIRECT_STATUSES = new Set([301, 302, 303, 307, 308]);
const CAPTURE_ARTIFACTS = ["rendered.html", "snapshot.mhtml", "screenshot.png", "page.pdf"];
const MANIFEST_COLUMNS = [
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
const FAILED_COLUMNS = ["source_url", "status", "http_status", "reason", "access_date"];
const STATUSES = new Set(["success", "needs_review", "failed", "skipped"]);
const CONTENT_TYPE_EXTENSIONS = new Map([
	["text/html", "html"],
	["application/xhtml+xml", "html"],
	["application/pdf", "pdf"],
	["text/plain", "txt"],
	["text/markdown", "md"],
	["text/csv", "csv"],
	["application/json", "json"],
	["application/xml", "xml"],
	["text/xml", "xml"],
	["application/msword", "doc"],
	["application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"],
	["application/vnd.ms-excel", "xls"],
	["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"],
	["application/rtf", "rtf"],
	["text/rtf", "rtf"],
	["application/zip", "zip"],
	["image/png", "png"],
	["image/jpeg", "jpg"],
	["image/gif", "gif"],
	["image/webp", "webp"],
	["image/svg+xml", "svg"],
]);

function fail(message, exitCode = 1) {
	process.stderr.write(`Error: ${message}\n`);
	process.exit(exitCode);
}

function sha256(buffer) {
	return createHash("sha256").update(buffer).digest("hex");
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
	const combined = `${result.stdout}\n${result.stderr}`.trim();
	return { available: true, version: combined.split(/\r?\n/, 1)[0] || "available" };
}

function safeStem(value) {
	const raw = String(value).normalize("NFKC").trim();
	const safe = raw.replace(/[^\p{L}\p{N}._-]+/gu, "-").replace(/^-+|-+$/g, "");
	return safe.slice(0, 80) || "resource";
}

function stemFromUrl(url) {
	try {
		const parsed = new URL(url);
		const last = parsed.pathname.split("/").filter(Boolean).at(-1);
		const base = last ? basename(last, extname(last)) : parsed.hostname;
		return safeStem(`${parsed.hostname}-${base}`);
	} catch {
		return safeStem(url);
	}
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
	if (host === "localhost" || host.endsWith(".localhost")) return true;
	if (host === "127.0.0.1" || host.startsWith("127.")) return true;
	if (host === "::1" || host === "0.0.0.0") return true;
	if (host === "169.254.169.254" || host.startsWith("169.254.")) return true;
	if (host === "metadata" || host === "metadata.google.internal") return true;
	return false;
}

function assertCollectableUrl(url) {
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

function extensionFor(contentType, finalUrl, dispositionName) {
	if (dispositionName) {
		const extension = extname(dispositionName).slice(1).toLowerCase();
		if (extension) return extension;
	}
	try {
		const extension = extname(new URL(finalUrl).pathname).slice(1).toLowerCase();
		if (extension && /^[a-z0-9]{1,8}$/.test(extension)) return extension;
	} catch {
		// fall through to content-type
	}
	const base = (contentType || "").split(";", 1)[0].trim().toLowerCase();
	return CONTENT_TYPE_EXTENSIONS.get(base) || "bin";
}

function parseContentDisposition(value) {
	if (!value) return null;
	const star = value.match(/filename\*=(?:UTF-8'')?([^;]+)/i);
	if (star) {
		try {
			return decodeURIComponent(star[1].replace(/^"|"$/g, ""));
		} catch {
			// fall through to plain filename
		}
	}
	const plain = value.match(/filename="?([^";]+)"?/i);
	return plain ? plain[1].trim() : null;
}

function htmlTitle(html) {
	const match = html.match(/<title[^>]*>([\s\S]*?)<\/title>/i);
	if (!match) return null;
	return match[1].replace(/\s+/g, " ").trim() || null;
}

function extractLinks(html, baseUrl) {
	const links = new Set();
	const attributePattern = /(?:href|src)\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+))/gi;
	let match = attributePattern.exec(html);
	while (match !== null) {
		const raw = (match[2] ?? match[3] ?? match[4] ?? "").trim();
		match = attributePattern.exec(html);
		if (!raw || raw.startsWith("#") || /^(?:javascript|mailto|tel|data):/i.test(raw)) continue;
		try {
			const absolute = new URL(raw, baseUrl);
			if (absolute.protocol === "http:" || absolute.protocol === "https:") {
				absolute.hash = "";
				links.add(absolute.toString());
			}
		} catch {
			// ignore unparseable links
		}
	}
	return [...links];
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

async function fetchWithRedirects(url, options) {
	const chain = [];
	const visited = new Set();
	let current = url;
	for (let hop = 0; hop <= MAX_REDIRECTS; hop += 1) {
		assertCollectableUrl(current);
		const controller = new AbortController();
		const timer = setTimeout(() => controller.abort(), options.timeoutMs);
		let response;
		try {
			response = await fetch(current, {
				redirect: "manual",
				signal: controller.signal,
				headers: { "user-agent": options.userAgent, accept: "*/*" },
			});
		} catch (error) {
			throw new Error(error.name === "AbortError" ? `request timed out after ${options.timeoutMs}ms` : error.message);
		} finally {
			clearTimeout(timer);
		}
		if (REDIRECT_STATUSES.has(response.status) && response.headers.get("location")) {
			const next = new URL(response.headers.get("location"), current).toString();
			chain.push({ from: current, to: next, status: response.status });
			if (visited.has(normalizeUrl(next))) throw new Error(`redirect loop detected at ${next}`);
			visited.add(normalizeUrl(current));
			current = next;
			continue;
		}
		return { response, finalUrl: current, chain };
	}
	throw new Error(`exceeded ${MAX_REDIRECTS} redirects`);
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

function writeJson(filePath, value) {
	writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`);
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

// --- Capability detection -------------------------------------------------

async function loadPlaywright() {
	try {
		const module = await import("playwright");
		return module.chromium ? module : null;
	} catch {
		return null;
	}
}

function searxngBase(explicit) {
	const base = explicit || process.env.FORGE_SEARXNG_URL || DEFAULT_SEARXNG_URL;
	return base.trim().replace(/\/+$/, "");
}

async function pingSearxng(base, userAgent, timeoutMs) {
	if (!base) return { configured: false, reachable: false, detail: "FORGE_SEARXNG_URL is not set" };
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

async function doctor(options) {
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
		curl: run("curl"),
		wget: run("wget"),
		playwright: { available: Boolean(playwright), version: playwright ? "importable" : null },
		chromium: { available: chromiumAvailable, version: chromiumAvailable ? chromiumPath : null },
	};
	const capabilities = {
		httpCollect: tools.fetch.available,
		renderedCapture: tools.playwright.available && tools.chromium.available,
		searxngSearch: searxng.configured && searxng.reachable,
	};
	const remediation = [];
	if (!tools.fetch.available) remediation.push("Node 22.19+ with global fetch is required.");
	if (!tools.playwright.available) remediation.push("Install Playwright (bundled with pi-forge; run pi-forge-update to refresh the installed package).");
	if (tools.playwright.available && !tools.chromium.available) {
		remediation.push("Install the Chromium browser: node_modules/.bin/playwright install chromium.");
	}
	if (!searxng.configured) remediation.push("Set FORGE_SEARXNG_URL or --searxng to enable search.");
	else if (!searxng.reachable) remediation.push(`SearXNG is configured but not reachable: ${searxng.detail}`);
	const report = { tools, capabilities, searxng, remediation };
	if (options.json) {
		process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
		return;
	}
	for (const [name, info] of Object.entries(tools)) {
		process.stdout.write(`${name}: ${info.available ? info.version || "available" : "missing"}\n`);
	}
	process.stdout.write(`HTTP collection: ${capabilities.httpCollect ? "available" : "unavailable"}\n`);
	process.stdout.write(`Rendered capture: ${capabilities.renderedCapture ? "available" : "unavailable"}\n`);
	process.stdout.write(`SearXNG search: ${capabilities.searxngSearch ? "available" : `unavailable (${searxng.detail})`}\n`);
	process.stdout.write(`SearXNG URL: ${searxngBase(options.searxng)}\n`);
	for (const item of remediation) process.stdout.write(`Action: ${item}\n`);
}

// --- robots.txt -----------------------------------------------------------

async function fetchRobots(origin, userAgent, timeoutMs) {
	const controller = new AbortController();
	const timer = setTimeout(() => controller.abort(), timeoutMs);
	try {
		const response = await fetch(`${origin}/robots.txt`, {
			signal: controller.signal,
			headers: { "user-agent": userAgent },
		});
		if (!response.ok) return [];
		return parseRobotsDisallows(await response.text());
	} catch {
		return [];
	} finally {
		clearTimeout(timer);
	}
}

function parseRobotsDisallows(text) {
	const disallows = [];
	let appliesToAll = false;
	for (const rawLine of text.split(/\r?\n/)) {
		const line = rawLine.replace(/#.*$/, "").trim();
		if (!line) continue;
		const [field, ...rest] = line.split(":");
		const key = field.trim().toLowerCase();
		const valuePart = rest.join(":").trim();
		if (key === "user-agent") appliesToAll = valuePart === "*";
		else if (key === "disallow" && appliesToAll && valuePart) disallows.push(valuePart);
	}
	return disallows;
}

function robotsBlocks(disallows, pathname) {
	return disallows.some((rule) => pathname.startsWith(rule));
}

// --- Collection -----------------------------------------------------------

function newRunState() {
	return {
		rows: [],
		records: [],
		bySha: new Map(),
		byUrl: new Map(),
		byTitle: new Map(),
		usedFilenames: new Set(),
	};
}

function duplicateOf(state, normalized, hash, title) {
	if (state.byUrl.has(normalized)) return { id: state.byUrl.get(normalized), key: "url" };
	if (hash && state.bySha.has(hash)) return { id: state.bySha.get(hash), key: "checksum" };
	if (title && state.byTitle.has(title)) return { id: state.byTitle.get(title), key: "title" };
	return null;
}

function uniqueFilename(state, stem, extension) {
	let candidate = `${stem}.${extension}`;
	let counter = 2;
	while (state.usedFilenames.has(candidate)) {
		candidate = `${stem}-${counter}.${extension}`;
		counter += 1;
	}
	state.usedFilenames.add(candidate);
	return candidate;
}

async function capturePage(playwright, url, captureDir, options) {
	mkdirSync(captureDir, { recursive: true });
	const browser = await playwright.chromium.launch({ headless: true });
	const result = { artifacts: [], title: null, finalUrl: url, consoleErrors: 0, warnings: [] };
	try {
		const context = await browser.newContext({ userAgent: options.userAgent });
		const page = await context.newPage();
		page.on("console", (message) => {
			if (message.type() === "error") result.consoleErrors += 1;
		});
		const response = await page.goto(url, { waitUntil: "networkidle", timeout: options.timeoutMs }).catch(async (error) => {
			result.warnings.push(`networkidle wait failed (${error.message}); retried with domcontentloaded`);
			return page.goto(url, { waitUntil: "domcontentloaded", timeout: options.timeoutMs });
		});
		result.finalUrl = page.url();
		result.title = (await page.title()) || null;
		const html = await page.content();
		writeFileSync(join(captureDir, "rendered.html"), html);
		result.artifacts.push("rendered.html");
		const session = await context.newCDPSession(page);
		try {
			const snapshot = await session.send("Page.captureSnapshot", { format: "mhtml" });
			writeFileSync(join(captureDir, "snapshot.mhtml"), snapshot.data);
			result.artifacts.push("snapshot.mhtml");
		} catch (error) {
			result.warnings.push(`MHTML snapshot failed: ${error.message}`);
		}
		await page.screenshot({ path: join(captureDir, "screenshot.png"), fullPage: true });
		result.artifacts.push("screenshot.png");
		try {
			await page.pdf({ path: join(captureDir, "page.pdf"), printBackground: true });
			result.artifacts.push("page.pdf");
		} catch (error) {
			result.warnings.push(`PDF capture failed: ${error.message}`);
		}
		result.httpStatus = response?.status() ?? null;
		writeJson(join(captureDir, "capture.json"), {
			schemaVersion: 1,
			requestedUrl: url,
			finalUrl: result.finalUrl,
			title: result.title,
			httpStatus: result.httpStatus,
			userAgent: options.userAgent,
			capturedAt: nowIso(),
			artifacts: result.artifacts,
			consoleErrors: result.consoleErrors,
			warnings: result.warnings,
		});
	} finally {
		await browser.close();
	}
	return result;
}

async function collectOne(url, runDirectory, state, options, playwright, sourceLabel) {
	const accessDate = nowIso();
	const baseRow = {
		resource_id: "",
		source_url: url,
		final_url: "",
		access_date: accessDate,
		status: "failed",
		http_status: "",
		content_type: "",
		title: "",
		filename: "",
		output_path: "",
		sha256: "",
		byte_size: "",
		capture_method: sourceLabel,
		rendered: false,
		duplicate_of: "",
		error: "",
	};
	try {
		const { response, finalUrl, chain } = await fetchWithRedirects(url, options);
		baseRow.final_url = finalUrl;
		baseRow.http_status = response.status;
		const contentType = response.headers.get("content-type") || "";
		baseRow.content_type = contentType;
		if (!response.ok) {
			const blocked = response.status === 401 || response.status === 402 || response.status === 403;
			baseRow.status = "failed";
			baseRow.error = `${blocked ? "blocked or paywalled" : "HTTP error"}: ${response.status}`;
			state.rows.push(baseRow);
			return baseRow;
		}
		const declared = Number.parseInt(response.headers.get("content-length") || "", 10);
		if (Number.isInteger(declared) && declared > options.maxBytes) {
			baseRow.error = `resource exceeds max-bytes (${declared} > ${options.maxBytes})`;
			state.rows.push(baseRow);
			return baseRow;
		}
		const { buffer, truncated } = await readCappedBody(response, options.maxBytes);
		if (truncated) {
			baseRow.error = `resource exceeded max-bytes (${options.maxBytes}); not saved`;
			state.rows.push(baseRow);
			return baseRow;
		}
		const hash = sha256(buffer);
		const dispositionName = parseContentDisposition(response.headers.get("content-disposition"));
		const title = /html/i.test(contentType) ? htmlTitle(buffer.toString("utf8")) : dispositionName || null;
		const normalized = normalizeUrl(finalUrl);
		const resourceId = `sha256:${hash}`;
		const duplicate = duplicateOf(state, normalized, hash, title);
		if (duplicate) {
			baseRow.resource_id = resourceId;
			baseRow.sha256 = hash;
			baseRow.byte_size = buffer.length;
			baseRow.title = title ?? "";
			baseRow.status = "skipped";
			baseRow.duplicate_of = duplicate.id;
			baseRow.error = `duplicate (${duplicate.key})`;
			state.rows.push(baseRow);
			return baseRow;
		}
		const extension = extensionFor(contentType, finalUrl, dispositionName);
		const filename = uniqueFilename(state, stemFromUrl(finalUrl), extension);
		const downloadsDir = join(runDirectory, "downloads");
		mkdirSync(downloadsDir, { recursive: true });
		writeFileSync(join(downloadsDir, filename), buffer, { flag: "wx" });
		let rendered = false;
		let captureRelative = null;
		const captureWarnings = [];
		if (options.render) {
			if (!playwright) {
				captureWarnings.push("rendered capture requested but Playwright is unavailable");
			} else {
				const captureDir = join(runDirectory, "captures", `${stemFromUrl(finalUrl)}-${hash.slice(0, 12)}`);
				try {
					const capture = await capturePage(playwright, finalUrl, captureDir, options);
					rendered = true;
					captureRelative = `captures/${basename(captureDir)}`;
					captureWarnings.push(...capture.warnings);
				} catch (error) {
					captureWarnings.push(`rendered capture failed: ${error.message}`);
				}
			}
		}
		state.byUrl.set(normalized, resourceId);
		state.bySha.set(hash, resourceId);
		if (title) state.byTitle.set(title, resourceId);
		const status = captureWarnings.length > 0 ? "needs_review" : "success";
		const row = {
			...baseRow,
			resource_id: resourceId,
			status,
			title: title ?? "",
			filename,
			output_path: `downloads/${filename}`,
			sha256: hash,
			byte_size: buffer.length,
			rendered,
			error: captureWarnings.join("; "),
		};
		state.rows.push(row);
		state.records.push({
			resourceId,
			sourceUrl: url,
			finalUrl,
			accessDate,
			status,
			httpStatus: response.status,
			contentType,
			title,
			filename,
			outputPath: `downloads/${filename}`,
			sha256: hash,
			byteSize: buffer.length,
			rendered,
			capture: captureRelative,
			captureArtifacts: rendered ? CAPTURE_ARTIFACTS : [],
			redirectChain: chain,
			contentDisposition: dispositionName,
			warnings: captureWarnings,
			source: sourceLabel,
		});
		return row;
	} catch (error) {
		baseRow.error = error instanceof Error ? error.message : String(error);
		state.rows.push(baseRow);
		return baseRow;
	}
}

function prepareRunDirectory(output) {
	if (existsSync(output)) fail(`output directory already exists: ${output}`);
	mkdirSync(output, { recursive: true });
}

function readUrlList(filePath) {
	return readFileSync(filePath, "utf8")
		.split(/\r?\n/)
		.map((line) => line.trim())
		.filter((line) => line && !line.startsWith("#"));
}

function failureRows(rows) {
	return rows
		.filter((row) => row.status === "failed" || (row.status === "skipped" && /blocked|paywall/i.test(row.error)))
		.map((row) => ({
			source_url: row.source_url,
			status: row.status,
			http_status: row.http_status,
			reason: row.error,
			access_date: row.access_date,
		}));
}

function writeRunArtifacts(runDirectory, state, extra) {
	writeCsv(join(runDirectory, "web_manifest.csv"), MANIFEST_COLUMNS, state.rows);
	writeCsv(join(runDirectory, "failed_downloads.csv"), FAILED_COLUMNS, failureRows(state.rows));
	writeJson(join(runDirectory, "web_manifest.json"), {
		schemaVersion: 1,
		generatedAt: nowIso(),
		command: extra.command,
		options: extra.options,
		search: extra.search ?? null,
		dedupKeys: ["normalized-url", "sha256", "content-disposition-filename", "title"],
		resources: state.records,
	});
	writeFileSync(join(runDirectory, "collection_report.md"), buildReport(state, extra));
}

function counts(rows) {
	return Object.fromEntries([...STATUSES].map((status) => [status, rows.filter((row) => row.status === status).length]));
}

function buildReport(state, extra) {
	const tally = counts(state.rows);
	const duplicates = state.rows.filter((row) => row.duplicate_of);
	const failures = state.rows.filter((row) => row.status === "failed");
	const captures = state.records.filter((record) => record.rendered);
	const list = (rows, render) => (rows.length > 0 ? rows.map(render).join("\n") : "- None.");
	return `# Web Collection Report

## Status

${extra.command} run completed: ${tally.success} success, ${tally.needs_review} needs review, ${tally.skipped} skipped, ${tally.failed} failed.

## Run Summary

- Command: ${extra.command}
- Generated: ${nowIso()}
- Rendered capture requested: ${extra.options.render ? "yes" : "no"}
- Total resources recorded: ${state.rows.length}

## Sources

${list(
	state.records,
	(record) => `- \`${record.outputPath}\` — ${record.finalUrl} (${record.contentType || "unknown type"}, ${record.byteSize} bytes)`,
)}

## Captures

${list(captures, (record) => `- \`${record.capture}/\` — ${record.finalUrl} (${record.captureArtifacts.join(", ")})`)}

## Duplicates

${list(duplicates, (row) => `- ${row.source_url} → duplicate of \`${row.duplicate_of}\` (${row.error})`)}

## Failures and Blocks

${list(failures, (row) => `- ${row.source_url} — ${row.error} (HTTP ${row.http_status || "n/a"})`)}

## Search

${extra.search ? `- Query: ${extra.search.query}\n- Engine: ${extra.search.base}\n- Results listed: ${extra.search.results.length}` : "- Not a search run."}

## Review

- Provenance, access dates, and SHA-256 checksums are recorded in \`web_manifest.csv\` and \`web_manifest.json\`.
- Review every \`needs_review\` row; capture warnings are recorded in the \`error\` column and per-capture \`capture.json\`.
- To extract text and metadata from the saved files, hand the \`downloads/\` directory to the document-ingest skill:
  \`node <document-ingest>/scripts/document-ingest.mjs prepare downloads --output <new-directory>\`.
`;
}

async function collectUrls(urls, runDirectory, state, options, playwright, sourceLabel) {
	for (const [index, url] of urls.entries()) {
		if (index > 0 && options.delayMs > 0) await sleep(options.delayMs);
		await collectOne(url, runDirectory, state, options, playwright, sourceLabel);
	}
}

// --- Commands -------------------------------------------------------------

function commonOptions(flags) {
	return {
		userAgent: flags.userAgent ?? DEFAULT_USER_AGENT,
		delayMs: flags.delayMs ?? DEFAULT_DELAY_MS,
		timeoutMs: flags.timeoutMs ?? DEFAULT_TIMEOUT_MS,
		maxBytes: flags.maxBytes ?? DEFAULT_MAX_BYTES,
		render: Boolean(flags.render),
	};
}

function reportOptions(options, flags) {
	return { ...options, sameHost: Boolean(flags.sameHost), ignoreRobots: Boolean(flags.ignoreRobots) };
}

async function commandCollect(positionals, flags) {
	if (!flags.output) fail("collect requires --output <new-directory>");
	const urls = [...positionals];
	if (flags.inputFile) urls.push(...readUrlList(resolve(flags.inputFile)));
	if (urls.length === 0) fail("collect requires at least one URL or --input-file");
	const options = commonOptions(flags);
	const playwright = options.render ? await loadPlaywright() : null;
	const runDirectory = resolve(flags.output);
	prepareRunDirectory(runDirectory);
	const state = newRunState();
	await collectUrls(urls, runDirectory, state, options, playwright, "http-collect");
	writeRunArtifacts(runDirectory, state, { command: "collect", options: reportOptions(options, flags) });
	process.stdout.write(`${JSON.stringify({ runDirectory, resources: state.rows.length, counts: counts(state.rows) }, null, 2)}\n`);
}

async function commandHarvest(positionals, flags) {
	if (positionals.length !== 1) fail("harvest requires exactly one page URL");
	if (!flags.output) fail("harvest requires --output <new-directory>");
	const pageUrl = positionals[0];
	const parsedPage = assertCollectableUrl(pageUrl);
	const options = commonOptions(flags);
	const playwright = options.render ? await loadPlaywright() : null;
	const runDirectory = resolve(flags.output);
	prepareRunDirectory(runDirectory);
	const extensions = flags.ext ? new Set(flags.ext.split(",").map((value) => value.trim().toLowerCase()).filter(Boolean)) : null;
	const matcher = flags.match ? new RegExp(flags.match) : null;
	const { response, finalUrl } = await fetchWithRedirects(pageUrl, options);
	if (!response.ok) fail(`could not fetch page (HTTP ${response.status}): ${pageUrl}`);
	const html = (await readCappedBody(response, options.maxBytes)).buffer.toString("utf8");
	let links = extractLinks(html, finalUrl);
	const disallows = flags.ignoreRobots ? [] : await fetchRobots(parsedPage.origin, options.userAgent, options.timeoutMs);
	const selected = [];
	for (const link of links) {
		const parsed = new URL(link);
		if (flags.sameHost && parsed.hostname !== parsedPage.hostname) continue;
		if (extensions && !extensions.has(extname(parsed.pathname).slice(1).toLowerCase())) continue;
		if (matcher && !matcher.test(link)) continue;
		if (!flags.ignoreRobots && robotsBlocks(disallows, parsed.pathname)) continue;
		selected.push(link);
		if (flags.limit && selected.length >= flags.limit) break;
	}
	const state = newRunState();
	await collectUrls(selected, runDirectory, state, options, playwright, "harvest");
	writeRunArtifacts(runDirectory, state, {
		command: "harvest",
		options: { ...reportOptions(options, flags), page: finalUrl, linkCount: links.length, matchedCount: selected.length },
	});
	process.stdout.write(
		`${JSON.stringify({ runDirectory, page: finalUrl, linksFound: links.length, matched: selected.length, counts: counts(state.rows) }, null, 2)}\n`,
	);
}

async function commandSearch(positionals, flags) {
	if (positionals.length === 0) fail("search requires a query");
	if (!flags.output) fail("search requires --output <new-directory>");
	const query = positionals.join(" ");
	const base = searxngBase(flags.searxng);
	if (!base) fail("search requires a SearXNG instance; set FORGE_SEARXNG_URL or pass --searxng <url>");
	const options = commonOptions(flags);
	const limit = flags.limit ?? 25;

	// Build SearXNG query parameters
	const params = new URLSearchParams({ q: query, format: "json" });
	if (flags.categories) params.set("categories", flags.categories);
	if (flags.engines) params.set("engines", flags.engines);
	if (flags.language) params.set("language", flags.language);
	if (flags.safesearch !== undefined) params.set("safesearch", String(flags.safesearch));
	if (flags.timeRange) params.set("time_range", flags.timeRange);
	if (flags.pageNo) params.set("pageno", String(flags.pageNo));

	const controller = new AbortController();
	const timer = setTimeout(() => controller.abort(), options.timeoutMs);
	let payload;
	try {
		const response = await fetch(`${base}/search?${params.toString()}`, {
			signal: controller.signal,
			headers: { "user-agent": options.userAgent, accept: "application/json" },
		});
		if (!response.ok) fail(`SearXNG returned HTTP ${response.status}`);
		payload = await response.json();
	} catch (error) {
		fail(`SearXNG request failed: ${error instanceof Error ? error.message : String(error)}`);
	} finally {
		clearTimeout(timer);
	}
	const results = (Array.isArray(payload.results) ? payload.results : []).slice(0, limit).map((result, index) => ({
		rank: index + 1,
		title: result.title ?? null,
		url: result.url ?? null,
		content: result.content ?? null,
		engine: result.engine ?? null,
	}));
	const searchMeta = {
		categories: flags.categories ?? null,
		engines: flags.engines ?? null,
		language: flags.language ?? null,
		safesearch: flags.safesearch ?? null,
		timeRange: flags.timeRange ?? null,
		pageNo: flags.pageNo ?? null,
	};
	const runDirectory = resolve(flags.output);
	prepareRunDirectory(runDirectory);
	writeJson(join(runDirectory, "search_results.json"), { query, base, params: searchMeta, retrievedAt: nowIso(), results });
	const playwright = options.render ? await loadPlaywright() : null;
	const state = newRunState();
	if (flags.collect) {
		const urls = results.map((result) => result.url).filter(Boolean);
		await collectUrls(urls, runDirectory, state, options, playwright, "search-collect");
	}
	writeRunArtifacts(runDirectory, state, {
		command: "search",
		options: reportOptions(options, flags),
		search: { query, base, params: searchMeta, results },
	});
	process.stdout.write(`${JSON.stringify({ runDirectory, query, results: results.length, collected: state.rows.length, counts: counts(state.rows) }, null, 2)}\n`);
}

// --- Validation -----------------------------------------------------------

function validate(runDirectory) {
	const errors = [];
	const warnings = [];
	const manifestPath = join(runDirectory, "web_manifest.csv");
	if (!existsSync(manifestPath)) fail(`web_manifest.csv does not exist in ${runDirectory}`);
	for (const required of ["web_manifest.json", "collection_report.md", "failed_downloads.csv"]) {
		if (!existsSync(join(runDirectory, required))) errors.push(`${required} is missing`);
	}
	const parsed = parseCsv(readFileSync(manifestPath, "utf8"));
	const headers = parsed.shift() ?? [];
	if (headers.join(",") !== MANIFEST_COLUMNS.join(",")) errors.push("web_manifest.csv columns do not match the required contract");
	const seenIds = new Set();
	for (const values of parsed.filter((row) => row.some((field) => field !== ""))) {
		if (values.length !== MANIFEST_COLUMNS.length) {
			errors.push(`manifest row has ${values.length} columns instead of ${MANIFEST_COLUMNS.length}`);
			continue;
		}
		const row = Object.fromEntries(MANIFEST_COLUMNS.map((column, index) => [column, values[index] ?? ""]));
		if (!STATUSES.has(row.status)) {
			errors.push(`invalid status for ${row.source_url}: ${row.status}`);
			continue;
		}
		if (row.resource_id) seenIds.add(row.resource_id);
		if (row.status === "failed") continue;
		if (row.status === "skipped") {
			if (row.duplicate_of && !seenIds.has(row.duplicate_of)) {
				warnings.push(`${row.source_url} references duplicate_of ${row.duplicate_of} which is not earlier in the manifest`);
			}
			continue;
		}
		const outputPath = resolve(runDirectory, row.output_path);
		if (!outputPath.startsWith(`${resolve(runDirectory)}${sep}`)) {
			errors.push(`output path escapes the run directory: ${row.output_path}`);
			continue;
		}
		if (!existsSync(outputPath)) {
			errors.push(`${row.output_path} is missing for ${row.source_url}`);
			continue;
		}
		const buffer = readFileSync(outputPath);
		if (`sha256:${sha256(buffer)}` !== row.resource_id) errors.push(`${row.output_path} SHA-256 does not match resource_id`);
		if (String(buffer.length) !== row.byte_size) errors.push(`${row.output_path} byte size does not match manifest`);
		if (row.rendered === "true") {
			const captureDir = join(runDirectory, "captures", `${stemFromUrl(row.final_url || row.source_url)}-${row.sha256.slice(0, 12)}`);
			if (!existsSync(join(captureDir, "rendered.html")) || !existsSync(join(captureDir, "capture.json"))) {
				errors.push(`${row.source_url} is marked rendered but capture artifacts are missing`);
			}
		}
	}
	const reportPath = join(runDirectory, "collection_report.md");
	if (existsSync(reportPath)) {
		const report = readFileSync(reportPath, "utf8");
		for (const heading of ["## Status", "## Run Summary", "## Sources", "## Captures", "## Duplicates", "## Failures and Blocks", "## Search", "## Review"]) {
			if (!report.includes(heading)) errors.push(`collection_report.md is missing ${heading}`);
		}
	}
	process.stdout.write(`${JSON.stringify({ valid: errors.length === 0, errors, warnings }, null, 2)}\n`);
	if (errors.length > 0) process.exit(1);
}

// --- Argument parsing -----------------------------------------------------

const FLAG_SPECS = {
	"--output": { key: "output", value: true },
	"--input-file": { key: "inputFile", value: true },
	"--user-agent": { key: "userAgent", value: true },
	"--searxng": { key: "searxng", value: true },
	"--match": { key: "match", value: true },
	"--ext": { key: "ext", value: true },
	"--delay-ms": { key: "delayMs", value: true, integer: true },
	"--timeout-ms": { key: "timeoutMs", value: true, integer: true },
	"--max-bytes": { key: "maxBytes", value: true, integer: true },
	"--limit": { key: "limit", value: true, integer: true },
	"--render": { key: "render", value: false },
	"--same-host": { key: "sameHost", value: false },
	"--ignore-robots": { key: "ignoreRobots", value: false },
	"--collect": { key: "collect", value: false },
	"--json": { key: "json", value: false },
	"--categories": { key: "categories", value: true },
	"--engines": { key: "engines", value: true },
	"--language": { key: "language", value: true },
	"--safesearch": { key: "safesearch", value: true, integer: true },
	"--time-range": { key: "timeRange", value: true },
	"--pageno": { key: "pageNo", value: true, integer: true },
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
	return { positionals, flags };
}

function usage() {
	process.stdout.write(`Usage:
  web-collection.mjs doctor [--json] [--searxng <url>]
  web-collection.mjs collect <url...> --output <dir> [--input-file <path>] [--render]
      [--user-agent <ua>] [--delay-ms N] [--timeout-ms N] [--max-bytes N]
  web-collection.mjs harvest <page-url> --output <dir> [--match <regex>] [--ext csv]
      [--same-host] [--limit N] [--render] [--ignore-robots]
  web-collection.mjs search <query...> --output <dir> [--searxng <url>] [--limit N] [--collect]
      [--categories <cats>] [--engines <engines>] [--language <lang>]
      [--safesearch <0|1|2>] [--time-range <day|week|month|year>] [--pageno N]
  web-collection.mjs validate <run-directory>
`);
}

async function main() {
	const [command, ...rest] = process.argv.slice(2);
	if (!command || command === "--help" || command === "-h") {
		usage();
		process.exit(command ? 0 : 2);
	}
	const { positionals, flags } = parseArguments(rest);
	if (command === "doctor") await doctor(flags);
	else if (command === "collect") await commandCollect(positionals, flags);
	else if (command === "harvest") await commandHarvest(positionals, flags);
	else if (command === "search") await commandSearch(positionals, flags);
	else if (command === "validate") {
		if (positionals.length !== 1) fail("validate requires exactly one run directory");
		const runDirectory = resolve(positionals[0]);
		if (!existsSync(runDirectory) || !lstatSync(runDirectory).isDirectory()) fail(`run directory does not exist: ${runDirectory}`);
		validate(runDirectory);
	} else fail(`unknown command: ${command}`, 2);
}

main().catch((error) => fail(error instanceof Error ? error.stack || error.message : String(error)));

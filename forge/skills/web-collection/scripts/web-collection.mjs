#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, lstatSync, mkdirSync, readFileSync, readdirSync } from "node:fs";
import { basename, extname, join, resolve, sep } from "node:path";
import * as readline from "node:readline/promises";
import { resolveConnectedServices } from "../../../lib/connected-services.mjs";
import { htmlToCleanMarkdown } from "../../../lib/html-cleaner.mjs";
import {
	DEFAULT_MAX_ATTEMPTS,
	assertCompatibleRun,
	atomicWriteFile,
	atomicWriteJson,
	configurationFingerprint,
	createRunState,
	initializeRunState,
	isTransientFailure,
	loadRunState,
	retryableItem,
	updateRunState,
	withRunLock,
} from "../../../lib/run-state.mjs";

const DEFAULT_USER_AGENT = "pi-forge-web-collection/1 (+https://github.com/pi-forge)";
const DEFAULT_DELAY_MS = 500;
const DEFAULT_TIMEOUT_MS = 30_000;
const DEFAULT_MAX_BYTES = 100 * 1024 * 1024;
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
	atomicWriteFile(filePath, `${lines.join("\n")}\n`);
}

function writeJson(filePath, value) {
	atomicWriteJson(filePath, value);
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

function playwrightWsEndpoint(explicit) {
	const services = resolveConnectedServices({ playwrightWsEndpoint: explicit });
	return services.playwright.enabled ? services.playwright.wsEndpoint : "";
}

async function connectPlaywrightBrowser(playwright, timeoutMs, explicitEndpoint) {
	const wsEndpoint = playwrightWsEndpoint(explicitEndpoint);
	if (!wsEndpoint) {
		throw new Error("Playwright rendered browsing is disabled in settings");
	}
	return playwright.chromium.connect(wsEndpoint, { timeout: timeoutMs });
}

function searxngBase(explicit) {
	const services = resolveConnectedServices({ searxngUrl: explicit });
	return services.searxng.enabled ? services.searxng.baseUrl : "";
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

async function doctor(options) {
	const playwright = await loadPlaywright();
	const playwrightEndpoint = playwrightWsEndpoint(options.playwrightWs);
	const searxng = await pingSearxng(searxngBase(options.searxng), DEFAULT_USER_AGENT, 5000);
	const tools = {
		fetch: { available: typeof fetch === "function", version: process.version },
		curl: run("curl"),
		wget: run("wget"),
		playwright: { available: Boolean(playwright), version: playwright ? "importable" : null },
		playwrightEndpoint: { available: Boolean(playwrightEndpoint), version: playwrightEndpoint || null },
	};
	const capabilities = {
		httpCollect: tools.fetch.available,
		renderedCapture: tools.playwright.available && tools.playwrightEndpoint.available,
		searxngSearch: searxng.configured && searxng.reachable,
	};
	const remediation = [];
	if (!tools.fetch.available) remediation.push("Node 22.19+ with global fetch is required.");
	if (!tools.playwright.available) remediation.push("Install Playwright (bundled with pi-forge; run pi-forge-update to refresh the installed package).");
	if (tools.playwright.available && !tools.playwrightEndpoint.available) {
		remediation.push("Set connectedServices.playwright.wsEndpoint or FORGE_PLAYWRIGHT_WS_ENDPOINT for rendered capture.");
	}
	if (!searxng.configured) remediation.push("Set connectedServices.searxng.baseUrl, FORGE_SEARXNG_URL, or --searxng to enable search.");
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
	const browser = await connectPlaywrightBrowser(playwright, options.timeoutMs, options.playwrightWsEndpoint);
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
		atomicWriteFile(join(captureDir, "rendered.html"), html);
		result.artifacts.push("rendered.html");
		const session = await context.newCDPSession(page);
		try {
			const snapshot = await session.send("Page.captureSnapshot", { format: "mhtml" });
			atomicWriteFile(join(captureDir, "snapshot.mhtml"), snapshot.data);
			result.artifacts.push("snapshot.mhtml");
		} catch (error) {
			result.warnings.push(`MHTML snapshot failed: ${error.message}`);
		}
		atomicWriteFile(join(captureDir, "screenshot.png"), await page.screenshot({ fullPage: true }));
		result.artifacts.push("screenshot.png");
		try {
			atomicWriteFile(join(captureDir, "page.pdf"), await page.pdf({ printBackground: true }));
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
		const downloadPath = join(downloadsDir, filename);
		if (existsSync(downloadPath)) {
			if (sha256(readFileSync(downloadPath)) !== hash) throw new Error(`existing download hash mismatch: ${filename}`);
		} else atomicWriteFile(downloadPath, buffer);
		let rendered = false;
		let captureRelative = null;
		const captureWarnings = [];
		if (options.cleanMarkdown && /html/i.test(contentType)) {
			try {
				const md = await htmlToCleanMarkdown(buffer, finalUrl);
				if (md) {
					const mdFilename = uniqueFilename(state, stemFromUrl(finalUrl), "md");
					const markdownPath = join(downloadsDir, mdFilename);
					if (!existsSync(markdownPath)) atomicWriteFile(markdownPath, md);
				}
			} catch (err) {
				captureWarnings.push(`clean markdown failed: ${err.message}`);
			}
		}
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

function collectionConfiguration(command, input, options) {
	return { workflow: "web-collection", command, input, options };
}

function initializeCollectionRun(runDirectory, configuration, urls) {
	if (existsSync(runDirectory)) {
		const state = loadRunState(runDirectory, "web-collection");
		assertCompatibleRun(state, configuration);
		return state;
	}
	mkdirSync(runDirectory, { recursive: true });
	const items = urls.map((url, index) => ({
		id: `url:${configurationFingerprint({ index, url: normalizeUrl(url) }).slice(0, 20)}`,
		url,
		status: "pending",
		attempts: 0,
		transient: false,
		error: null,
	}));
	const state = createRunState({ ...configuration, items, phase: "collecting", nextAction: "collect" });
	initializeRunState(runDirectory, state);
	const domain = newRunState();
	writeRunArtifacts(runDirectory, domain, { command: configuration.command, options: configuration.options });
	return state;
}

function loadCollectionDomain(runDirectory) {
	const state = newRunState();
	if (existsSync(join(runDirectory, "web_manifest.csv"))) {
		const parsed = parseCsv(readFileSync(join(runDirectory, "web_manifest.csv"), "utf8"));
		const headers = parsed.shift() ?? [];
		state.rows = parsed.filter((row) => row.some(Boolean)).map((values) => Object.fromEntries(headers.map((header, index) => [header, values[index] ?? ""])));
	}
	if (existsSync(join(runDirectory, "web_manifest.json"))) {
		state.records = JSON.parse(readFileSync(join(runDirectory, "web_manifest.json"), "utf8")).resources ?? [];
	}
	for (const row of state.rows) {
		if (row.filename) state.usedFilenames.add(row.filename);
		if (!row.resource_id) continue;
		if (row.final_url) state.byUrl.set(normalizeUrl(row.final_url), row.resource_id);
		if (row.sha256) state.bySha.set(row.sha256, row.resource_id);
		if (row.title) state.byTitle.set(row.title, row.resource_id);
	}
	return state;
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
	atomicWriteFile(join(runDirectory, "collection_report.md"), buildReport(state, extra));
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

async function collectUrls(runDirectory, domain, options, playwright, sourceLabel, extra) {
	await withRunLock(runDirectory, async () => {
		let state = loadRunState(runDirectory, "web-collection");
		let processed = 0;
		for (const snapshot of state.items) {
			if (snapshot.retired) continue;
			if (!retryableItem(snapshot, DEFAULT_MAX_ATTEMPTS)) continue;
			if (processed > 0 && options.delayMs > 0) await sleep(options.delayMs);
			let item = snapshot;
			while (retryableItem(item, DEFAULT_MAX_ATTEMPTS)) {
				const attempt = (item.attempts ?? 0) + 1;
				state = updateRunState(runDirectory, (draft) => {
					const current = draft.items.find((candidate) => candidate.id === item.id);
					Object.assign(current, { status: "in_progress", attempts: attempt, error: null });
					return draft;
				}, { type: "item_started", itemId: item.id, attempt });
				item = state.items.find((candidate) => candidate.id === item.id);
				const row = await collectOne(item.url, runDirectory, domain, options, playwright, sourceLabel);
				const statusCode = Number(row.http_status);
				const transient = row.status === "failed" && ((statusCode >= 500 && statusCode < 600) || isTransientFailure(new Error(row.error)));
				state = updateRunState(runDirectory, (draft) => {
					const current = draft.items.find((candidate) => candidate.id === item.id);
					Object.assign(current, { status: row.status, transient, error: row.error || null, resourceId: row.resource_id || null });
					return draft;
				}, { type: row.status === "failed" ? "item_failed" : "item_completed", itemId: item.id, status: row.status, transient, attempt });
				writeRunArtifacts(runDirectory, domain, extra);
				item = state.items.find((candidate) => candidate.id === item.id);
				if (!transient || attempt >= DEFAULT_MAX_ATTEMPTS) break;
				domain.rows.pop();
			}
			processed += 1;
		}
		updateRunState(runDirectory, (draft) => {
			const pending = draft.items.some((item) => retryableItem(item, DEFAULT_MAX_ATTEMPTS));
			Object.assign(draft, { status: pending ? "running" : "complete", phase: pending ? "collecting" : "complete", nextAction: pending ? "collect" : null });
			return draft;
		}, { type: "phase_updated" });
	});
}

// --- Commands -------------------------------------------------------------

function commonOptions(flags) {
	return {
		userAgent: flags.userAgent ?? DEFAULT_USER_AGENT,
		delayMs: flags.delayMs ?? DEFAULT_DELAY_MS,
		timeoutMs: flags.timeoutMs ?? DEFAULT_TIMEOUT_MS,
		maxBytes: flags.maxBytes ?? DEFAULT_MAX_BYTES,
		render: Boolean(flags.render),
		cleanMarkdown: Boolean(flags.clean),
		playwrightWsEndpoint: flags.playwrightWs,
	};
}

function reportOptions(options, flags) {
	return { ...options, sameHost: Boolean(flags.sameHost), ignoreRobots: Boolean(flags.ignoreRobots) };
}

async function commandCollect(positionals, flags) {
	if (!flags.output) fail("collect requires --output <new-directory>");
	const staticUrls = [...positionals];
	const inputFile = flags.inputFile ? resolve(flags.inputFile) : null;
	const urls = [...staticUrls];
	if (inputFile) urls.push(...readUrlList(inputFile));
	if (urls.length === 0) fail("collect requires at least one URL or --input-file");
	const options = commonOptions(flags);
	const playwright = options.render ? await loadPlaywright() : null;
	const runDirectory = resolve(flags.output);
	const extra = { command: "collect", options: reportOptions(options, flags) };
	const configuration = collectionConfiguration("collect", { urls, staticUrls, inputFile }, extra.options);
	initializeCollectionRun(runDirectory, configuration, urls);
	const domain = loadCollectionDomain(runDirectory);
	await collectUrls(runDirectory, domain, options, playwright, "http-collect", extra);
	process.stdout.write(`${JSON.stringify({ runDirectory, resources: domain.rows.length, counts: counts(domain.rows), complete: loadRunState(runDirectory).status === "complete" }, null, 2)}\n`);
}

async function commandHarvest(positionals, flags) {
	if (positionals.length !== 1) fail("harvest requires exactly one page URL");
	if (!flags.output) fail("harvest requires --output <new-directory>");
	const pageUrl = positionals[0];
	const parsedPage = assertCollectableUrl(pageUrl);
	const options = commonOptions(flags);
	const playwright = options.render ? await loadPlaywright() : null;
	const runDirectory = resolve(flags.output);
	const reportedOptions = reportOptions(options, flags);
	const configuration = collectionConfiguration("harvest", { pageUrl }, reportedOptions);
	if (existsSync(runDirectory)) {
		const state = initializeCollectionRun(runDirectory, configuration, []);
		const domain = loadCollectionDomain(runDirectory);
		const extra = { command: "harvest", options: reportedOptions };
		await collectUrls(runDirectory, domain, options, playwright, "harvest", extra);
		process.stdout.write(`${JSON.stringify({ runDirectory, matched: state.items.length, counts: counts(domain.rows), complete: loadRunState(runDirectory).status === "complete" }, null, 2)}\n`);
		return;
	}
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
	const extra = {
		command: "harvest",
		options: { ...reportOptions(options, flags), page: finalUrl, linkCount: links.length, matchedCount: selected.length },
	};
	initializeCollectionRun(runDirectory, configuration, selected);
	const domain = loadCollectionDomain(runDirectory);
	await collectUrls(runDirectory, domain, options, playwright, "harvest", extra);
	process.stdout.write(
		`${JSON.stringify({ runDirectory, page: finalUrl, linksFound: links.length, matched: selected.length, counts: counts(domain.rows) }, null, 2)}\n`,
	);
}

async function filterLinksWithLlm(links, instruction) {
	const baseChatUrl = process.env.FORGE_BASE_CHAT_URL || process.env.FORGE_CHAT_URL || "http://llms:8008/v1/chat/completions";
	const baseModel = process.env.FORGE_BASE_MODEL || "llama-3.3-70b-versatile";
	const prompt = `You are an expert web spider. I will provide a list of URLs and an instruction for what I'm looking for. Please return ONLY a JSON array of strings containing the URLs that are most likely to contain the requested information. Do not return any other text.
Instruction: ${instruction}
URLs:
${links.join("\n")}`;

	try {
		const response = await fetch(baseChatUrl, {
			method: "POST",
			headers: { "Content-Type": "application/json", "Authorization": "Bearer local" },
			body: JSON.stringify({
				model: baseModel,
				messages: [{ role: "user", content: prompt }],
			})
		});
		if (!response.ok) fail(`LLM API returned HTTP ${response.status}`);
		const payload = await response.json();
		const content = payload.choices[0]?.message?.content || "[]";
		const match = content.match(/\[.*\]/s);
		const jsonString = match ? match[0] : content;
		const selected = JSON.parse(jsonString);
		if (!Array.isArray(selected)) return links;
		return selected;
	} catch (error) {
		fail(`Failed to filter links with LLM: ${error.message}`);
	}
}

async function commandSpider(positionals, flags) {
	if (positionals.length !== 1) fail("spider requires exactly one page URL");
	if (!flags.output) fail("spider requires --output <new-directory>");
	const pageUrl = positionals[0];
	const parsedPage = assertCollectableUrl(pageUrl);
	const options = commonOptions(flags);
	const playwright = options.render ? await loadPlaywright() : null;
	const runDirectory = resolve(flags.output);
	const configuration = collectionConfiguration("spider", { pageUrl }, reportOptions(options, flags));
	if (existsSync(runDirectory)) {
		const state = initializeCollectionRun(runDirectory, configuration, []);
		if (state.runtimeOptions) Object.assign(options, state.runtimeOptions);
		const domain = loadCollectionDomain(runDirectory);
		const extra = { command: "spider", options: reportOptions(options, flags) };
		await collectUrls(runDirectory, domain, options, playwright, "spider", extra);
		process.stdout.write(`${JSON.stringify({ runDirectory, matched: loadRunState(runDirectory).items.length, counts: counts(domain.rows), complete: loadRunState(runDirectory).status === "complete" }, null, 2)}\n`);
		return;
	}
	
	const { response, finalUrl } = await fetchWithRedirects(pageUrl, options);
	if (!response.ok) fail(`could not fetch page (HTTP ${response.status}): ${pageUrl}`);
	const html = (await readCappedBody(response, options.maxBytes)).buffer.toString("utf8");
	let links = extractLinks(html, finalUrl);
	const disallows = flags.ignoreRobots ? [] : await fetchRobots(parsedPage.origin, options.userAgent, options.timeoutMs);
	
	const validLinks = [];
	for (const link of links) {
		const parsed = new URL(link);
		if (parsed.hostname !== parsedPage.hostname) continue;
		if (!flags.ignoreRobots && robotsBlocks(disallows, parsed.pathname)) continue;
		validLinks.push(link);
	}
	
	process.stdout.write(`Found ${validLinks.length} valid same-host links on ${finalUrl}\n`);
	
	const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
	process.stdout.write(`What information are you looking for on this domain?
1. All links (standard crawl)
2. Pricing, Packages, and Services
3. About and Contact
4. Custom (Provide instructions for the model)
`);
	const choice = (await rl.question("> ")).trim();
	let selectedLinks = validLinks;
	
	if (choice === "2") {
		process.stdout.write("Asking LLM to filter for Pricing, Packages, and Services...\n");
		selectedLinks = await filterLinksWithLlm(validLinks, "Find URLs related to pricing, pricing plans, service packages, or product offerings.");
	} else if (choice === "3") {
		process.stdout.write("Asking LLM to filter for About and Contact...\n");
		selectedLinks = await filterLinksWithLlm(validLinks, "Find URLs related to 'About Us', company background, team, or contact information.");
	} else if (choice === "4") {
		const customInstruction = await rl.question("Enter custom instruction: ");
		process.stdout.write("Asking LLM to filter based on custom instruction...\n");
		selectedLinks = await filterLinksWithLlm(validLinks, customInstruction);
	} else {
		process.stdout.write("Collecting all links...\n");
	}
	
	if (!options.cleanMarkdown) {
		const cleanChoice = (await rl.question("Do you want to automatically clean downloaded HTML into Markdown? (y/N) ")).trim().toLowerCase();
		if (cleanChoice === 'y' || cleanChoice === 'yes') {
			options.cleanMarkdown = true;
		}
	}
	
	rl.close();
	
	if (flags.limit && selectedLinks.length > flags.limit) {
		selectedLinks = selectedLinks.slice(0, flags.limit);
	}
	
	process.stdout.write(`Collecting ${selectedLinks.length} selected links...\n`);
	const extra = {
		command: "spider",
		options: { ...reportOptions(options, flags), page: finalUrl, linkCount: validLinks.length, matchedCount: selectedLinks.length },
	};
	initializeCollectionRun(runDirectory, configuration, selectedLinks);
	updateRunState(runDirectory, (draft) => {
		draft.runtimeOptions = options;
		return draft;
	}, { type: "runtime_options_recorded" });
	const domain = loadCollectionDomain(runDirectory);
	await collectUrls(runDirectory, domain, options, playwright, "spider", extra);
	process.stdout.write(
		`${JSON.stringify({ runDirectory, page: finalUrl, linksFound: validLinks.length, matched: selectedLinks.length, counts: counts(domain.rows) }, null, 2)}\n`,
	);
}

async function commandSearch(positionals, flags) {
	if (positionals.length === 0) fail("search requires a query");
	if (!flags.output) fail("search requires --output <new-directory>");
	const query = positionals.join(" ");
	const base = searxngBase(flags.searxng);
	if (!base) fail("search requires a SearXNG instance; set connectedServices.searxng.baseUrl, FORGE_SEARXNG_URL, or --searxng <url>");
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
	const searchMeta = {
		categories: flags.categories ?? null,
		engines: flags.engines ?? null,
		language: flags.language ?? null,
		safesearch: flags.safesearch ?? null,
		timeRange: flags.timeRange ?? null,
		pageNo: flags.pageNo ?? null,
	};
	const runDirectory = resolve(flags.output);
	const configuration = collectionConfiguration("search", { query, base, params: searchMeta }, reportOptions(options, flags));
	if (existsSync(runDirectory)) {
		initializeCollectionRun(runDirectory, configuration, []);
		const stored = JSON.parse(readFileSync(join(runDirectory, "search_results.json"), "utf8"));
		const domain = loadCollectionDomain(runDirectory);
		const extra = { command: "search", options: reportOptions(options, flags), search: { query, base, params: searchMeta, results: stored.results } };
		const playwright = options.render ? await loadPlaywright() : null;
		await collectUrls(runDirectory, domain, options, playwright, "search-collect", extra);
		process.stdout.write(`${JSON.stringify({ runDirectory, query, results: stored.results.length, collected: domain.rows.length, counts: counts(domain.rows), complete: loadRunState(runDirectory).status === "complete" }, null, 2)}\n`);
		return;
	}

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
	const urls = flags.collect ? results.map((result) => result.url).filter(Boolean) : [];
	initializeCollectionRun(runDirectory, configuration, urls);
	writeJson(join(runDirectory, "search_results.json"), { query, base, params: searchMeta, retrievedAt: nowIso(), results });
	const playwright = options.render ? await loadPlaywright() : null;
	const domain = loadCollectionDomain(runDirectory);
	const extra = {
		command: "search",
		options: reportOptions(options, flags),
		search: { query, base, params: searchMeta, results },
	};
	await collectUrls(runDirectory, domain, options, playwright, "search-collect", extra);
	process.stdout.write(`${JSON.stringify({ runDirectory, query, results: results.length, collected: domain.rows.length, counts: counts(domain.rows) }, null, 2)}\n`);
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

function commandStatus(runDirectory) {
	const state = loadRunState(runDirectory, "web-collection");
	const domain = loadCollectionDomain(runDirectory);
	let inputDrift = { added: [], removed: [], changed: [] };
	if (state.command === "collect" && state.input.inputFile) {
		const currentUrls = [...state.input.staticUrls, ...readUrlList(state.input.inputFile)];
		const frozen = new Map(state.input.urls.map((url) => [normalizeUrl(url), url]));
		const current = new Map(currentUrls.map((url) => [normalizeUrl(url), url]));
		inputDrift = {
			added: [...current].filter(([key]) => !frozen.has(key)).map(([, url]) => ({ url })),
			removed: [...frozen].filter(([key]) => !current.has(key)).map(([, url]) => ({ url })),
			changed: [],
		};
	}
	process.stdout.write(`${JSON.stringify({ runDirectory, status: state.status, phase: state.phase, nextAction: state.nextAction, counts: counts(domain.rows), processed: domain.rows.length, total: state.items.filter((item) => !item.retired).length, inputDrift, refreshRequired: inputDrift.added.length + inputDrift.removed.length > 0 }, null, 2)}\n`);
}

function commandRefresh(runDirectory) {
	const state = loadRunState(runDirectory, "web-collection");
	if (state.command !== "collect" || !state.input.inputFile) fail("refresh is only applicable to collect runs created with --input-file");
	const currentUrls = [...state.input.staticUrls, ...readUrlList(state.input.inputFile)];
	const frozen = new Map(state.input.urls.map((url) => [normalizeUrl(url), url]));
	const current = new Map(currentUrls.map((url) => [normalizeUrl(url), url]));
	const added = [...current].filter(([key]) => !frozen.has(key)).map(([, url]) => url);
	const removed = new Set([...frozen].filter(([key]) => !current.has(key)).map(([key]) => key));
	if (added.length === 0 && removed.size === 0) {
		process.stdout.write(`${JSON.stringify({ runDirectory, refreshed: false, added: 0, removed: 0 })}\n`);
		return;
	}
	const updated = updateRunState(
		runDirectory,
		(draft) => {
			for (const item of draft.items) {
				if (removed.has(normalizeUrl(item.url))) item.retired = true;
			}
			for (const [offset, url] of added.entries()) {
				draft.items.push({ id: `url:${configurationFingerprint({ index: draft.items.length + offset, url: normalizeUrl(url) }).slice(0, 20)}`, url, status: "pending", attempts: 0, transient: false, error: null });
			}
			draft.input.urls = currentUrls;
			draft.optionsFingerprint = configurationFingerprint({ workflow: draft.workflow, command: draft.command, input: draft.input, options: draft.options });
			Object.assign(draft, { status: "running", phase: "collecting", nextAction: "collect" });
			return draft;
		},
		{ type: "input_refreshed", added: added.length, removed: removed.size },
	);
	process.stdout.write(`${JSON.stringify({ runDirectory, refreshed: true, added: added.length, removed: removed.size, total: updated.items.filter((item) => !item.retired).length })}\n`);
}

function commandRetry(runDirectory, flags) {
	const state = loadRunState(runDirectory, "web-collection");
	const targets = state.items.filter((item) => item.status === "failed" && (flags.allFailed || item.id === flags.item));
	if (targets.length === 0) fail(flags.item ? `failed item not found: ${flags.item}` : "run has no failed items");
	const targetUrls = new Set(targets.map((item) => item.url));
	const domain = loadCollectionDomain(runDirectory);
	domain.rows = domain.rows.filter((row) => !targetUrls.has(row.source_url));
	domain.records = domain.records.filter((record) => !targetUrls.has(record.sourceUrl));
	updateRunState(runDirectory, (draft) => {
		for (const item of draft.items) {
			if (!targets.some((target) => target.id === item.id)) continue;
			Object.assign(item, { status: "pending", attempts: 0, transient: false, error: null, resourceId: null });
		}
		Object.assign(draft, { status: "running", phase: "collecting", nextAction: "collect" });
		return draft;
	}, { type: "items_retried", itemIds: targets.map((item) => item.id) });
	const manifest = existsSync(join(runDirectory, "web_manifest.json")) ? JSON.parse(readFileSync(join(runDirectory, "web_manifest.json"), "utf8")) : {};
	writeRunArtifacts(runDirectory, domain, { command: manifest.command ?? state.command, options: manifest.options ?? state.options, search: manifest.search ?? null });
	process.stdout.write(`${JSON.stringify({ runDirectory, retried: targets.length, nextAction: "collect" })}\n`);
}

// --- Argument parsing -----------------------------------------------------

const FLAG_SPECS = {
	"--output": { key: "output", value: true },
	"--input-file": { key: "inputFile", value: true },
	"--user-agent": { key: "userAgent", value: true },
	"--searxng": { key: "searxng", value: true },
	"--playwright-ws": { key: "playwrightWs", value: true },
	"--match": { key: "match", value: true },
	"--ext": { key: "ext", value: true },
	"--delay-ms": { key: "delayMs", value: true, integer: true },
	"--timeout-ms": { key: "timeoutMs", value: true, integer: true },
	"--max-bytes": { key: "maxBytes", value: true, integer: true },
	"--limit": { key: "limit", value: true, integer: true },
	"--render": { key: "render", value: false },
	"--clean": { key: "clean", value: false },
	"--same-host": { key: "sameHost", value: false },
	"--ignore-robots": { key: "ignoreRobots", value: false },
	"--collect": { key: "collect", value: false },
	"--json": { key: "json", value: false },
	"--item": { key: "item", value: true },
	"--all-failed": { key: "allFailed", value: false },
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
  web-collection.mjs doctor [--json] [--searxng <url>] [--playwright-ws <ws-endpoint>]
  web-collection.mjs collect <url...> --output <dir> [--input-file <path>] [--render] [--playwright-ws <ws-endpoint>] [--clean]
      [--user-agent <ua>] [--delay-ms N] [--timeout-ms N] [--max-bytes N]
  web-collection.mjs harvest <page-url> --output <dir> [--match <regex>] [--ext csv]
      [--same-host] [--limit N] [--render] [--playwright-ws <ws-endpoint>] [--clean] [--ignore-robots]
  web-collection.mjs spider <page-url> --output <dir> [--limit N] [--render] [--playwright-ws <ws-endpoint>] [--clean] [--ignore-robots]
  web-collection.mjs search <query...> --output <dir> [--searxng <url>] [--limit N] [--collect]
      [--categories <cats>] [--engines <engines>] [--language <lang>]
      [--safesearch <0|1|2>] [--time-range <day|week|month|year>] [--pageno N]
  web-collection.mjs validate <run-directory>
  web-collection.mjs status <run-directory> [--json]
  web-collection.mjs refresh <run-directory>
  web-collection.mjs retry <run-directory> (--item <id> | --all-failed)
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
	else if (command === "spider") await commandSpider(positionals, flags);
	else if (command === "search") await commandSearch(positionals, flags);
	else if (command === "validate") {
		if (positionals.length !== 1) fail("validate requires exactly one run directory");
		const runDirectory = resolve(positionals[0]);
		if (!existsSync(runDirectory) || !lstatSync(runDirectory).isDirectory()) fail(`run directory does not exist: ${runDirectory}`);
		validate(runDirectory);
	} else if (command === "status") {
		if (positionals.length !== 1) fail("status requires exactly one run directory");
		commandStatus(resolve(positionals[0]));
	} else if (command === "refresh") {
		if (positionals.length !== 1) fail("refresh requires exactly one run directory");
		commandRefresh(resolve(positionals[0]));
	} else if (command === "retry") {
		if (positionals.length !== 1 || Boolean(flags.item) === Boolean(flags.allFailed)) fail("retry requires a run directory and exactly one of --item or --all-failed");
		commandRetry(resolve(positionals[0]), flags);
	} else fail(`unknown command: ${command}`, 2);
}

main().catch((error) => fail(error instanceof Error ? error.stack || error.message : String(error)));

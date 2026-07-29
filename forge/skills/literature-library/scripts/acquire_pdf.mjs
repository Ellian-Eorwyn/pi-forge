#!/usr/bin/env node

// Network acquisition for literature-library. Stateless by contract: this tool
// never reads or writes run_state.json, so a crash mid-batch leaves every
// uncommitted unit pending and the Python orchestrator returns it again.
//
// Lives in Node because the settings ladder in ../../../lib/connected-services.mjs
// and the Playwright endpoint are Node-side. Everything durable -- run state,
// filenames, publication -- stays in literature-library.py.

import { createHash } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { resolveConnectedServices } from "../../../lib/connected-services.mjs";
import { ToolInputError, okResult, optionalInteger, requiredString, runTool } from "../../../lib/tool_contract.mjs";

const UNPAYWALL_BASE = "https://api.unpaywall.org/v2";
const DOI_RESOLVER = "https://doi.org";
const MAX_REDIRECTS = 8;
const REDIRECT_STATUSES = new Set([301, 302, 303, 307, 308]);
const DEFAULT_TIMEOUT_MS = 45_000;
const DEFAULT_MAX_BYTES = 50 * 1024 * 1024;

// Per-host spacing. Calibrated with the owner: 3s for open-access work, a little
// wider for institutional requests, both inside the 2-5s band they set.
const DEFAULT_HOST_DELAY_MS = 3000;
const DEFAULT_INSTITUTIONAL_HOST_DELAY_MS = 4000;

// Three consecutive refusals from one host means that host has decided about us.
// Continuing to ask is what turns a slow run into a blocked institution.
const CIRCUIT_BREAKER_THRESHOLD = 3;
const REFUSAL_STATUSES = new Set([401, 402, 403, 429]);

// A PDF is identified by its magic number, never by Content-Type. Publishers
// routinely serve an HTML paywall interstitial labeled application/pdf.
const PDF_MAGIC = Buffer.from("%PDF-", "ascii");

// Only used for the direct path, and it says who we are. No consumer-browser
// spoofing: an honest agent string is what lets a publisher rate-limit us
// instead of banning the address block.
function userAgent(contactEmail) {
	return `pi-forge-literature-library/1 (+https://github.com/pi-forge; mailto:${contactEmail})`;
}

const hostState = new Map();

function hostFor(url) {
	try {
		return new URL(url).hostname.toLowerCase();
	} catch {
		return null;
	}
}

function stateFor(host) {
	let state = hostState.get(host);
	if (!state) {
		state = { lastRequestAt: 0, consecutiveRefusals: 0, tripped: false, requests: 0, retryAfterUntil: 0 };
		hostState.set(host, state);
	}
	return state;
}

const sleep = (milliseconds) => new Promise((done) => setTimeout(done, milliseconds));

async function waitForHost(host, delayMs) {
	const state = stateFor(host);
	const now = Date.now();
	const earliest = Math.max(state.lastRequestAt + delayMs, state.retryAfterUntil);
	if (earliest > now) await sleep(earliest - now);
	state.lastRequestAt = Date.now();
	state.requests += 1;
}

function isPrivateOrMetadataHost(host) {
	return (
		host === "localhost" ||
		host.endsWith(".localhost") ||
		host === "169.254.169.254" ||
		host.startsWith("169.254.") ||
		host === "metadata" ||
		host === "metadata.google.internal" ||
		host === "0.0.0.0" ||
		host === "::1" ||
		host === "[::1]" ||
		/^127\./.test(host) ||
		/^10\./.test(host) ||
		/^192\.168\./.test(host) ||
		/^172\.(1[6-9]|2\d|3[01])\./.test(host)
	);
}

function assertFetchableUrl(rawUrl, options) {
	let parsed;
	try {
		parsed = new URL(rawUrl);
	} catch {
		throw new ToolInputError("invalid_url", `not a URL: ${rawUrl}`);
	}
	if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
		throw new ToolInputError("unsupported_scheme", `only http/https is fetchable: ${rawUrl}`);
	}
	const host = parsed.hostname.toLowerCase();
	// Candidate URLs come from a citation file and from publisher HTML, so they
	// are outside data: refuse loopback, private, and cloud-metadata addresses.
	// `allowPrivateHosts` exists so the test suite can serve fixtures from
	// 127.0.0.1 without contacting a real publisher. literature-library.py never
	// sets it, so the production path always enforces the guard.
	if (!options?.allowPrivateHosts && isPrivateOrMetadataHost(host)) {
		throw new ToolInputError("refused_host", `refused loopback, private, or metadata host: ${host}`);
	}
	return parsed;
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
			// Refuse rather than keep a partial file: a truncated PDF that opens
			// is worse than no PDF, because nothing downstream can tell.
			return { buffer: Buffer.concat(chunks), truncated: true };
		}
		chunks.push(Buffer.from(value));
	}
	return { buffer: Buffer.concat(chunks), truncated: false };
}

async function fetchFollowing(url, options) {
	const chain = [];
	const visited = new Set();
	let current = url;
	for (let hop = 0; hop <= MAX_REDIRECTS; hop += 1) {
		const parsed = assertFetchableUrl(current, options);
		const host = parsed.hostname.toLowerCase();
		const state = stateFor(host);
		if (state.tripped) {
			const error = new Error(`host tripped the circuit breaker earlier in this run: ${host}`);
			error.code = "host_tripped";
			throw error;
		}
		await waitForHost(host, options.hostDelayMs);

		const controller = new AbortController();
		const timer = setTimeout(() => controller.abort(), options.timeoutMs);
		let response;
		try {
			response = await fetch(current, {
				redirect: "manual",
				signal: controller.signal,
				headers: {
					"user-agent": options.userAgent,
					accept: options.accept,
					"accept-language": "en-US,en;q=0.9",
				},
			});
		} catch (error) {
			throw new Error(
				error.name === "AbortError" ? `request timed out after ${options.timeoutMs}ms` : error.message,
			);
		} finally {
			clearTimeout(timer);
		}

		if (REFUSAL_STATUSES.has(response.status)) {
			state.consecutiveRefusals += 1;
			const retryAfter = Number.parseInt(response.headers.get("retry-after") ?? "", 10);
			if (Number.isInteger(retryAfter) && retryAfter > 0) {
				state.retryAfterUntil = Date.now() + Math.min(retryAfter, 300) * 1000;
			}
			if (state.consecutiveRefusals >= CIRCUIT_BREAKER_THRESHOLD) state.tripped = true;
			return { response, finalUrl: current, chain, body: null, refused: true, tripped: state.tripped };
		}
		state.consecutiveRefusals = 0;

		if (REDIRECT_STATUSES.has(response.status) && response.headers.get("location")) {
			const next = new URL(response.headers.get("location"), current).toString();
			chain.push({ from: current, to: next, status: response.status });
			if (visited.has(next)) throw new Error(`redirect loop detected at ${next}`);
			visited.add(current);
			current = next;
			continue;
		}

		const body = await readCappedBody(response, options.maxBytes);
		return { response, finalUrl: current, chain, body, refused: false, tripped: false };
	}
	throw new Error(`exceeded ${MAX_REDIRECTS} redirects`);
}

function isPdf(buffer) {
	// Some publishers prepend whitespace or a BOM before the header.
	return buffer.subarray(0, 1024).includes(PDF_MAGIC);
}

// Landing-page discovery, in priority order. `citation_pdf_url` is the standard
// publishers and repositories are supposed to expose, and when present it is
// authoritative. The repository path patterns below are fallbacks for Pure,
// DSpace, EPrints, OJS, and PMC; they are the part of this file that rots,
// because repository software changes its URL shapes between major versions.
// Verified against live repositories on 2026-07-29.
const PDF_META_PATTERNS = [
	/<meta[^>]+name=["']citation_pdf_url["'][^>]+content=["']([^"']+)["']/i,
	/<meta[^>]+content=["']([^"']+)["'][^>]+name=["']citation_pdf_url["']/i,
	/<link[^>]+type=["']application\/pdf["'][^>]+href=["']([^"']+)["']/i,
	/<link[^>]+href=["']([^"']+)["'][^>]+type=["']application\/pdf["']/i,
];
const REPOSITORY_HREF_PATTERNS = [
	/href=["']([^"']*\/bitstream\/[^"']+\.pdf[^"']*)["']/i,
	// PMC exposes the PDF as a relative /articles/PMC*/pdf/*.pdf link.
	/href=["']([^"']*\/articles\/PMC\d+\/pdf\/[^"']*)["']/i,
	/href=["']([^"']*\/download\/[^"']+)["']/i,
	/href=["']([^"']*\/files\/[^"']+\.pdf[^"']*)["']/i,
	/href=["']([^"']*\/content\/pdf\/[^"']+\.pdf[^"']*)["']/i,
	/href=["']([^"']*pdfdirect[^"']*)["']/i,
	// DSpace and EPrints landing pages link the file without a .pdf suffix.
	/href=["']([^"']*\/bitstreams?\/[^"']+\/(?:download|content)[^"']*)["']/i,
	/href=["']([^"']+\.pdf(?:\?[^"']*)?)["']/i,
];

function discoverPdfLinks(html, baseUrl) {
	const found = [];
	const add = (raw, source) => {
		if (!raw) return;
		try {
			const absolute = new URL(raw.replace(/&amp;/g, "&"), baseUrl);
			absolute.hash = "";
			const value = absolute.toString();
			if ((absolute.protocol === "https:" || absolute.protocol === "http:") && !found.some((e) => e.url === value)) {
				found.push({ url: value, source });
			}
		} catch {
			// An unparseable href is not worth failing the record over.
		}
	};
	for (const pattern of PDF_META_PATTERNS) add(html.match(pattern)?.[1], "citation_pdf_url");
	for (const pattern of REPOSITORY_HREF_PATTERNS) {
		for (const match of html.matchAll(new RegExp(pattern, "gi"))) add(match[1], "repository-link");
	}
	return found;
}

// Stage 2: an ordinary browser. Several publishers refuse a plain HTTP client
// for content they publish openly -- the refusal is bot management, not a
// paywall -- and a real browser visit satisfies it because it runs the scripts
// and collects the cookies the check expects.
//
// This is deliberately a stock Chromium with its own user agent. No stealth
// plugin, no fingerprint patching, no navigator.webdriver spoofing: behaving
// like a browser is fine, but evading detection is what gets an institution's
// whole address range blocked, and it would also make this tool a liability to
// run. If a publisher still refuses, the record goes to the manual queue.
async function acquireViaBrowser(record, candidates, options, attempts) {
	let playwright;
	try {
		playwright = await import("playwright");
	} catch {
		return { outcome: "unavailable", detail: "playwright is not installed" };
	}

	const services = resolveConnectedServices({});
	const wsEndpoint = services.playwright?.wsEndpoint;
	if (!services.playwright?.enabled || !wsEndpoint) {
		return { outcome: "unavailable", detail: "no Playwright endpoint is configured" };
	}

	let browser;
	try {
		browser = await playwright.chromium.connect(wsEndpoint, { timeout: options.browserConnectTimeoutMs });
	} catch (error) {
		return { outcome: "unavailable", detail: `could not reach the browser service: ${error.message.slice(0, 120)}` };
	}

	const context = await browser.newContext({ acceptDownloads: false });
	try {
		for (const candidate of candidates.slice(0, options.maxCandidates)) {
			const host = hostFor(candidate.url);
			if (!host || stateFor(host).tripped) continue;
			await waitForHost(host, options.hostDelayMs);

			const page = await context.newPage();
			try {
				let response;
				try {
					response = await page.goto(candidate.url, {
						waitUntil: "domcontentloaded",
						timeout: options.browserNavigationTimeoutMs,
					});
				} catch (error) {
					attempts.push({ url: candidate.url, source: `browser:${candidate.source}`, outcome: "error", detail: error.message.slice(0, 160) });
					continue;
				}
				if (!response) {
					attempts.push({ url: candidate.url, source: `browser:${candidate.source}`, outcome: "error", detail: "no response" });
					continue;
				}

				const status = response.status();
				const contentType = response.headers()["content-type"] ?? "";
				const base = { url: candidate.url, source: `browser:${candidate.source}`, finalUrl: page.url(), status, contentType };

				// The navigation itself may have delivered the PDF.
				let body = null;
				try {
					body = await response.body();
				} catch {
					// Chromium hands PDF navigations to its viewer, which makes the
					// body unreadable. The in-page request below recovers it.
				}
				if (body && isPdf(body)) {
					attempts.push({ ...base, outcome: "pdf", bytes: body.length });
					return { outcome: "pdf", buffer: body, finalUrl: page.url(), via: candidate.url };
				}
				if (REFUSAL_STATUSES.has(status)) {
					attempts.push({ ...base, outcome: "blocked" });
					continue;
				}

				// Ask the page for its own PDF link, then fetch it through the
				// context so the cookies the visit established travel with it.
				const links = await page
					.evaluate(() => {
						const found = [];
						const meta = document.querySelector('meta[name="citation_pdf_url"]');
						if (meta?.content) found.push(meta.content);
						for (const link of document.querySelectorAll('link[type="application/pdf"]')) {
							if (link.href) found.push(link.href);
						}
						for (const anchor of document.querySelectorAll("a[href]")) {
							const href = anchor.href || "";
							if (/\.pdf($|[?#])|pdfdirect|\/content\/pdf\/|\/bitstream\/|\/pdf\/?$/i.test(href)) found.push(href);
						}
						return found.slice(0, 8);
					})
					.catch(() => []);

				attempts.push({ ...base, outcome: links.length ? "landing-page" : "not-pdf", discovered: links.length });

				for (const link of links.slice(0, options.maxDiscoveredLinks)) {
					const linkHost = hostFor(link);
					if (!linkHost || stateFor(linkHost).tripped) continue;
					await waitForHost(linkHost, options.hostDelayMs);
					let fetched;
					try {
						fetched = await context.request.get(link, { timeout: options.browserNavigationTimeoutMs, maxRedirects: 5 });
					} catch (error) {
						attempts.push({ url: link, source: "browser:in-page-request", outcome: "error", detail: error.message.slice(0, 160) });
						continue;
					}
					const buffer = Buffer.from(await fetched.body());
					if (buffer.length > options.maxBytes) {
						attempts.push({ url: link, source: "browser:in-page-request", outcome: "too-large", status: fetched.status() });
						continue;
					}
					if (isPdf(buffer)) {
						attempts.push({ url: link, source: "browser:in-page-request", outcome: "pdf", status: fetched.status(), bytes: buffer.length });
						return { outcome: "pdf", buffer, finalUrl: link, via: candidate.url };
					}
					attempts.push({ url: link, source: "browser:in-page-request", outcome: "not-pdf", status: fetched.status() });
				}
			} finally {
				await page.close().catch(() => {});
			}
		}
		return { outcome: "exhausted" };
	} finally {
		await context.close().catch(() => {});
		await browser.close().catch(() => {});
	}
}

async function resolveOpenAccess(doi, options) {
	const url = `${UNPAYWALL_BASE}/${encodeURIComponent(doi)}?email=${encodeURIComponent(options.contactEmail)}`;
	await waitForHost("api.unpaywall.org", 1000);
	const controller = new AbortController();
	const timer = setTimeout(() => controller.abort(), options.timeoutMs);
	try {
		const response = await fetch(url, { signal: controller.signal, headers: { "user-agent": options.userAgent } });
		if (response.status === 404) return { found: false, oaStatus: null, locations: [] };
		if (!response.ok) throw new Error(`Unpaywall returned HTTP ${response.status}`);
		const body = await response.json();
		const locations = [];
		const push = (location) => {
			if (!location) return;
			locations.push({
				url: location.url ?? null,
				pdfUrl: location.url_for_pdf ?? null,
				hostType: location.host_type ?? null,
				version: location.version ?? null,
				license: location.license ?? null,
			});
		};
		push(body.best_oa_location);
		for (const location of body.oa_locations ?? []) {
			if (location !== body.best_oa_location) push(location);
		}
		return { found: true, oaStatus: body.oa_status ?? null, isOa: Boolean(body.is_oa), locations, title: body.title ?? null };
	} finally {
		clearTimeout(timer);
	}
}

function candidateUrls(record, resolved) {
	const seen = new Set();
	const candidates = [];
	const add = (url, source) => {
		if (!url || seen.has(url)) return;
		seen.add(url);
		candidates.push({ url, source });
	};
	// A file link the citation file already carried costs nothing to try first.
	for (const candidate of record.fullTextCandidates ?? []) add(candidate.url, candidate.source ?? "citation-file");

	// arXiv and PMC are canonical open archives that serve PDFs at a predictable
	// path and do not bot-block. Unpaywall does not index every arXiv DOI -- a
	// `10.48550/arxiv.*` DOI is itself the identifier -- so deriving the URL
	// recovers records that would otherwise have no candidate at all.
	if (record.arxivId) add(`https://arxiv.org/pdf/${record.arxivId}`, "arxiv-derived");
	if (record.pmcid) add(`https://pmc.ncbi.nlm.nih.gov/articles/${record.pmcid}/pdf/`, "pmc-derived");

	for (const location of resolved?.locations ?? []) add(location.pdfUrl, "unpaywall-pdf");
	for (const location of resolved?.locations ?? []) add(location.url, "unpaywall-landing");
	return candidates;
}

async function attemptDirect(candidate, options, attempts) {
	let result;
	try {
		result = await fetchFollowing(candidate.url, { ...options, accept: "application/pdf,*/*;q=0.8" });
	} catch (error) {
		attempts.push({
			url: candidate.url,
			source: candidate.source,
			outcome: error.code === "host_tripped" ? "host-tripped" : "error",
			detail: error.message,
		});
		return { outcome: error.code === "host_tripped" ? "host-tripped" : "error" };
	}

	const status = result.response.status;
	const contentType = result.response.headers.get("content-type") ?? "";
	const base = { url: candidate.url, source: candidate.source, finalUrl: result.finalUrl, status, contentType };

	if (result.refused) {
		attempts.push({ ...base, outcome: "blocked", tripped: result.tripped });
		return { outcome: "blocked", tripped: result.tripped };
	}
	if (status === 404 || status === 410) {
		attempts.push({ ...base, outcome: "not-found" });
		return { outcome: "not-found" };
	}
	if (!result.response.ok) {
		attempts.push({ ...base, outcome: "error", detail: `HTTP ${status}` });
		return { outcome: "error", status };
	}
	if (result.body.truncated) {
		attempts.push({ ...base, outcome: "too-large", detail: `exceeded ${options.maxBytes} bytes` });
		return { outcome: "too-large" };
	}
	if (isPdf(result.body.buffer)) {
		attempts.push({ ...base, outcome: "pdf", bytes: result.body.buffer.length });
		return { outcome: "pdf", buffer: result.body.buffer, finalUrl: result.finalUrl, contentType };
	}

	// Not a PDF. If it is HTML it is a landing page, which stage 3 can mine for
	// a real PDF link exactly once -- deeper would make this a crawler.
	const text = result.body.buffer.toString("utf8");
	const looksHtml = /text\/html|application\/xhtml/i.test(contentType) || /<html[\s>]/i.test(text.slice(0, 2048));
	attempts.push({ ...base, outcome: looksHtml ? "landing-page" : "not-pdf", bytes: result.body.buffer.length });
	return looksHtml ? { outcome: "landing-page", html: text, finalUrl: result.finalUrl } : { outcome: "not-pdf" };
}

async function acquireOne(record, options) {
	const attempts = [];
	const warnings = [];
	let accessClass = record.accessClass ?? "unknown";
	let resolved = null;

	const doi = record.doi ?? null;
	if (doi && options.resolve) {
		try {
			resolved = await resolveOpenAccess(doi, options);
			if (resolved.found) {
				accessClass = resolved.isOa ? "open-access" : "institutional";
			} else {
				warnings.push(`Unpaywall has no record for ${doi}.`);
			}
		} catch (error) {
			warnings.push(`Unpaywall lookup failed for ${doi}: ${error.message}`);
		}
	}

	const institutional = accessClass === "institutional";
	const hostDelayMs = institutional ? options.institutionalHostDelayMs : options.hostDelayMs;

	// A remote Playwright service egresses from another host, so it cannot carry
	// the operator's institutional access. Refusing here rather than in prose
	// keeps a licensed resource from being requested by an unrelated address.
	if (institutional && options.browser) {
		warnings.push("Browser-assisted fetching is not available for institutional records; a remote browser cannot carry institutional access.");
	}

	// Institutional records are only reachable when the operator's own machine is
	// on the institution's network, and that is established out of band.
	if (institutional && !options.campusEgress) {
		return {
			id: record.id,
			disposition: "deferred-institutional",
			accessClass,
			oaStatus: resolved?.oaStatus ?? null,
			oaLocations: resolved?.locations ?? [],
			attempts,
			warnings,
			nextAction: "connect-vpn-and-resume",
		};
	}

	const candidates = candidateUrls(record, resolved);
	if (candidates.length === 0 && doi && institutional) {
		// With no open-access location, the DOI resolver is the only way to reach
		// the publisher's copy, and it must travel from the local process.
		candidates.push({ url: `${DOI_RESOLVER}/${doi}`, source: "doi-resolver" });
	}
	if (candidates.length === 0) {
		return {
			id: record.id,
			disposition: "no-candidate",
			accessClass,
			oaStatus: resolved?.oaStatus ?? null,
			oaLocations: resolved?.locations ?? [],
			attempts,
			warnings,
		};
	}

	const fetchOptions = { ...options, hostDelayMs };
	let sawBlocked = false;
	let sawNotFound = false;
	const landingPages = [];

	for (const candidate of candidates.slice(0, options.maxCandidates)) {
		const result = await attemptDirect(candidate, fetchOptions, attempts);
		if (result.outcome === "pdf") {
			return {
				id: record.id,
				disposition: "acquired",
				accessClass,
				oaStatus: resolved?.oaStatus ?? null,
				oaLocations: resolved?.locations ?? [],
				bytes: result.buffer.length,
				sha256: createHash("sha256").update(result.buffer).digest("hex"),
				stagedPath: stage(record, result.buffer, options),
				sourceUrl: candidate.url,
				finalUrl: result.finalUrl,
				stage: "direct",
				attempts,
				warnings,
			};
		}
		if (result.outcome === "landing-page") landingPages.push({ html: result.html, finalUrl: result.finalUrl });
		if (result.outcome === "blocked") sawBlocked = true;
		if (result.outcome === "not-found") sawNotFound = true;
		if (result.tripped) break;
	}

	// Stage 3: one level of landing-page mining, then stop.
	for (const landing of landingPages.slice(0, 2)) {
		const discovered = discoverPdfLinks(landing.html, landing.finalUrl);
		for (const link of discovered.slice(0, options.maxDiscoveredLinks)) {
			const result = await attemptDirect({ url: link.url, source: `landing:${link.source}` }, fetchOptions, attempts);
			if (result.outcome === "pdf") {
				return {
					id: record.id,
					disposition: "acquired",
					accessClass,
					oaStatus: resolved?.oaStatus ?? null,
					oaLocations: resolved?.locations ?? [],
					bytes: result.buffer.length,
					sha256: createHash("sha256").update(result.buffer).digest("hex"),
					stagedPath: stage(record, result.buffer, options),
					sourceUrl: link.url,
					finalUrl: result.finalUrl,
					stage: "landing-scrape",
					attempts,
					warnings,
				};
			}
			if (result.outcome === "blocked") sawBlocked = true;
			if (result.tripped) break;
		}
	}

	// Stage 2, last: only for open-access records, and only once the cheap paths
	// have failed. A remote browser cannot carry institutional access, so this is
	// gated on access class rather than on configuration.
	if (options.browser && accessClass === "open-access") {
		const viaBrowser = await acquireViaBrowser(record, candidates, fetchOptions, attempts);
		if (viaBrowser.outcome === "pdf") {
			return {
				id: record.id,
				disposition: "acquired",
				accessClass,
				oaStatus: resolved?.oaStatus ?? null,
				oaLocations: resolved?.locations ?? [],
				bytes: viaBrowser.buffer.length,
				sha256: createHash("sha256").update(viaBrowser.buffer).digest("hex"),
				stagedPath: stage(record, viaBrowser.buffer, options),
				sourceUrl: viaBrowser.via,
				finalUrl: viaBrowser.finalUrl,
				stage: "browser",
				attempts,
				warnings,
			};
		}
		if (viaBrowser.outcome === "unavailable") warnings.push(`Browser stage skipped: ${viaBrowser.detail}.`);
	}

	const disposition = sawBlocked ? "manual" : sawNotFound ? "not-found" : "manual";
	if (sawBlocked) {
		warnings.push(
			accessClass === "open-access"
				? "An open-access PDF was refused by the publisher's bot protection; it needs manual retrieval or a browser."
				: "The publisher refused the request; it needs manual retrieval.",
		);
	}
	return {
		id: record.id,
		disposition,
		accessClass,
		oaStatus: resolved?.oaStatus ?? null,
		oaLocations: resolved?.locations ?? [],
		attempts,
		warnings,
	};
}

function stage(record, buffer, options) {
	// Written outside its final path, per the run-state contract: Python verifies
	// and publishes it under a hash-bound move operation.
	const target = resolve(options.stageDirectory, `${record.id}.attempt.bin`);
	mkdirSync(dirname(target), { recursive: true });
	writeFileSync(target, buffer);
	return target;
}

await runTool(async (input) => {
	const contactEmail = requiredString(input, "contactEmail");
	const stageDirectory = resolve(requiredString(input, "stageDirectory"));
	if (!Array.isArray(input.records) || input.records.length === 0) {
		throw new ToolInputError("missing_required_field", "records must be a non-empty array");
	}

	const hostDelayMs = optionalInteger(input, "hostDelayMs", DEFAULT_HOST_DELAY_MS);
	const institutionalHostDelayMs = optionalInteger(
		input,
		"institutionalHostDelayMs",
		Math.max(hostDelayMs, DEFAULT_INSTITUTIONAL_HOST_DELAY_MS),
	);
	// A floor, not a default. Nothing in the input can ask this tool to hammer a
	// publisher, because the cost of that lands on an institution rather than on
	// whoever configured the run.
	const floorMs = 2000;
	const options = {
		contactEmail,
		stageDirectory,
		userAgent: userAgent(contactEmail),
		timeoutMs: optionalInteger(input, "timeoutMs", DEFAULT_TIMEOUT_MS),
		maxBytes: optionalInteger(input, "maxBytes", DEFAULT_MAX_BYTES),
		hostDelayMs: Math.max(hostDelayMs, floorMs),
		institutionalHostDelayMs: Math.max(institutionalHostDelayMs, floorMs),
		maxCandidates: optionalInteger(input, "maxCandidates", 4),
		maxDiscoveredLinks: optionalInteger(input, "maxDiscoveredLinks", 3),
		browserConnectTimeoutMs: optionalInteger(input, "browserConnectTimeoutMs", 15_000),
		browserNavigationTimeoutMs: optionalInteger(input, "browserNavigationTimeoutMs", 60_000),
		resolve: input.resolve !== false,
		allowPrivateHosts: Boolean(input.allowPrivateHosts),
		campusEgress: Boolean(input.campusEgress),
		browser: Boolean(input.browser),
	};

	const results = [];
	for (const record of input.records) {
		results.push(await acquireOne(record, options));
	}

	const trippedHosts = [...hostState.entries()].filter(([, state]) => state.tripped).map(([host]) => host);
	return okResult({
		warnings: trippedHosts.map((host) => `Stopped requesting ${host} after ${CIRCUIT_BREAKER_THRESHOLD} consecutive refusals.`),
		data: {
			results,
			hosts: [...hostState.entries()].map(([host, state]) => ({
				host,
				requests: state.requests,
				tripped: state.tripped,
				consecutiveRefusals: state.consecutiveRefusals,
			})),
			trippedHosts,
		},
	});
});

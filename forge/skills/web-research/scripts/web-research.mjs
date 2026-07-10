#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { basename, join, resolve, sep } from "node:path";
import { JSDOM, VirtualConsole } from "jsdom";
import { Readability } from "@mozilla/readability";
import { resolveConnectedServices } from "../../../lib/connected-services.mjs";

const DEFAULT_USER_AGENT = "pi-forge-web-research/1 (+https://github.com/pi-forge)";
const DEFAULT_TIMEOUT_MS = 30_000;
const DEFAULT_LIMIT = 10;
const DEFAULT_READ_COUNT = 5;
const DEFAULT_DEEP_ITERATIONS = 2;
const DEFAULT_DEEP_LIMIT = 6;
const DEFAULT_DEEP_READ_COUNT = 3;
const DEFAULT_DEEP_MAX_QUERIES = 6;
const DEFAULT_DEEP_MAX_SOURCES = 12;
const DEFAULT_DEEP_MAX_FOLLOWUP_QUERIES = 2;
const DEFAULT_DEEP_MAX_MODEL_CALLS = 16;
const DEFAULT_DEEP_MAX_RUNTIME_MS = 600_000;
const DEFAULT_DEEP_MAX_EVIDENCE_CHARS = 12_000;
const DEFAULT_DEEP_MAX_CLAIM_EVIDENCE_ITEMS = 48;
const DEEP_SCHEMA_VERSION = 1;
const ACADEMIC_SCHEMA_VERSION = 1;
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
const ACADEMIC_DEFAULT_PROVIDERS = ["crossref", "semantic-scholar", "europepmc", "pubmed", "arxiv", "datacite", "openaire", "doaj"];
const ACADEMIC_PROVIDER_BASES = {
	crossref: "https://api.crossref.org",
	"semantic-scholar": "https://api.semanticscholar.org",
	europepmc: "https://www.ebi.ac.uk/europepmc/webservices/rest",
	pubmed: "https://eutils.ncbi.nlm.nih.gov/entrez/eutils",
	arxiv: "https://export.arxiv.org/api",
	datacite: "https://api.datacite.org",
	openaire: "https://api.openaire.eu",
	doaj: "https://doaj.org/api",
	unpaywall: "https://api.unpaywall.org/v2",
};
const ACADEMIC_PROVIDER_ENV = {
	crossref: "FORGE_ACADEMIC_CROSSREF_URL",
	"semantic-scholar": "FORGE_ACADEMIC_SEMANTIC_SCHOLAR_URL",
	europepmc: "FORGE_ACADEMIC_EUROPEPMC_URL",
	pubmed: "FORGE_ACADEMIC_PUBMED_URL",
	arxiv: "FORGE_ACADEMIC_ARXIV_URL",
	datacite: "FORGE_ACADEMIC_DATACITE_URL",
	openaire: "FORGE_ACADEMIC_OPENAIRE_URL",
	doaj: "FORGE_ACADEMIC_DOAJ_URL",
	unpaywall: "FORGE_ACADEMIC_UNPAYWALL_URL",
};
const ACADEMIC_PROVIDER_LABELS = {
	crossref: "Crossref",
	"semantic-scholar": "Semantic Scholar",
	europepmc: "Europe PMC",
	pubmed: "PubMed",
	arxiv: "arXiv",
	datacite: "DataCite",
	openaire: "OpenAIRE",
	doaj: "DOAJ",
	unpaywall: "Unpaywall",
};
const quietJsdomConsole = new VirtualConsole();
quietJsdomConsole.on("jsdomError", () => {});

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

function normalizedWords(value) {
	return normalizeTitleKey(value)
		.split(/\s+/)
		.filter((word) => word.length >= 4);
}

function truncateAtBoundary(value, maxChars) {
	const text = String(value ?? "").trim();
	if (text.length <= maxChars) return text;
	const candidate = text.slice(0, maxChars);
	const boundary = Math.max(candidate.lastIndexOf("\n\n"), candidate.lastIndexOf(". "), candidate.lastIndexOf("\n"));
	return `${candidate.slice(0, boundary > maxChars * 0.6 ? boundary + 1 : maxChars).trim()}\n\n[truncated]`;
}

function selectRelevantText(text, question, maxChars) {
	const normalized = normalizeWhitespace(text);
	if (normalized.length <= maxChars) return String(text ?? "").trim();
	const queryWords = new Set(normalizedWords(question));
	const chunks = String(text ?? "")
		.split(/\n{2,}/)
		.map((chunk) => chunk.trim())
		.filter(Boolean);
	if (chunks.length <= 1) return truncateAtBoundary(text, maxChars);
	const scored = chunks.map((chunk, index) => {
		const lower = normalizeTitleKey(chunk);
		let score = index === 0 ? 2 : 0;
		for (const word of queryWords) {
			if (lower.includes(word)) score += 1;
		}
		return { chunk, index, score };
	});
	const selected = [];
	let used = 0;
	for (const item of scored.sort((a, b) => b.score - a.score || a.index - b.index)) {
		const nextSize = item.chunk.length + 2;
		if (used + nextSize > maxChars && selected.length > 0) continue;
		selected.push(item);
		used += nextSize;
		if (used >= maxChars) break;
	}
	return selected
		.sort((a, b) => a.index - b.index)
		.map((item) => item.chunk)
		.join("\n\n")
		.slice(0, maxChars)
		.trim();
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

function stripTags(value) {
	return String(value ?? "")
		.replace(/<[^>]+>/g, " ")
		.replace(/&nbsp;/g, " ")
		.replace(/&amp;/g, "&")
		.replace(/&lt;/g, "<")
		.replace(/&gt;/g, ">")
		.replace(/&quot;/g, '"')
		.replace(/&#39;/g, "'")
		.replace(/\s+/g, " ")
		.trim();
}

function firstText(value) {
	if (Array.isArray(value)) return normalizeWhitespace(value.find((item) => typeof item === "string" && item.trim()) ?? "");
	return normalizeWhitespace(value ?? "");
}

function compactObject(value) {
	const output = {};
	for (const [key, entry] of Object.entries(value)) {
		if (entry === undefined || entry === null) continue;
		if (Array.isArray(entry) && entry.length === 0) continue;
		if (typeof entry === "string" && !entry.trim()) continue;
		output[key] = entry;
	}
	return output;
}

function parseDateParts(parts) {
	if (!Array.isArray(parts) || !Array.isArray(parts[0])) return { date: null, year: null };
	const [year, month, day] = parts[0];
	if (!Number.isInteger(year)) return { date: null, year: null };
	const date = [year, month, day]
		.filter((part) => Number.isInteger(part))
		.map((part, index) => (index === 0 ? String(part) : String(part).padStart(2, "0")))
		.join("-");
	return { date, year };
}

function yearFromDate(value) {
	const match = String(value ?? "").match(/\b(16|17|18|19|20)\d{2}\b/);
	return match ? Number.parseInt(match[0], 10) : null;
}

function normalizeDoi(value) {
	const text = String(value ?? "")
		.trim()
		.replace(/^https?:\/\/(dx\.)?doi\.org\//i, "")
		.replace(/^doi:\s*/i, "")
		.toLowerCase();
	return text || null;
}

function normalizeArxivId(value) {
	const text = String(value ?? "")
		.trim()
		.replace(/^https?:\/\/arxiv\.org\/abs\//i, "")
		.replace(/^arxiv:/i, "")
		.replace(/v\d+$/i, "");
	return text || null;
}

function normalizeIdentifier(value) {
	const text = String(value ?? "").trim();
	return text || null;
}

function normalizeTitleKey(value) {
	return normalizeWhitespace(value)
		.toLowerCase()
		.normalize("NFKD")
		.replace(/[\u0300-\u036f]/g, "")
		.replace(/[^\p{L}\p{N}]+/gu, " ")
		.trim();
}

function authorDisplayName(author) {
	if (!author) return "";
	if (typeof author === "string") return normalizeWhitespace(author);
	const parts = [author.family, author.given].filter(Boolean);
	if (parts.length > 0) return normalizeWhitespace(parts.join(", "));
	if (author.name) return normalizeWhitespace(author.name);
	return "";
}

function firstAuthorKey(authors) {
	const name = authorDisplayName(authors?.[0]);
	return normalizeTitleKey(name).split(" ", 1)[0] ?? "";
}

function academicProviderBase(provider, flags = {}) {
	const envName = ACADEMIC_PROVIDER_ENV[provider];
	const explicit = flags[`${provider}Base`];
	const base = explicit || (envName ? process.env[envName] : null) || ACADEMIC_PROVIDER_BASES[provider];
	return String(base ?? "").trim().replace(/\/+$/, "");
}

function academicProviderList(flags, classification) {
	const requested = flags.providers
		? String(flags.providers)
				.split(",")
				.map((provider) => provider.trim())
				.filter(Boolean)
		: null;
	const providers = requested ?? ACADEMIC_DEFAULT_PROVIDERS;
	const unique = [...new Set(providers)];
	if (flags.contactEmail || process.env.FORGE_ACADEMIC_CONTACT_EMAIL || process.env.UNPAYWALL_EMAIL) unique.push("unpaywall");
	return unique.filter((provider) => {
		if (!ACADEMIC_PROVIDER_BASES[provider]) return false;
		if (provider === "arxiv" && classification.domain !== "technical-preprint" && classification.domain !== "general") return flags.providers?.includes(provider);
		if (provider === "pubmed" && classification.domain !== "biomedical" && !flags.providers?.includes(provider)) return true;
		return true;
	});
}

function classifyAcademicQuery(query) {
	const lower = query.toLowerCase();
	const identifiers = {
		doi: lower.match(/\b10\.\d{4,9}\/[-._;()/:a-z0-9]+\b/i)?.[0] ?? null,
		pmid: lower.match(/\bpmid[:\s]*(\d{6,9})\b/i)?.[1] ?? null,
		pmcid: lower.match(/\bpmc\d+\b/i)?.[0]?.toUpperCase() ?? null,
		arxivId: lower.match(/\barxiv[:\s]*(\d{4}\.\d{4,5}(v\d+)?|[a-z-]+\/\d{7}(v\d+)?)\b/i)?.[1] ?? null,
	};
	let domain = "general";
	if (/\b(clinical|biomedical|pubmed|pmid|pmc|disease|drug|therapy|genetic|neuroscience|epidemiology|public health)\b/.test(lower)) {
		domain = "biomedical";
	} else if (/\b(arxiv|preprint|machine learning|computer science|physics|mathematics|statistics|algorithm)\b/.test(lower)) {
		domain = "technical-preprint";
	} else if (/\b(dataset|software|repository|zenodo|figshare|data citation)\b/.test(lower)) {
		domain = "data-software";
	} else if (/\b(open access|oa|doaj|unpaywall|repository copy)\b/.test(lower)) {
		domain = "oa-focused";
	}
	const goals = ["broad-discovery"];
	if (identifiers.doi || /\bdoi\b/.test(lower)) goals.push("doi-lookup");
	if (/\babstract\b/.test(lower)) goals.push("abstract-retrieval");
	if (/\bfull text|open access|pdf\b/.test(lower)) goals.push("oa-discovery");
	return { domain, goals, identifiers };
}

function academicWorkKey(record) {
	const identifiers = record.identifiers ?? {};
	if (identifiers.doi) return `doi:${normalizeDoi(identifiers.doi)}`;
	if (identifiers.pmid) return `pmid:${identifiers.pmid}`;
	if (identifiers.pmcid) return `pmcid:${identifiers.pmcid}`;
	if (identifiers.arxiv_id) return `arxiv:${normalizeArxivId(identifiers.arxiv_id)}`;
	if (identifiers.semantic_scholar_paper_id) return `s2:${identifiers.semantic_scholar_paper_id}`;
	const title = normalizeTitleKey(record.canonical_title);
	return `title:${title}|${record.publication_year ?? ""}|${firstAuthorKey(record.authors)}`;
}

function sourceRecordId(provider, providerRecordId, index) {
	return `sr-${sha256(`${provider}:${providerRecordId ?? "record"}:${index}`).slice(0, 12)}`;
}

function fieldValueMap(record) {
	return {
		canonical_title: record.canonical_title,
		abstract_best: record.abstract_best,
		authors: record.authors,
		publication_year: record.publication_year,
		publication_date: record.publication_date,
		venue_name: record.venue_name,
		venue_type: record.venue_type,
		publisher: record.publisher,
		type: record.type,
		identifiers: record.identifiers,
		urls: record.urls,
		subjects: record.subjects,
		licenses: record.licenses,
		oa_status: record.oa_status,
		oa_locations: record.oa_locations,
		full_text_candidates: record.full_text_candidates,
		funders: record.funders,
	};
}

function providerPriority(provider) {
	return {
		pubmed: 1,
		europepmc: 2,
		crossref: 3,
		arxiv: 4,
		"semantic-scholar": 5,
		datacite: 6,
		openaire: 7,
		doaj: 8,
		unpaywall: 9,
	}[provider] ?? 10;
}

function preferValue(field, current, incoming, currentProvider, incomingProvider) {
	if (incoming === undefined || incoming === null) return false;
	if (Array.isArray(incoming) && incoming.length === 0) return false;
	if (typeof incoming === "string" && !incoming.trim()) return false;
	if (current === undefined || current === null) return true;
	if (Array.isArray(current) && current.length === 0) return true;
	if (typeof current === "string" && !current.trim()) return true;
	if (field === "abstract_best") return providerPriority(incomingProvider) < providerPriority(currentProvider);
	return false;
}

function mergeArrayUnique(current = [], incoming = [], keyFn = (value) => JSON.stringify(value)) {
	const byKey = new Map();
	for (const value of [...current, ...incoming]) {
		const key = keyFn(value);
		if (!key || byKey.has(key)) continue;
		byKey.set(key, value);
	}
	return [...byKey.values()];
}

function mergeIdentifiers(current = {}, incoming = {}) {
	const merged = { ...current };
	for (const [key, value] of Object.entries(incoming)) {
		if (value === undefined || value === null || value === "") continue;
		if (Array.isArray(value)) merged[key] = mergeArrayUnique(asArray(merged[key]), value, (entry) => String(entry).toLowerCase());
		else if (!merged[key]) merged[key] = value;
	}
	return compactObject(merged);
}

function createCanonicalWork(normalized, sourceRecord) {
	const key = academicWorkKey(normalized);
	const workId = `work-${sha256(key).slice(0, 12)}`;
	return {
		work_id: workId,
		canonical_title: normalized.canonical_title ?? null,
		normalized_title: normalizeTitleKey(normalized.canonical_title),
		abstract_best: normalized.abstract_best ?? null,
		abstract_best_source: normalized.abstract_best ? sourceRecord.source_record_id : null,
		abstract_alternates: normalized.abstract_best ? [{ value: normalized.abstract_best, sourceRecordId: sourceRecord.source_record_id }] : [],
		authors: normalized.authors ?? [],
		publication_year: normalized.publication_year ?? null,
		publication_date: normalized.publication_date ?? null,
		venue_name: normalized.venue_name ?? null,
		venue_type: normalized.venue_type ?? null,
		publisher: normalized.publisher ?? null,
		type: normalized.type ?? "unknown",
		identifiers: compactObject(normalized.identifiers ?? {}),
		urls: normalized.urls ?? [],
		source_records: [sourceRecord.source_record_id],
		oa_status: normalized.oa_status ?? null,
		oa_locations: normalized.oa_locations ?? [],
		full_text_candidates: normalized.full_text_candidates ?? [],
		licenses: normalized.licenses ?? [],
		subjects: normalized.subjects ?? [],
		fields_of_study: normalized.fields_of_study ?? [],
		funders: normalized.funders ?? [],
		grants: normalized.grants ?? [],
		institutions: normalized.institutions ?? [],
		citations_count_by_provider: normalized.citations_count_by_provider ?? [],
		references: [],
		citations: [],
		related_works: [],
		retraction_or_update_status: normalized.retraction_or_update_status ?? null,
		confidence_score: 0.5,
		dedupe_cluster_id: `cluster-${sha256(key).slice(0, 12)}`,
		created_at: nowIso(),
		updated_at: nowIso(),
		_fieldSources: Object.fromEntries(Object.keys(fieldValueMap(normalized)).map((field) => [field, sourceRecord.provider])),
	};
}

function addFieldProvenance(rows, work, sourceRecord, normalized, conflictStatus = "uncontested") {
	for (const [field, value] of Object.entries(fieldValueMap(normalized))) {
		if (value === undefined || value === null) continue;
		if (Array.isArray(value) && value.length === 0) continue;
		if (typeof value === "string" && !value.trim()) continue;
		rows.push({
			field_name: field,
			work_id: work.work_id,
			value,
			source_provider: sourceRecord.provider,
			source_record_id: sourceRecord.source_record_id,
			source_path: normalized.field_paths?.[field] ?? null,
			retrieved_at: sourceRecord.retrieved_at,
			transformation_applied: normalized.transformations?.[field] ?? null,
			confidence: normalized.field_confidence?.[field] ?? "medium",
			conflict_status: conflictStatus,
			notes: null,
		});
	}
}

function mergeCanonicalWork(work, normalized, sourceRecord, provenanceRows) {
	work.source_records = mergeArrayUnique(work.source_records, [sourceRecord.source_record_id], String);
	work.identifiers = mergeIdentifiers(work.identifiers, normalized.identifiers);
	work.authors = mergeArrayUnique(work.authors, normalized.authors ?? [], (author) => authorDisplayName(author).toLowerCase());
	work.urls = mergeArrayUnique(work.urls ?? [], normalized.urls ?? [], (url) => String(url).toLowerCase());
	work.subjects = mergeArrayUnique(work.subjects, normalized.subjects ?? [], (subject) => String(subject).toLowerCase());
	work.fields_of_study = mergeArrayUnique(work.fields_of_study, normalized.fields_of_study ?? [], (field) => String(field).toLowerCase());
	work.funders = mergeArrayUnique(work.funders, normalized.funders ?? [], (funder) => JSON.stringify(funder).toLowerCase());
	work.licenses = mergeArrayUnique(work.licenses, normalized.licenses ?? [], (license) => JSON.stringify(license).toLowerCase());
	work.oa_locations = mergeArrayUnique(work.oa_locations, normalized.oa_locations ?? [], (location) => location.url ?? JSON.stringify(location));
	work.full_text_candidates = mergeArrayUnique(work.full_text_candidates, normalized.full_text_candidates ?? [], (location) => location.url ?? String(location));
	if (normalized.abstract_best) {
		work.abstract_alternates = mergeArrayUnique(
			work.abstract_alternates,
			[{ value: normalized.abstract_best, sourceRecordId: sourceRecord.source_record_id }],
			(item) => normalizeWhitespace(item.value).toLowerCase(),
		);
	}
	for (const field of ["canonical_title", "publication_year", "publication_date", "venue_name", "venue_type", "publisher", "type", "oa_status"]) {
		if (preferValue(field, work[field], normalized[field], work._fieldSources?.[field], sourceRecord.provider)) {
			work[field] = normalized[field];
			work._fieldSources[field] = sourceRecord.provider;
		}
	}
	if (preferValue("abstract_best", work.abstract_best, normalized.abstract_best, work._fieldSources?.abstract_best, sourceRecord.provider)) {
		work.abstract_best = normalized.abstract_best;
		work.abstract_best_source = sourceRecord.source_record_id;
		work._fieldSources.abstract_best = sourceRecord.provider;
	}
	work.normalized_title = normalizeTitleKey(work.canonical_title);
	work.updated_at = nowIso();
	addFieldProvenance(provenanceRows, work, sourceRecord, normalized, "merged");
}

function findRelatedWork(work, normalized) {
	const title = normalizeTitleKey(normalized.canonical_title);
	if (!title || work.normalized_title !== title) return null;
	const year = normalized.publication_year;
	const closeYear = !work.publication_year || !year || Math.abs(work.publication_year - year) <= 1;
	if (!closeYear) return null;
	const types = new Set([work.type, normalized.type]);
	if (types.has("preprint") && types.has("article")) return "preprint_published_version";
	return null;
}

function dedupeAcademicRecords(sourceRecords, normalizedRecords) {
	const works = [];
	const byKey = new Map();
	const provenanceRows = [];
	const decisions = [];
	for (const [index, normalized] of normalizedRecords.entries()) {
		const sourceRecord = sourceRecords[index];
		const key = academicWorkKey(normalized);
		const existing = byKey.get(key);
		if (existing) {
			mergeCanonicalWork(existing, normalized, sourceRecord, provenanceRows);
			decisions.push({
				decision: "merge",
				reason: `Matched ${key}`,
				work_id: existing.work_id,
				source_record_id: sourceRecord.source_record_id,
				compared_fields: { key },
				confidence: "high",
				provider_evidence: [sourceRecord.provider],
			});
			continue;
		}
		const work = createCanonicalWork(normalized, sourceRecord);
		works.push(work);
		byKey.set(key, work);
		addFieldProvenance(provenanceRows, work, sourceRecord, normalized);
		decisions.push({
			decision: "keep_separate",
			reason: `New canonical work from ${key}`,
			work_id: work.work_id,
			source_record_id: sourceRecord.source_record_id,
			compared_fields: { key },
			confidence: "medium",
			provider_evidence: [sourceRecord.provider],
		});
		for (const other of works) {
			if (other.work_id === work.work_id) continue;
			const relation = findRelatedWork(other, normalized);
			if (!relation) continue;
			other.related_works.push({ workId: work.work_id, relation, sourceRecordId: sourceRecord.source_record_id });
			work.related_works.push({ workId: other.work_id, relation, sourceRecordId: sourceRecord.source_record_id });
			decisions.push({
				decision: "link_related",
				reason: relation,
				work_id: work.work_id,
				related_work_id: other.work_id,
				source_record_id: sourceRecord.source_record_id,
				compared_fields: { title: work.normalized_title, year: work.publication_year },
				confidence: "medium",
				provider_evidence: [sourceRecord.provider],
			});
		}
	}
	for (const work of works) delete work._fieldSources;
	return { works, provenanceRows, decisions };
}

// --- Academic providers ----------------------------------------------------

function providerCapabilities(provider) {
	const common = { authRequired: false, optionalAuth: false, rateLimit: "conservative", searchSyntaxNotes: "provider native query syntax" };
	return {
		crossref: {
			...common,
			fields: ["doi", "title", "authors", "venue", "publisher", "license", "funder", "references"],
			strengths: ["DOI metadata", "publisher-deposited bibliographic verification"],
			limits: ["abstract coverage varies", "not full-text access"],
		},
		"semantic-scholar": {
			...common,
			optionalAuth: true,
			fields: ["paperId", "externalIds", "title", "authors", "abstract", "citations", "fieldsOfStudy", "openAccessPdf"],
			strengths: ["broad paper discovery", "abstracts", "citation graph signals"],
			limits: ["public quota can throttle", "aggregated metadata is not canonical"],
		},
		europepmc: {
			...common,
			fields: ["pmid", "pmcid", "doi", "title", "authors", "journal", "abstract", "fullTextUrlList"],
			strengths: ["biomedical metadata", "PubMed/PMC abstracts", "OA links"],
			limits: ["domain-specific coverage"],
		},
		pubmed: {
			...common,
			optionalAuth: true,
			fields: ["pmid", "title", "authors", "journal", "publicationTypes"],
			strengths: ["canonical PubMed lookup", "medical indexing"],
			limits: ["biomedical scope", "ESummary omits abstracts"],
		},
		arxiv: {
			...common,
			fields: ["arxiv_id", "title", "authors", "abstract", "categories", "doi", "journal_ref"],
			strengths: ["technical preprints", "stable arXiv identifiers"],
			limits: ["limited disciplinary coverage", "preprints are not peer-reviewed"],
		},
		datacite: {
			...common,
			fields: ["doi", "resourceType", "creators", "descriptions", "publisher"],
			strengths: ["datasets", "software", "reports", "repository objects"],
			limits: ["not primarily a journal article database"],
		},
		openaire: {
			...common,
			fields: ["doi", "title", "authors", "publisher", "projects", "oa status"],
			strengths: ["open scholarly graph", "projects and funders"],
			limits: ["aggregated metadata can be noisy"],
		},
		doaj: {
			...common,
			fields: ["doi", "title", "authors", "journal", "abstract", "license", "fullTextUrl"],
			strengths: ["OA journal metadata", "journal legitimacy signal"],
			limits: ["only DOAJ-indexed OA journals"],
		},
		unpaywall: {
			...common,
			authRequired: true,
			fields: ["doi", "oa_status", "best_oa_location", "oa_locations"],
			strengths: ["legal OA copy discovery"],
			limits: ["requires DOI and email", "not a search provider"],
		},
	}[provider];
}

function rawResponsePath(runDirectory, provider, requestId, extension) {
	mkdirSync(join(runDirectory, "raw", provider), { recursive: true });
	return join("raw", provider, `${requestId}.${extension}`);
}

async function providerFetch(runDirectory, provider, url, options, state) {
	const startedAt = nowIso();
	const requestId = `${provider}-${sha256(`${startedAt}:${url}`).slice(0, 12)}`;
	const record = {
		request_id: requestId,
		provider,
		url,
		method: "GET",
		status: "failed",
		http_status: null,
		started_at: startedAt,
		ended_at: null,
		raw_response_path: null,
		raw_hash: null,
		error: null,
	};
	const controller = new AbortController();
	const timer = setTimeout(() => controller.abort(), options.timeoutMs);
	try {
		assertFetchableUrl(url);
		const response = await fetch(url, {
			signal: controller.signal,
			headers: { "user-agent": options.userAgent, accept: options.accept ?? "application/json, application/xml;q=0.9, text/xml;q=0.8" },
		});
		record.http_status = response.status;
		const text = await response.text();
		const hash = sha256(text);
		const contentType = response.headers.get("content-type") ?? "";
		const extension = contentType.includes("xml") || text.trimStart().startsWith("<") ? "xml" : "json";
		const relativePath = rawResponsePath(runDirectory, provider, requestId, extension);
		writeFileSync(join(runDirectory, relativePath), text);
		record.raw_response_path = relativePath;
		record.raw_hash = hash;
		if (!response.ok) throw new Error(`${provider} returned HTTP ${response.status}`);
		record.status = "success";
		record.ended_at = nowIso();
		state.providerRequests.push(record);
		return { text, json: extension === "json" ? JSON.parse(text) : null, record };
	} catch (error) {
		record.error = error instanceof Error ? error.message : String(error);
		record.ended_at = nowIso();
		state.providerRequests.push(record);
		state.providerErrors.push({
			provider,
			url,
			error: record.error,
			http_status: record.http_status,
			recorded_at: record.ended_at,
		});
		return { text: "", json: null, record, error: record.error };
	} finally {
		clearTimeout(timer);
	}
}

function buildUrl(base, pathname, params = {}) {
	const url = new URL(`${base}${pathname}`);
	for (const [key, value] of Object.entries(params)) {
		if (value === undefined || value === null || value === "") continue;
		url.searchParams.set(key, String(value));
	}
	return url.toString();
}

function normalizeCrossrefType(type) {
	if (type === "journal-article") return "article";
	if (type === "proceedings-article") return "proceedings";
	if (type === "book-chapter") return "chapter";
	if (type === "posted-content") return "preprint";
	if (type === "book") return "book";
	if (type === "report") return "report";
	return type || "unknown";
}

function normalizeCrossrefItem(item) {
	const published = parseDateParts(item["published-print"]?.["date-parts"] ?? item["published-online"]?.["date-parts"] ?? item.created?.["date-parts"]);
	const authors = asArray(item.author).map((author) =>
		compactObject({
			given: author.given ?? null,
			family: author.family ?? null,
			name: author.name ?? [author.given, author.family].filter(Boolean).join(" "),
			orcid: author.ORCID ?? null,
		}),
	);
	return {
		canonical_title: firstText(item.title),
		abstract_best: stripTags(item.abstract),
		authors,
		publication_year: published.year,
		publication_date: published.date,
		venue_name: firstText(item["container-title"]),
		venue_type: item.type?.includes("journal") ? "journal" : null,
		publisher: item.publisher ?? null,
		type: normalizeCrossrefType(item.type),
		identifiers: compactObject({
			doi: normalizeDoi(item.DOI),
			issn: asArray(item.ISSN),
			isbn: asArray(item.ISBN),
		}),
		urls: asArray(item.URL),
		subjects: asArray(item.subject),
		licenses: asArray(item.license).map((license) => compactObject({ url: license.URL, start: license.start?.["date-time"] })),
		funders: asArray(item.funder).map((funder) => compactObject({ name: funder.name, doi: normalizeDoi(funder.DOI), awards: asArray(funder.award) })),
		field_paths: {
			canonical_title: "message.items[].title",
			abstract_best: "message.items[].abstract",
			identifiers: "message.items[].DOI",
		},
		transformations: { abstract_best: item.abstract ? "stripped XML/HTML tags and normalized whitespace" : null, identifiers: "normalized DOI" },
	};
}

function normalizeSemanticScholarItem(item) {
	const externalIds = item.externalIds ?? {};
	return {
		canonical_title: normalizeWhitespace(item.title),
		abstract_best: normalizeWhitespace(item.abstract),
		authors: asArray(item.authors).map((author) => compactObject({ name: author.name, semanticScholarAuthorId: author.authorId })),
		publication_year: Number.isInteger(item.year) ? item.year : null,
		publication_date: item.publicationDate ?? null,
		venue_name: item.venue || item.journal?.name || null,
		venue_type: item.journal?.name ? "journal" : null,
		publisher: null,
		type: asArray(item.publicationTypes).some((type) => /preprint/i.test(type)) ? "preprint" : "article",
		identifiers: compactObject({
			doi: normalizeDoi(externalIds.DOI),
			pmid: normalizeIdentifier(externalIds.PubMed),
			pmcid: normalizeIdentifier(externalIds.PubMedCentral),
			arxiv_id: normalizeArxivId(externalIds.ArXiv),
			semantic_scholar_paper_id: normalizeIdentifier(item.paperId),
		}),
		urls: [item.url, item.openAccessPdf?.url].filter(Boolean),
		fields_of_study: asArray(item.fieldsOfStudy),
		full_text_candidates: item.openAccessPdf?.url ? [{ url: item.openAccessPdf.url, source: "Semantic Scholar openAccessPdf" }] : [],
		citations_count_by_provider: Number.isInteger(item.citationCount)
			? [{ provider: "semantic-scholar", citationCount: item.citationCount, influentialCitationCount: item.influentialCitationCount ?? null }]
			: [],
		field_paths: { canonical_title: "data[].title", abstract_best: "data[].abstract", identifiers: "data[].externalIds" },
		transformations: { identifiers: "normalized external IDs" },
	};
}

function normalizeEuropePmcItem(item) {
	const date = item.firstPublicationDate || item.pubYear || item.journalInfo?.printPublicationDate || null;
	return {
		canonical_title: stripTags(item.title),
		abstract_best: stripTags(item.abstractText),
		authors: asArray(item.authorList?.author).map((author) => compactObject({ name: author.fullName, given: author.firstName, family: author.lastName })),
		publication_year: Number.parseInt(item.pubYear, 10) || yearFromDate(date),
		publication_date: date,
		venue_name: item.journalInfo?.journal?.title || item.journalTitle || null,
		venue_type: "journal",
		publisher: null,
		type: item.pubType?.toLowerCase?.().includes("preprint") ? "preprint" : "article",
		identifiers: compactObject({
			doi: normalizeDoi(item.doi),
			pmid: normalizeIdentifier(item.pmid),
			pmcid: normalizeIdentifier(item.pmcid),
			issn: item.journalInfo?.journal?.issn ? [item.journalInfo.journal.issn] : [],
		}),
		urls: [item.fullTextUrlList?.fullTextUrl?.[0]?.url, item.authorString ? null : item.source].filter(Boolean),
		full_text_candidates: asArray(item.fullTextUrlList?.fullTextUrl).map((entry) => compactObject({ url: entry.url, availability: entry.availability, documentStyle: entry.documentStyle })),
		subjects: asArray(item.meshHeadingList?.meshHeading).map((heading) => heading.descriptorName).filter(Boolean),
		field_paths: { canonical_title: "resultList.result[].title", abstract_best: "resultList.result[].abstractText", identifiers: "resultList.result[]" },
		transformations: { abstract_best: item.abstractText ? "stripped XML/HTML tags and normalized whitespace" : null, identifiers: "normalized DOI/PMID/PMCID" },
	};
}

function normalizePubmedSummary(uid, item) {
	const authors = asArray(item.authors).map((author) => compactObject({ name: author.name }));
	return {
		canonical_title: stripTags(item.title),
		abstract_best: null,
		authors,
		publication_year: yearFromDate(item.pubdate),
		publication_date: item.pubdate ?? null,
		venue_name: item.fulljournalname || item.source || null,
		venue_type: "journal",
		publisher: null,
		type: "article",
		identifiers: compactObject({ pmid: uid, doi: normalizeDoi(asArray(item.articleids).find((id) => id.idtype === "doi")?.value) }),
		urls: [`https://pubmed.ncbi.nlm.nih.gov/${uid}/`],
		subjects: asArray(item.pubtype),
		field_paths: { canonical_title: "result.<pmid>.title", identifiers: "result.<pmid>.articleids" },
		transformations: { identifiers: "normalized PubMed article IDs" },
	};
}

function parseArxivFeed(text) {
	const dom = new JSDOM(text, { contentType: "text/xml" });
	try {
		return [...dom.window.document.querySelectorAll("entry")].map((entry) => {
			const textOf = (selector) => entry.querySelector(selector)?.textContent?.trim() ?? null;
			const id = textOf("id");
			const authors = [...entry.querySelectorAll("author")].map((author) => compactObject({ name: normalizeWhitespace(author.querySelector("name")?.textContent) }));
			const categories = [...entry.querySelectorAll("category")].map((category) => category.getAttribute("term")).filter(Boolean);
			const links = [...entry.querySelectorAll("link")].map((link) => link.getAttribute("href")).filter(Boolean);
			return {
				canonical_title: normalizeWhitespace(textOf("title")),
				abstract_best: normalizeWhitespace(textOf("summary")),
				authors,
				publication_year: yearFromDate(textOf("published")),
				publication_date: textOf("published"),
				venue_name: textOf("arxiv\\:journal_ref") ?? textOf("journal_ref"),
				venue_type: null,
				publisher: "arXiv",
				type: "preprint",
				identifiers: compactObject({
					arxiv_id: normalizeArxivId(id),
					doi: normalizeDoi(textOf("arxiv\\:doi") ?? textOf("doi")),
				}),
				urls: links.length > 0 ? links : [id].filter(Boolean),
				subjects: categories,
				field_paths: { canonical_title: "feed.entry.title", abstract_best: "feed.entry.summary", identifiers: "feed.entry.id" },
				transformations: { identifiers: "normalized arXiv ID and DOI" },
			};
		});
	} finally {
		dom.window.close();
	}
}

function normalizeDataCiteItem(item) {
	const attributes = item.attributes ?? {};
	const creators = asArray(attributes.creators).map((creator) => compactObject({ name: creator.name, given: creator.givenName, family: creator.familyName }));
	const abstract = asArray(attributes.descriptions).find((description) => /abstract/i.test(description.descriptionType ?? ""))?.description;
	const resourceType = attributes.types?.resourceTypeGeneral || attributes.types?.resourceType || null;
	return {
		canonical_title: firstText(asArray(attributes.titles).map((title) => title.title)),
		abstract_best: stripTags(abstract),
		authors: creators,
		publication_year: Number.parseInt(attributes.publicationYear, 10) || null,
		publication_date: attributes.published ?? attributes.created ?? null,
		venue_name: attributes.container?.title ?? null,
		venue_type: attributes.container?.type ?? null,
		publisher: attributes.publisher ?? null,
		type: /dataset/i.test(resourceType ?? "") ? "dataset" : /software/i.test(resourceType ?? "") ? "software" : /report/i.test(resourceType ?? "") ? "report" : "unknown",
		identifiers: compactObject({ doi: normalizeDoi(attributes.doi), datacite_id: item.id }),
		urls: [attributes.url].filter(Boolean),
		subjects: asArray(attributes.subjects).map((subject) => subject.subject).filter(Boolean),
		licenses: asArray(attributes.rightsList).map((right) => compactObject({ rights: right.rights, url: right.rightsUri })),
		field_paths: { canonical_title: "data[].attributes.titles", abstract_best: "data[].attributes.descriptions", identifiers: "data[].attributes.doi" },
		transformations: { abstract_best: abstract ? "stripped XML/HTML tags and normalized whitespace" : null, identifiers: "normalized DOI" },
	};
}

function normalizeOpenAireResult(item) {
	const metadata = item.metadata?.["oaf:entity"]?.["oaf:result"] ?? item.metadata ?? item;
	const title = firstText(metadata.title?.content ?? metadata.title);
	const authors = asArray(metadata.creator).map((creator) => compactObject({ name: creator.content ?? creator }));
	return {
		canonical_title: title,
		abstract_best: stripTags(metadata.description?.content ?? metadata.description),
		authors,
		publication_year: yearFromDate(metadata.dateofacceptance ?? metadata.date),
		publication_date: metadata.dateofacceptance ?? metadata.date ?? null,
		venue_name: metadata.journal?.content ?? metadata.publisher ?? null,
		venue_type: metadata.journal ? "journal" : null,
		publisher: metadata.publisher ?? null,
		type: "article",
		identifiers: compactObject({ doi: normalizeDoi(metadata.pid?.content ?? metadata.doi), openaire_id: item.header?.dri?.objIdentifier ?? item.id }),
		urls: asArray(metadata.children?.instance).map((instance) => instance.webresource?.url).filter(Boolean),
		field_paths: { canonical_title: "response.results.result[].metadata", identifiers: "response.results.result[].metadata.pid" },
		transformations: { identifiers: "normalized DOI/OpenAIRE ID" },
	};
}

function normalizeDoajItem(item) {
	const bibjson = item.bibjson ?? item;
	const identifiers = asArray(bibjson.identifier);
	return {
		canonical_title: normalizeWhitespace(bibjson.title),
		abstract_best: stripTags(bibjson.abstract),
		authors: asArray(bibjson.author).map((author) => compactObject({ name: author.name, affiliation: author.affiliation })),
		publication_year: yearFromDate(bibjson.year ?? bibjson.month),
		publication_date: bibjson.year ? String(bibjson.year) : null,
		venue_name: bibjson.journal?.title ?? null,
		venue_type: "journal",
		publisher: bibjson.journal?.publisher ?? null,
		type: "article",
		identifiers: compactObject({
			doi: normalizeDoi(identifiers.find((id) => id.type === "doi")?.id),
			issn: identifiers.filter((id) => /issn/i.test(id.type ?? "")).map((id) => id.id),
		}),
		urls: asArray(bibjson.link).map((link) => link.url).filter(Boolean),
		subjects: asArray(bibjson.subject).map((subject) => subject.term).filter(Boolean),
		licenses: asArray(bibjson.license).map((license) => compactObject({ type: license.type, url: license.url })),
		field_paths: { canonical_title: "results[].bibjson.title", abstract_best: "results[].bibjson.abstract", identifiers: "results[].bibjson.identifier" },
		transformations: { abstract_best: bibjson.abstract ? "stripped XML/HTML tags and normalized whitespace" : null, identifiers: "normalized DOI/ISSN" },
	};
}

function normalizeUnpaywallItem(item) {
	const locations = asArray(item.oa_locations).map((location) =>
		compactObject({
			url: location.url_for_landing_page ?? location.url,
			pdfUrl: location.url_for_pdf,
			hostType: location.host_type,
			version: location.version,
			license: location.license,
		}),
	);
	return {
		canonical_title: normalizeWhitespace(item.title),
		abstract_best: null,
		authors: [],
		publication_year: Number.parseInt(item.year, 10) || null,
		publication_date: null,
		venue_name: item.journal_name ?? null,
		venue_type: "journal",
		publisher: item.publisher ?? null,
		type: "article",
		identifiers: compactObject({ doi: normalizeDoi(item.doi) }),
		urls: locations.map((location) => location.url).filter(Boolean),
		oa_status: item.oa_status ?? null,
		oa_locations: locations,
		full_text_candidates: locations.filter((location) => location.pdfUrl).map((location) => ({ url: location.pdfUrl, source: "Unpaywall" })),
		field_paths: { oa_status: "oa_status", oa_locations: "oa_locations", identifiers: "doi" },
		transformations: { identifiers: "normalized DOI" },
	};
}

const ACADEMIC_PROVIDERS = {
	crossref: {
		providerCapabilities: () => providerCapabilities("crossref"),
		async search(context) {
			const url = buildUrl(context.base, "/works", { query: context.query, rows: context.limit, mailto: context.contactEmail });
			const response = await providerFetch(context.runDirectory, "crossref", url, context.options, context.state);
			return asArray(response.json?.message?.items);
		},
		lookup: async () => [],
		hydrate: async (record) => record,
		normalize: normalizeCrossrefItem,
	},
	"semantic-scholar": {
		providerCapabilities: () => providerCapabilities("semantic-scholar"),
		async search(context) {
			const fields = "paperId,externalIds,title,authors,year,venue,abstract,citationCount,influentialCitationCount,publicationTypes,fieldsOfStudy,openAccessPdf,url,journal,publicationDate";
			const url = buildUrl(context.base, "/graph/v1/paper/search", { query: context.query, limit: context.limit, fields });
			const response = await providerFetch(context.runDirectory, "semantic-scholar", url, context.options, context.state);
			return asArray(response.json?.data);
		},
		lookup: async () => [],
		hydrate: async (record) => record,
		normalize: normalizeSemanticScholarItem,
	},
	europepmc: {
		providerCapabilities: () => providerCapabilities("europepmc"),
		async search(context) {
			const url = buildUrl(context.base, "/search", { query: context.query, format: "json", pageSize: context.limit, resultType: "core" });
			const response = await providerFetch(context.runDirectory, "europepmc", url, context.options, context.state);
			return asArray(response.json?.resultList?.result);
		},
		lookup: async () => [],
		hydrate: async (record) => record,
		normalize: normalizeEuropePmcItem,
	},
	pubmed: {
		providerCapabilities: () => providerCapabilities("pubmed"),
		async search(context) {
			const searchUrl = buildUrl(context.base, "/esearch.fcgi", {
				db: "pubmed",
				term: context.query,
				retmode: "json",
				retmax: context.limit,
				tool: "pi-forge",
				email: context.contactEmail,
			});
			const searchResponse = await providerFetch(context.runDirectory, "pubmed", searchUrl, context.options, context.state);
			const ids = asArray(searchResponse.json?.esearchresult?.idlist).slice(0, context.limit);
			if (ids.length === 0) return [];
			const summaryUrl = buildUrl(context.base, "/esummary.fcgi", {
				db: "pubmed",
				id: ids.join(","),
				retmode: "json",
				tool: "pi-forge",
				email: context.contactEmail,
			});
			const summaryResponse = await providerFetch(context.runDirectory, "pubmed", summaryUrl, context.options, context.state);
			return ids.map((id) => ({ uid: id, summary: summaryResponse.json?.result?.[id] })).filter((item) => item.summary);
		},
		lookup: async () => [],
		hydrate: async (record) => record,
		normalize: (record) => normalizePubmedSummary(record.uid, record.summary),
	},
	arxiv: {
		providerCapabilities: () => providerCapabilities("arxiv"),
		async search(context) {
			const url = buildUrl(context.base, "/query", { search_query: `all:${context.query}`, start: 0, max_results: context.limit });
			const response = await providerFetch(context.runDirectory, "arxiv", url, { ...context.options, accept: "application/atom+xml, application/xml" }, context.state);
			return response.text ? parseArxivFeed(response.text) : [];
		},
		lookup: async () => [],
		hydrate: async (record) => record,
		normalize: (record) => record,
	},
	datacite: {
		providerCapabilities: () => providerCapabilities("datacite"),
		async search(context) {
			const url = buildUrl(context.base, "/dois", { query: context.query, "page[size]": context.limit });
			const response = await providerFetch(context.runDirectory, "datacite", url, context.options, context.state);
			return asArray(response.json?.data);
		},
		lookup: async () => [],
		hydrate: async (record) => record,
		normalize: normalizeDataCiteItem,
	},
	openaire: {
		providerCapabilities: () => providerCapabilities("openaire"),
		async search(context) {
			const url = buildUrl(context.base, "/search/publications", { keywords: context.query, format: "json", size: context.limit });
			const response = await providerFetch(context.runDirectory, "openaire", url, context.options, context.state);
			return asArray(response.json?.response?.results?.result);
		},
		lookup: async () => [],
		hydrate: async (record) => record,
		normalize: normalizeOpenAireResult,
	},
	doaj: {
		providerCapabilities: () => providerCapabilities("doaj"),
		async search(context) {
			const url = `${context.base}/search/articles/${encodeURIComponent(context.query)}?page=1&pageSize=${context.limit}`;
			const response = await providerFetch(context.runDirectory, "doaj", url, context.options, context.state);
			return asArray(response.json?.results);
		},
		lookup: async () => [],
		hydrate: async (record) => record,
		normalize: normalizeDoajItem,
	},
	unpaywall: {
		providerCapabilities: () => providerCapabilities("unpaywall"),
		async search(context) {
			const dois = [...new Set(context.knownDois)].filter(Boolean);
			const records = [];
			for (const doi of dois.slice(0, context.limit)) {
				const url = buildUrl(context.base, `/${encodeURIComponent(doi)}`, { email: context.contactEmail });
				const response = await providerFetch(context.runDirectory, "unpaywall", url, context.options, context.state);
				if (response.json) records.push(response.json);
			}
			return records;
		},
		lookup: async () => [],
		hydrate: async (record) => record,
		normalize: normalizeUnpaywallItem,
	},
};

async function runAcademicProvider(providerName, context, sourceRecords, normalizedRecords) {
	const provider = ACADEMIC_PROVIDERS[providerName];
	if (!provider) return;
	const records = await provider.search(context);
	let index = 0;
	for (const record of records) {
		const hydrated = await provider.hydrate(record, context);
		const normalized = provider.normalize(hydrated, context);
		if (!normalized?.canonical_title && !normalized?.identifiers?.doi && !normalized?.identifiers?.pmid && !normalized?.identifiers?.arxiv_id) continue;
		const providerRecordId =
			normalized.identifiers?.doi ??
			normalized.identifiers?.pmid ??
			normalized.identifiers?.arxiv_id ??
			normalized.identifiers?.semantic_scholar_paper_id ??
			normalized.canonical_title;
		const sourceRecord = {
			source_record_id: sourceRecordId(providerName, providerRecordId, index++),
			work_id: null,
			provider: providerName,
			provider_record_id: providerRecordId ?? null,
			provider_url: normalized.urls?.[0] ?? null,
			request_url: context.state.providerRequests.filter((request) => request.provider === providerName).at(-1)?.url ?? null,
			request_params: { query: context.query, limit: context.limit },
			retrieved_at: nowIso(),
			raw_response_path: context.state.providerRequests.filter((request) => request.provider === providerName).at(-1)?.raw_response_path ?? null,
			raw_hash: context.state.providerRequests.filter((request) => request.provider === providerName).at(-1)?.raw_hash ?? null,
			normalized_payload: normalized,
			field_provenance: normalized.field_paths ?? {},
			provider_confidence_notes: provider.providerCapabilities().strengths.join("; "),
			rate_limit_state: null,
		};
		sourceRecords.push(sourceRecord);
		normalizedRecords.push(normalized);
	}
}

function risType(work) {
	if (work.type === "article") return "JOUR";
	if (work.type === "report") return "RPRT";
	if (work.type === "thesis") return "THES";
	if (work.type === "book") return "BOOK";
	if (work.type === "chapter") return "CHAP";
	if (work.type === "dataset" || work.type === "software") return "DATA";
	return "GEN";
}

function risValue(value) {
	return normalizeWhitespace(value).replace(/\s+ER\s+-\s*/g, " ");
}

function risLine(tag, value) {
	const text = risValue(value);
	return text ? `${tag}  - ${text}` : null;
}

function buildRisRecord(work, sourceRecords) {
	const sourceById = new Map(sourceRecords.map((source) => [source.source_record_id, source]));
	const providers = [...new Set(work.source_records.map((id) => sourceById.get(id)?.provider).filter(Boolean))];
	const lines = [`TY  - ${risType(work)}`];
	lines.push(risLine("TI", work.canonical_title));
	for (const author of work.authors ?? []) lines.push(risLine("AU", authorDisplayName(author)));
	lines.push(risLine("AB", work.abstract_best));
	if (work.publication_year) lines.push(risLine("PY", work.publication_year));
	lines.push(risLine("Y1", work.publication_date));
	lines.push(risLine("JO", work.venue_name));
	lines.push(risLine("T2", work.venue_name));
	lines.push(risLine("PB", work.publisher));
	lines.push(risLine("DO", work.identifiers?.doi));
	const primaryUrl = work.urls?.[0] ?? work.oa_locations?.[0]?.url ?? work.full_text_candidates?.[0]?.url;
	lines.push(risLine("UR", primaryUrl));
	for (const subject of [...(work.subjects ?? []), ...(work.fields_of_study ?? [])].slice(0, 20)) lines.push(risLine("KW", subject));
	for (const issn of asArray(work.identifiers?.issn)) lines.push(risLine("SN", issn));
	for (const isbn of asArray(work.identifiers?.isbn)) lines.push(risLine("SN", isbn));
	const notes = [
		`work_id=${work.work_id}`,
		`providers=${providers.map((provider) => ACADEMIC_PROVIDER_LABELS[provider] ?? provider).join(", ")}`,
		`dedupe_cluster=${work.dedupe_cluster_id}`,
		work.identifiers?.pmid ? `PMID=${work.identifiers.pmid}` : null,
		work.identifiers?.pmcid ? `PMCID=${work.identifiers.pmcid}` : null,
		work.identifiers?.arxiv_id ? `arXiv=${work.identifiers.arxiv_id}` : null,
		work.oa_status ? `OA=${work.oa_status}` : null,
	].filter(Boolean);
	lines.push(risLine("N1", notes.join("; ")));
	lines.push("ER  -");
	return `${lines.filter(Boolean).join("\n")}\n`;
}

function risDuplicateKey(work) {
	if (work.identifiers?.doi) return `doi:${normalizeDoi(work.identifiers.doi)}`;
	if (work.identifiers?.pmid) return `pmid:${work.identifiers.pmid}`;
	if (work.identifiers?.arxiv_id) return `arxiv:${normalizeArxivId(work.identifiers.arxiv_id)}`;
	return `title:${work.normalized_title}|${work.publication_year ?? ""}`;
}

function writeRisArtifacts(runDirectory, works, sourceRecords) {
	mkdirSync(join(runDirectory, "ris"), { recursive: true });
	const records = [];
	const manifest = [];
	for (const work of works) {
		const relativePath = `ris/${work.work_id}.ris`;
		const record = buildRisRecord(work, sourceRecords);
		writeFileSync(join(runDirectory, relativePath), record);
		records.push(record.trimEnd());
		const providers = [
			...new Set(
				work.source_records
					.map((id) => sourceRecords.find((source) => source.source_record_id === id)?.provider)
					.filter(Boolean),
			),
		];
		manifest.push({
			workId: work.work_id,
			risPath: relativePath,
			risKey: risDuplicateKey(work),
			identifiers: work.identifiers ?? {},
			providers,
			dedupeClusterId: work.dedupe_cluster_id,
		});
	}
	writeFileSync(join(runDirectory, "works.ris"), records.length > 0 ? `${records.join("\n\n")}\n` : "");
	writeJson(join(runDirectory, "ris_manifest.json"), { schemaVersion: ACADEMIC_SCHEMA_VERSION, generatedAt: nowIso(), records: manifest });
	return manifest;
}

function buildAcademicReport(state) {
	const lines = ["# Academic Research Report", "", `**Query**: ${state.query}`, `**Generated**: ${nowIso()}`, ""];
	lines.push("## Provider Summary", "");
	for (const provider of state.providers) {
		const requests = state.providerRequests.filter((request) => request.provider === provider);
		const errors = state.providerErrors.filter((error) => error.provider === provider);
		lines.push(`- ${ACADEMIC_PROVIDER_LABELS[provider] ?? provider}: ${requests.length} request(s), ${errors.length} error(s)`);
	}
	lines.push("");
	lines.push("## Works", "");
	for (const work of state.works) {
		lines.push(`### ${work.work_id}`);
		lines.push("");
		lines.push(`- Title: ${work.canonical_title || "Untitled"}`);
		lines.push(`- Type: ${work.type}`);
		if (work.publication_year) lines.push(`- Year: ${work.publication_year}`);
		if (work.venue_name) lines.push(`- Venue: ${work.venue_name}`);
		if (work.identifiers?.doi) lines.push(`- DOI: ${work.identifiers.doi}`);
		if (work.identifiers?.pmid) lines.push(`- PMID: ${work.identifiers.pmid}`);
		if (work.identifiers?.arxiv_id) lines.push(`- arXiv: ${work.identifiers.arxiv_id}`);
		lines.push(`- RIS: ris/${work.work_id}.ris`);
		lines.push("");
	}
	return `${lines.join("\n")}\n`;
}

function validateAcademicRun(runDirectory, options = {}) {
	const errors = [];
	const warnings = [];
	const required = [
		"academic_run.json",
		"works.jsonl",
		"source_records.jsonl",
		"field_provenance.jsonl",
		"dedupe_decisions.jsonl",
		"provider_requests.jsonl",
		"provider_errors.jsonl",
		"works.ris",
		"ris_manifest.json",
		"academic_report.md",
	];
	for (const name of required) {
		if (!existsSync(join(runDirectory, name))) errors.push(`${name} is missing`);
	}
	let works = [];
	let risManifest = { records: [] };
	let worksRis = "";
	try {
		works = readJsonl(join(runDirectory, "works.jsonl"));
		risManifest = existsSync(join(runDirectory, "ris_manifest.json")) ? JSON.parse(readFileSync(join(runDirectory, "ris_manifest.json"), "utf8")) : { records: [] };
		worksRis = existsSync(join(runDirectory, "works.ris")) ? readFileSync(join(runDirectory, "works.ris"), "utf8") : "";
	} catch (error) {
		errors.push(`could not parse academic artifacts: ${error instanceof Error ? error.message : String(error)}`);
	}
	const manifestByWork = new Map(asArray(risManifest.records).map((record) => [record.workId, record]));
	const keys = new Set();
	for (const work of works) {
		const manifest = manifestByWork.get(work.work_id);
		if (!manifest) {
			errors.push(`${work.work_id} has no RIS manifest record`);
			continue;
		}
		const risPath = resolve(runDirectory, manifest.risPath);
		if (!risPath.startsWith(`${resolve(runDirectory)}${sep}`)) errors.push(`${work.work_id} RIS path escapes run directory: ${manifest.risPath}`);
		else if (!existsSync(risPath)) errors.push(`${work.work_id} per-work RIS file is missing: ${manifest.risPath}`);
		const key = manifest.risKey ?? risDuplicateKey(work);
		if (keys.has(key)) errors.push(`duplicate RIS key after dedupe: ${key}`);
		keys.add(key);
	}
	const recordCount = (worksRis.match(/^TY  - /gm) ?? []).length;
	const endCount = (worksRis.match(/^ER  -$/gm) ?? []).length;
	if (recordCount !== works.length) errors.push(`works.ris contains ${recordCount} records for ${works.length} works`);
	if (endCount !== recordCount) errors.push("works.ris has records without ER terminators");
	const result = { valid: errors.length === 0, errors, warnings };
	writeJson(join(runDirectory, "validation_report.json"), result);
	if (options.emit !== false) process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
	if (errors.length > 0 && options.exitOnError) process.exit(1);
	return result;
}

// --- SearXNG ---------------------------------------------------------------

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

function readabilityMetadata(html, url) {
	let dom = null;
	try {
		dom = new JSDOM(html, { url, virtualConsole: quietJsdomConsole });
		const document = dom.window.document;
		const article = new Readability(document).parse();
		if (!article) return {};
		const readableText = htmlToReadableText(article.content) || normalizeWhitespace(article.textContent);
		return {
			title: article.title ?? null,
			textContent: readableText || null,
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

async function extractWithPlaywright(playwright, url, timeoutMs, userAgent, playwrightWsEndpointOverride) {
	const browser = await connectPlaywrightBrowser(playwright, timeoutMs, playwrightWsEndpointOverride);
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

		text = text.replace(/[ \t]{2,}/g, " ").replace(/\n{3,}/g, "\n\n").trim();
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

	const contentType = response.headers.get("content-type") ?? "";
	if (/application\/pdf/i.test(contentType) || /\.pdf($|[?#])/i.test(url)) {
		throw new Error(`unsupported readable content type: ${contentType || "application/pdf"}`);
	}
	if (contentType && !/(text\/html|application\/xhtml\+xml|text\/plain|text\/xml|application\/xml)/i.test(contentType)) {
		throw new Error(`unsupported readable content type: ${contentType}`);
	}
	const html = await response.text();
	if (looksBinaryLike(html)) throw new Error("response looked like binary content");
	const titleMatch = html.match(/<title[^>]*>([\s\S]*?)<\/title>/i);
	const title = titleMatch ? titleMatch[1].replace(/\s+/g, " ").trim() : null;
	const metadata = readabilityMetadata(html, url);

	const text = htmlToReadableText(html);

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
			extraction = await extractWithPlaywright(
				playwright,
				url,
				options.timeoutMs,
				options.userAgent,
				options.playwrightWsEndpoint,
			);
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

function isLowValueReading(reading) {
	const title = normalizeWhitespace(reading.title ?? "").toLowerCase();
	const text = normalizeWhitespace(reading.text ?? "");
	const lowerText = text.toLowerCase();
	if (!text) return true;
	if (text.length < 200 && /\b(vercel security checkpoint|security checkpoint|captcha|checking your browser|just a moment)\b/.test(`${title} ${lowerText}`)) {
		return true;
	}
	if (/\b(vercel security checkpoint|cloudflare|attention required)\b/.test(title) && text.length < 1_000) {
		return true;
	}
	return false;
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
		limit: flags.limit ?? DEFAULT_DEEP_LIMIT,
		readCount: flags.readCount ?? DEFAULT_DEEP_READ_COUNT,
		maxQueries: flags.maxQueries ?? DEFAULT_DEEP_MAX_QUERIES,
		maxSources: flags.maxSources ?? DEFAULT_DEEP_MAX_SOURCES,
		maxFollowupQueries: flags.maxFollowupQueries ?? DEFAULT_DEEP_MAX_FOLLOWUP_QUERIES,
		maxModelCalls: flags.maxModelCalls ?? DEFAULT_DEEP_MAX_MODEL_CALLS,
		maxRuntimeMs: flags.maxRuntimeMs ?? DEFAULT_DEEP_MAX_RUNTIME_MS,
		maxEvidenceChars: flags.maxEvidenceChars ?? DEFAULT_DEEP_MAX_EVIDENCE_CHARS,
		maxClaimEvidenceItems: flags.maxClaimEvidenceItems ?? DEFAULT_DEEP_MAX_CLAIM_EVIDENCE_ITEMS,
		delayMs: flags.delayMs ?? 500,
		timeoutMs: flags.timeoutMs ?? DEFAULT_TIMEOUT_MS,
		userAgent: flags.userAgent ?? DEFAULT_USER_AGENT,
		render: flags.render !== false,
		playwrightWsEndpoint: flags.playwrightWs,
	};
}

function addBudgetEvent(state, reason, detail) {
	if (state.budgetEvents.some((event) => event.reason === reason && event.detail === detail)) return;
	state.budgetEvents.push({ reason, detail, recordedAt: nowIso() });
}

function runtimeBudgetExceeded(state, options) {
	return Date.now() - state.startedAtMs >= options.maxRuntimeMs;
}

function canCallDeepModel(state, options, task) {
	if (runtimeBudgetExceeded(state, options)) {
		addBudgetEvent(state, "maxRuntimeMs", `Skipped ${task}; runtime budget reached.`);
		return false;
	}
	if (state.modelCalls.length >= options.maxModelCalls) {
		addBudgetEvent(state, "maxModelCalls", `Skipped ${task}; model call budget reached.`);
		return false;
	}
	return true;
}

function appendBudgetGaps(state, gaps) {
	let index = gaps.length + 1;
	for (const event of state.budgetEvents) {
		gaps.push({
			gapId: nextId("gap", index++),
			text: `Deep research stopped early because ${event.reason} was reached.`,
			reason: event.detail,
			sourceIds: [],
			createdAt: event.recordedAt,
		});
	}
	return gaps;
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

function evidencePrompt(source, question, maxEvidenceChars) {
	const text = selectRelevantText(source.text, question, maxEvidenceChars);
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

	Return at most 2 queries. Do not repeat existing queries.`;
}

function claimEvidenceItems(evidenceItems, maxItems) {
	const bySource = new Set();
	const selected = [];
	for (const item of evidenceItems) {
		if (selected.length >= maxItems) break;
		if (bySource.has(item.sourceId)) continue;
		selected.push(item);
		bySource.add(item.sourceId);
	}
	for (const item of evidenceItems) {
		if (selected.length >= maxItems) break;
		if (selected.includes(item)) continue;
		selected.push(item);
	}
	return selected;
}

function claimPrompt(question, evidenceItems, maxItems) {
	const selectedEvidence = claimEvidenceItems(evidenceItems, maxItems);
	const evidence = selectedEvidence
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

function verifiedDirectQuote(candidate, sourceText) {
	if (!candidate) return null;
	const quote = normalizeWhitespace(candidate);
	if (!quote) return null;
	return includesQuote(sourceText, quote) ? quote : null;
}

function sanitizeEvidence(rawEvidence, source, startIndex) {
	const rows = Array.isArray(rawEvidence?.evidence) ? rawEvidence.evidence : Array.isArray(rawEvidence) ? rawEvidence : [];
	const items = [];
	let index = startIndex;
	for (const row of rows) {
		if (typeof row !== "object" || row === null) continue;
		const text = typeof row.text === "string" ? row.text.trim() : "";
		if (!text) continue;
		const directQuote =
			typeof row.direct_quote === "string" && row.direct_quote.trim() ? verifiedDirectQuote(row.direct_quote, source.text) : null;
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
		const fallbackQuote = source.text.trim().slice(0, 180);
		items.push({
			evidenceId: nextId("ev", index++),
			sourceId: source.sourceId,
			text: fallbackText,
			directQuote: fallbackQuote,
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
			budgetEvents: state.budgetEvents.length,
		},
		budgetEvents: state.budgetEvents,
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
	const playwrightEndpoint = playwrightWsEndpoint(options.playwrightWs);
	const searxng = await pingSearxng(searxngBase(options.searxng), DEFAULT_USER_AGENT, 5000);
	const tools = {
		fetch: { available: typeof fetch === "function", version: process.version },
		playwright: { available: Boolean(playwright), version: playwright ? "importable" : null },
		playwrightEndpoint: { available: Boolean(playwrightEndpoint), version: playwrightEndpoint || null },
	};
	const capabilities = {
		search: searxng.configured && searxng.reachable,
		extraction: tools.playwright.available && tools.playwrightEndpoint.available,
		httpFallback: tools.fetch.available,
	};
	const remediation = [];
	if (!searxng.configured) remediation.push("Set connectedServices.searxng.baseUrl, FORGE_SEARXNG_URL, or --searxng to enable search.");
	else if (!searxng.reachable) remediation.push(`SearXNG unreachable: ${searxng.detail}`);
	if (!tools.playwright.available) remediation.push("Install Playwright for rendered page extraction.");
	if (tools.playwright.available && !tools.playwrightEndpoint.available) {
		remediation.push("Set connectedServices.playwright.wsEndpoint or FORGE_PLAYWRIGHT_WS_ENDPOINT for rendered browsing.");
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
	if (!base) fail("search requires a SearXNG instance; set connectedServices.searxng.baseUrl, FORGE_SEARXNG_URL, or --searxng <url>");

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
		playwrightWsEndpoint: flags.playwrightWs,
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
	if (!base) fail("research requires a SearXNG instance; set connectedServices.searxng.baseUrl, FORGE_SEARXNG_URL, or --searxng <url>");

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
		playwrightWsEndpoint: flags.playwrightWs,
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
	if (!base) fail("deep requires a SearXNG instance; set connectedServices.searxng.baseUrl, FORGE_SEARXNG_URL, or --searxng <url>");
	const runDirectory = resolve(flags.output);
	if (existsSync(runDirectory)) fail(`output directory already exists: ${runDirectory}`);
	mkdirSync(runDirectory, { recursive: true });

	const options = deepDefaults(flags);
	const state = {
		question,
		startedAt: nowIso(),
		startedAtMs: Date.now(),
		options: { ...options, searxng: base },
		seedQueries: uniqueSeedQueries,
		queryLog: [],
		sources: [],
		evidenceItems: [],
		claims: [],
		gaps: [],
		modelCalls: [],
		budgetEvents: [],
	};
	const seenQueries = new Set();
	const queuedQueries = [...uniqueSeedQueries];
	const seenUrls = new Map();

	for (let iteration = 1; iteration <= options.maxIterations; iteration += 1) {
		if (runtimeBudgetExceeded(state, options)) {
			addBudgetEvent(state, "maxRuntimeMs", "Stopped before starting another iteration.");
			break;
		}
		const iterationQueries = [];
		while (queuedQueries.length > 0) {
			if (seenQueries.size >= options.maxQueries) {
				addBudgetEvent(state, "maxQueries", "Stopped before scheduling another query.");
				queuedQueries.length = 0;
				break;
			}
			const query = queuedQueries.shift();
			const key = query.toLowerCase();
			if (seenQueries.has(key)) continue;
			seenQueries.add(key);
			iterationQueries.push(query);
		}
		if (iterationQueries.length === 0) break;

		for (const query of iterationQueries) {
			if (runtimeBudgetExceeded(state, options)) {
				addBudgetEvent(state, "maxRuntimeMs", "Stopped before searching another query.");
				break;
			}
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
				if (runtimeBudgetExceeded(state, options)) {
					addBudgetEvent(state, "maxRuntimeMs", "Stopped before reading another source.");
					break;
				}
				if (index > 0 && options.delayMs > 0) await sleep(options.delayMs);
				const normalized = normalizeUrl(result.url);
				if (seenUrls.has(normalized)) {
					const source = seenUrls.get(normalized);
					source.searchOrigins.push({ iteration, query, rank: result.rank, engine: result.engine, score: result.score });
					continue;
				}
				if (state.sources.length >= options.maxSources) {
					addBudgetEvent(state, "maxSources", "Stopped before reading another unique source.");
					break;
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
					if (isLowValueReading(reading)) {
						source.status = "needs_review";
						source.warnings.push("skipped evidence extraction because the page looked like a security checkpoint or low-value response");
					}
					const archived = writeDeepSource(runDirectory, source);
					Object.assign(source, archived);
					if (source.status !== "success") continue;
					if (!canCallDeepModel(state, options, "extract-evidence")) {
						source.warnings.push("skipped evidence extraction because the model-call budget was reached");
						continue;
					}
					const { value, record } = await callLocalJsonModel(runDirectory, "extract-evidence", evidencePrompt(source, question, options.maxEvidenceChars), {
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
			if (!canCallDeepModel(state, options, "expand-queries")) continue;
			const { value, record } = await callLocalJsonModel(
				runDirectory,
				"expand-queries",
				queryExpansionPrompt(question, [...seenQueries], state.evidenceItems, state.gaps, iteration + 1),
				{ queries: [] },
			);
			state.modelCalls.push(record);
			const followUps = Array.isArray(value?.queries) ? value.queries.slice(0, options.maxFollowupQueries) : [];
			for (const query of followUps) {
				if (typeof query !== "string" || !query.trim()) continue;
				if (queuedQueries.length + seenQueries.size >= options.maxQueries) {
					addBudgetEvent(state, "maxQueries", "Dropped follow-up query because the query budget was reached.");
					break;
				}
				const normalized = query.trim().toLowerCase();
				if (!seenQueries.has(normalized)) queuedQueries.push(query.trim());
			}
		}
	}

	let claimValue = { claims: [], gaps: [] };
	if (canCallDeepModel(state, options, "register-claims")) {
		const claimResult = await callLocalJsonModel(runDirectory, "register-claims", claimPrompt(question, state.evidenceItems, options.maxClaimEvidenceItems), {
			claims: [],
			gaps: [],
		});
		claimValue = claimResult.value;
		state.modelCalls.push(claimResult.record);
	}
	const { claims, gaps } = sanitizeClaims(claimValue, state.evidenceItems);
	state.claims = claims;
	state.gaps = appendBudgetGaps(state, gaps);
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
				modelCalls: state.modelCalls.length,
				budgetEvents: state.budgetEvents.length,
				valid: validation.valid,
				validationErrors: validation.errors,
			},
			null,
			2,
		)}\n`,
	);
	if (!validation.valid) process.exit(1);
}

async function commandAcademic(positionals, flags) {
	if (positionals.length === 0) fail("academic requires a query");
	if (!flags.output) fail("academic requires --output <new-directory>");
	const query = positionals.join(" ");
	const runDirectory = resolve(flags.output);
	if (existsSync(runDirectory)) fail(`output directory already exists: ${runDirectory}`);
	mkdirSync(runDirectory, { recursive: true });

	const classification = classifyAcademicQuery(query);
	const providers = academicProviderList(flags, classification);
	const contactEmail = flags.contactEmail || process.env.FORGE_ACADEMIC_CONTACT_EMAIL || process.env.UNPAYWALL_EMAIL || null;
	const options = {
		userAgent: flags.userAgent ?? DEFAULT_USER_AGENT,
		timeoutMs: flags.timeoutMs ?? DEFAULT_TIMEOUT_MS,
	};
	const state = {
		query,
		startedAt: nowIso(),
		classification,
		providers,
		options,
		providerRequests: [],
		providerErrors: [],
		works: [],
	};
	const sourceRecords = [];
	const normalizedRecords = [];
	const limit = flags.limit ?? DEFAULT_LIMIT;
	for (const providerName of providers.filter((provider) => provider !== "unpaywall")) {
		const provider = ACADEMIC_PROVIDERS[providerName];
		if (!provider) continue;
		const context = {
			query,
			limit,
			base: academicProviderBase(providerName, flags),
			runDirectory,
			options,
			state,
			contactEmail,
			classification,
			knownDois: [],
		};
		await runAcademicProvider(providerName, context, sourceRecords, normalizedRecords);
	}
	const knownDois = normalizedRecords.map((record) => record.identifiers?.doi).filter(Boolean);
	if (providers.includes("unpaywall") && contactEmail && knownDois.length > 0) {
		await runAcademicProvider(
			"unpaywall",
			{
				query,
				limit,
				base: academicProviderBase("unpaywall", flags),
				runDirectory,
				options,
				state,
				contactEmail,
				classification,
				knownDois,
			},
			sourceRecords,
			normalizedRecords,
		);
	}
	const { works, provenanceRows, decisions } = dedupeAcademicRecords(sourceRecords, normalizedRecords);
	const workBySource = new Map();
	for (const work of works) {
		for (const sourceRecordId of work.source_records) workBySource.set(sourceRecordId, work.work_id);
	}
	for (const source of sourceRecords) source.work_id = workBySource.get(source.source_record_id) ?? null;
	state.works = works;
	const risManifest = writeRisArtifacts(runDirectory, works, sourceRecords);
	writeJson(join(runDirectory, "academic_run.json"), {
		schemaVersion: ACADEMIC_SCHEMA_VERSION,
		query,
		startedAt: state.startedAt,
		completedAt: nowIso(),
		classification,
		providers,
		options: { ...options, limit, contactEmailConfigured: Boolean(contactEmail) },
		counts: {
			works: works.length,
			sourceRecords: sourceRecords.length,
			fieldProvenance: provenanceRows.length,
			dedupeDecisions: decisions.length,
			providerRequests: state.providerRequests.length,
			providerErrors: state.providerErrors.length,
			risRecords: risManifest.length,
		},
		providerCapabilities: Object.fromEntries(providers.map((provider) => [provider, providerCapabilities(provider)])),
	});
	writeJsonl(join(runDirectory, "works.jsonl"), works);
	writeJsonl(join(runDirectory, "source_records.jsonl"), sourceRecords);
	writeJsonl(join(runDirectory, "field_provenance.jsonl"), provenanceRows);
	writeJsonl(join(runDirectory, "dedupe_decisions.jsonl"), decisions);
	writeJsonl(join(runDirectory, "provider_requests.jsonl"), state.providerRequests);
	writeJsonl(join(runDirectory, "provider_errors.jsonl"), state.providerErrors);
	writeFileSync(join(runDirectory, "academic_report.md"), buildAcademicReport(state));
	const validation = validateAcademicRun(runDirectory, { emit: false });
	process.stdout.write(
		`${JSON.stringify(
			{
				runDirectory,
				query,
				providers,
				works: works.length,
				sourceRecords: sourceRecords.length,
				providerErrors: state.providerErrors.length,
				risRecords: risManifest.length,
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
	"--playwright-ws": { key: "playwrightWs", value: true },
	"--limit": { key: "limit", value: true, integer: true },
	"--read-count": { key: "readCount", value: true, integer: true },
	"--max-iterations": { key: "maxIterations", value: true, integer: true },
	"--max-queries": { key: "maxQueries", value: true, integer: true },
	"--max-sources": { key: "maxSources", value: true, integer: true },
	"--max-followup-queries": { key: "maxFollowupQueries", value: true, integer: true },
	"--max-model-calls": { key: "maxModelCalls", value: true, integer: true },
	"--max-runtime-ms": { key: "maxRuntimeMs", value: true, integer: true },
	"--max-evidence-chars": { key: "maxEvidenceChars", value: true, integer: true },
	"--max-claim-evidence-items": { key: "maxClaimEvidenceItems", value: true, integer: true },
	"--delay-ms": { key: "delayMs", value: true, integer: true },
	"--timeout-ms": { key: "timeoutMs", value: true, integer: true },
	"--categories": { key: "categories", value: true },
	"--engines": { key: "engines", value: true },
	"--language": { key: "language", value: true },
	"--safesearch": { key: "safesearch", value: true, integer: true },
	"--time-range": { key: "timeRange", value: true },
	"--pageno": { key: "pageNo", value: true, integer: true },
	"--providers": { key: "providers", value: true },
	"--contact-email": { key: "contactEmail", value: true },
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
  web-research.mjs doctor [--json] [--searxng <url>] [--playwright-ws <ws-endpoint>]
  web-research.mjs search <query...> --output <dir> [--searxng <url>] [--limit N]
      [--categories <cats>] [--engines <engines>] [--language <lang>]
      [--safesearch <0|1|2>] [--time-range <day|week|month|year>] [--pageno N]
  web-research.mjs read <url...> --output <dir> [--input-file <path>] [--playwright-ws <ws-endpoint>]
      [--render] [--no-render] [--delay-ms N] [--timeout-ms N]
  web-research.mjs research <query...> --output <dir> [--searxng <url>]
      [--limit N] [--read-count N] [--render] [--no-render] [--delay-ms N] [--playwright-ws <ws-endpoint>]
      [--categories <cats>] [--engines <engines>] [--language <lang>]
      [--safesearch <0|1|2>] [--time-range <day|week|month|year>] [--pageno N]
  web-research.mjs deep <query...> --output <dir> [--question <text>] [--query <query>] [--query-file <path>]
      [--max-iterations N] [--limit N] [--read-count N] [--render] [--no-render] [--playwright-ws <ws-endpoint>]
      [--max-queries N] [--max-sources N] [--max-followup-queries N] [--max-model-calls N]
      [--max-runtime-ms N] [--max-evidence-chars N] [--max-claim-evidence-items N]
      [--categories <cats>] [--engines <engines>] [--language <lang>]
      [--safesearch <0|1|2>] [--time-range <day|week|month|year>] [--pageno N]
  web-research.mjs academic <query...> --output <dir> [--limit N]
      [--providers <comma-separated>] [--contact-email <email>] [--timeout-ms N]
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
	else if (command === "academic") await commandAcademic(positionals, flags);
	else if (command === "validate") {
		if (positionals.length !== 1) fail("validate requires exactly one run directory");
		const runDirectory = resolve(positionals[0]);
		if (existsSync(join(runDirectory, "academic_run.json"))) validateAcademicRun(runDirectory, { exitOnError: true });
		else validateDeepRun(runDirectory, { exitOnError: true });
	}
	else fail(`unknown command: ${command}`, 2);
}

main().catch((error) => fail(error instanceof Error ? error.stack || error.message : String(error)));

#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
	existsSync,
	copyFileSync,
	lstatSync,
	mkdirSync,
	readdirSync,
	renameSync,
	readFileSync,
	rmSync,
	statSync,
	writeFileSync,
} from "node:fs";
import { basename, dirname, extname, join, relative, resolve, sep } from "node:path";

const DEFAULT_CHUNK_CHARACTERS = 150_000;
const DEFAULT_GLMOCR_URL = "http://llms:5002/glmocr/parse";
const DEFAULT_GLMOCR_TIMEOUT_MS = 300_000;
const DEFAULT_BASE_CHAT_URL = "http://llms:8008/v1/chat/completions";
const DEFAULT_BASE_MODEL = "code";
const LOW_TEXT_CHARACTERS = 40;
const MINIMUM_ALPHANUMERIC_RATIO = 0.2;
const MAXIMUM_PUNCTUATION_RATIO = 0.55;
const MAXIMUM_DOT_RUN_RATIO = 0.2;
const IMAGE_EXTENSIONS = new Set([".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"]);
const AUDIO_VIDEO_EXTENSIONS = new Set([".mp4", ".mov", ".mkv", ".webm", ".avi", ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus"]);
const SUPPORTED_EXTENSIONS = new Set([".pdf", ".docx", ".txt", ".md", ".markdown", ".html", ".htm", ".rtf", ...IMAGE_EXTENSIONS, ...AUDIO_VIDEO_EXTENSIONS]);
const RESERVED_WORKSPACE_DIRECTORIES = new Set(["Ingest", "Originals", "Generated"]);
const GENERATED_ARTIFACT_NAMES = new Set([
	"evidence_table.csv",
	"methods_matrix.csv",
	"claims_matrix.md",
	"key_terms.md",
	"literature_summary.md",
	"citation_notes.md",
	"research_gaps.md",
]);
const MANIFEST_COLUMNS = [
	"document_id",
	"source_path",
	"source_sha256",
	"source_format",
	"status",
	"suggested_pipeline",
	"output_directory",
	"title",
	"author",
	"document_date",
	"page_count",
	"extraction_method",
	"ocr_used",
	"warning_count",
	"error",
];
const ARTIFACT_MANIFEST_COLUMNS = [
	"role",
	"document_id",
	"source_path",
	"destination_path",
	"sha256",
	"created_at",
];

function fail(message, exitCode = 1) {
	process.stderr.write(`Error: ${message}\n`);
	process.exit(exitCode);
}

function run(command, args, options = {}) {
	return spawnSync(command, args, { encoding: "utf8", maxBuffer: 100 * 1024 * 1024, ...options });
}

function toolInfo(command, args = ["--version"]) {
	const result = run(command, args);
	if (result.error?.code === "ENOENT") return { available: false, version: null };
	if (result.error || result.status !== 0) return { available: false, version: null };
	const combined = `${result.stdout}\n${result.stderr}`.trim();
	return { available: true, version: combined.split(/\r?\n/, 1)[0] || "available" };
}

function inspectTools() {
	const glmocrUrl = process.env.FORGE_GLMOCR_URL || process.env.FORGE_OCR_URL || process.env.OCR_URL || DEFAULT_GLMOCR_URL;
	return {
		pandoc: toolInfo("pandoc"),
		pdftotext: toolInfo("pdftotext", ["-v"]),
		pdfinfo: toolInfo("pdfinfo", ["-v"]),
		pdfimages: toolInfo("pdfimages", ["-v"]),
		pdftoppm: toolInfo("pdftoppm", ["-v"]),
		ocrmypdf: toolInfo("ocrmypdf"),
		tesseract: toolInfo("tesseract"),
		ffmpeg: toolInfo("ffmpeg", ["-version"]),
		glmocr: { available: Boolean(glmocrUrl), version: glmocrUrl },
	};
}

function printDoctor(asJson) {
	const tools = inspectTools();
	const capabilities = {
		pandocDocuments: tools.pandoc.available,
		pdfText: tools.pdftotext.available && tools.pdfinfo.available,
		pdfImageDetection: tools.pdfimages.available,
		pdfPageRendering: tools.pdftoppm.available,
		pdfOcr: tools.ocrmypdf.available && tools.tesseract.available,
		ffmpegMedia: tools.ffmpeg.available,
		glmocrSdk: tools.glmocr.available,
	};
	const remediation = [];
	if (!tools.pandoc.available) remediation.push("Install Pandoc (macOS: brew install pandoc; Debian/Ubuntu: apt install pandoc).");
	if (!tools.ffmpeg.available) remediation.push("Install FFmpeg (macOS: brew install ffmpeg; Debian/Ubuntu: apt install ffmpeg).");
	if (!capabilities.pdfText || !capabilities.pdfImageDetection || !capabilities.pdfPageRendering) {
		remediation.push("Install Poppler (macOS: brew install poppler; Debian/Ubuntu: apt install poppler-utils).");
	}
	if (!capabilities.pdfOcr) {
		remediation.push("Install OCRmyPDF and Tesseract (macOS: brew install ocrmypdf tesseract; Debian/Ubuntu: apt install ocrmypdf tesseract-ocr).");
	}
	if (asJson) {
		process.stdout.write(`${JSON.stringify({ tools, capabilities, remediation }, null, 2)}\n`);
		return;
	}
	for (const [name, info] of Object.entries(tools)) {
		process.stdout.write(`${name}: ${info.available ? info.version : "missing"}\n`);
	}
	process.stdout.write(`Pandoc document conversion: ${capabilities.pandocDocuments ? "available" : "unavailable"}\n`);
	process.stdout.write(`PDF text extraction: ${capabilities.pdfText ? "available" : "unavailable"}\n`);
	process.stdout.write(`Automatic PDF OCR: ${capabilities.pdfOcr ? "available" : "unavailable"}\n`);
	process.stdout.write(`GLM-OCR SDK endpoint: ${tools.glmocr.version}\n`);
	for (const item of remediation) process.stdout.write(`Action: ${item}\n`);
}

function sha256(buffer) {
	return createHash("sha256").update(buffer).digest("hex");
}

function ensureFinalNewline(value) {
	return value.length === 0 || value.endsWith("\n") ? value : `${value}\n`;
}

function unicodeLength(value) {
	return Array.from(value).length;
}

function safeStem(filePath) {
	const extension = extname(filePath);
	const raw = basename(filePath, extension).normalize("NFKC").trim();
	const safe = raw.replace(/[^\p{L}\p{N}._-]+/gu, "-").replace(/^-+|-+$/g, "");
	return safe || "document";
}

function safeFilenameStem(value) {
	const raw = String(value ?? "").normalize("NFKC").trim();
	const safe = raw
		.replace(/[<>:"/\\|?*\u0000-\u001F]+/gu, " ")
		.replace(/\s+/g, " ")
		.replace(/\.+$/g, "")
		.trim();
	return safe || "document";
}

function safeMarkdownFilename(value) {
	const raw = String(value ?? "").normalize("NFKC").trim();
	const withoutExtension = raw.toLowerCase().endsWith(".md") ? raw.slice(0, -3) : raw;
	return `${safeFilenameStem(withoutExtension)}.md`;
}

function isSafeMarkdownFilename(value) {
	return (
		typeof value === "string" &&
		value.trim() === value &&
		value.endsWith(".md") &&
		value === basename(value) &&
		!/[<>:"/\\|?*\u0000-\u001F]/u.test(value) &&
		value !== ".md" &&
		value !== "..md"
	);
}

function sourceFormat(filePath) {
	const extension = extname(filePath).toLowerCase();
	if (extension === ".markdown") return "md";
	if (extension === ".htm") return "html";
	if (extension === ".jpeg") return "jpg";
	if (extension === ".tiff") return "tif";
	return extension.slice(1);
}

function mimeType(filePath) {
	const extension = extname(filePath).toLowerCase();
	if (extension === ".pdf") return "application/pdf";
	if (extension === ".jpg" || extension === ".jpeg") return "image/jpeg";
	if (extension === ".png") return "image/png";
	if (extension === ".webp") return "image/webp";
	if (extension === ".tif" || extension === ".tiff") return "image/tiff";
	if (extension === ".bmp") return "image/bmp";
	return "application/octet-stream";
}

function evidence(value, origin = null, confidence = null, locator = null) {
	return { value: value || null, origin: value ? origin : null, confidence: value ? confidence : null, locator: value ? locator : null };
}

function parsePandocInline(value) {
	if (typeof value === "string") return value;
	if (Array.isArray(value)) return value.map(parsePandocInline).join("");
	if (!value || typeof value !== "object") return "";
	if (value.t === "Str" || value.t === "Code" || value.t === "Math") return parsePandocInline(value.c);
	if (value.t === "Space" || value.t === "SoftBreak" || value.t === "LineBreak") return " ";
	if (value.t === "MetaString") return String(value.c ?? "");
	if (["MetaInlines", "MetaBlocks", "MetaList"].includes(value.t)) return parsePandocInline(value.c);
	if (value.t === "MetaBool") return String(value.c);
	if (Array.isArray(value.c)) return parsePandocInline(value.c);
	return "";
}

function normalizeMetadataValue(value) {
	const normalized = parsePandocInline(value).replace(/\s+/g, " ").trim();
	return normalized || null;
}

function parsePdfInfo(output) {
	const values = {};
	for (const line of output.split(/\r?\n/)) {
		const match = line.match(/^([^:]+):\s*(.*)$/);
		if (match) values[match[1].trim()] = match[2].trim();
	}
	return values;
}

function countContentCharacters(value) {
	return (value.match(/[\p{L}\p{N}]/gu) ?? []).length;
}

function textQuality(value) {
	const nonWhitespace = (value.match(/\S/g) ?? []).length;
	const alphanumeric = countContentCharacters(value);
	const wordLikeTokens = (value.match(/\p{L}{3,}/gu) ?? []).length;
	const punctuation = (value.match(/[.!?,;:$%#&*()]/g) ?? []).length;
	const dotRunCharacters = (value.match(/\.{3,}/g) ?? []).reduce((total, run) => total + run.length, 0);
	const alphanumericRatio = nonWhitespace === 0 ? 0 : alphanumeric / nonWhitespace;
	const punctuationRatio = nonWhitespace === 0 ? 0 : punctuation / nonWhitespace;
	const dotRunRatio = nonWhitespace === 0 ? 0 : dotRunCharacters / nonWhitespace;
	const reasons = [];
	if (alphanumeric < LOW_TEXT_CHARACTERS) reasons.push("low-text");
	if (nonWhitespace > 100 && alphanumericRatio < MINIMUM_ALPHANUMERIC_RATIO) reasons.push("low-alphanumeric-ratio");
	if (nonWhitespace > 100 && punctuationRatio > MAXIMUM_PUNCTUATION_RATIO) reasons.push("punctuation-heavy");
	if (nonWhitespace > 100 && dotRunRatio > MAXIMUM_DOT_RUN_RATIO) reasons.push("dot-run-heavy");
	if (alphanumeric >= LOW_TEXT_CHARACTERS && wordLikeTokens < 3) reasons.push("insufficient-word-like-text");
	const score =
		Math.min(1, alphanumericRatio / 0.7) * 0.55 +
		Math.min(1, wordLikeTokens / 20) * 0.25 +
		Math.max(0, 1 - punctuationRatio / MAXIMUM_PUNCTUATION_RATIO) * 0.1 +
		Math.max(0, 1 - dotRunRatio / MAXIMUM_DOT_RUN_RATIO) * 0.1;
	return {
		nonWhitespace,
		alphanumeric,
		wordLikeTokens,
		alphanumericRatio: Number(alphanumericRatio.toFixed(4)),
		punctuationRatio: Number(punctuationRatio.toFixed(4)),
		dotRunRatio: Number(dotRunRatio.toFixed(4)),
		score: Number(score.toFixed(4)),
		suspicious: reasons.length > 0,
		reasons,
	};
}

function textWarnings(value) {
	const warnings = [];
	const replacementCharacters = (value.match(/\uFFFD/g) ?? []).length;
	const controlCharacters = (value.match(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g) ?? []).length;
	const quality = textQuality(value);
	if (replacementCharacters > 0) warnings.push(`Found ${replacementCharacters} Unicode replacement characters; encoding may be damaged.`);
	if (controlCharacters > 0) warnings.push(`Found ${controlCharacters} unexpected control characters.`);
	if (quality.nonWhitespace > 100 && quality.alphanumericRatio < MINIMUM_ALPHANUMERIC_RATIO) {
		warnings.push("Extracted text has an unusually low proportion of letters and numbers; review for garbled output.");
	}
	if (quality.nonWhitespace === 0) warnings.push("No readable text was extracted.");
	return warnings;
}

function markdownStructure(markdown) {
	const headings = [];
	for (const [index, line] of markdown.split("\n").entries()) {
		const match = line.match(/^(#{1,6})\s+(.+?)\s*#*$/);
		if (match) headings.push({ text: match[2], level: match[1].length, locator: `document.md:${index + 1}` });
	}
	const tableSeparators = markdown.match(/^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$/gm) ?? [];
	const citations = markdown.match(/\[(?:\d+|[A-Z][^\]]+?,\s*\d{4}[a-z]?)\]/g) ?? [];
	const footnotes = markdown.match(/^\[\^[^\]]+\]:/gm) ?? [];
	const appendices = headings.filter((heading) => /^(appendix|appendices)\b/i.test(heading.text));
	return {
		headings,
		tables: { count: tableSeparators.length, method: "markdown-pattern", confidence: "medium" },
		citations: { count: citations.length, method: "text-pattern", confidence: "low" },
		footnotes: { count: footnotes.length, method: "markdown-pattern", confidence: "high" },
		appendices: { count: appendices.length, headings: appendices, method: "heading-pattern", confidence: "medium" },
	};
}

function splitIntoChunks(markdown, maximumCharacters) {
	if (unicodeLength(markdown) <= maximumCharacters) return [markdown];
	const blocks = markdown.match(/[\s\S]*?(?:\n{2,}|$)/g)?.filter((block) => block.length > 0) ?? [markdown];
	const chunks = [];
	let current = "";
	for (const block of blocks) {
		if (current && unicodeLength(current) + unicodeLength(block) > maximumCharacters) {
			chunks.push(current);
			current = "";
		}
		current += block;
	}
	if (current) chunks.push(current);
	if (chunks.join("") !== markdown) throw new Error("internal chunking error: chunks do not reconstruct the extracted document");
	return chunks;
}

function extractPdfPages(filePath, pageCount) {
	const result = run("pdftotext", ["-layout", "-enc", "UTF-8", filePath, "-"]);
	if (result.error?.code === "ENOENT") throw new Error("pdftotext is required for PDF ingestion but was not found on PATH");
	if (result.error) throw new Error(`pdftotext could not start: ${result.error.message}`);
	if (result.status !== 0) throw new Error(`pdftotext failed: ${result.stderr.trim() || `exit status ${result.status}`}`);
	const pages = result.stdout.replace(/\r\n/g, "\n").split("\f");
	if (pages.length > pageCount && pages.at(-1)?.trim() === "") pages.pop();
	const observedPageSegments = pages.length;
	const warnings = [];
	if (observedPageSegments < pageCount) {
		warnings.push(`pdftotext returned ${observedPageSegments} page segments for a ${pageCount}-page PDF; missing extraction pages were padded as empty.`);
	}
	if (observedPageSegments > pageCount) {
		warnings.push(`pdftotext returned ${observedPageSegments} page segments for a ${pageCount}-page PDF; extra segments were merged into the final page.`);
	}
	while (pages.length < pageCount) pages.push("");
	if (pages.length > pageCount && pageCount > 0) {
		pages.splice(pageCount - 1, pages.length - pageCount + 1, pages.slice(pageCount - 1).join("\n"));
	}
	return { pages, warnings };
}

function pdfImagePages(filePath) {
	const result = run("pdfimages", ["-list", filePath]);
	if (result.error?.code === "ENOENT") return { pages: new Set(), available: false, warning: "pdfimages is unavailable; image-backed page detection could not run." };
	if (result.error || result.status !== 0) {
		return { pages: new Set(), available: false, warning: `pdfimages failed: ${result.stderr.trim() || result.error?.message || `exit status ${result.status}`}` };
	}
	const pages = new Set();
	for (const line of result.stdout.split(/\r?\n/)) {
		const match = line.match(/^\s*(\d+)\s+\d+\s+\S+/);
		if (match) pages.add(Number(match[1]));
	}
	return { pages, available: true, warning: null };
}

function renderVisionPages(filePath, documentDirectory, pages, tools) {
	if (pages.length === 0) return { renderedPages: [], warnings: [] };
	if (!tools.pdftoppm.available) {
		return {
			renderedPages: [],
			warnings: ["pdftoppm is unavailable; unresolved PDF pages could not be rendered for vision fallback."],
		};
	}
	const directory = join(documentDirectory, "derived", "vision-pages");
	mkdirSync(directory, { recursive: true });
	const renderedPages = [];
	const warnings = [];
	for (const page of pages) {
		const name = `page-${String(page).padStart(4, "0")}`;
		const outputPrefix = join(directory, name);
		const result = run("pdftoppm", ["-f", String(page), "-l", String(page), "-singlefile", "-png", "-r", "180", filePath, outputPrefix]);
		const outputPath = `${outputPrefix}.png`;
		if (result.error || result.status !== 0 || !existsSync(outputPath)) {
			warnings.push(`Could not render PDF page ${page} for vision fallback: ${result.stderr.trim() || result.error?.message || `exit status ${result.status}`}`);
			continue;
		}
		renderedPages.push({
			page,
			path: `derived/vision-pages/${name}.png`,
			sha256: sha256(readFileSync(outputPath)),
		});
	}
	return { renderedPages, warnings };
}

function joinPdfPages(pages) {
	let markdown = "";
	const entries = [];
	for (const [index, rawPage] of pages.entries()) {
		if (index > 0) markdown += "\n\n";
		const page = rawPage.replace(/[\t ]+$/gm, "").replace(/\s+$/g, "");
		const startOffset = markdown.length;
		markdown += page;
		const startLine = page ? (markdown.slice(0, startOffset).match(/\n/g) ?? []).length + 1 : null;
		const endLine = page ? (markdown.match(/\n/g) ?? []).length + 1 : null;
		entries.push({
			markdownStartLine: startLine,
			markdownEndLine: endLine,
			sourceLocator: { type: "pdf-page", page: index + 1 },
			method: "page-extraction",
			confidence: "high",
		});
	}
	return { markdown: ensureFinalNewline(markdown), entries };
}

function extractGlmocrMarkdown(body) {
	if (typeof body.markdown_result === "string" && body.markdown_result.trim()) return body.markdown_result;
	if (typeof body.md_results === "string" && body.md_results.trim()) return body.md_results;
	if (typeof body.text === "string" && body.text.trim()) return body.text;
	return "";
}

function extractGlmocrLayout(body) {
	if (body.json_result && typeof body.json_result === "object") return body.json_result;
	if (body.layout_details && typeof body.layout_details === "object") return body.layout_details;
	return null;
}

function glmocrPageCount(body, fallback) {
	const pages = body.data_info?.pages;
	if (Array.isArray(pages) && pages.length > 0) return pages.length;
	if (Number.isInteger(body.page_count) && body.page_count > 0) return body.page_count;
	return fallback;
}

async function callGlmocr(filePath, options) {
	const buffer = readFileSync(filePath);
	const type = mimeType(filePath);
	const dataUrl = `data:${type};base64,${buffer.toString("base64")}`;
	const payload = type === "application/pdf" ? { file: dataUrl } : { image_url: dataUrl };
	if (options.glmocrLayoutVisualization) payload.need_layout_visualization = true;
	const controller = new AbortController();
	const timeout = setTimeout(() => controller.abort(), options.glmocrTimeoutMs);
	try {
		const response = await fetch(options.glmocrUrl, {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify(payload),
			signal: controller.signal,
		});
		const text = await response.text();
		let body = {};
		try {
			body = text ? JSON.parse(text) : {};
		} catch {
			throw new Error(`GLM-OCR returned non-JSON response with HTTP ${response.status}`);
		}
		if (!response.ok || body.error || body.ok === false) {
			throw new Error(body.error || `GLM-OCR failed with HTTP ${response.status}`);
		}
		return body.raw && typeof body.raw === "object" ? { ...body.raw, ...body } : body;
	} catch (error) {
		if (error?.name === "AbortError") throw new Error(`GLM-OCR timed out after ${options.glmocrTimeoutMs} ms`);
		throw error;
	} finally {
		clearTimeout(timeout);
	}
}

function writeGlmocrArtifacts(documentDirectory, body, layout) {
	const derivedDirectory = join(documentDirectory, "derived");
	mkdirSync(derivedDirectory, { recursive: true });
	const rawPath = join(derivedDirectory, "glmocr-response.json");
	const layoutPath = join(derivedDirectory, "glmocr-layout.json");
	writeJson(rawPath, body);
	if (layout) writeJson(layoutPath, layout);
	return {
		responsePath: "derived/glmocr-response.json",
		responseSha256: sha256(readFileSync(rawPath)),
		layoutPath: layout ? "derived/glmocr-layout.json" : null,
		layoutSha256: layout ? sha256(readFileSync(layoutPath)) : null,
	};
}

async function extractGlmocr(filePath, documentDirectory, options, fallbackPageCount = null) {
	const body = await callGlmocr(filePath, options);
	const markdown = ensureFinalNewline(extractGlmocrMarkdown(body).replace(/\r\n/g, "\n").replace(/\r/g, "\n"));
	if (markdown.trim() === "") throw new Error("GLM-OCR response did not contain markdown_result, md_results, or text");
	const layout = extractGlmocrLayout(body);
	const artifacts = writeGlmocrArtifacts(documentDirectory, body, layout);
	const pageCount = glmocrPageCount(body, fallbackPageCount ?? (IMAGE_EXTENSIONS.has(extname(filePath).toLowerCase()) ? 1 : null));
	const warnings = textWarnings(markdown);
	return {
		markdown,
		method: "glm-ocr-sdk",
		pageCount,
		warnings,
		embedded: { title: null, author: null, date: null, source: null },
		sourceMapEntries: [
			{
				markdownStartLine: markdown ? 1 : null,
				markdownEndLine: markdown ? markdown.split("\n").length : null,
				sourceLocator: { type: IMAGE_EXTENSIONS.has(extname(filePath).toLowerCase()) ? "image" : "document", path: filePath },
				method: "document-conversion",
				confidence: "medium",
			},
		],
		ocr: {
			mode: options.ocr,
			backend: "glmocr",
			attempted: true,
			used: true,
			reason: "GLM-OCR SDK backend",
			commandMode: "remote-sdk",
			candidatePages: pageCount ? Array.from({ length: pageCount }, (_, index) => index + 1) : [],
			selectedPages: pageCount ? Array.from({ length: pageCount }, (_, index) => index + 1) : [],
			beforeQuality: [],
			afterQuality: [textQuality(markdown)],
			unresolvedPages: [],
			derivedPath: artifacts.responsePath,
			derivedSha256: artifacts.responseSha256,
			layoutPath: artifacts.layoutPath,
			layoutSha256: artifacts.layoutSha256,
			error: null,
			url: options.glmocrUrl,
		},
		vision: {
			mode: "not-applicable",
			required: false,
			candidatePages: [],
			renderedPages: [],
			completedPages: [],
			used: false,
			unavailableReason: null,
		},
	};
}

async function extractPdf(filePath, documentDirectory, options, tools) {
	const ocrMode = options.ocr;
	if (!tools.pdfinfo.available) throw new Error("pdfinfo is required for PDF ingestion but was not found on PATH");
	if (!tools.pdftotext.available) throw new Error("pdftotext is required for PDF ingestion but was not found on PATH");
	const infoResult = run("pdfinfo", ["-isodates", filePath]);
	if (infoResult.status !== 0) throw new Error(`pdfinfo failed: ${infoResult.stderr.trim() || `exit status ${infoResult.status}`}`);
	const info = parsePdfInfo(infoResult.stdout);
	const pageCount = Number.parseInt(info.Pages, 10);
	if (!Number.isInteger(pageCount) || pageCount < 1) throw new Error("pdfinfo did not report a valid page count");
	const warnings = [];
	if (options.ocrBackend !== "local" && ocrMode !== "never") {
		try {
			const remote = await extractGlmocr(filePath, documentDirectory, options, pageCount);
			if (textQuality(remote.markdown).suspicious) {
				const allPages = Array.from({ length: pageCount }, (_, index) => index + 1);
				const visionRendering = renderVisionPages(filePath, documentDirectory, allPages, tools);
				remote.warnings.push(...visionRendering.warnings);
				if (visionRendering.renderedPages.length > 0) {
					remote.warnings.push(`GLM-OCR output is low quality; vision fallback is required for pages: ${visionRendering.renderedPages.map(({ page }) => page).join(", ")}.`);
					remote.ocr.unresolvedPages = allPages;
					remote.vision = {
						mode: "auto",
						required: true,
						candidatePages: allPages,
						renderedPages: visionRendering.renderedPages,
						completedPages: [],
						used: false,
						unavailableReason: null,
					};
				} else {
					remote.warnings.push("GLM-OCR output is low quality, but pages could not be rendered for the vision fallback.");
				}
			}
			remote.warnings.unshift(...warnings);
			return remote;
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			if (options.ocrBackend === "glmocr") throw new Error(`GLM-OCR SDK failed: ${message}`);
			warnings.push(`GLM-OCR SDK failed; falling back to local OCR: ${message}`);
		}
	}
	let pageExtraction = extractPdfPages(filePath, pageCount);
	const originalPages = pageExtraction.pages;
	let pages = originalPages;
	warnings.push(...pageExtraction.warnings);
	const beforeQuality = pages.map(textQuality);
	const images = pdfImagePages(filePath);
	if (images.warning) warnings.push(images.warning);
	const detectedCandidatePages = beforeQuality
		.map((quality, index) => ({ quality, page: index + 1 }))
		.filter(({ quality, page }) => quality.suspicious && (quality.reasons.some((reason) => reason !== "low-text") || images.pages.has(page)))
		.map(({ page }) => page);
	const candidatePages = ocrMode === "force" ? pages.map((_, index) => index + 1) : detectedCandidatePages;
	let ocrUsed = false;
	let ocrAttempted = false;
	let ocrError = null;
	let ocrSha256 = null;
	let ocrCommandMode = null;
	let selectedPages = [];
	let afterQuality = beforeQuality;
	let unresolvedPages = detectedCandidatePages;
	if (ocrMode === "force" || (ocrMode === "auto" && candidatePages.length > 0)) {
		ocrAttempted = true;
		if (!tools.ocrmypdf.available || !tools.tesseract.available) {
			ocrError = "OCRmyPDF and Tesseract are required for PDF OCR but one or both are unavailable.";
			warnings.push(ocrError);
		} else {
			const derivedDirectory = join(documentDirectory, "derived");
			mkdirSync(derivedDirectory, { recursive: true });
			const ocrPath = join(derivedDirectory, "ocr.pdf");
			const hasGarbledText = candidatePages.some((page) => beforeQuality[page - 1].reasons.some((reason) => reason !== "low-text"));
			ocrCommandMode = ocrMode === "force" || hasGarbledText ? "force" : "skip-text";
			const textModeArgument = ocrCommandMode === "force" ? "--force-ocr" : "--skip-text";
			const result = run("ocrmypdf", [textModeArgument, "--rotate-pages", "--deskew", "--quiet", filePath, ocrPath]);
			if (result.error || result.status !== 0) {
				ocrError = `OCRmyPDF failed: ${result.stderr.trim() || result.error?.message || `exit status ${result.status}`}`;
				warnings.push(ocrError);
				rmSync(ocrPath, { force: true });
			} else {
				ocrSha256 = sha256(readFileSync(ocrPath));
				pageExtraction = extractPdfPages(ocrPath, pageCount);
				const ocrPages = pageExtraction.pages;
				warnings.push(...pageExtraction.warnings);
				afterQuality = ocrPages.map(textQuality);
				selectedPages = candidatePages.filter((page) => {
					const before = beforeQuality[page - 1];
					const after = afterQuality[page - 1];
					return after.score > before.score || (before.suspicious && !after.suspicious);
				});
				pages = originalPages.map((page, index) => (selectedPages.includes(index + 1) ? ocrPages[index] : page));
				ocrUsed = selectedPages.length > 0;
				unresolvedPages = detectedCandidatePages.filter((page) => textQuality(pages[page - 1]).suspicious);
				if (unresolvedPages.length > 0) {
					warnings.push(`OCR left suspicious pages unresolved: ${unresolvedPages.join(", ")}.`);
				}
			}
		}
	}
	if (ocrMode === "never" && candidatePages.length > 0) {
		warnings.push(`Suspicious PDF pages were not OCRed because OCR was disabled: ${candidatePages.join(", ")}.`);
	}
	if (!images.available && beforeQuality.every((quality) => quality.alphanumeric < LOW_TEXT_CHARACTERS)) {
		warnings.push("The PDF contains very little text, but image-backed page detection was unavailable.");
	}
	const visionRendering = renderVisionPages(filePath, documentDirectory, unresolvedPages, tools);
	warnings.push(...visionRendering.warnings);
	if (visionRendering.renderedPages.length > 0) {
		warnings.push(`Vision fallback is required for unresolved pages: ${visionRendering.renderedPages.map(({ page }) => page).join(", ")}.`);
	}
	const joined = joinPdfPages(pages);
	warnings.push(...textWarnings(joined.markdown));
	if (/^[\t ]*\S[^\n]*[\t ]{3,}\S[^\n]*$/m.test(joined.markdown)) {
		warnings.push("Possible fixed-layout columns or tables require structural review.");
	}
	return {
		markdown: joined.markdown,
		method: ocrUsed ? (selectedPages.length === pageCount ? "ocrmypdf+pdftotext-layout" : "pdftotext-layout+ocr-fallback") : "pdftotext-layout",
		pageCount,
		warnings,
		embedded: {
			title: info.Title || null,
			author: info.Author || null,
			date: info.CreationDate || null,
			source: null,
		},
		sourceMapEntries: joined.entries,
		ocr: {
			mode: ocrMode,
			attempted: ocrAttempted,
			used: ocrUsed,
			reason: candidatePages.length > 0 ? (ocrMode === "force" ? "forced by user" : "suspicious page text quality") : null,
			commandMode: ocrCommandMode,
			candidatePages,
			selectedPages,
			beforeQuality,
			afterQuality,
			unresolvedPages,
			derivedPath: ocrSha256 ? "derived/ocr.pdf" : null,
			derivedSha256: ocrSha256,
			error: ocrError,
		},
		vision: {
			mode: "auto",
			required: visionRendering.renderedPages.length > 0,
			candidatePages: unresolvedPages,
			renderedPages: visionRendering.renderedPages,
			completedPages: [],
			used: false,
			unavailableReason: null,
		},
	};
}

function extractPandoc(filePath, documentDirectory, format, tools) {
	if (!tools.pandoc.available) throw new Error(`Pandoc is required for ${format.toUpperCase()} ingestion but was not found on PATH`);
	const from = format === "md" ? "markdown" : format;
	const result = run("pandoc", [filePath, `--from=${from}`, "--to=gfm", "--wrap=none", "--extract-media=derived"], { cwd: documentDirectory });
	if (result.error || result.status !== 0) {
		throw new Error(`Pandoc conversion failed: ${result.stderr.trim() || result.error?.message || `exit status ${result.status}`}`);
	}
	const warnings = result.stderr.trim() ? [result.stderr.trim()] : [];
	const metadataResult = run("pandoc", [filePath, `--from=${from}`, "--to=json"], { cwd: documentDirectory });
	let pandocMetadata = {};
	if (metadataResult.status === 0) {
		try {
			pandocMetadata = JSON.parse(metadataResult.stdout).meta ?? {};
		} catch {
			warnings.push("Pandoc metadata output was not valid JSON.");
		}
	} else {
		warnings.push(`Pandoc metadata extraction failed: ${metadataResult.stderr.trim() || `exit status ${metadataResult.status}`}`);
	}
	const markdown = ensureFinalNewline(result.stdout.replace(/\r\n/g, "\n"));
	warnings.push(...textWarnings(markdown));
	return {
		markdown,
		method: "pandoc-gfm",
		pageCount: null,
		warnings,
		embedded: {
			title: normalizeMetadataValue(pandocMetadata.title),
			author: normalizeMetadataValue(pandocMetadata.author),
			date: normalizeMetadataValue(pandocMetadata.date),
			source: normalizeMetadataValue(pandocMetadata.source),
		},
		sourceMapEntries: [
			{
				markdownStartLine: markdown ? 1 : null,
				markdownEndLine: markdown ? markdown.split("\n").length : null,
				sourceLocator: { type: "document", path: filePath },
				method: "document-conversion",
				confidence: "medium",
			},
		],
		ocr: { mode: "not-applicable", attempted: false, used: false, reason: null, candidatePages: [], beforeContentCharacters: [], afterContentCharacters: [], remainingLowTextPages: [], derivedPath: null, derivedSha256: null, error: null },
	};
}

function extractText(filePath) {
	const buffer = readFileSync(filePath);
	let markdown;
	try {
		markdown = new TextDecoder("utf-8", { fatal: true }).decode(buffer);
	} catch {
		throw new Error("Text input is not valid UTF-8");
	}
	markdown = ensureFinalNewline(markdown.replace(/\r\n/g, "\n").replace(/\r/g, "\n"));
	return {
		markdown,
		method: "direct-utf8",
		pageCount: null,
		warnings: textWarnings(markdown),
		embedded: { title: null, author: null, date: null, source: null },
		sourceMapEntries: [
			{
				markdownStartLine: markdown ? 1 : null,
				markdownEndLine: markdown ? markdown.split("\n").length : null,
				sourceLocator: { type: "document", path: filePath },
				method: "document-conversion",
				confidence: "high",
			},
		],
		ocr: { mode: "not-applicable", attempted: false, used: false, reason: null, candidatePages: [], beforeContentCharacters: [], afterContentCharacters: [], remainingLowTextPages: [], derivedPath: null, derivedSha256: null, error: null },
	};
}

function extractMedia(filePath, documentDirectory, tools) {
	if (!tools.ffmpeg.available) throw new Error("FFmpeg is required for media ingestion but was not found on PATH");
	const derivedDirectory = join(documentDirectory, "derived");
	mkdirSync(derivedDirectory, { recursive: true });
	const audioPath = join(derivedDirectory, "audio.mp3");
	const result = run("ffmpeg", ["-i", filePath, "-vn", "-ar", "44100", "-ac", "2", "-b:a", "160k", audioPath]);
	if (result.error || result.status !== 0 || !existsSync(audioPath)) {
		throw new Error(`FFmpeg extraction failed: ${result.stderr?.trim() || result.error?.message || "exit status " + result.status}`);
	}
	const markdown = `Media file extracted to derived/audio.mp3. Waiting for transcription.\\n`;
	return {
		markdown,
		method: "ffmpeg-audio-extraction",
		pageCount: null,
		warnings: [],
		embedded: { title: null, author: null, date: null, source: null },
		sourceMapEntries: [
			{
				markdownStartLine: 1,
				markdownEndLine: 1,
				sourceLocator: { type: "media", path: filePath },
				method: "document-conversion",
				confidence: "high",
			},
		],
		ocr: { mode: "not-applicable", attempted: false, used: false, reason: null, candidatePages: [], beforeQuality: [], afterQuality: [], unresolvedPages: [], derivedPath: null, derivedSha256: null, error: null },
	};
}

async function categorizeFolder(inputs) {
	const baseChatUrl = process.env.FORGE_BASE_CHAT_URL || process.env.FORGE_CHAT_URL || DEFAULT_BASE_CHAT_URL;
	const baseModel = process.env.FORGE_BASE_MODEL || DEFAULT_BASE_MODEL;
	try {
		const response = await fetch(baseChatUrl, {
			method: "POST",
			headers: { "Content-Type": "application/json", "Authorization": "Bearer local" },
			body: JSON.stringify({
				model: baseModel,
				messages: [
					{
						role: "system",
						content: "You are a folder categorization assistant. Analyze the list of file paths. Reply with exactly one word: 'personal-admin', 'literature', or 'general'."
					},
					{
						role: "user",
						content: inputs.join("\\n")
					}
				],
				temperature: 0
			})
		});
		if (response.ok) {
			const body = await response.json();
			let text = body.choices[0].message.content.trim().toLowerCase();
			if (text.includes("</think>")) {
				text = text.split("</think>").pop().trim();
			}
			if (text.includes("personal-admin") || text.includes("admin")) return "personal-admin";
			if (text.includes("literature") || text.includes("academic")) return "literature";
		}
	} catch (e) {
		// Ignore error and return general
	}
	return "general";
}

function writeJson(filePath, value) {
	writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`, { flag: "wx" });
}

function csvValue(value) {
	const text = value === null || value === undefined ? "" : String(value);
	return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

function writeManifest(runDirectory, rows) {
	const lines = [MANIFEST_COLUMNS.join(",")];
	for (const row of rows) lines.push(MANIFEST_COLUMNS.map((column) => csvValue(row[column])).join(","));
	writeFileSync(join(runDirectory, "manifest.csv"), `${lines.join("\n")}\n`);
}

function writeArtifactManifest(runDirectory, rows) {
	const lines = [ARTIFACT_MANIFEST_COLUMNS.join(",")];
	for (const row of rows) lines.push(ARTIFACT_MANIFEST_COLUMNS.map((column) => csvValue(row[column])).join(","));
	writeFileSync(join(runDirectory, "artifact_manifest.csv"), `${lines.join("\n")}\n`);
}

function collectInputs(inputPath) {
	const files = [];
	const skipped = [];
	const rootStat = lstatSync(inputPath);
	if (rootStat.isSymbolicLink()) return { files, skipped: [{ path: inputPath, reason: "symlink input is not followed" }] };
	if (rootStat.isFile()) {
		if (SUPPORTED_EXTENSIONS.has(extname(inputPath).toLowerCase())) files.push(inputPath);
		else skipped.push({ path: inputPath, reason: "unsupported file format" });
		return { files, skipped };
	}
	if (!rootStat.isDirectory()) return { files, skipped: [{ path: inputPath, reason: "input is not a regular file or directory" }] };
	const visit = (directory) => {
		for (const entry of readdirSync(directory, { withFileTypes: true }).sort((left, right) => left.name.localeCompare(right.name))) {
			const entryPath = join(directory, entry.name);
			if (entry.name.startsWith(".")) {
				skipped.push({ path: entryPath, reason: "hidden path" });
				continue;
			}
			if (directory === inputPath && entry.isDirectory() && RESERVED_WORKSPACE_DIRECTORIES.has(entry.name)) {
				continue;
			}
			if (entry.isSymbolicLink()) {
				skipped.push({ path: entryPath, reason: "symlink is not followed" });
				continue;
			}
			if (entry.isDirectory()) visit(entryPath);
			else if (entry.isFile() && SUPPORTED_EXTENSIONS.has(extname(entry.name).toLowerCase())) files.push(entryPath);
			else if (entry.isFile()) skipped.push({ path: entryPath, reason: "unsupported file format" });
		}
	};
	visit(inputPath);
	return { files, skipped };
}

function extractionReport(source, extraction, status) {
	const ocr = extraction.ocr;
	const vision = extraction.vision;
	const warnings = extraction.warnings.length > 0 ? extraction.warnings.map((warning) => `- ${warning}`).join("\n") : "- None.";
	return `# Extraction Report

## Status

${status}

## Source

- Path: \`${source.path}\`
- SHA-256: \`${source.sha256}\`
- Format: ${source.format}
- Size: ${source.sizeBytes} bytes

## Methods and Tools

- Extraction method: ${extraction.method}
${Object.entries(extraction.toolVersions).map(([name, version]) => `- ${name}: ${version}`).join("\n")}

## Coverage and OCR

- Page count: ${extraction.pageCount ?? "Not available"}
- OCR mode: ${ocr.mode}
- OCR attempted: ${ocr.attempted}
- OCR used: ${ocr.used}
- OCR candidate pages: ${ocr.candidatePages.join(", ") || "None"}
- OCR selected pages: ${ocr.selectedPages?.join(", ") || "None"}
- OCR command mode: ${ocr.commandMode ?? "Not run"}
- Remaining suspicious pages: ${ocr.unresolvedPages?.join(", ") || "None"}
- Derived PDF retained: ${ocr.derivedPath ?? "No"}
- Vision fallback required: ${vision?.required ?? false}
- Vision candidate pages: ${vision?.candidatePages?.join(", ") || "None"}
- Vision rendered pages: ${vision?.renderedPages?.map(({ page }) => page).join(", ") || "None"}
- Vision used: ${vision?.used ?? false}

## Structure and Encoding

Deterministic extraction is complete. Model normalization and structural review are pending.

## Warnings

${warnings}

## Review

Review every prepared document or chunk against \`working/extracted.md\`. Do not summarize or omit source content.
`;
}

async function prepareDocument(filePath, runDirectory, options, tools, usedDirectories) {
	const sourceBuffer = readFileSync(filePath);
	const sourceHash = sha256(sourceBuffer);
	const directoryName = `${safeStem(filePath)}-${sourceHash.slice(0, 12)}`;
	if (usedDirectories.has(directoryName)) {
		return {
			row: {
				document_id: `sha256:${sourceHash}`,
				source_path: filePath,
				source_sha256: sourceHash,
				source_format: sourceFormat(filePath),
				status: "skipped",
				output_directory: directoryName,
				title: "",
				author: "",
				document_date: "",
				page_count: "",
				extraction_method: "",
				ocr_used: false,
				warning_count: 0,
				error: "duplicate document identity in this run",
			},
		};
	}
	usedDirectories.add(directoryName);
	const documentDirectory = join(runDirectory, directoryName);
	mkdirSync(documentDirectory);
	const sourceStat = statSync(filePath);
	const format = sourceFormat(filePath);
	let extracted;
	if (format === "pdf") extracted = await extractPdf(filePath, documentDirectory, options, tools);
	else if (IMAGE_EXTENSIONS.has(extname(filePath).toLowerCase())) {
		if (options.ocr === "never") throw new Error("image ingestion requires OCR, but OCR was disabled");
		if (options.ocrBackend === "local") throw new Error("image ingestion requires --ocr-backend glmocr or auto");
		extracted = await extractGlmocr(filePath, documentDirectory, options, 1);
	}
	else if (AUDIO_VIDEO_EXTENSIONS.has(extname(filePath).toLowerCase())) extracted = extractMedia(filePath, documentDirectory, tools);
	else if (["docx", "html", "rtf", "md"].includes(format)) extracted = extractPandoc(filePath, documentDirectory, format, tools);
	else extracted = extractText(filePath);
	const chunks = splitIntoChunks(extracted.markdown, options.chunkCharacters);
	const workingDirectory = join(documentDirectory, "working");
	const chunksDirectory = join(workingDirectory, "chunks");
	mkdirSync(chunksDirectory, { recursive: true });
	writeFileSync(join(workingDirectory, "extracted.md"), extracted.markdown, { flag: "wx" });
	for (const [index, chunk] of chunks.entries()) {
		writeFileSync(join(chunksDirectory, `chunk-${String(index + 1).padStart(4, "0")}.md`), chunk, { flag: "wx" });
	}
	writeFileSync(join(documentDirectory, "document.md"), extracted.markdown, { flag: "wx" });
	const source = {
		path: filePath,
		basename: basename(filePath),
		extension: extname(filePath).toLowerCase(),
		format,
		sizeBytes: sourceStat.size,
		modifiedAt: sourceStat.mtime.toISOString(),
		sha256: sourceHash,
	};
	const toolVersions = Object.fromEntries(Object.entries(tools).filter(([, value]) => value.available).map(([name, value]) => [name, value.version]));
	const warnings = [...new Set(extracted.warnings)];
	if (chunks.some((chunk) => unicodeLength(chunk) > options.chunkCharacters)) {
		warnings.push("A single paragraph exceeds the chunk threshold and was preserved without splitting.");
	}
	const extraction = {
		status: "needs_review",
		method: extracted.method,
		toolVersions,
		warnings,
		pageCount: extracted.pageCount,
		chunkCharacters: options.chunkCharacters,
		chunks: chunks.map((chunk, index) => ({ path: `working/chunks/chunk-${String(index + 1).padStart(4, "0")}.md`, characters: unicodeLength(chunk) })),
		ocr: extracted.ocr,
		vision: extracted.vision ?? {
			mode: "not-applicable",
			required: false,
			candidatePages: [],
			renderedPages: [],
			completedPages: [],
			used: false,
			unavailableReason: null,
		},
	};
	const metadata = {
		schemaVersion: 1,
		documentId: `sha256:${sourceHash}`,
		source,
		extraction,
		fields: {
			title: evidence(extracted.embedded.title, "embedded-metadata", "high", extracted.embedded.title ? "embedded metadata: title" : null),
			author: evidence(extracted.embedded.author, "embedded-metadata", "high", extracted.embedded.author ? "embedded metadata: author" : null),
			date: evidence(extracted.embedded.date, "embedded-metadata", "medium", extracted.embedded.date ? "embedded metadata: date" : null),
			source: evidence(extracted.embedded.source, "embedded-metadata", "medium", extracted.embedded.source ? "embedded metadata: source" : null),
		},
		structure: markdownStructure(extracted.markdown),
		review: { completed: false, notes: [] },
		finalOutput: { filename: null, namingReason: null },
	};
	writeJson(join(documentDirectory, "metadata.json"), metadata);
	writeJson(join(documentDirectory, "source_map.json"), {
		schemaVersion: 1,
		documentId: metadata.documentId,
		markdownFile: "document.md",
		entries: extracted.sourceMapEntries,
	});
	writeFileSync(join(documentDirectory, "extraction_report.md"), extractionReport(source, extraction, "needs_review — model normalization pending"), { flag: "wx" });
	return {
		row: {
			document_id: metadata.documentId,
			source_path: filePath,
			source_sha256: sourceHash,
			source_format: format,
			status: "needs_review",
			suggested_pipeline: "", // To be populated later
			output_directory: directoryName,
			title: metadata.fields.title.value ?? "",
			author: metadata.fields.author.value ?? "",
			document_date: metadata.fields.date.value ?? "",
			page_count: extracted.pageCount ?? "",
			extraction_method: extracted.method,
			ocr_used: extracted.ocr.used,
			warning_count: warnings.length,
			error: "",
		},
	};
}

function parsePrepareArguments(args) {
	if (args.length === 0) fail("prepare requires an input path");
	const input = resolve(args[0]);
	let output = null;
	let ocr = "auto";
	let ocrBackend = process.env.FORGE_OCR_BACKEND || "auto";
	let glmocrUrl = process.env.FORGE_GLMOCR_URL || process.env.FORGE_OCR_URL || process.env.OCR_URL || DEFAULT_GLMOCR_URL;
	let glmocrTimeoutMs = Number.parseInt(process.env.FORGE_GLMOCR_TIMEOUT_MS || process.env.FORGE_OCR_TIMEOUT_MS || String(DEFAULT_GLMOCR_TIMEOUT_MS), 10);
	let glmocrLayoutVisualization = false;
	let chunkCharacters = DEFAULT_CHUNK_CHARACTERS;
	for (let index = 1; index < args.length; index += 1) {
		const argument = args[index];
		if (argument === "--output") output = resolve(args[++index] ?? fail("--output requires a path"));
		else if (argument === "--ocr") ocr = args[++index] ?? fail("--ocr requires auto, force, or never");
		else if (argument === "--ocr-backend") ocrBackend = args[++index] ?? fail("--ocr-backend requires local, glmocr, or auto");
		else if (argument === "--glmocr-url") glmocrUrl = args[++index] ?? fail("--glmocr-url requires a URL");
		else if (argument === "--glmocr-timeout-ms") glmocrTimeoutMs = Number.parseInt(args[++index] ?? "", 10);
		else if (argument === "--glmocr-layout-visualization") glmocrLayoutVisualization = true;
		else if (argument === "--chunk-chars") chunkCharacters = Number.parseInt(args[++index] ?? "", 10);
		else fail(`unknown prepare option: ${argument}`);
	}
	if (!output) fail("prepare requires --output <new-directory>");
	if (!new Set(["auto", "force", "never"]).has(ocr)) fail("--ocr must be auto, force, or never");
	if (!new Set(["local", "glmocr", "auto"]).has(ocrBackend)) fail("--ocr-backend must be local, glmocr, or auto");
	if (ocrBackend !== "local") {
		try {
			new URL(glmocrUrl);
		} catch {
			fail("--glmocr-url must be a valid URL");
		}
	}
	if (!Number.isInteger(glmocrTimeoutMs) || glmocrTimeoutMs < 1) fail("--glmocr-timeout-ms must be a positive integer");
	if (!Number.isInteger(chunkCharacters) || chunkCharacters < 1) fail("--chunk-chars must be a positive integer");
	return { input, output, ocr, ocrBackend, glmocrUrl, glmocrTimeoutMs, glmocrLayoutVisualization, chunkCharacters };
}

async function prepare(args) {
	const options = parsePrepareArguments(args);
	if (!existsSync(options.input)) fail(`input does not exist: ${options.input}`);
	if (existsSync(options.output)) fail(`output directory already exists: ${options.output}`);
	mkdirSync(dirname(options.output), { recursive: true });
	mkdirSync(options.output);
	const tools = inspectTools();
	const inputs = collectInputs(options.input);
	const folderCategory = await categorizeFolder(inputs.files);
	const rows = [];
	const usedDirectories = new Set();
	for (const skipped of inputs.skipped) {
		rows.push({
			document_id: "",
			source_path: skipped.path,
			source_sha256: "",
			source_format: sourceFormat(skipped.path),
			status: "skipped",
			suggested_pipeline: "",
			output_directory: "",
			title: "",
			author: "",
			document_date: "",
			page_count: "",
			extraction_method: "",
			ocr_used: false,
			warning_count: 0,
			error: skipped.reason,
		});
	}
	for (const filePath of inputs.files.sort()) {
		try {
			rows.push((await prepareDocument(filePath, options.output, options, tools, usedDirectories)).row);
		} catch (error) {
			let sourceHash = "";
			try {
				sourceHash = sha256(readFileSync(filePath));
				const partialDirectory = join(options.output, `${safeStem(filePath)}-${sourceHash.slice(0, 12)}`);
				rmSync(partialDirectory, { recursive: true, force: true });
			} catch {
				// Preserve the original extraction error when cleanup or hashing also fails.
			}
			rows.push({
				document_id: sourceHash ? `sha256:${sourceHash}` : "",
				source_path: filePath,
				source_sha256: sourceHash,
				source_format: sourceFormat(filePath),
				status: "failed",
				suggested_pipeline: "",
				output_directory: "",
				title: "",
				author: "",
				document_date: "",
				page_count: "",
				extraction_method: "",
				ocr_used: false,
				warning_count: 0,
				error: error instanceof Error ? error.message : String(error),
			});
		}
	}

	// Set suggested pipelines based on format and category
	for (const row of rows) {
		if (row.status !== "needs_review") continue;
		const format = row.source_format;
		if (["mp4", "mov", "mkv", "webm", "avi", "mp3", "wav", "m4a", "flac", "ogg", "opus"].includes(format)) {
			row.suggested_pipeline = "transcription,transcript-cleanup";
		} else {
			row.suggested_pipeline = folderCategory === "general" ? "basic-markdown" : folderCategory;
		}
	}

	rows.sort((left, right) => left.source_path.localeCompare(right.source_path));
	writeManifest(options.output, rows);
	const counts = Object.fromEntries(["success", "needs_review", "failed", "skipped"].map((status) => [status, rows.filter((row) => row.status === status).length]));
	process.stdout.write(`${JSON.stringify({ runDirectory: options.output, documents: rows.length, counts }, null, 2)}\n`);
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

function tokenCounts(value) {
	const counts = new Map();
	for (const token of value.toLocaleLowerCase().match(/[\p{L}\p{N}]+/gu) ?? []) counts.set(token, (counts.get(token) ?? 0) + 1);
	return counts;
}

function contentCoverage(source, normalized) {
	const sourceCounts = tokenCounts(source);
	const normalizedCounts = tokenCounts(normalized);
	let total = 0;
	let preserved = 0;
	for (const [token, count] of sourceCounts) {
		total += count;
		preserved += Math.min(count, normalizedCounts.get(token) ?? 0);
	}
	return total === 0 ? 1 : preserved / total;
}

function validateEvidence(value, label, errors) {
	const keys = value && typeof value === "object" ? Object.keys(value).sort() : [];
	if (keys.join(",") !== "confidence,locator,origin,value") errors.push(`${label} must contain exactly value, origin, confidence, and locator`);
	if (value?.value === null) {
		if (value.origin !== null || value.confidence !== null || value.locator !== null) errors.push(`${label} must use all null evidence fields when value is null`);
		return;
	}
	if (typeof value?.value !== "string" || value.value.trim() === "") errors.push(`${label}.value must be a non-empty string or null`);
	if (!["embedded-metadata", "document-text", "filename", "user-provided"].includes(value?.origin)) errors.push(`${label}.origin is invalid`);
	if (!["high", "medium", "low"].includes(value?.confidence)) errors.push(`${label}.confidence is invalid`);
	if (typeof value?.locator !== "string" || value.locator.trim() === "") errors.push(`${label}.locator must be a non-empty string`);
}

function validateFinalOutput(value, label, errors) {
	if (value === undefined) return;
	if (!value || typeof value !== "object" || Array.isArray(value)) {
		errors.push(`${label} must be an object when present`);
		return;
	}
	const filename = value.filename;
	if (filename !== null && filename !== undefined && !isSafeMarkdownFilename(filename)) {
		errors.push(`${label}.filename must be a safe Markdown filename with no path separators`);
	}
	const namingReason = value.namingReason;
	if (namingReason !== null && namingReason !== undefined && typeof namingReason !== "string") {
		errors.push(`${label}.namingReason must be a string or null`);
	}
}

function validateRun(runDirectory, { emit = true, exitOnError = true } = {}) {
	const errors = [];
	const warnings = [];
	const manifestPath = join(runDirectory, "manifest.csv");
	if (!existsSync(manifestPath)) fail(`manifest.csv does not exist in ${runDirectory}`);
	const parsed = parseCsv(readFileSync(manifestPath, "utf8"));
	const headers = parsed.shift() ?? [];
	if (headers.join(",") !== MANIFEST_COLUMNS.join(",")) errors.push("manifest.csv columns do not match the required contract");
	for (const values of parsed.filter((row) => row.some((field) => field !== ""))) {
		if (values.length !== MANIFEST_COLUMNS.length) errors.push(`manifest row has ${values.length} columns instead of ${MANIFEST_COLUMNS.length}`);
		const row = Object.fromEntries(MANIFEST_COLUMNS.map((column, index) => [column, values[index] ?? ""]));
		if (!["success", "needs_review", "failed", "skipped"].includes(row.status)) {
			errors.push(`invalid manifest status for ${row.source_path}: ${row.status}`);
			continue;
		}
		if (["failed", "skipped"].includes(row.status)) continue;
		const documentDirectory = resolve(runDirectory, row.output_directory);
		if (!documentDirectory.startsWith(`${resolve(runDirectory)}${sep}`)) {
			errors.push(`output directory escapes the run directory: ${row.output_directory}`);
			continue;
		}
		for (const required of ["document.md", "metadata.json", "extraction_report.md", "source_map.json", "working/extracted.md"]) {
			if (!existsSync(join(documentDirectory, required))) errors.push(`${row.output_directory}/${required} is missing`);
		}
		if (!existsSync(join(documentDirectory, "metadata.json")) || !existsSync(join(documentDirectory, "document.md"))) continue;
		let metadata;
		let sourceMap;
		try {
			metadata = JSON.parse(readFileSync(join(documentDirectory, "metadata.json"), "utf8"));
			sourceMap = JSON.parse(readFileSync(join(documentDirectory, "source_map.json"), "utf8"));
		} catch (error) {
			errors.push(`${row.output_directory} contains invalid JSON: ${error instanceof Error ? error.message : String(error)}`);
			continue;
		}
		if (metadata.schemaVersion !== 1) errors.push(`${row.output_directory}/metadata.json has an unsupported schemaVersion`);
		if (metadata.documentId !== row.document_id) errors.push(`${row.output_directory} documentId does not match manifest.csv`);
		if (metadata.source?.sha256 !== row.source_sha256) errors.push(`${row.output_directory} source hash does not match manifest.csv`);
		if (metadata.extraction?.status !== row.status) errors.push(`${row.output_directory} extraction status does not match manifest.csv`);
		for (const field of ["title", "author", "date", "source"]) validateEvidence(metadata.fields?.[field], `${row.output_directory}.fields.${field}`, errors);
		validateFinalOutput(metadata.finalOutput, `${row.output_directory}.finalOutput`, errors);
		if (metadata.review?.completed !== true) errors.push(`${row.output_directory} model review is not marked complete`);
		if (sourceMap.schemaVersion !== 1 || sourceMap.documentId !== row.document_id || sourceMap.markdownFile !== "document.md" || !Array.isArray(sourceMap.entries)) {
			errors.push(`${row.output_directory}/source_map.json is invalid`);
		}
		const document = readFileSync(join(documentDirectory, "document.md"), "utf8");
		const extractedPath = join(documentDirectory, "working/extracted.md");
		if (existsSync(extractedPath) && metadata.extraction?.vision?.used !== true) {
			const coverage = contentCoverage(readFileSync(extractedPath, "utf8"), document);
			if (coverage < 0.98) errors.push(`${row.output_directory} preserves only ${(coverage * 100).toFixed(2)}% of extracted word tokens`);
			else if (coverage < 0.995) warnings.push(`${row.output_directory} content coverage is ${(coverage * 100).toFixed(2)}%`);
		}
		const lineCount = document.split("\n").length;
		for (const [index, entry] of (sourceMap.entries ?? []).entries()) {
			if (entry.markdownStartLine === null && entry.markdownEndLine === null) continue;
			if (!Number.isInteger(entry.markdownStartLine) || !Number.isInteger(entry.markdownEndLine) || entry.markdownStartLine < 1 || entry.markdownEndLine < entry.markdownStartLine || entry.markdownEndLine > lineCount) {
				errors.push(`${row.output_directory}/source_map.json entry ${index + 1} has invalid line ranges`);
			}
			if (!entry.sourceLocator || typeof entry.sourceLocator !== "object") errors.push(`${row.output_directory}/source_map.json entry ${index + 1} lacks a source locator`);
			if (!["page-extraction", "document-conversion", "model-alignment", "vision-transcription"].includes(entry.method)) errors.push(`${row.output_directory}/source_map.json entry ${index + 1} has an invalid method`);
			if (!["high", "medium", "low"].includes(entry.confidence)) errors.push(`${row.output_directory}/source_map.json entry ${index + 1} has an invalid confidence`);
		}
		const chunkMetadata = metadata.extraction?.chunks;
		if (!Array.isArray(chunkMetadata) || chunkMetadata.length < 1) errors.push(`${row.output_directory} has no extraction chunk metadata`);
		if (Array.isArray(chunkMetadata) && existsSync(extractedPath)) {
			let deterministicChunks = "";
			for (const [index, chunk] of chunkMetadata.entries()) {
				const chunkPath = resolve(documentDirectory, chunk.path ?? "");
				if (!chunkPath.startsWith(`${resolve(documentDirectory, "working", "chunks")}${sep}`) || !existsSync(chunkPath)) {
					errors.push(`${row.output_directory} extraction chunk ${index + 1} is missing or outside working/chunks`);
					continue;
				}
				const chunkText = readFileSync(chunkPath, "utf8");
				if (unicodeLength(chunkText) !== chunk.characters) errors.push(`${row.output_directory} extraction chunk ${index + 1} character count is incorrect`);
				deterministicChunks += chunkText;
			}
			if (deterministicChunks !== readFileSync(extractedPath, "utf8")) errors.push(`${row.output_directory} deterministic chunks do not reconstruct working/extracted.md`);
		}
		if (Array.isArray(chunkMetadata) && chunkMetadata.length > 1) {
			const reviewedDirectory = join(documentDirectory, "working", "reviewed-chunks");
			if (!existsSync(reviewedDirectory)) errors.push(`${row.output_directory}/working/reviewed-chunks is missing`);
			else {
				const reviewedFiles = readdirSync(reviewedDirectory).filter((name) => /^chunk-\d{4}\.md$/.test(name)).sort();
				if (reviewedFiles.length !== chunkMetadata.length) errors.push(`${row.output_directory} reviewed chunk count does not match extraction metadata`);
				else {
					const assembled = reviewedFiles.map((name) => readFileSync(join(reviewedDirectory, name), "utf8")).join("");
					if (assembled !== document) errors.push(`${row.output_directory} document.md is not the exact concatenation of reviewed chunks`);
				}
			}
		}
		if (metadata.extraction?.ocr?.used) {
			if (metadata.extraction.ocr.backend === "glmocr") {
				const responsePath = join(documentDirectory, metadata.extraction.ocr.derivedPath ?? "");
				if (!responsePath.startsWith(`${resolve(documentDirectory, "derived")}${sep}`) || !existsSync(responsePath)) errors.push(`${row.output_directory} GLM-OCR response artifact is missing`);
				else if (sha256(readFileSync(responsePath)) !== metadata.extraction.ocr.derivedSha256) errors.push(`${row.output_directory} GLM-OCR response artifact hash does not match metadata`);
				if (metadata.extraction.ocr.layoutPath) {
					const layoutPath = join(documentDirectory, metadata.extraction.ocr.layoutPath);
					if (!layoutPath.startsWith(`${resolve(documentDirectory, "derived")}${sep}`) || !existsSync(layoutPath)) errors.push(`${row.output_directory} GLM-OCR layout artifact is missing`);
					else if (sha256(readFileSync(layoutPath)) !== metadata.extraction.ocr.layoutSha256) errors.push(`${row.output_directory} GLM-OCR layout artifact hash does not match metadata`);
				}
			} else {
				const ocrPath = join(documentDirectory, "derived", "ocr.pdf");
				if (!existsSync(ocrPath)) errors.push(`${row.output_directory}/derived/ocr.pdf is missing`);
				else if (sha256(readFileSync(ocrPath)) !== metadata.extraction.ocr.derivedSha256) errors.push(`${row.output_directory}/derived/ocr.pdf hash does not match metadata`);
			}
		}
		const vision = metadata.extraction?.vision;
		if (vision?.required) {
			for (const rendered of vision.renderedPages ?? []) {
				const imagePath = resolve(documentDirectory, rendered.path ?? "");
				if (!imagePath.startsWith(`${resolve(documentDirectory, "derived", "vision-pages")}${sep}`) || !existsSync(imagePath)) {
					errors.push(`${row.output_directory} vision image for page ${rendered.page} is missing or outside derived/vision-pages`);
				} else if (sha256(readFileSync(imagePath)) !== rendered.sha256) {
					errors.push(`${row.output_directory} vision image for page ${rendered.page} has changed`);
				}
			}
			if (vision.used) {
				const expectedPages = [...(vision.candidatePages ?? [])].sort((left, right) => left - right);
				const completedPages = [...(vision.completedPages ?? [])].sort((left, right) => left - right);
				if (expectedPages.join(",") !== completedPages.join(",")) {
					errors.push(`${row.output_directory} vision fallback does not cover every candidate page`);
				}
				for (const page of completedPages) {
					const transcriptPath = join(documentDirectory, "working", "vision-pages", `page-${String(page).padStart(4, "0")}.md`);
					if (!existsSync(transcriptPath) || readFileSync(transcriptPath, "utf8").trim() === "") {
						errors.push(`${row.output_directory} vision transcript for page ${page} is missing or empty`);
					} else if (!document.includes(readFileSync(transcriptPath, "utf8").trim())) {
						errors.push(`${row.output_directory} document.md does not contain the vision transcript for page ${page}`);
					}
					if (!(sourceMap.entries ?? []).some((entry) => entry.method === "vision-transcription" && entry.sourceLocator?.page === page)) {
						errors.push(`${row.output_directory}/source_map.json lacks a vision mapping for page ${page}`);
					}
				}
			} else if (metadata.review?.completed && !vision.unavailableReason) {
				errors.push(`${row.output_directory} completed review without using required vision fallback or recording why it was unavailable`);
			}
		}
		if (String(metadata.extraction?.ocr?.used ?? false) !== row.ocr_used) errors.push(`${row.output_directory} OCR state does not match manifest.csv`);
		if (String(metadata.extraction?.pageCount ?? "") !== row.page_count) errors.push(`${row.output_directory} page count does not match manifest.csv`);
		if ((metadata.extraction?.method ?? "") !== row.extraction_method) errors.push(`${row.output_directory} extraction method does not match manifest.csv`);
		if (String(metadata.extraction?.warnings?.length ?? 0) !== row.warning_count) errors.push(`${row.output_directory} warning count does not match manifest.csv`);
		const reportPath = join(documentDirectory, "extraction_report.md");
		if (existsSync(reportPath)) {
			const report = readFileSync(reportPath, "utf8");
			for (const heading of ["## Status", "## Source", "## Methods and Tools", "## Coverage and OCR", "## Structure and Encoding", "## Warnings", "## Review"]) {
				if (!report.includes(heading)) errors.push(`${row.output_directory}/extraction_report.md is missing ${heading}`);
			}
			if (metadata.review?.completed && report.includes("model normalization pending")) errors.push(`${row.output_directory}/extraction_report.md still reports pending model normalization`);
		}
		if ((metadata.fields.title.value ?? "") !== row.title) errors.push(`${row.output_directory} title does not match manifest.csv`);
		if ((metadata.fields.author.value ?? "") !== row.author) errors.push(`${row.output_directory} author does not match manifest.csv`);
		if ((metadata.fields.date.value ?? "") !== row.document_date) errors.push(`${row.output_directory} date does not match manifest.csv`);
	}
	const result = { valid: errors.length === 0, errors, warnings };
	if (emit) process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
	if (exitOnError && errors.length > 0) process.exit(1);
	return result;
}

function parseFinalizeArguments(args) {
	if (args.length === 0) fail("finalize requires a run directory");
	const runDirectory = resolve(args[0]);
	let destination = null;
	let layout = "auto";
	for (let index = 1; index < args.length; index += 1) {
		const argument = args[index];
		if (argument === "--destination") destination = resolve(args[++index] ?? fail("--destination requires a folder"));
		else if (argument === "--layout") layout = args[++index] ?? fail("--layout requires auto, flat, or structured");
		else fail(`unknown finalize option: ${argument}`);
	}
	if (!destination) fail("finalize requires --destination <source-folder>");
	if (!new Set(["auto", "flat", "structured"]).has(layout)) fail("--layout must be auto, flat, or structured");
	return { runDirectory, destination, layout };
}

function pathInside(parent, child) {
	const relativePath = relative(parent, child);
	return relativePath === "" || (!relativePath.startsWith("..") && !relativePath.startsWith("/") && relativePath !== "..");
}

function relativeSourcePath(destination, sourcePath) {
	if (!pathInside(destination, sourcePath)) fail(`source path is outside destination folder: ${sourcePath}`);
	return relative(destination, sourcePath);
}

function detectWorkspaceLayout(destination) {
	const entries = readdirSync(destination, { withFileTypes: true }).filter((entry) => !entry.name.startsWith(".") && !RESERVED_WORKSPACE_DIRECTORIES.has(entry.name));
	const sourceDirectories = entries.filter((entry) => entry.isDirectory());
	if (sourceDirectories.length === 0) return "flat";
	const directoriesWithSources = sourceDirectories.filter((entry) => collectInputs(join(destination, entry.name)).files.length > 0);
	return directoriesWithSources.length >= 2 ? "structured" : "flat";
}

function inferredMarkdownFilename(row, metadata, sourcePath) {
	const explicit = metadata?.finalOutput?.filename;
	if (explicit) return explicit;
	const title = metadata?.fields?.title?.value || row.title || "";
	const date = metadata?.fields?.date?.value || row.document_date || "";
	const pipeline = row.suggested_pipeline || "";
	const sourceStem = basename(sourcePath, extname(sourcePath));
	let stem = title || sourceStem || "document";
	if (pipeline.includes("transcription") && !/\btranscript\b/i.test(stem)) stem = `${stem} transcript`;
	if (pipeline.includes("personal-admin") && /^\d{4}-\d{2}-\d{2}/.test(date) && !stem.startsWith(date)) stem = `${date} ${stem}`;
	return safeMarkdownFilename(stem);
}

function markdownOutputRelativePath(destination, sourcePath, layout, row, metadata) {
	const sourceRelative = relativeSourcePath(destination, sourcePath);
	const outputName = inferredMarkdownFilename(row, metadata, sourcePath);
	if (layout === "flat") return outputName;
	return join(dirname(sourceRelative), outputName);
}

function collectGeneratedArtifacts(runDirectory) {
	const artifacts = [];
	const visit = (directory) => {
		for (const entry of readdirSync(directory, { withFileTypes: true }).sort((left, right) => left.name.localeCompare(right.name))) {
			const entryPath = join(directory, entry.name);
			if (entry.isDirectory()) visit(entryPath);
			else if (entry.isFile() && GENERATED_ARTIFACT_NAMES.has(entry.name)) artifacts.push(entryPath);
		}
	};
	visit(runDirectory);
	return artifacts;
}

function readManifestRows(runDirectory) {
	const parsed = parseCsv(readFileSync(join(runDirectory, "manifest.csv"), "utf8"));
	const headers = parsed.shift() ?? [];
	if (headers.join(",") !== MANIFEST_COLUMNS.join(",")) fail("manifest.csv columns do not match the required contract");
	return parsed
		.filter((row) => row.some((field) => field !== ""))
		.map((values) => Object.fromEntries(MANIFEST_COLUMNS.map((column, index) => [column, values[index] ?? ""])));
}

function requireNoDuplicateDestinations(operations) {
	const byDestination = new Map();
	for (const operation of operations) {
		const existing = byDestination.get(operation.to);
		if (existing) fail(`multiple finalize operations target the same path: ${operation.to}`);
		byDestination.set(operation.to, operation);
	}
}

function commandFinalize(args) {
	const options = parseFinalizeArguments(args);
	if (!existsSync(options.runDirectory) || !lstatSync(options.runDirectory).isDirectory()) fail(`run directory does not exist: ${options.runDirectory}`);
	if (!existsSync(options.destination) || !lstatSync(options.destination).isDirectory()) fail(`destination folder does not exist: ${options.destination}`);
	const expectedRunDirectory = join(options.destination, "Ingest");
	if (resolve(options.runDirectory) !== resolve(expectedRunDirectory)) {
		fail(`finalize expects the run directory to be the destination Ingest folder: ${expectedRunDirectory}`);
	}
	const validation = validateRun(options.runDirectory, { emit: false, exitOnError: false });
	if (!validation.valid) fail(`run must validate before finalize: ${validation.errors.join("; ")}`);
	const layout = options.layout === "auto" ? detectWorkspaceLayout(options.destination) : options.layout;
	const rows = readManifestRows(options.runDirectory);
	const completedRows = rows.filter((row) => row.status === "success");
	const moveOperations = [];
	const publishOperations = [];
	const generatedOperations = [];
	const movableSourcePaths = new Set();
	for (const row of completedRows) {
		const sourcePath = resolve(row.source_path);
		if (!existsSync(sourcePath) || !lstatSync(sourcePath).isFile()) fail(`source file is missing before finalize: ${sourcePath}`);
		const sourceRelative = relativeSourcePath(options.destination, sourcePath);
		const originalDestination = join(options.destination, "Originals", sourceRelative);
		const documentPath = join(options.runDirectory, row.output_directory, "document.md");
		const metadataPath = join(options.runDirectory, row.output_directory, "metadata.json");
		if (!existsSync(documentPath)) fail(`final document is missing: ${documentPath}`);
		const metadata = JSON.parse(readFileSync(metadataPath, "utf8"));
		const markdownDestination = join(options.destination, markdownOutputRelativePath(options.destination, sourcePath, layout, row, metadata));
		moveOperations.push({ role: "original", documentId: row.document_id, from: sourcePath, to: originalDestination });
		publishOperations.push({ role: "final_markdown", documentId: row.document_id, from: documentPath, to: markdownDestination });
		movableSourcePaths.add(sourcePath);
	}
	for (const artifactPath of collectGeneratedArtifacts(options.runDirectory)) {
		const generatedRelative = relative(options.runDirectory, artifactPath);
		generatedOperations.push({
			role: "generated_artifact",
			documentId: "",
			from: artifactPath,
			to: join(options.destination, "Generated", generatedRelative),
		});
	}
	requireNoDuplicateDestinations([...moveOperations, ...publishOperations, ...generatedOperations]);
	for (const operation of [...moveOperations, ...publishOperations, ...generatedOperations]) {
		if (existsSync(operation.to) && !movableSourcePaths.has(operation.to)) fail(`finalize destination already exists: ${operation.to}`);
	}
	const artifactRows = [];
	const createdAt = new Date().toISOString();
	for (const operation of moveOperations) {
		mkdirSync(dirname(operation.to), { recursive: true });
		renameSync(operation.from, operation.to);
		artifactRows.push({
			role: operation.role,
			document_id: operation.documentId,
			source_path: operation.from,
			destination_path: relative(options.destination, operation.to),
			sha256: sha256(readFileSync(operation.to)),
			created_at: createdAt,
		});
	}
	for (const operation of [...publishOperations, ...generatedOperations]) {
		mkdirSync(dirname(operation.to), { recursive: true });
		copyFileSync(operation.from, operation.to);
		artifactRows.push({
			role: operation.role,
			document_id: operation.documentId,
			source_path: operation.from,
			destination_path: relative(options.destination, operation.to),
			sha256: sha256(readFileSync(operation.to)),
			created_at: createdAt,
		});
	}
	writeArtifactManifest(options.runDirectory, artifactRows);
	process.stdout.write(
		`${JSON.stringify(
			{
				finalized: true,
				layout,
				movedOriginals: moveOperations.length,
				publishedMarkdown: publishOperations.length,
				generatedArtifacts: generatedOperations.length,
				artifactManifest: join(options.runDirectory, "artifact_manifest.csv"),
			},
			null,
			2,
		)}\n`,
	);
}

function usage() {
	process.stdout.write(`Usage:
  document-ingest.mjs doctor [--json]
  document-ingest.mjs prepare <input> --output <new-directory> [--ocr auto|force|never] [--ocr-backend local|glmocr|auto] [--glmocr-url URL] [--chunk-chars N]
  document-ingest.mjs validate <run-directory>
  document-ingest.mjs finalize <run-directory> --destination <source-folder> [--layout auto|flat|structured]
`);
}

const [command, ...args] = process.argv.slice(2);
if (!command || command === "--help" || command === "-h") {
	usage();
	process.exit(command ? 0 : 2);
}
if (command === "doctor") {
	if (args.some((argument) => argument !== "--json")) fail("doctor accepts only --json");
	printDoctor(args.includes("--json"));
} else if (command === "prepare") await prepare(args);
else if (command === "validate") {
	if (args.length !== 1) fail("validate requires exactly one run directory");
	const runDirectory = resolve(args[0]);
	if (!existsSync(runDirectory) || !lstatSync(runDirectory).isDirectory()) fail(`run directory does not exist: ${runDirectory}`);
	validateRun(runDirectory);
} else if (command === "finalize") commandFinalize(args);
else fail(`unknown command: ${command}`, 2);

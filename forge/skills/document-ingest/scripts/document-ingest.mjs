#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
	existsSync,
	lstatSync,
	mkdirSync,
	readdirSync,
	readFileSync,
	rmSync,
	statSync,
	writeFileSync,
} from "node:fs";
import { basename, dirname, extname, join, resolve, sep } from "node:path";

const DEFAULT_CHUNK_CHARACTERS = 150_000;
const LOW_TEXT_CHARACTERS = 40;
const SUPPORTED_EXTENSIONS = new Set([".pdf", ".docx", ".txt", ".md", ".markdown", ".html", ".htm", ".rtf"]);
const MANIFEST_COLUMNS = [
	"document_id",
	"source_path",
	"source_sha256",
	"source_format",
	"status",
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
	return {
		pandoc: toolInfo("pandoc"),
		pdftotext: toolInfo("pdftotext", ["-v"]),
		pdfinfo: toolInfo("pdfinfo", ["-v"]),
		pdfimages: toolInfo("pdfimages", ["-v"]),
		ocrmypdf: toolInfo("ocrmypdf"),
		tesseract: toolInfo("tesseract"),
	};
}

function printDoctor(asJson) {
	const tools = inspectTools();
	const capabilities = {
		pandocDocuments: tools.pandoc.available,
		pdfText: tools.pdftotext.available && tools.pdfinfo.available,
		pdfImageDetection: tools.pdfimages.available,
		pdfOcr: tools.ocrmypdf.available && tools.tesseract.available,
	};
	const remediation = [];
	if (!tools.pandoc.available) remediation.push("Install Pandoc (macOS: brew install pandoc; Debian/Ubuntu: apt install pandoc).");
	if (!capabilities.pdfText || !capabilities.pdfImageDetection) {
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

function sourceFormat(filePath) {
	const extension = extname(filePath).toLowerCase();
	if (extension === ".markdown") return "md";
	if (extension === ".htm") return "html";
	return extension.slice(1);
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

function textWarnings(value) {
	const warnings = [];
	const replacementCharacters = (value.match(/\uFFFD/g) ?? []).length;
	const controlCharacters = (value.match(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g) ?? []).length;
	const nonWhitespace = (value.match(/\S/g) ?? []).length;
	const alphanumeric = countContentCharacters(value);
	if (replacementCharacters > 0) warnings.push(`Found ${replacementCharacters} Unicode replacement characters; encoding may be damaged.`);
	if (controlCharacters > 0) warnings.push(`Found ${controlCharacters} unexpected control characters.`);
	if (nonWhitespace > 100 && alphanumeric / nonWhitespace < 0.05) {
		warnings.push("Extracted text has an unusually low proportion of letters and numbers; review for garbled output.");
	}
	if (nonWhitespace === 0) warnings.push("No readable text was extracted.");
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

function extractPdf(filePath, documentDirectory, ocrMode, tools) {
	if (!tools.pdfinfo.available) throw new Error("pdfinfo is required for PDF ingestion but was not found on PATH");
	if (!tools.pdftotext.available) throw new Error("pdftotext is required for PDF ingestion but was not found on PATH");
	const infoResult = run("pdfinfo", ["-isodates", filePath]);
	if (infoResult.status !== 0) throw new Error(`pdfinfo failed: ${infoResult.stderr.trim() || `exit status ${infoResult.status}`}`);
	const info = parsePdfInfo(infoResult.stdout);
	const pageCount = Number.parseInt(info.Pages, 10);
	if (!Number.isInteger(pageCount) || pageCount < 1) throw new Error("pdfinfo did not report a valid page count");
	const warnings = [];
	let pageExtraction = extractPdfPages(filePath, pageCount);
	let pages = pageExtraction.pages;
	warnings.push(...pageExtraction.warnings);
	const beforeCounts = pages.map(countContentCharacters);
	const images = pdfImagePages(filePath);
	if (images.warning) warnings.push(images.warning);
	const candidatePages = beforeCounts
		.map((count, index) => ({ count, page: index + 1 }))
		.filter(({ count, page }) => count < LOW_TEXT_CHARACTERS && images.pages.has(page))
		.map(({ page }) => page);
	let ocrUsed = false;
	let ocrAttempted = false;
	let ocrError = null;
	let ocrSha256 = null;
	let afterCounts = beforeCounts;
	let remainingLowTextPages = candidatePages;
	if (ocrMode === "auto" && candidatePages.length > 0) {
		ocrAttempted = true;
		if (!tools.ocrmypdf.available || !tools.tesseract.available) {
			ocrError = "OCRmyPDF and Tesseract are required for automatic OCR but one or both are unavailable.";
			warnings.push(ocrError);
		} else {
			const derivedDirectory = join(documentDirectory, "derived");
			mkdirSync(derivedDirectory, { recursive: true });
			const ocrPath = join(derivedDirectory, "ocr.pdf");
			const result = run("ocrmypdf", ["--skip-text", "--rotate-pages", "--deskew", "--quiet", filePath, ocrPath]);
			if (result.error || result.status !== 0) {
				ocrError = `OCRmyPDF failed: ${result.stderr.trim() || result.error?.message || `exit status ${result.status}`}`;
				warnings.push(ocrError);
				rmSync(ocrPath, { force: true });
			} else {
				ocrUsed = true;
				ocrSha256 = sha256(readFileSync(ocrPath));
				pageExtraction = extractPdfPages(ocrPath, pageCount);
				pages = pageExtraction.pages;
				warnings.push(...pageExtraction.warnings);
				afterCounts = pages.map(countContentCharacters);
				remainingLowTextPages = candidatePages.filter((page) => afterCounts[page - 1] < LOW_TEXT_CHARACTERS);
				if (remainingLowTextPages.length > 0) {
					warnings.push(`OCR left low-text image pages unresolved: ${remainingLowTextPages.join(", ")}.`);
				}
			}
		}
	}
	if (ocrMode === "never" && candidatePages.length > 0) {
		warnings.push(`Possible scanned pages were not OCRed because OCR was disabled: ${candidatePages.join(", ")}.`);
	}
	if (!images.available && beforeCounts.every((count) => count < LOW_TEXT_CHARACTERS)) {
		warnings.push("The PDF contains very little text, but image-backed page detection was unavailable.");
	}
	const joined = joinPdfPages(pages);
	warnings.push(...textWarnings(joined.markdown));
	if (/^[\t ]*\S[^\n]*[\t ]{3,}\S[^\n]*$/m.test(joined.markdown)) {
		warnings.push("Possible fixed-layout columns or tables require structural review.");
	}
	return {
		markdown: joined.markdown,
		method: ocrUsed ? "ocrmypdf+pdftotext-layout" : "pdftotext-layout",
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
			reason: candidatePages.length > 0 ? "image-backed pages below low-text threshold" : null,
			candidatePages,
			beforeContentCharacters: beforeCounts,
			afterContentCharacters: afterCounts,
			remainingLowTextPages,
			derivedPath: ocrUsed ? "derived/ocr.pdf" : null,
			derivedSha256: ocrSha256,
			error: ocrError,
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
- Remaining low-text pages: ${ocr.remainingLowTextPages.join(", ") || "None"}
- Derived PDF retained: ${ocr.derivedPath ?? "No"}

## Structure and Encoding

Deterministic extraction is complete. Model normalization and structural review are pending.

## Warnings

${warnings}

## Review

Review every prepared document or chunk against \`working/extracted.md\`. Do not summarize or omit source content.
`;
}

function prepareDocument(filePath, runDirectory, options, tools, usedDirectories) {
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
	if (format === "pdf") extracted = extractPdf(filePath, documentDirectory, options.ocr, tools);
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
	let chunkCharacters = DEFAULT_CHUNK_CHARACTERS;
	for (let index = 1; index < args.length; index += 1) {
		const argument = args[index];
		if (argument === "--output") output = resolve(args[++index] ?? fail("--output requires a path"));
		else if (argument === "--ocr") ocr = args[++index] ?? fail("--ocr requires auto or never");
		else if (argument === "--chunk-chars") chunkCharacters = Number.parseInt(args[++index] ?? "", 10);
		else fail(`unknown prepare option: ${argument}`);
	}
	if (!output) fail("prepare requires --output <new-directory>");
	if (!new Set(["auto", "never"]).has(ocr)) fail("--ocr must be auto or never");
	if (!Number.isInteger(chunkCharacters) || chunkCharacters < 1) fail("--chunk-chars must be a positive integer");
	return { input, output, ocr, chunkCharacters };
}

function prepare(args) {
	const options = parsePrepareArguments(args);
	if (!existsSync(options.input)) fail(`input does not exist: ${options.input}`);
	if (existsSync(options.output)) fail(`output directory already exists: ${options.output}`);
	mkdirSync(dirname(options.output), { recursive: true });
	mkdirSync(options.output);
	const tools = inspectTools();
	const inputs = collectInputs(options.input);
	const rows = [];
	const usedDirectories = new Set();
	for (const skipped of inputs.skipped) {
		rows.push({
			document_id: "",
			source_path: skipped.path,
			source_sha256: "",
			source_format: sourceFormat(skipped.path),
			status: "skipped",
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
			rows.push(prepareDocument(filePath, options.output, options, tools, usedDirectories).row);
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

function validateRun(runDirectory) {
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
		if (metadata.review?.completed !== true) errors.push(`${row.output_directory} model review is not marked complete`);
		if (sourceMap.schemaVersion !== 1 || sourceMap.documentId !== row.document_id || sourceMap.markdownFile !== "document.md" || !Array.isArray(sourceMap.entries)) {
			errors.push(`${row.output_directory}/source_map.json is invalid`);
		}
		const document = readFileSync(join(documentDirectory, "document.md"), "utf8");
		const extractedPath = join(documentDirectory, "working/extracted.md");
		if (existsSync(extractedPath)) {
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
			if (!["page-extraction", "document-conversion", "model-alignment"].includes(entry.method)) errors.push(`${row.output_directory}/source_map.json entry ${index + 1} has an invalid method`);
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
			const ocrPath = join(documentDirectory, "derived", "ocr.pdf");
			if (!existsSync(ocrPath)) errors.push(`${row.output_directory}/derived/ocr.pdf is missing`);
			else if (sha256(readFileSync(ocrPath)) !== metadata.extraction.ocr.derivedSha256) errors.push(`${row.output_directory}/derived/ocr.pdf hash does not match metadata`);
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
	process.stdout.write(`${JSON.stringify({ valid: errors.length === 0, errors, warnings }, null, 2)}\n`);
	if (errors.length > 0) process.exit(1);
}

function usage() {
	process.stdout.write(`Usage:
  document-ingest.mjs doctor [--json]
  document-ingest.mjs prepare <input> --output <new-directory> [--ocr auto|never] [--chunk-chars N]
  document-ingest.mjs validate <run-directory>
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
} else if (command === "prepare") prepare(args);
else if (command === "validate") {
	if (args.length !== 1) fail("validate requires exactly one run directory");
	const runDirectory = resolve(args[0]);
	if (!existsSync(runDirectory) || !lstatSync(runDirectory).isDirectory()) fail(`run directory does not exist: ${runDirectory}`);
	validateRun(runDirectory);
} else fail(`unknown command: ${command}`, 2);

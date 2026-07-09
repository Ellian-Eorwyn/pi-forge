import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, statSync } from "node:fs";
import { dirname, extname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { ToolInputError } from "../../../lib/tool_contract.mjs";

const scriptDirectory = dirname(fileURLToPath(import.meta.url));
const workflowScript = join(scriptDirectory, "document-ingest.mjs");

function csvRows(value) {
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

export function runIngest(args) {
	const result = spawnSync(process.execPath, [workflowScript, ...args], {
		encoding: "utf8",
		maxBuffer: 100 * 1024 * 1024,
	});
	if (result.status !== 0) {
		throw new ToolInputError("document_ingest_command_failed", result.stderr.trim() || result.stdout.trim() || `exit status ${result.status}`);
	}
	return JSON.parse(result.stdout);
}

export function prepareSingleFile(input, output, options = {}) {
	const source = resolve(input);
	const extension = extname(source).toLowerCase();
	const runDirectory = resolve(output);
	const args = ["prepare", source, "--output", runDirectory];
	if (options.ocr) args.push("--ocr", options.ocr);
	if (options.ocrBackend) args.push("--ocr-backend", options.ocrBackend);
	if (options.glmocrUrl) args.push("--glmocr-url", options.glmocrUrl);
	if (options.chunkCharacters) args.push("--chunk-chars", String(options.chunkCharacters));
	const summary = runIngest(args);
	const rows = manifestRows(runDirectory);
	const failed = rows.find((row) => row.status === "failed");
	if (failed) throw new ToolInputError("document_extraction_failed", failed.error || "Document extraction failed");
	const documents = documentRecords(runDirectory);
	if (documents.length === 0) throw new ToolInputError("document_extraction_failed", "No prepared document was produced");
	return { source, extension, runDirectory, summary, documents };
}

export function manifestRows(runDirectory) {
	const manifestPath = join(runDirectory, "manifest.csv");
	if (!existsSync(manifestPath)) throw new ToolInputError("manifest_missing", `manifest.csv is missing: ${manifestPath}`);
	const rows = csvRows(readFileSync(manifestPath, "utf8")).filter((row) => row.some((field) => field !== ""));
	const headers = rows.shift() ?? [];
	return rows.map((row) => Object.fromEntries(headers.map((header, index) => [header, row[index] ?? ""])));
}

export function documentRecords(runDirectory) {
	return manifestRows(runDirectory)
		.filter((row) => row.output_directory)
		.map((row) => {
			const directory = join(runDirectory, row.output_directory);
			return {
				row,
				directory,
				metadataPath: join(directory, "metadata.json"),
				markdownPath: join(directory, "document.md"),
				sourceMapPath: join(directory, "source_map.json"),
				extractionReportPath: join(directory, "extraction_report.md"),
			};
		});
}

export function artifactsForDocuments(documents) {
	return documents.flatMap((document) => [
		{ role: "markdown", path: document.markdownPath },
		{ role: "metadata", path: document.metadataPath },
		{ role: "source_map", path: document.sourceMapPath },
		{ role: "extraction_report", path: document.extractionReportPath },
	]);
}

export function metadataForDocument(document) {
	const metadata = JSON.parse(readFileSync(document.metadataPath, "utf8"));
	return {
		documentDirectory: document.directory,
		markdownPath: document.markdownPath,
		metadataPath: document.metadataPath,
		sourceMapPath: document.sourceMapPath,
		extractionReportPath: document.extractionReportPath,
		metadata,
	};
}

export function preparedMetadata(input) {
	const target = resolve(input);
	const stat = statSync(target);
	if (stat.isFile()) {
		if (target.endsWith("metadata.json")) return [{ metadataPath: target, metadata: JSON.parse(readFileSync(target, "utf8")) }];
		throw new ToolInputError("unsupported_input", "Prepared metadata input must be a metadata.json file or document-ingest directory");
	}
	const directMetadata = join(target, "metadata.json");
	if (existsSync(directMetadata)) {
		return [{ documentDirectory: target, metadataPath: directMetadata, metadata: JSON.parse(readFileSync(directMetadata, "utf8")) }];
	}
	if (existsSync(join(target, "manifest.csv"))) {
		return documentRecords(target).map(metadataForDocument);
	}
	throw new ToolInputError("metadata_not_found", `No document-ingest metadata found in ${target}`);
}

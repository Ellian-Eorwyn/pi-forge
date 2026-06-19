#!/usr/bin/env node

import { createHash } from "node:crypto";
import {
	accessSync,
	constants,
	copyFileSync,
	existsSync,
	mkdirSync,
	readFileSync,
	rmSync,
	statSync,
	writeFileSync,
} from "node:fs";
import { basename, dirname, extname, join, resolve } from "node:path";
import { spawnSync } from "node:child_process";

function fail(message) {
	process.stderr.write(`Error: ${message}\n`);
	process.exit(1);
}

const args = process.argv.slice(2);
if (args.length === 1 && (args[0] === "--help" || args[0] === "-h")) {
	process.stdout.write("Usage: extract-transcript.mjs <input> <output>\n");
	process.exit(0);
}
if (args.length !== 2) {
	fail("expected an input path and an explicit output path");
}

const inputPath = resolve(args[0]);
const outputPath = resolve(args[1]);
if (inputPath === outputPath) fail("input and output paths must differ");
if (!existsSync(inputPath)) fail(`input does not exist: ${inputPath}`);
if (!statSync(inputPath).isFile()) fail(`input is not a file: ${inputPath}`);
if (existsSync(outputPath)) fail(`output already exists: ${outputPath}`);

const extension = extname(inputPath).toLowerCase();
if (![".txt", ".md", ".docx"].includes(extension)) {
	fail(`unsupported input format ${extension || "(none)"}; expected .txt, .md, or .docx`);
}

const source = readFileSync(inputPath);
const checksum = createHash("sha256").update(source).digest("hex");
const outputDirectory = dirname(outputPath);
mkdirSync(outputDirectory, { recursive: true });
accessSync(outputDirectory, constants.W_OK);

const temporaryPath = join(outputDirectory, `.${basename(outputPath)}.${process.pid}.tmp`);
const warnings = [];

try {
	if (extension === ".docx") {
		const result = spawnSync(
			"pandoc",
			["--from=docx", "--to=gfm", "--wrap=none", `--output=${temporaryPath}`, inputPath],
			{ encoding: "utf8" },
		);
		if (result.error?.code === "ENOENT") {
			throw new Error("Pandoc is required to extract .docx transcripts but was not found on PATH");
		}
		if (result.error) throw new Error(`Pandoc could not start: ${result.error.message}`);
		if (result.status !== 0) {
			throw new Error(`Pandoc extraction failed: ${result.stderr.trim() || `exit status ${result.status}`}`);
		}
		warnings.push("DOCX extraction is lossy; review tables, comments, images, text boxes, and speaker formatting.");
		if (result.stderr.trim()) warnings.push(result.stderr.trim());
	} else {
		new TextDecoder("utf-8", { fatal: true }).decode(source);
		writeFileSync(temporaryPath, source, { flag: "wx" });
	}

	copyFileSync(temporaryPath, outputPath, constants.COPYFILE_EXCL);
	rmSync(temporaryPath);
} catch (error) {
	rmSync(temporaryPath, { force: true });
	if (error instanceof TypeError && extension !== ".docx") {
		fail(`input is not valid UTF-8: ${inputPath}`);
	}
	if (error?.code === "EEXIST") fail(`output already exists: ${outputPath}`);
	fail(error instanceof Error ? error.message : String(error));
}

for (const warning of warnings) process.stderr.write(`Warning: ${warning}\n`);
process.stdout.write(
	`${JSON.stringify({
		input: inputPath,
		output: outputPath,
		format: extension.slice(1),
		sha256: checksum,
		warnings,
	})}\n`,
);

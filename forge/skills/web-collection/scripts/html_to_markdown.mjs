#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { basename, dirname, extname, join, resolve } from "node:path";
import { okResult, requiredString, runTool, ToolInputError } from "../../../lib/tool_contract.mjs";
import { htmlToCleanMarkdown } from "../../../lib/html-cleaner.mjs";

function sha256(path) {
	return createHash("sha256").update(readFileSync(path)).digest("hex");
}

await runTool(async (input) => {
	const source = resolve(requiredString(input, "input"));
	if (!existsSync(source)) throw new ToolInputError("input_not_found", `Input does not exist: ${source}`);
	const output = resolve(
		typeof input.output === "string" && input.output.trim()
			? input.output
			: join(resolve(requiredString(input, "outputDirectory")), `${basename(source, extname(source))}.md`),
	);
	if (existsSync(output)) throw new ToolInputError("output_exists", `Output already exists: ${output}`);
	mkdirSync(dirname(output), { recursive: true });
	try {
		const buffer = readFileSync(source);
		const cleanMd = await htmlToCleanMarkdown(buffer, basename(source));
		if (!cleanMd) throw new Error("Extracted markdown is empty");
		writeFileSync(output, cleanMd, "utf8");
	} catch (error) {
		throw new ToolInputError("html_cleaner_failed", `HTML cleaning failed: ${error.message}`);
	}
	const stat = statSync(output);
	return okResult({
		artifacts: [{ role: "markdown", path: output }],
		warnings: [],
		data: { input: source, output, sha256: sha256(output), sizeBytes: stat.size },
	});
});

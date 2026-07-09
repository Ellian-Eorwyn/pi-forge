#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, statSync } from "node:fs";
import { basename, dirname, extname, join, resolve } from "node:path";
import { okResult, requiredString, runTool, ToolInputError } from "../../../lib/tool_contract.mjs";

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
	const result = spawnSync("pandoc", [source, "--from=html", "--to=gfm", "--wrap=none", "--output", output], { encoding: "utf8" });
	if (result.error?.code === "ENOENT") throw new ToolInputError("pandoc_missing", "Pandoc is required for HTML to Markdown conversion");
	if (result.error || result.status !== 0) {
		throw new ToolInputError("pandoc_failed", result.stderr.trim() || result.error?.message || `exit status ${result.status}`);
	}
	const stat = statSync(output);
	return okResult({
		artifacts: [{ role: "markdown", path: output }],
		warnings: result.stderr.trim() ? [result.stderr.trim()] : [],
		data: { input: source, output, sha256: sha256(output), sizeBytes: stat.size },
	});
});

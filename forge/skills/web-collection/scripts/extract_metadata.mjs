#!/usr/bin/env node

import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { okResult, optionalInteger, requiredString, runTool, ToolInputError } from "../../../lib/tool_contract.mjs";
import { metadataForInput } from "./web_tool_common.mjs";

await runTool(async (input) => {
	const target = resolve(requiredString(input, "input"));
	if (!existsSync(target)) throw new ToolInputError("input_not_found", `Input does not exist: ${target}`);
	const entries = metadataForInput(target, {
		baseUrl: typeof input.baseUrl === "string" ? input.baseUrl : null,
		maxFiles: optionalInteger(input, "maxFiles", 500),
	});
	return okResult({
		data: {
			input: target,
			count: entries.length,
			entries,
		},
	});
});

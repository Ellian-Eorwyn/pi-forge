#!/usr/bin/env node

import { extname, resolve } from "node:path";
import { okResult, optionalInteger, requiredString, runTool, ToolInputError } from "../../../lib/tool_contract.mjs";
import { artifactsForDocuments, metadataForDocument, prepareSingleFile } from "./document_tool_common.mjs";

await runTool(async (input) => {
	const source = resolve(requiredString(input, "input"));
	if (extname(source).toLowerCase() !== ".pptx") throw new ToolInputError("unsupported_input_format", "pptx_to_markdown requires a .pptx input");
	const prepared = prepareSingleFile(source, requiredString(input, "output"), {
		chunkCharacters: optionalInteger(input, "chunkCharacters", undefined),
	});
	const documents = prepared.documents.map(metadataForDocument);
	return okResult({
		artifacts: artifactsForDocuments(prepared.documents),
		warnings: documents.flatMap((document) => document.metadata.extraction?.warnings ?? []),
		data: { summary: prepared.summary, documents },
	});
});

#!/usr/bin/env node

import { extname, resolve } from "node:path";
import { okResult, optionalInteger, requiredString, runTool, ToolInputError } from "../../../lib/tool_contract.mjs";
import { artifactsForDocuments, metadataForDocument, prepareSingleFile } from "./document_tool_common.mjs";

await runTool(async (input) => {
	const source = resolve(requiredString(input, "input"));
	if (extname(source).toLowerCase() !== ".pdf") throw new ToolInputError("unsupported_input_format", "pdf_to_markdown requires a .pdf input");
	const prepared = prepareSingleFile(source, requiredString(input, "output"), {
		ocr: typeof input.ocr === "string" ? input.ocr : undefined,
		ocrBackend: typeof input.ocrBackend === "string" ? input.ocrBackend : undefined,
		glmocrUrl: typeof input.glmocrUrl === "string" ? input.glmocrUrl : undefined,
		chunkCharacters: optionalInteger(input, "chunkCharacters", undefined),
	});
	const documents = prepared.documents.map(metadataForDocument);
	return okResult({
		artifacts: artifactsForDocuments(prepared.documents),
		warnings: documents.flatMap((document) => document.metadata.extraction?.warnings ?? []),
		data: { summary: prepared.summary, documents },
	});
});

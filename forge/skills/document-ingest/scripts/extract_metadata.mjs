#!/usr/bin/env node

import { extname, resolve } from "node:path";
import { okResult, optionalInteger, requiredString, runTool, ToolInputError } from "../../../lib/tool_contract.mjs";
import { preparedMetadata, prepareSingleFile } from "./document_tool_common.mjs";

await runTool(async (input) => {
	const target = resolve(requiredString(input, "input"));
	const extension = extname(target).toLowerCase();
	if (extension === ".pdf" || extension === ".docx") {
		const output = requiredString(input, "output");
		const prepared = prepareSingleFile(target, output, {
			ocr: typeof input.ocr === "string" ? input.ocr : undefined,
			ocrBackend: typeof input.ocrBackend === "string" ? input.ocrBackend : undefined,
			glmocrUrl: typeof input.glmocrUrl === "string" ? input.glmocrUrl : undefined,
			chunkCharacters: optionalInteger(input, "chunkCharacters", undefined),
		});
		return okResult({
			warnings: prepared.documents.flatMap((document) => preparedMetadata(document.directory)[0].metadata.extraction?.warnings ?? []),
			data: { summary: prepared.summary, documents: preparedMetadata(prepared.runDirectory) },
		});
	}
	const documents = preparedMetadata(target);
	if (documents.length === 0) throw new ToolInputError("metadata_not_found", `No metadata found: ${target}`);
	return okResult({
		warnings: documents.flatMap((document) => document.metadata.extraction?.warnings ?? []),
		data: { documents },
	});
});

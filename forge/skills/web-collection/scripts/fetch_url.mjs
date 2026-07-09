#!/usr/bin/env node

import { resolve } from "node:path";
import { okResult, optionalInteger, requiredString, runTool } from "../../../lib/tool_contract.mjs";
import { assertDownloaded, collectionArtifacts, collectionWarnings, readCollectionManifest, runCollection } from "./web_tool_common.mjs";

await runTool(async (input) => {
	const url = requiredString(input, "url");
	const output = resolve(requiredString(input, "output"));
	const summary = runCollection(url, output, {
		render: Boolean(input.render),
		userAgent: typeof input.userAgent === "string" ? input.userAgent : undefined,
		timeoutMs: optionalInteger(input, "timeoutMs", undefined),
		maxBytes: optionalInteger(input, "maxBytes", undefined),
	});
	assertDownloaded(output, "fetch_failed");
	return okResult({
		artifacts: collectionArtifacts(output),
		warnings: collectionWarnings(output),
		data: { summary, manifest: readCollectionManifest(output) },
	});
});

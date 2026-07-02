#!/usr/bin/env node

import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import {
	DEFAULT_SKILLS_ROOT,
	createBridgeServer,
	parseArguments,
	runChildProcess,
	runMcpServerMain,
} from "../forge/scripts/pi-forge-mcp-server.mjs";

export { DEFAULT_SKILLS_ROOT, createBridgeServer, parseArguments, runChildProcess };

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
	runMcpServerMain().catch((error) => {
		process.stderr.write(`pi-forge-mcp: ${error.message}\n`);
		process.exitCode = 1;
	});
}

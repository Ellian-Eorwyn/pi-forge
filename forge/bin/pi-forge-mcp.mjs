#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { join } from "node:path";
import { applyRuntimeEnvironment, exitWithResult, PACKAGE_ROOT } from "../scripts/runtime-env.mjs";

applyRuntimeEnvironment();
const result = spawnSync(process.execPath, [join(PACKAGE_ROOT, "scripts", "pi-forge-mcp-server.mjs"), ...process.argv.slice(2)], {
	env: process.env,
	stdio: "inherit",
});
exitWithResult(result);

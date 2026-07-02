#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { applyRuntimeEnvironment, exitWithResult, resolveCodingAgentCli } from "../scripts/runtime-env.mjs";

applyRuntimeEnvironment();
const result = spawnSync(process.execPath, [resolveCodingAgentCli(), ...process.argv.slice(2)], {
	env: process.env,
	stdio: "inherit",
});
exitWithResult(result);

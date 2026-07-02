#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const forgeDirectory = "forge";
const packageJson = JSON.parse(readFileSync(join(forgeDirectory, "package.json"), "utf8"));

if (packageJson.name !== "@ellian-eorwyn/pi-forge") {
	throw new Error(`forge/package.json has name ${packageJson.name}`);
}
if (packageJson.private !== false) {
	throw new Error("forge/package.json must be publishable with private: false");
}
for (const command of ["pi-forge", "pi-forge-mcp", "pi-forge-update"]) {
	if (packageJson.bin?.[command] !== `bin/${command}.mjs`) {
		throw new Error(`forge/package.json is missing bin.${command}`);
	}
}

const npmCache = mkdtempSync(join(tmpdir(), "pi-forge-pack-cache-"));
let result;
try {
	result = spawnSync("npm", ["pack", "--dry-run", "--ignore-scripts", "--json"], {
		cwd: forgeDirectory,
		encoding: "utf8",
		env: { ...process.env, npm_config_cache: npmCache },
		stdio: ["inherit", "pipe", "pipe"],
	});
	if (result.status !== 0) {
		throw new Error([result.stdout, result.stderr].filter(Boolean).join("\n") || "npm pack --dry-run failed");
	}
} finally {
	rmSync(npmCache, { force: true, recursive: true });
}

const [pack] = JSON.parse(result.stdout);
const files = pack.files.map((file) => file.path).sort();
const required = [
	"AGENTS.md",
	"bin/pi-forge.mjs",
	"bin/pi-forge-mcp.mjs",
	"bin/pi-forge-update.mjs",
	"scripts/configure-pi-forge.mjs",
	"scripts/pi-forge-mcp-server.mjs",
	"scripts/runtime-env.mjs",
	"skills/document-ingest/SKILL.md",
];
for (const path of required) {
	if (!files.includes(path)) {
		throw new Error(`forge package is missing ${path}`);
	}
}

const forbiddenPatterns = [
	/\.DS_Store$/,
	/(^|\/)node_modules\//,
	/(^|\/)__pycache__\//,
	/\.pyc$/,
	/^scripts\/pi-forge-install\.(sh|ps1)$/,
	/^scripts\/pi-forge-uninstall\.(sh|ps1)$/,
];
const forbidden = files.filter((path) => forbiddenPatterns.some((pattern) => pattern.test(path)));
if (forbidden.length > 0) {
	throw new Error(`forge package includes forbidden files:\n${forbidden.join("\n")}`);
}

console.log(`forge package dry-run OK: ${files.length} files, ${pack.unpackedSize} bytes unpacked`);

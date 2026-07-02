import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { existsSync, mkdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const PACKAGE_NAME = "@ellian-eorwyn/pi-forge";
export const DEFAULT_PACKAGE_SPEC = `${PACKAGE_NAME}@latest`;

const SCRIPT_DIRECTORY = dirname(fileURLToPath(import.meta.url));
export const PACKAGE_ROOT = resolve(SCRIPT_DIRECTORY, "..");

export function commandForPlatform(command) {
	return process.platform === "win32" && !/[\\/]/.test(command) ? `${command}.cmd` : command;
}

export function getForgePaths() {
	const home = resolve(process.env.PI_FORGE_HOME || join(homedir(), ".pi-forge"));
	const appDir = resolve(process.env.PI_FORGE_INSTALL_DIR || join(home, "app"));
	const binDir = resolve(process.env.PI_FORGE_BIN_DIR || join(home, "bin"));
	const agentDir = resolve(process.env.PI_FORGE_AGENT_DIR || join(home, "agent"));
	const npmCacheDir = resolve(process.env.PI_FORGE_NPM_CACHE || join(agentDir, "npm-cache"));
	const playwrightBrowsersDir = resolve(process.env.PI_FORGE_PLAYWRIGHT_BROWSERS || join(agentDir, "playwright-browsers"));
	return { home, appDir, binDir, agentDir, npmCacheDir, playwrightBrowsersDir };
}

export function applyRuntimeEnvironment() {
	const paths = getForgePaths();
	process.env.PI_CODING_AGENT_DIR = process.env.PI_CODING_AGENT_DIR || paths.agentDir;
	process.env.PI_SKIP_VERSION_CHECK = process.env.PI_SKIP_VERSION_CHECK || "1";
	process.env.PLAYWRIGHT_BROWSERS_PATH = process.env.PLAYWRIGHT_BROWSERS_PATH || paths.playwrightBrowsersDir;
	process.env.FORGE_SEARXNG_URL = process.env.FORGE_SEARXNG_URL || "http://llms/searxng";
	return paths;
}

export function runChecked(command, args, options = {}) {
	const result = spawnSync(commandForPlatform(command), args, {
		cwd: options.cwd,
		encoding: "utf8",
		env: options.env,
		stdio: options.stdio || "inherit",
	});
	if (result.status !== 0) {
		const output = [result.stdout, result.stderr].filter(Boolean).join("\n");
		throw new Error(output || `Command failed: ${[command, ...args].join(" ")}`);
	}
	return result;
}

export function exitWithResult(result) {
	if (result.error) {
		process.stderr.write(`${result.error.message}\n`);
		process.exit(1);
	}
	if (result.signal) process.exit(1);
	process.exit(result.status ?? 0);
}

export function resolveCodingAgentCli() {
	const entrypointPath = fileURLToPath(import.meta.resolve("@earendil-works/pi-coding-agent"));
	return join(dirname(entrypointPath), "cli.js");
}

export function configurePackage(packageRoot) {
	const paths = getForgePaths();
	mkdirSync(paths.agentDir, { recursive: true });
	runChecked(process.execPath, [join(packageRoot, "scripts", "configure-pi-forge.mjs"), paths.agentDir, packageRoot]);
	return paths;
}

export function ensureAppProject(appDir) {
	mkdirSync(appDir, { recursive: true });
	const packageJsonPath = join(appDir, "package.json");
	if (!existsSync(packageJsonPath)) {
		writeFileSync(packageJsonPath, `${JSON.stringify({ private: true, dependencies: {} }, undefined, "\t")}\n`);
	}
}

export function installConfiguredPackage(packageSpec = process.env.PI_FORGE_PACKAGE_SPEC || DEFAULT_PACKAGE_SPEC, options = {}) {
	const paths = getForgePaths();
	ensureAppProject(paths.appDir);
	mkdirSync(paths.npmCacheDir, { recursive: true });
	runChecked("npm", ["--prefix", paths.appDir, "install", "--omit=dev", "--ignore-scripts", packageSpec], {
		env: { ...process.env, npm_config_cache: paths.npmCacheDir },
		stdio: options.stdio,
	});
	return resolveInstalledPackageRoot(paths.appDir);
}

export function packPackageDirectory(packageRoot) {
	const paths = getForgePaths();
	const packageCacheDir = join(paths.appDir, "package-cache");
	mkdirSync(packageCacheDir, { recursive: true });
	mkdirSync(paths.npmCacheDir, { recursive: true });
	const result = runChecked("npm", ["pack", "--json", "--pack-destination", packageCacheDir], {
		cwd: packageRoot,
		env: { ...process.env, npm_config_cache: paths.npmCacheDir },
		stdio: ["inherit", "pipe", "pipe"],
	});
	const packed = JSON.parse(result.stdout)[0];
	return `file:${join(packageCacheDir, packed.filename)}`;
}

export function resolveInstalledPackageRoot(appDir) {
	const require = createRequire(join(appDir, "package.json"));
	return dirname(require.resolve(`${PACKAGE_NAME}/package.json`));
}

export function refreshLaunchers(paths) {
	const npmBinDir = join(paths.appDir, "node_modules", ".bin");
	mkdirSync(paths.binDir, { recursive: true });
	for (const command of ["pi-forge", "pi-forge-mcp", "pi-forge-update"]) {
		if (process.platform === "win32") {
			writeFileSync(
				join(paths.binDir, `${command}.cmd`),
				`@ECHO off\r\n"%~dp0..\\app\\node_modules\\.bin\\${command}.cmd" %*\r\n`,
			);
			writeFileSync(
				join(paths.binDir, `${command}.ps1`),
				`& "$PSScriptRoot/../app/node_modules/.bin/${command}.ps1" @args\n`,
			);
			continue;
		}
		const launcher = join(paths.binDir, command);
		rmSync(launcher, { force: true });
		symlinkSync(join(npmBinDir, command), launcher);
	}
}

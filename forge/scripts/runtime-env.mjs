import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import {
	copyFileSync,
	existsSync,
	mkdirSync,
	mkdtempSync,
	readdirSync,
	readFileSync,
	realpathSync,
	rmSync,
	symlinkSync,
	writeFileSync,
} from "node:fs";
import { homedir, tmpdir } from "node:os";
import { dirname, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

export const PACKAGE_NAME = "@ellian-eorwyn/pi-forge";
export const PI_PACKAGE_NAME = "@earendil-works/pi-coding-agent";
export const DEFAULT_PI_PACKAGE_SPEC = "GitHub source archive runtime packages";
export const DEFAULT_SOURCE_ARCHIVE_URL = "https://github.com/Ellian-Eorwyn/pi-forge/archive/refs/heads/main.tar.gz";
export const DEFAULT_UPSTREAM_SOURCE_ARCHIVE_URL = "https://github.com/earendil-works/pi/archive/refs/heads/main.tar.gz";
const SOURCE_RUNTIME_PACKAGE_DIRS = ["packages/ai", "packages/agent", "packages/tui", "packages/coding-agent"];

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
	const paths = getForgePaths();
	const appDir = existsSync(paths.appDir) ? realpathSync(paths.appDir) : resolve(paths.appDir);
	const packageRoot = existsSync(PACKAGE_ROOT) ? realpathSync(PACKAGE_ROOT) : resolve(PACKAGE_ROOT);
	if (packageRoot === appDir || packageRoot.startsWith(`${appDir}${sep}`)) {
		const appPackageCli = join(appDir, "node_modules", ...PI_PACKAGE_NAME.split("/"), "dist", "cli.js");
		if (existsSync(appPackageCli)) return appPackageCli;
		const appBin = join(appDir, "node_modules", ".bin", "pi");
		if (process.platform !== "win32" && existsSync(appBin)) return realpathSync(appBin);
	}
	const entrypointPath = fileURLToPath(import.meta.resolve(PI_PACKAGE_NAME));
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

function installAppPackage(packageSpec, options = {}) {
	const packageSpecs = Array.isArray(packageSpec) ? packageSpec : [packageSpec];
	const paths = getForgePaths();
	ensureAppProject(paths.appDir);
	mkdirSync(paths.npmCacheDir, { recursive: true });
	runChecked("npm", ["--prefix", paths.appDir, "install", "--omit=dev", "--ignore-scripts", ...packageSpecs], {
		env: { ...process.env, npm_config_cache: paths.npmCacheDir },
		stdio: options.stdio,
	});
	return paths;
}

export function installConfiguredPackage(packageSpec = process.env.PI_FORGE_PACKAGE_SPEC || packSourceArchivePackageSpec(), options = {}) {
	const paths = installAppPackage(packageSpec, options);
	return resolveInstalledPackageRoot(paths.appDir);
}

export function installConfiguredPiPackage(
	piPackageSpec = process.env.PI_FORGE_PI_PACKAGE_SPEC || packSourceArchivePiPackageSpecs(),
	options = {},
) {
	installAppPackage(piPackageSpec, options);
}

function downloadFile(url, outputPath) {
	if (url.startsWith("file://")) {
		copyFileSync(fileURLToPath(url), outputPath);
		return;
	}
	const curl = spawnSync("curl", ["-fsSL", url, "-o", outputPath], {
		encoding: "utf8",
		stdio: ["inherit", "pipe", "pipe"],
	});
	if (curl.status === 0) return;
	if (!curl.error || curl.error.code !== "ENOENT") {
		const output = [curl.stdout, curl.stderr].filter(Boolean).join("\n");
		throw new Error(output || `Command failed: curl -fsSL ${url} -o ${outputPath}`);
	}
	const wget = spawnSync("wget", ["-qO", outputPath, url], {
		encoding: "utf8",
		stdio: ["inherit", "pipe", "pipe"],
	});
	if (wget.status === 0) return;
	if (wget.error?.code === "ENOENT") {
		throw new Error("pi-forge update requires curl or wget to fetch the source archive.");
	}
	const output = [wget.stdout, wget.stderr].filter(Boolean).join("\n");
	throw new Error(output || `Command failed: wget -qO ${outputPath} ${url}`);
}

export function packSourceArchivePackageSpec(sourceArchiveUrl = process.env.PI_FORGE_SOURCE_ARCHIVE_URL || DEFAULT_SOURCE_ARCHIVE_URL) {
	return withSourceArchive(sourceArchiveUrl, (sourceRoot) => packPackageDirectory(join(sourceRoot, "forge")));
}

export function packSourceArchivePackageSpecs(
	sourceArchiveUrl = process.env.PI_FORGE_SOURCE_ARCHIVE_URL || DEFAULT_SOURCE_ARCHIVE_URL,
	upstreamArchiveUrl = process.env.PI_FORGE_UPSTREAM_SOURCE_ARCHIVE_URL || DEFAULT_UPSTREAM_SOURCE_ARCHIVE_URL
) {
	if (sourceArchiveUrl === upstreamArchiveUrl) {
		return withSourceArchive(sourceArchiveUrl, (sourceRoot) => ({
			forgePackageSpec: packPackageDirectory(join(sourceRoot, "forge")),
			piPackageSpecs: packSourceRuntimePackageSpecs(sourceRoot),
		}));
	}
	return {
		forgePackageSpec: packSourceArchivePackageSpec(sourceArchiveUrl),
		piPackageSpecs: packSourceArchivePiPackageSpecs(upstreamArchiveUrl),
	};
}

export function packSourceArchivePiPackageSpecs(sourceArchiveUrl = process.env.PI_FORGE_UPSTREAM_SOURCE_ARCHIVE_URL || DEFAULT_UPSTREAM_SOURCE_ARCHIVE_URL) {
	return withSourceArchive(sourceArchiveUrl, (sourceRoot) => packSourceRuntimePackageSpecs(sourceRoot));
}

function withSourceArchive(sourceArchiveUrl, callback) {
	const tempDir = mkdtempSync(join(tmpdir(), "pi-forge-source-"));
	try {
		const archivePath = join(tempDir, "pi-forge.tar.gz");
		const extractDir = join(tempDir, "source");
		mkdirSync(extractDir);
		downloadFile(sourceArchiveUrl, archivePath);
		runChecked("tar", ["-xzf", archivePath, "-C", extractDir]);
		const sourceRoot = findSourceRoot(extractDir, sourceArchiveUrl);
		return callback(sourceRoot);
	} finally {
		rmSync(tempDir, { recursive: true, force: true });
	}
}

function findSourceRoot(extractDir, sourceArchiveUrl) {
	const sourceRoot = readdirSync(extractDir, { withFileTypes: true })
		.filter((entry) => entry.isDirectory())
		.map((entry) => join(extractDir, entry.name))
		.find((entry) => existsSync(join(entry, "forge", "package.json")) || existsSync(join(entry, "packages", "coding-agent", "package.json")));
	if (!sourceRoot) {
		throw new Error(`Source archive did not contain a valid pi or pi-forge checkout: ${sourceArchiveUrl}`);
	}
	return sourceRoot;
}

function packSourceRuntimePackageSpecs(sourceRoot) {
	const paths = getForgePaths();
	mkdirSync(paths.npmCacheDir, { recursive: true });
	runChecked("npm", ["ci", "--ignore-scripts"], {
		cwd: sourceRoot,
		env: { ...process.env, npm_config_cache: paths.npmCacheDir },
	});
	const pkg = JSON.parse(readFileSync(join(sourceRoot, "package.json"), "utf8"));
	const buildScript = pkg.scripts?.["build:install"] ? "build:install" : "build";
	runChecked("npm", ["run", buildScript], {
		cwd: sourceRoot,
		env: { ...process.env, npm_config_cache: paths.npmCacheDir },
	});
	// Source-archive installs provide local workspace tarballs for these packages.
	// Keeping the published shrinkwrap would force nested @earendil-works packages
	// back to the npm registry and bypass the freshly built source packages.
	rmSync(join(sourceRoot, "packages", "coding-agent", "npm-shrinkwrap.json"), { force: true });
	return SOURCE_RUNTIME_PACKAGE_DIRS.map((packageDir) => packPackageDirectory(join(sourceRoot, packageDir)));
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

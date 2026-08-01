#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";
import {
	DEFAULT_PI_PACKAGE_SPEC,
	DEFAULT_SOURCE_ARCHIVE_URL,
	DEFAULT_UPSTREAM_SOURCE_ARCHIVE_URL,
	configurePackage,
	exitWithResult,
	getForgePaths,
	installConfiguredPackage,
	installConfiguredPiPackage,
	packSourceArchivePackageSpec,
	packSourceArchivePiPackageSpecs,
	refreshLaunchers,
	resolveInstalledPackageRoot,
} from "../scripts/runtime-env.mjs";

// Set on the handoff below. An older pi-forge simply ignores it and repeats the whole
// update, which an unknown command line flag would not survive.
const REEXEC_ENV_NAME = "PI_FORGE_UPDATE_REEXEC";

function usage() {
	process.stdout.write(`Usage: pi-forge-update [--resources-only]

Updates the npm-installed pi-forge package, refreshes managed configuration, and
rewrites the stable launchers in ~/.pi-forge/bin.

Environment:
  PI_FORGE_PACKAGE_SPEC      pi-forge package spec override (default: packed GitHub source archive)
  PI_FORGE_PI_PACKAGE_SPEC   Pi CLI package spec override (default: ${DEFAULT_PI_PACKAGE_SPEC})
  PI_FORGE_SOURCE_ARCHIVE_URL GitHub source archive used for default pi-forge updates
  PI_FORGE_UPSTREAM_SOURCE_ARCHIVE_URL GitHub source archive used for default pi runtime updates
`);
}

const args = process.argv.slice(2);
for (const arg of args) {
	if (arg === "--help" || arg === "-h") {
		usage();
		process.exit(0);
	}
	if (arg === "--resources-only") continue;
	process.stderr.write(`Unknown option: ${arg}\n`);
	usage();
	process.exit(2);
}

function installForgePackage() {
	if (process.env.PI_FORGE_PACKAGE_SPEC) return installConfiguredPackage();
	const sourceArchiveUrl = process.env.PI_FORGE_SOURCE_ARCHIVE_URL || DEFAULT_SOURCE_ARCHIVE_URL;
	process.stderr.write(`pi-forge-update: installing pi-forge from ${sourceArchiveUrl}.\n`);
	return installConfiguredPackage(packSourceArchivePackageSpec(sourceArchiveUrl));
}

function finishUpdate(packageRoot) {
	let piPackageLabel = process.env.PI_FORGE_PI_PACKAGE_SPEC || DEFAULT_PI_PACKAGE_SPEC;
	if (process.env.PI_FORGE_PI_PACKAGE_SPEC) {
		installConfiguredPiPackage();
	} else {
		const upstreamArchiveUrl = process.env.PI_FORGE_UPSTREAM_SOURCE_ARCHIVE_URL || DEFAULT_UPSTREAM_SOURCE_ARCHIVE_URL;
		process.stderr.write(`pi-forge-update: installing Pi runtime from ${upstreamArchiveUrl}.\n`);
		installConfiguredPiPackage(packSourceArchivePiPackageSpecs(upstreamArchiveUrl));
		piPackageLabel = `runtime packages from ${upstreamArchiveUrl}`;
	}
	const paths = configurePackage(packageRoot);
	refreshLaunchers(paths);
	process.stdout.write(`pi-forge is up to date.\n`);
	process.stdout.write(`  Package: ${packageRoot}\n`);
	process.stdout.write(`  Pi package: ${piPackageLabel}\n`);
	process.stdout.write(`  CLI: ${getForgePaths().binDir}/pi-forge\n`);
	process.stdout.write(`  State: ${paths.agentDir}\n`);
}

try {
	if (process.env[REEXEC_ENV_NAME]) {
		// Second phase: pi-forge is already up to date, so this is the freshly installed
		// updater finishing the runtime install with its own packaging rules.
		finishUpdate(resolveInstalledPackageRoot(getForgePaths().appDir));
	} else {
		const packageRoot = installForgePackage();
		const installedUpdater = join(packageRoot, "bin", "pi-forge-update.mjs");
		if (existsSync(installedUpdater)) {
			// Hand the rest of the update to the pi-forge that was just installed, so a change
			// to how runtime packages are packed applies to this run rather than the next one.
			exitWithResult(
				spawnSync(process.execPath, [installedUpdater, ...args], {
					env: { ...process.env, [REEXEC_ENV_NAME]: "1" },
					stdio: "inherit",
				}),
			);
		}
		finishUpdate(packageRoot);
	}
} catch (error) {
	process.stderr.write(`pi-forge-update: ${error.message}\n`);
	process.exit(1);
}

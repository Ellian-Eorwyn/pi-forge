#!/usr/bin/env node

import {
	DEFAULT_PACKAGE_SPEC,
	PACKAGE_ROOT,
	configurePackage,
	getForgePaths,
	installConfiguredPackage,
	packPackageDirectory,
	refreshLaunchers,
} from "../scripts/runtime-env.mjs";

function usage() {
	process.stdout.write(`Usage: pi-forge-update [--resources-only]

Updates the npm-installed pi-forge package, refreshes managed configuration, and
rewrites the stable launchers in ~/.pi-forge/bin.

Environment:
  PI_FORGE_PACKAGE_SPEC   Package spec to install (default: ${DEFAULT_PACKAGE_SPEC})
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

try {
	let packageRoot;
	try {
		packageRoot = installConfiguredPackage(undefined, process.env.PI_FORGE_PACKAGE_SPEC ? {} : { stdio: ["inherit", "pipe", "pipe"] });
	} catch (error) {
		if (process.env.PI_FORGE_PACKAGE_SPEC) throw error;
		process.stderr.write(`pi-forge-update: ${DEFAULT_PACKAGE_SPEC} is unavailable; refreshing from the installed package copy.\n`);
		packageRoot = installConfiguredPackage(packPackageDirectory(PACKAGE_ROOT));
	}
	const paths = configurePackage(packageRoot);
	refreshLaunchers(paths);
	process.stdout.write(`pi-forge is up to date.\n`);
	process.stdout.write(`  Package: ${packageRoot}\n`);
	process.stdout.write(`  CLI: ${getForgePaths().binDir}/pi-forge\n`);
	process.stdout.write(`  State: ${paths.agentDir}\n`);
} catch (error) {
	process.stderr.write(`pi-forge-update: ${error.message}\n`);
	process.exit(1);
}

#!/usr/bin/env node

import {
	DEFAULT_PACKAGE_SPEC,
	configurePackage,
	getForgePaths,
	installConfiguredPackage,
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
	const packageRoot = installConfiguredPackage();
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

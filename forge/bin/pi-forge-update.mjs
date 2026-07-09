#!/usr/bin/env node

import {
	DEFAULT_PI_PACKAGE_SPEC,
	DEFAULT_SOURCE_ARCHIVE_URL,
	DEFAULT_UPSTREAM_SOURCE_ARCHIVE_URL,
	configurePackage,
	getForgePaths,
	installConfiguredPackage,
	installConfiguredPiPackage,
	packSourceArchivePackageSpecs,
	packSourceArchivePiPackageSpecs,
	refreshLaunchers,
} from "../scripts/runtime-env.mjs";

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

try {
	let packageRoot;
	let piPackageLabel = process.env.PI_FORGE_PI_PACKAGE_SPEC || DEFAULT_PI_PACKAGE_SPEC;
	if (process.env.PI_FORGE_PACKAGE_SPEC) {
		packageRoot = installConfiguredPackage();
		if (process.env.PI_FORGE_PI_PACKAGE_SPEC) {
			installConfiguredPiPackage();
		} else {
			const upstreamArchiveUrl = process.env.PI_FORGE_UPSTREAM_SOURCE_ARCHIVE_URL || DEFAULT_UPSTREAM_SOURCE_ARCHIVE_URL;
			process.stderr.write(`pi-forge-update: installing Pi runtime from ${upstreamArchiveUrl}.\n`);
			installConfiguredPiPackage(packSourceArchivePiPackageSpecs(upstreamArchiveUrl));
			piPackageLabel = `runtime packages from ${upstreamArchiveUrl}`;
		}
	} else {
		const sourceArchiveUrl = process.env.PI_FORGE_SOURCE_ARCHIVE_URL || DEFAULT_SOURCE_ARCHIVE_URL;
		const upstreamArchiveUrl = process.env.PI_FORGE_UPSTREAM_SOURCE_ARCHIVE_URL || sourceArchiveUrl;
		process.stderr.write(`pi-forge-update: installing pi-forge from ${sourceArchiveUrl}.\n`);
		process.stderr.write(`pi-forge-update: installing Pi runtime from ${upstreamArchiveUrl}.\n`);
		const packageSpecs = packSourceArchivePackageSpecs(sourceArchiveUrl, upstreamArchiveUrl);
		packageRoot = installConfiguredPackage(packageSpecs.forgePackageSpec);
		if (process.env.PI_FORGE_PI_PACKAGE_SPEC) {
			installConfiguredPiPackage();
		} else {
			installConfiguredPiPackage(packageSpecs.piPackageSpecs);
			piPackageLabel = `runtime packages from ${upstreamArchiveUrl}`;
		}
	}
	const paths = configurePackage(packageRoot);
	refreshLaunchers(paths);
	process.stdout.write(`pi-forge is up to date.\n`);
	process.stdout.write(`  Package: ${packageRoot}\n`);
	process.stdout.write(`  Pi package: ${piPackageLabel}\n`);
	process.stdout.write(`  CLI: ${getForgePaths().binDir}/pi-forge\n`);
	process.stdout.write(`  State: ${paths.agentDir}\n`);
} catch (error) {
	process.stderr.write(`pi-forge-update: ${error.message}\n`);
	process.exit(1);
}

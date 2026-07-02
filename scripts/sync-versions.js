#!/usr/bin/env node

/**
 * Syncs all publishable workspace package dependency versions to match their
 * current versions. This keeps release packages lockstep-versioned.
 */

import { readdirSync, readFileSync, writeFileSync } from "fs";
import { join } from "path";

const packagesDirectory = join(process.cwd(), "packages");
const packagePaths = [
	...readdirSync(packagesDirectory, { withFileTypes: true })
		.filter((dirent) => dirent.isDirectory())
		.map((dirent) => join(packagesDirectory, dirent.name, "package.json")),
	join(process.cwd(), "forge", "package.json"),
];

const packages = {};
const versionMap = {};

for (const packagePath of packagePaths) {
	try {
		const packageJson = JSON.parse(readFileSync(packagePath, "utf8"));
		packages[packageJson.name] = { path: packagePath, data: packageJson };
		versionMap[packageJson.name] = packageJson.version;
	} catch (error) {
		console.error(`Failed to read ${packagePath}:`, error.message);
	}
}

console.log("Current versions:");
for (const [name, version] of Object.entries(versionMap).sort()) {
	console.log(`  ${name}: ${version}`);
}

const versions = new Set(Object.values(versionMap));
if (versions.size > 1) {
	console.error("\nERROR: Not all packages have the same version.");
	console.error("Expected lockstep versioning. Run one of:");
	console.error("  npm run version:patch");
	console.error("  npm run version:minor");
	console.error("  npm run version:major");
	process.exit(1);
}

console.log("\nAll packages at same version (lockstep)");

let totalUpdates = 0;
for (const pkg of Object.values(packages)) {
	let updated = false;
	for (const field of ["dependencies", "devDependencies"]) {
		const dependencies = pkg.data[field];
		if (!dependencies) continue;
		for (const [dependencyName, currentVersion] of Object.entries(dependencies)) {
			if (!versionMap[dependencyName]) continue;
			const newVersion = `^${versionMap[dependencyName]}`;
			if (currentVersion === newVersion) continue;
			console.log(`\n${pkg.data.name}:`);
			console.log(`  ${dependencyName}: ${currentVersion} -> ${newVersion}${field === "devDependencies" ? " (devDependencies)" : ""}`);
			dependencies[dependencyName] = newVersion;
			updated = true;
			totalUpdates += 1;
		}
	}
	if (updated) {
		writeFileSync(pkg.path, `${JSON.stringify(pkg.data, null, "\t")}\n`);
	}
}

if (totalUpdates === 0) {
	console.log("\nAll inter-package dependencies already in sync.");
} else {
	console.log(`\nUpdated ${totalUpdates} dependency version(s)`);
}

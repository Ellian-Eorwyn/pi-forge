#!/usr/bin/env node

import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const skillsDirectory = join(repositoryRoot, "forge", "skills");
const errors = [];

function repositoryPath(path) {
	return relative(repositoryRoot, path).split(sep).join("/");
}

function isRecord(value) {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requireStringField(manifest, field, expected, manifestPath) {
	const actual = manifest[field];
	if (actual !== expected) {
		errors.push(`${repositoryPath(manifestPath)}: ${field} must be ${JSON.stringify(expected)}`);
	}
}

function validateSkillManifest(skillName, skillDirectory) {
	const skillPath = join(skillDirectory, "SKILL.md");
	const manifestPath = join(skillDirectory, "manifest.json");

	if (!existsSync(skillPath)) {
		errors.push(`${repositoryPath(skillDirectory)}: missing SKILL.md`);
	}
	if (!existsSync(manifestPath)) {
		errors.push(`${repositoryPath(skillDirectory)}: missing manifest.json`);
		return;
	}

	let manifest;
	try {
		manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
	} catch (error) {
		const message = error instanceof Error ? error.message : String(error);
		errors.push(`${repositoryPath(manifestPath)}: invalid JSON: ${message}`);
		return;
	}

	if (!isRecord(manifest)) {
		errors.push(`${repositoryPath(manifestPath)}: manifest must be a JSON object`);
		return;
	}

	requireStringField(manifest, "name", skillName, manifestPath);
	requireStringField(manifest, "skill_file", "SKILL.md", manifestPath);

	if (!Array.isArray(manifest.mechanical_operations)) {
		errors.push(`${repositoryPath(manifestPath)}: mechanical_operations must be an array`);
	}
	if (!isRecord(manifest.safety)) {
		errors.push(`${repositoryPath(manifestPath)}: safety must be a JSON object`);
	}
}

if (!existsSync(skillsDirectory) || !statSync(skillsDirectory).isDirectory()) {
	throw new Error(`${repositoryPath(skillsDirectory)} does not exist`);
}

const skillDirectories = readdirSync(skillsDirectory, { withFileTypes: true })
	.filter((entry) => entry.isDirectory() && !entry.name.startsWith(".") && entry.name !== "node_modules")
	.map((entry) => ({ name: entry.name, path: join(skillsDirectory, entry.name) }))
	.sort((left, right) => left.name.localeCompare(right.name));

for (const skill of skillDirectories) {
	validateSkillManifest(skill.name, skill.path);
}

if (errors.length > 0) {
	for (const error of errors) {
		console.error(error);
	}
	process.exit(1);
}

console.log(`Forge skill manifests OK: ${skillDirectories.length} skills.`);

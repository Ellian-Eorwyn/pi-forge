#!/usr/bin/env node

import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
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

function pathIsInside(parent, candidate) {
	const relativePath = relative(parent, candidate);
	return relativePath === "" || (relativePath !== ".." && !relativePath.startsWith(`..${sep}`) && !isAbsolute(relativePath));
}

function validateOptionalPath(value, field, skillDirectory, manifestPath) {
	if (value === undefined) return;
	if (typeof value !== "string" || value.trim() === "") {
		errors.push(`${repositoryPath(manifestPath)}: ${field} must be a non-empty string when present`);
		return;
	}
	const resolvedPath = resolve(skillDirectory, value);
	if (!pathIsInside(skillDirectory, resolvedPath)) {
		errors.push(`${repositoryPath(manifestPath)}: ${field} must stay inside the skill directory`);
		return;
	}
	if (!existsSync(resolvedPath)) {
		errors.push(`${repositoryPath(manifestPath)}: ${field} does not exist: ${value}`);
	}
}

function commandScriptPath(command) {
	const parts = command.trim().split(/\s+/);
	const first = parts[0];
	if (["node", "python", "python3"].includes(first)) return parts[1];
	return first;
}

function validateTool(tool, index, skillDirectory, manifestPath) {
	const label = `tools[${index}]`;
	if (!isRecord(tool)) {
		errors.push(`${repositoryPath(manifestPath)}: ${label} must be a JSON object`);
		return;
	}
	for (const field of ["name", "description", "command"]) {
		if (typeof tool[field] !== "string" || tool[field].trim() === "") {
			errors.push(`${repositoryPath(manifestPath)}: ${label}.${field} must be a non-empty string`);
		}
	}
	if (typeof tool.destructive !== "boolean") {
		errors.push(`${repositoryPath(manifestPath)}: ${label}.destructive must be a boolean`);
	}
	if (typeof tool.command === "string" && tool.command.trim() !== "") {
		const scriptPath = commandScriptPath(tool.command);
		if (!scriptPath) {
			errors.push(`${repositoryPath(manifestPath)}: ${label}.command must include a script path`);
		} else {
			validateOptionalPath(scriptPath, `${label}.command script`, skillDirectory, manifestPath);
		}
	}
	validateOptionalPath(tool.input_schema, `${label}.input_schema`, skillDirectory, manifestPath);
	validateOptionalPath(tool.output_schema, `${label}.output_schema`, skillDirectory, manifestPath);
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
	requireStringField(manifest, "type", "pi-forge-skill", manifestPath);
	requireStringField(manifest, "skill_file", "SKILL.md", manifestPath);

	if ("mechanical_operations" in manifest) {
		errors.push(`${repositoryPath(manifestPath)}: mechanical_operations is obsolete; use tools`);
	}
	if (!Array.isArray(manifest.tools)) {
		errors.push(`${repositoryPath(manifestPath)}: tools must be an array`);
	} else {
		for (const [index, tool] of manifest.tools.entries()) {
			validateTool(tool, index, skillDirectory, manifestPath);
		}
	}
	if (!isRecord(manifest.safety)) {
		errors.push(`${repositoryPath(manifestPath)}: safety must be a JSON object`);
	} else {
		for (const field of ["destructive_by_default", "requires_review_for_filesystem_changes", "preserve_provenance_when_applicable"]) {
			if (typeof manifest.safety[field] !== "boolean") {
				errors.push(`${repositoryPath(manifestPath)}: safety.${field} must be a boolean`);
			}
		}
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

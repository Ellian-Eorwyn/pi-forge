#!/usr/bin/env node

// PI_FORGE_SKILLS_REFERENCE.md is hand-maintained prose, but its skill inventory
// duplicates forge/CAPABILITIES.md and drifted twice because nothing checked it.
// Only the block between the markers below is generated; the surrounding
// orientation prose stays hand-written.

import { existsSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

// Tests point this at a fixture tree so the drift cases can be exercised without
// mutating the real CAPABILITIES.md and reference file.
const repositoryRoot = process.env.FORGE_REFERENCE_ROOT
	? resolve(process.env.FORGE_REFERENCE_ROOT)
	: resolve(dirname(fileURLToPath(import.meta.url)), "..");
const skillsDirectory = join(repositoryRoot, "forge", "skills");
const capabilitiesPath = join(repositoryRoot, "forge", "CAPABILITIES.md");
const referencePath = join(repositoryRoot, "PI_FORGE_SKILLS_REFERENCE.md");
const startMarker = "<!-- forge:skills-list start -->";
const endMarker = "<!-- forge:skills-list end -->";
const capabilitiesHeading = "## Built-in capabilities";

const checkOnly = process.argv.slice(2).includes("--check");
const unknownOptions = process.argv.slice(2).filter((argument) => argument !== "--check");

if (unknownOptions.length > 0) {
	console.error(`Unknown option: ${unknownOptions[0]}`);
	process.exit(2);
}

function repositoryPath(path) {
	return relative(repositoryRoot, path).split(sep).join("/");
}

function fail(message) {
	console.error(message);
	process.exit(1);
}

function readSkillNames() {
	if (!existsSync(skillsDirectory) || !statSync(skillsDirectory).isDirectory()) {
		fail(`${repositoryPath(skillsDirectory)} does not exist`);
	}
	return readdirSync(skillsDirectory, { withFileTypes: true })
		.filter((entry) => entry.isDirectory() && !entry.name.startsWith(".") && entry.name !== "node_modules")
		.map((entry) => entry.name)
		.sort((left, right) => left.localeCompare(right));
}

// Entries look like: - `name`: description (possibly wrapped across lines).
function readCapabilities() {
	if (!existsSync(capabilitiesPath)) {
		fail(`${repositoryPath(capabilitiesPath)} does not exist`);
	}
	const lines = readFileSync(capabilitiesPath, "utf8").split(/\r?\n/);
	const headingIndex = lines.findIndex((line) => line.trim() === capabilitiesHeading);
	if (headingIndex === -1) {
		fail(`${repositoryPath(capabilitiesPath)}: missing "${capabilitiesHeading}" heading`);
	}

	const capabilities = new Map();
	let current;
	for (const line of lines.slice(headingIndex + 1)) {
		if (line.startsWith("## ")) break;
		const entry = line.match(/^- `([^`]+)`:\s*(.*)$/);
		if (entry) {
			const [, name, description] = entry;
			if (capabilities.has(name)) {
				fail(`${repositoryPath(capabilitiesPath)}: duplicate entry for \`${name}\``);
			}
			current = { name, parts: [description.trim()] };
			capabilities.set(name, current);
			continue;
		}
		if (current && line.startsWith("  ") && line.trim() !== "") {
			current.parts.push(line.trim());
			continue;
		}
		current = undefined;
	}

	if (capabilities.size === 0) {
		fail(`${repositoryPath(capabilitiesPath)}: no capability entries found under "${capabilitiesHeading}"`);
	}
	return new Map([...capabilities].map(([name, entry]) => [name, entry.parts.join(" ").trim()]));
}

const skillNames = readSkillNames();
const capabilities = readCapabilities();

// CAPABILITIES.md is the source of truth for descriptions, but it is hand-written
// too. If it has fallen behind the directory, regenerating the reference from it
// would only launder the drift into a second file.
const missing = skillNames.filter((name) => !capabilities.has(name));
const extra = [...capabilities.keys()].filter((name) => !skillNames.includes(name));
if (missing.length > 0 || extra.length > 0) {
	for (const name of missing) {
		console.error(`${repositoryPath(capabilitiesPath)}: missing an entry for \`${name}\` (forge/skills/${name}/ exists)`);
	}
	for (const name of extra) {
		console.error(`${repositoryPath(capabilitiesPath)}: lists \`${name}\`, but forge/skills/${name}/ does not exist`);
	}
	fail(`Add or remove the entries above in ${repositoryPath(capabilitiesPath)}, then re-run.`);
}

const workflowCount = skillNames.length;
const block = [
	startMarker,
	"",
	`The live skill inventory currently contains ${workflowCount} capability ${workflowCount === 1 ? "workflow" : "workflows"}, listed`,
	"here with the one-line descriptions maintained in `forge/CAPABILITIES.md`:",
	"",
	...skillNames.map((name) => `- \`${name}\`: ${capabilities.get(name)}`),
	"",
	"This block is generated. After adding, renaming, or re-describing a skill,",
	"update `forge/CAPABILITIES.md` and run:",
	"",
	"```bash",
	"npm run forge:sync-reference",
	"```",
	"",
	endMarker,
].join("\n");

if (!existsSync(referencePath)) {
	fail(`${repositoryPath(referencePath)} does not exist`);
}
const reference = readFileSync(referencePath, "utf8");
const startIndex = reference.indexOf(startMarker);
const endIndex = reference.indexOf(endMarker);
if (startIndex === -1 || endIndex === -1 || endIndex < startIndex) {
	fail(`${repositoryPath(referencePath)}: missing the ${startMarker} / ${endMarker} markers`);
}

const updated = reference.slice(0, startIndex) + block + reference.slice(endIndex + endMarker.length);

if (checkOnly) {
	if (updated !== reference) {
		fail(`${repositoryPath(referencePath)} is stale. Run: npm run forge:sync-reference`);
	}
	console.log(`${repositoryPath(referencePath)} is up to date: ${workflowCount} skills.`);
} else if (updated === reference) {
	console.log(`${repositoryPath(referencePath)} already up to date: ${workflowCount} skills.`);
} else {
	writeFileSync(referencePath, updated, "utf8");
	console.log(`Updated ${repositoryPath(referencePath)}: ${workflowCount} skills.`);
}

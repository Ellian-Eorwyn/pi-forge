#!/usr/bin/env node

import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const SKILL_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const NAME_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const MAX_NAME_LENGTH = 64;
const MAX_DESCRIPTION_LENGTH = 1024;
const TARGETS = new Set(["project", "user", "forge", "path"]);
const RESOURCE_NAMES = new Set(["scripts", "references", "assets", "tests"]);
const VAGUE_DESCRIPTION = /\b(helps?|helper|general|misc|utilities|tools?|stuff|various)\b/i;
const ROUTING_LANGUAGE = /\b(use when|use for|when the user|requests?|tasks?|files?|urls?|repositories|datasets|skill|SKILL\.md)\b/i;
const EXCLUSION_LANGUAGE = /\b(do not use|don't use|unless|exclude|not for|avoid using)\b/i;
const BROAD_TERMS = new Set([
	"research",
	"document",
	"documents",
	"files",
	"coding",
	"code",
	"web",
	"data",
	"analysis",
	"reports",
	"build",
	"create",
	"process",
]);

function fail(message, exitCode = 1) {
	process.stderr.write(`Error: ${message}\n`);
	process.exit(exitCode);
}

function usage() {
	process.stdout.write(`Usage:
  skill-builder.mjs doctor [--json]
  skill-builder.mjs inventory --root <dir> [--json]
  skill-builder.mjs scaffold <name> --target project|user|forge|path --root <dir> [--resources scripts,references,assets,tests] [--json]
  skill-builder.mjs validate <skill-dir> [--strict] [--json]
  skill-builder.mjs check-triggers <skill-dir> --against <skills-root> [--json]
`);
}

function writeJson(value) {
	process.stdout.write(`${JSON.stringify(value, null, 2)}\n`);
}

function consumeOption(args, name) {
	const index = args.indexOf(name);
	if (index === -1) return undefined;
	const value = args[index + 1];
	if (!value || value.startsWith("--")) fail(`${name} requires a value`, 2);
	args.splice(index, 2);
	return value;
}

function consumeFlag(args, name) {
	const index = args.indexOf(name);
	if (index === -1) return false;
	args.splice(index, 1);
	return true;
}

function assertNoExtraArgs(args) {
	if (args.length > 0) fail(`unexpected argument: ${args[0]}`, 2);
}

function validateName(name) {
	const errors = [];
	if (!name) errors.push("name is required");
	if (name.length > MAX_NAME_LENGTH) errors.push(`name exceeds ${MAX_NAME_LENGTH} characters`);
	if (!NAME_PATTERN.test(name)) {
		errors.push("name must use lowercase letters, digits, and single hyphens without leading or trailing hyphens");
	}
	return errors;
}

function parseFrontmatter(content) {
	if (!content.startsWith("---\n")) {
		throw new Error("SKILL.md must begin with YAML frontmatter");
	}
	const end = content.indexOf("\n---", 4);
	if (end === -1) {
		throw new Error("SKILL.md frontmatter is not closed");
	}
	const raw = content.slice(4, end);
	const frontmatter = {};
	for (const line of raw.split(/\r?\n/)) {
		const trimmed = line.trim();
		if (!trimmed || trimmed.startsWith("#")) continue;
		const match = trimmed.match(/^([A-Za-z0-9_-]+):\s*(.*)$/);
		if (!match) {
			throw new Error(`invalid frontmatter line: ${line}`);
		}
		let value = match[2].trim();
		if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
			value = value.slice(1, -1);
		}
		frontmatter[match[1]] = value;
	}
	const bodyStart = content.indexOf("\n", end + 1);
	return { frontmatter, body: bodyStart === -1 ? "" : content.slice(bodyStart + 1) };
}

function pathIsInside(parent, candidate) {
	const relativePath = relative(parent, candidate);
	return relativePath === "" || (relativePath !== ".." && !relativePath.startsWith(`..${sep}`) && !isAbsolute(relativePath));
}

function skillFilePath(skillDirectory) {
	return join(skillDirectory, "SKILL.md");
}

function readSkill(skillDirectory) {
	const filePath = skillFilePath(skillDirectory);
	if (!existsSync(filePath)) {
		throw new Error("missing SKILL.md");
	}
	const raw = readFileSync(filePath, "utf8");
	return { filePath, raw, ...parseFrontmatter(raw) };
}

function tokenize(value) {
	return new Set(
		value
			.toLowerCase()
			.replace(/[^a-z0-9 -]+/g, " ")
			.split(/\s+/)
			.filter((word) => word.length > 3 && !["when", "with", "from", "that", "this", "into", "user", "users"].includes(word)),
	);
}

function markdownLinks(content) {
	const links = [];
	const pattern = /!?\[[^\]]*]\(([^)\s]+)(?:\s+[^)]*)?\)/g;
	for (const match of content.matchAll(pattern)) {
		links.push(match[1]);
	}
	return links;
}

function isExternalReference(target) {
	return /^(?:[a-z][a-z0-9+.-]*:|#)/i.test(target);
}

function normalizeLocalReference(target) {
	const withoutAnchor = target.split("#", 1)[0];
	const withoutQuery = withoutAnchor.split("?", 1)[0];
	return decodeURIComponent(withoutQuery);
}

function validateReferences(skillDirectory, raw) {
	const errors = [];
	const warnings = [];
	for (const target of markdownLinks(raw)) {
		if (isExternalReference(target)) continue;
		const reference = normalizeLocalReference(target);
		if (!reference) continue;
		const resolved = resolve(skillDirectory, reference);
		if (!pathIsInside(skillDirectory, resolved)) {
			errors.push(`reference escapes skill directory: ${target}`);
			continue;
		}
		if (!existsSync(resolved)) {
			errors.push(`referenced file does not exist: ${target}`);
		}
	}

	const referencesDirectory = join(skillDirectory, "references");
	if (existsSync(referencesDirectory)) {
		for (const filePath of findFiles(referencesDirectory, (path) => path.endsWith(".md"))) {
			const rel = relative(skillDirectory, filePath).split(sep).join("/");
			for (const target of markdownLinks(readFileSync(filePath, "utf8"))) {
				if (isExternalReference(target)) continue;
				const reference = normalizeLocalReference(target);
				if (!reference) continue;
				const resolved = resolve(dirname(filePath), reference);
				const resolvedRel = relative(skillDirectory, resolved).split(sep).join("/");
				if (resolvedRel.startsWith("references/") && resolvedRel.split("/").length > 2) {
					warnings.push(`${rel}: deep reference chain points to ${resolvedRel}`);
				}
			}
		}
	}
	return { errors, warnings };
}

function validateForgeManifest(skillDirectory, skillName) {
	const manifestPath = join(skillDirectory, "manifest.json");
	if (!existsSync(manifestPath)) return { errors: [], warnings: [] };
	const errors = [];
	const warnings = [];
	let manifest;
	try {
		manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
	} catch (error) {
		errors.push(`invalid Forge manifest JSON: ${error.message}`);
		return { errors, warnings };
	}
	if (!manifest || typeof manifest !== "object" || Array.isArray(manifest)) {
		errors.push("invalid Forge manifest: manifest must be an object");
		return { errors, warnings };
	}
	if (manifest.name !== skillName) errors.push("invalid Forge manifest: name must match skill name");
	if (manifest.type !== "pi-forge-skill") errors.push("invalid Forge manifest: type must be pi-forge-skill");
	if (manifest.skill_file !== "SKILL.md") errors.push("invalid Forge manifest: skill_file must be SKILL.md");
	if (!Array.isArray(manifest.tools)) {
		errors.push("invalid Forge manifest: tools must be an array");
	} else {
		for (const [index, tool] of manifest.tools.entries()) {
			if (!tool || typeof tool !== "object" || Array.isArray(tool)) {
				errors.push(`invalid Forge manifest: tools[${index}] must be an object`);
				continue;
			}
			for (const field of ["name", "description", "command"]) {
				if (typeof tool[field] !== "string" || tool[field].trim() === "") {
					errors.push(`invalid Forge manifest: tools[${index}].${field} is required`);
				}
			}
			if (typeof tool.destructive !== "boolean") {
				errors.push(`invalid Forge manifest: tools[${index}].destructive must be boolean`);
			}
			if (typeof tool.command === "string" && tool.command.trim()) {
				const [program, scriptPath] = tool.command.trim().split(/\s+/, 2);
				const localPath = ["node", "python", "python3"].includes(program) ? scriptPath : program;
				if (localPath) {
					const resolved = resolve(skillDirectory, localPath);
					if (!pathIsInside(skillDirectory, resolved)) {
						errors.push(`invalid Forge manifest: tools[${index}].command escapes skill directory`);
					} else if (!existsSync(resolved)) {
						errors.push(`invalid Forge manifest: tools[${index}].command script is missing`);
					}
				}
			}
		}
	}
	if (!manifest.safety || typeof manifest.safety !== "object" || Array.isArray(manifest.safety)) {
		errors.push("invalid Forge manifest: safety object is required");
	} else {
		for (const field of ["destructive_by_default", "requires_review_for_filesystem_changes", "preserve_provenance_when_applicable"]) {
			if (typeof manifest.safety[field] !== "boolean") {
				errors.push(`invalid Forge manifest: safety.${field} must be boolean`);
			}
		}
	}
	for (const field of ["scripts_dir", "references_dir", "assets_dir", "schemas_dir"]) {
		if (manifest[field] && !existsSync(join(skillDirectory, manifest[field]))) {
			warnings.push(`Forge manifest ${field} points to missing path: ${manifest[field]}`);
		}
	}
	return { errors, warnings };
}

function validateTriggerFile(skillDirectory) {
	const triggerPath = join(skillDirectory, "tests", "triggers.json");
	if (!existsSync(triggerPath)) return { errors: [], warnings: ["tests/triggers.json is missing"] };
	try {
		const parsed = JSON.parse(readFileSync(triggerPath, "utf8"));
		const positive = parsed.positive ?? parsed.positives;
		const negative = parsed.negative ?? parsed.negatives;
		const errors = [];
		if (!Array.isArray(positive) || positive.length === 0) {
			errors.push("tests/triggers.json must include a non-empty positive array");
		}
		if (!Array.isArray(negative) || negative.length === 0) {
			errors.push("tests/triggers.json must include a non-empty negative array");
		}
		return { errors, warnings: [] };
	} catch (error) {
		return { errors: [`invalid tests/triggers.json: ${error.message}`], warnings: [] };
	}
}

function validateSkillDirectory(skillDirectory, options = {}) {
	const errors = [];
	const warnings = [];
	let name = null;
	let description = "";
	let body = "";
	let raw = "";
	try {
		const skill = readSkill(skillDirectory);
		raw = skill.raw;
		body = skill.body;
		name = skill.frontmatter.name;
		description = skill.frontmatter.description ?? "";
	} catch (error) {
		errors.push(error.message);
		return { valid: false, errors, warnings, skillDirectory, name };
	}

	if (!name || String(name).trim() === "") {
		errors.push("name is required");
	} else {
		errors.push(...validateName(String(name)));
		if (name !== basename(skillDirectory)) {
			errors.push(`name "${name}" must match directory "${basename(skillDirectory)}"`);
		}
	}
	if (!description || String(description).trim() === "") {
		errors.push("description is required");
	} else if (description.length > MAX_DESCRIPTION_LENGTH) {
		errors.push(`description exceeds ${MAX_DESCRIPTION_LENGTH} characters`);
	}

	if (description && (description.length < 40 || VAGUE_DESCRIPTION.test(description))) {
		warnings.push("description is vague; explain what the skill does and when to use it");
	}
	if (description && !ROUTING_LANGUAGE.test(description)) {
		warnings.push("description is missing clear trigger language");
	}
	if (description && broadTermsIntersect(description).length > 0 && !EXCLUSION_LANGUAGE.test(description)) {
		warnings.push("description may overlap neighboring skills; add exclusions when relevant");
	}

	const lines = raw.split(/\r?\n/).length;
	if (lines > 500) warnings.push(`SKILL.md is long (${lines} lines); move details into references`);
	if (Math.ceil(raw.length / 4) > 5000) warnings.push("SKILL.md is likely over 5,000 tokens");

	const references = validateReferences(skillDirectory, raw);
	errors.push(...references.errors);
	warnings.push(...references.warnings);

	const triggerValidation = validateTriggerFile(skillDirectory);
	errors.push(...triggerValidation.errors);
	warnings.push(...triggerValidation.warnings);

	const forgeManifest = validateForgeManifest(skillDirectory, name);
	errors.push(...forgeManifest.errors);
	warnings.push(...forgeManifest.warnings);

	if (!/^#\s+/m.test(body)) {
		warnings.push("SKILL.md body should include a title heading");
	}

	const valid = errors.length === 0 && (!options.strict || warnings.length === 0);
	return { valid, errors, warnings, skillDirectory, name, description };
}

function broadTermsIntersect(description) {
	const terms = tokenize(description);
	return [...terms].filter((term) => BROAD_TERMS.has(term));
}

function findFiles(root, predicate) {
	const files = [];
	const stack = [root];
	while (stack.length > 0) {
		const current = stack.pop();
		let entries;
		try {
			entries = readdirSync(current, { withFileTypes: true });
		} catch {
			continue;
		}
		for (const entry of entries) {
			if (entry.name === "node_modules" || entry.name === ".git") continue;
			const fullPath = join(current, entry.name);
			if (entry.isDirectory()) {
				stack.push(fullPath);
			} else if (entry.isFile() && predicate(fullPath)) {
				files.push(fullPath);
			}
		}
	}
	return files.sort();
}

function findSkillDirectories(root) {
	const directories = [];
	if (!existsSync(root)) return directories;
	const stack = [root];
	while (stack.length > 0) {
		const current = stack.pop();
		if (existsSync(skillFilePath(current))) {
			directories.push(current);
			continue;
		}
		let entries;
		try {
			entries = readdirSync(current, { withFileTypes: true });
		} catch {
			continue;
		}
		for (const entry of entries) {
			if (entry.name.startsWith(".") || entry.name === "node_modules") continue;
			if (entry.isDirectory()) stack.push(join(current, entry.name));
		}
	}
	return directories.sort();
}

function inventory(root) {
	return findSkillDirectories(root).map((directory) => {
		try {
			const skill = readSkill(directory);
			return {
				name: skill.frontmatter.name ?? basename(directory),
				description: skill.frontmatter.description ?? "",
				directory,
				skillFile: skill.filePath,
			};
		} catch (error) {
			return {
				name: basename(directory),
				description: "",
				directory,
				skillFile: skillFilePath(directory),
				error: error.message,
			};
		}
	});
}

function parseResources(value) {
	if (!value) return [];
	const resources = value
		.split(",")
		.map((item) => item.trim())
		.filter(Boolean);
	for (const resource of resources) {
		if (!RESOURCE_NAMES.has(resource)) fail(`unknown resource directory: ${resource}`, 2);
	}
	return [...new Set(resources)];
}

function targetSkillsRoot(target, rootValue) {
	if (!TARGETS.has(target)) fail(`target must be one of ${[...TARGETS].join(", ")}`, 2);
	const root = resolve(rootValue ?? (target === "user" ? homedir() : process.cwd()));
	if (target === "project") return join(root, ".agents", "skills");
	if (target === "user") return join(root, ".agents", "skills");
	if (target === "path") return root;
	if (basename(root) === "forge") return join(root, "skills");
	return join(root, "forge", "skills");
}

function titleize(name) {
	return name
		.split("-")
		.map((part) => `${part.slice(0, 1).toUpperCase()}${part.slice(1)}`)
		.join(" ");
}

function scaffold(args) {
	const name = args.shift();
	if (!name) fail("scaffold requires a skill name", 2);
	const target = consumeOption(args, "--target") ?? "project";
	const rootValue = consumeOption(args, "--root");
	const resources = parseResources(consumeOption(args, "--resources"));
	const asJson = consumeFlag(args, "--json");
	assertNoExtraArgs(args);

	const nameErrors = validateName(name);
	if (nameErrors.length > 0) fail(nameErrors.join("; "), 2);

	const skillsRoot = targetSkillsRoot(target, rootValue);
	const skillDirectory = join(skillsRoot, name);
	if (existsSync(skillDirectory)) fail(`skill already exists: ${skillDirectory}`);
	mkdirSync(skillDirectory, { recursive: true });
	for (const resource of resources) {
		mkdirSync(join(skillDirectory, resource), { recursive: true });
	}

	const displayName = titleize(name);
	writeFileSync(
		skillFilePath(skillDirectory),
		`---\nname: ${name}\ndescription: ${displayName} workflow. Use when the user asks for ${name.replaceAll("-", " ")} work, related files, trigger examples, validation, or reusable task guidance. Do not use for unrelated tasks.\n---\n\n# ${displayName}\n\n## Workflow\n\n1. Inspect the user's request and available inputs.\n2. Choose the appropriate workflow branch.\n3. Load only the references needed for that branch.\n4. Use bundled scripts for deterministic operations when available.\n5. Validate the result before completion.\n\n## Output Contract\n\nReport the files created or changed, validation performed, and any unresolved assumptions.\n`,
	);

	if (resources.includes("tests")) {
		writeFileSync(
			join(skillDirectory, "tests", "triggers.json"),
			`${JSON.stringify(
				{
					positive: [`Use ${displayName} to complete this workflow.`],
					negative: ["Summarize this unrelated paragraph."],
				},
				null,
				2,
			)}\n`,
		);
	}

	if (target === "forge") {
		mkdirSync(join(skillDirectory, "agents"), { recursive: true });
		writeFileSync(
			join(skillDirectory, "agents", "openai.yaml"),
			`interface:\n  display_name: "${displayName}"\n  short_description: "${displayName} workflow"\n  default_prompt: "Use $${name} to run this workflow."\n`,
		);
		writeFileSync(
			join(skillDirectory, "manifest.json"),
			`${JSON.stringify(
				{
					name,
					type: "pi-forge-skill",
					version: "0.1.0",
					description: `${displayName} workflow.`,
					skill_file: "SKILL.md",
					tools: [],
					safety: {
						destructive_by_default: false,
						requires_review_for_filesystem_changes: true,
						preserve_provenance_when_applicable: true,
					},
				},
				null,
				"\t",
			)}\n`,
		);
	}

	const result = {
		status: "ok",
		target,
		skillsRoot,
		skillDirectory,
		skillFile: skillFilePath(skillDirectory),
		resources,
	};
	if (asJson) {
		writeJson(result);
	} else {
		process.stdout.write(`Created ${skillDirectory}\n`);
	}
}

function commandDoctor(args) {
	const asJson = consumeFlag(args, "--json");
	assertNoExtraArgs(args);
	const report = {
		status: "ok",
		node: process.version,
		skillRoot: SKILL_ROOT,
		defaults: {
			project: "<project>/.agents/skills",
			user: "~/.agents/skills",
			forge: "forge/skills",
		},
	};
	if (asJson) {
		writeJson(report);
		return;
	}
	process.stdout.write(`Skill builder OK (${process.version})\n`);
	process.stdout.write(`Skill root: ${SKILL_ROOT}\n`);
	process.stdout.write("Defaults: project .agents/skills, user ~/.agents/skills, Forge forge/skills\n");
}

function commandInventory(args) {
	const root = consumeOption(args, "--root");
	const asJson = consumeFlag(args, "--json");
	assertNoExtraArgs(args);
	if (!root) fail("inventory requires --root", 2);
	const skills = inventory(resolve(root));
	if (asJson) {
		writeJson({ status: "ok", root: resolve(root), skills });
		return;
	}
	for (const skill of skills) {
		process.stdout.write(`${skill.name}: ${skill.skillFile}\n`);
	}
}

function commandValidate(args) {
	const skillDirectory = args.shift();
	if (!skillDirectory) fail("validate requires <skill-dir>", 2);
	const strict = consumeFlag(args, "--strict");
	const asJson = consumeFlag(args, "--json");
	assertNoExtraArgs(args);
	const report = validateSkillDirectory(resolve(skillDirectory), { strict });
	if (asJson) {
		writeJson(report);
	} else if (report.valid) {
		process.stdout.write(`Valid skill: ${report.skillDirectory}\n`);
		if (report.warnings.length > 0) process.stdout.write(`Warnings:\n- ${report.warnings.join("\n- ")}\n`);
	} else {
		for (const error of report.errors) process.stderr.write(`Error: ${error}\n`);
		for (const warning of report.warnings) process.stderr.write(`Warning: ${warning}\n`);
	}
	if (!report.valid) process.exit(1);
}

function readTriggers(skillDirectory) {
	const triggerPath = join(skillDirectory, "tests", "triggers.json");
	if (!existsSync(triggerPath)) {
		return { positive: [], negative: [], warnings: ["tests/triggers.json is missing"], errors: [] };
	}
	try {
		const parsed = JSON.parse(readFileSync(triggerPath, "utf8"));
		return {
			positive: parsed.positive ?? parsed.positives ?? [],
			negative: parsed.negative ?? parsed.negatives ?? [],
			warnings: [],
			errors: [],
		};
	} catch (error) {
		return { positive: [], negative: [], warnings: [], errors: [`invalid tests/triggers.json: ${error.message}`] };
	}
}

function commandCheckTriggers(args) {
	const skillDirectory = args.shift();
	if (!skillDirectory) fail("check-triggers requires <skill-dir>", 2);
	const against = consumeOption(args, "--against");
	const asJson = consumeFlag(args, "--json");
	assertNoExtraArgs(args);
	if (!against) fail("check-triggers requires --against", 2);
	const resolvedSkillDirectory = resolve(skillDirectory);
	const validation = validateSkillDirectory(resolvedSkillDirectory);
	const triggers = readTriggers(resolvedSkillDirectory);
	const warnings = [...validation.warnings, ...triggers.warnings];
	const errors = [...validation.errors, ...triggers.errors];

	const sourceTerms = tokenize(validation.description ?? "");
	const broadDescription = validation.description && (validation.description.length < 40 || VAGUE_DESCRIPTION.test(validation.description));
	const minimumOverlapScore = broadDescription ? 1 : 3;
	const overlaps = inventory(resolve(against))
		.filter((skill) => resolve(skill.directory) !== resolvedSkillDirectory)
		.map((skill) => {
			const terms = tokenize(skill.description);
			const shared = [...sourceTerms].filter((term) => terms.has(term));
			return { name: skill.name, skillFile: skill.skillFile, sharedTerms: shared, score: shared.length };
		})
		.filter((overlap) => overlap.score >= minimumOverlapScore)
		.sort((left, right) => right.score - left.score);
	if (overlaps.length > 0) {
		warnings.push(`description overlaps neighboring skills: ${overlaps.map((overlap) => overlap.name).join(", ")}`);
	}
	if (broadDescription) {
		warnings.push("description is broad or vague");
	}
	if (!Array.isArray(triggers.positive) || triggers.positive.length === 0) {
		errors.push("positive trigger examples are required");
	}
	if (!Array.isArray(triggers.negative) || triggers.negative.length === 0) {
		errors.push("negative trigger examples are required");
	}

	const report = {
		valid: errors.length === 0,
		errors,
		warnings: [...new Set(warnings)],
		triggers: {
			positive: triggers.positive,
			negative: triggers.negative,
		},
		overlaps,
	};
	if (asJson) {
		writeJson(report);
	} else {
		process.stdout.write(`Positive triggers: ${triggers.positive.length}\n`);
		process.stdout.write(`Negative triggers: ${triggers.negative.length}\n`);
		if (overlaps.length > 0) process.stdout.write(`Overlaps: ${overlaps.map((overlap) => overlap.name).join(", ")}\n`);
		if (report.warnings.length > 0) process.stdout.write(`Warnings:\n- ${report.warnings.join("\n- ")}\n`);
	}
	if (!report.valid) process.exit(1);
}

const args = process.argv.slice(2);
const command = args.shift();
if (!command || command === "--help" || command === "-h") {
	usage();
	process.exit(command ? 0 : 2);
}

switch (command) {
	case "doctor":
		commandDoctor(args);
		break;
	case "inventory":
		commandInventory(args);
		break;
	case "scaffold":
		scaffold(args);
		break;
	case "validate":
		commandValidate(args);
		break;
	case "check-triggers":
		commandCheckTriggers(args);
		break;
	default:
		fail(`unknown command: ${command}`, 2);
}

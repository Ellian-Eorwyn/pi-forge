#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { basename, join, resolve } from "node:path";

const REQUIRED_SUMMARY_HEADINGS = ["## Summary", "## Motivation", "## Files changed", "## Verification"];
const IGNORED_DIRECTORIES = new Set([
	".git",
	"node_modules",
	".venv",
	"venv",
	"dist",
	"build",
	"target",
	".next",
	"__pycache__",
	".mypy_cache",
	".pytest_cache",
	"coverage",
	".idea",
	".vscode",
]);
const LANGUAGE_BY_EXTENSION = new Map([
	[".ts", "TypeScript"],
	[".tsx", "TypeScript"],
	[".js", "JavaScript"],
	[".jsx", "JavaScript"],
	[".mjs", "JavaScript"],
	[".cjs", "JavaScript"],
	[".py", "Python"],
	[".rb", "Ruby"],
	[".go", "Go"],
	[".rs", "Rust"],
	[".java", "Java"],
	[".kt", "Kotlin"],
	[".c", "C"],
	[".h", "C"],
	[".cc", "C++"],
	[".cpp", "C++"],
	[".hpp", "C++"],
	[".cs", "C#"],
	[".php", "PHP"],
	[".swift", "Swift"],
	[".sh", "Shell"],
	[".bash", "Shell"],
]);

function fail(message, exitCode = 1) {
	process.stderr.write(`Error: ${message}\n`);
	process.exit(exitCode);
}

function warn(message) {
	process.stderr.write(`Warning: ${message}\n`);
}

function run(command, args, options = {}) {
	return spawnSync(command, args, { encoding: "utf8", maxBuffer: 64 * 1024 * 1024, ...options });
}

function toolInfo(command, args = ["--version"]) {
	const result = run(command, args);
	if (result.error?.code === "ENOENT") return { available: false, version: null };
	if (result.error || result.status !== 0) return { available: false, version: null };
	const combined = `${result.stdout}\n${result.stderr}`.trim();
	return { available: true, version: combined.split(/\r?\n/, 1)[0] || "available" };
}

function readJson(path) {
	try {
		return JSON.parse(readFileSync(path, "utf8"));
	} catch {
		return null;
	}
}

function requireOption(args, name) {
	const index = args.indexOf(name);
	if (index === -1) return undefined;
	const value = args[index + 1];
	if (value === undefined || value.startsWith("--")) fail(`${name} requires a value`);
	args.splice(index, 2);
	return value;
}

// --- doctor -----------------------------------------------------------------

function detectToolchain() {
	return {
		git: toolInfo("git"),
		node: toolInfo("node"),
		npm: toolInfo("npm"),
		pnpm: toolInfo("pnpm"),
		yarn: toolInfo("yarn"),
		bun: toolInfo("bun", ["--version"]),
		python3: toolInfo("python3"),
		pip3: toolInfo("pip3", ["--version"]),
		make: toolInfo("make", ["--version"]),
	};
}

function doctor(args) {
	const asJson = args.includes("--json");
	const tools = detectToolchain();
	if (asJson) {
		process.stdout.write(`${JSON.stringify({ tools }, undefined, "\t")}\n`);
		return;
	}
	process.stdout.write("Coding skill toolchain:\n");
	for (const [name, info] of Object.entries(tools)) {
		const status = info.available ? info.version : "not found";
		process.stdout.write(`  ${name.padEnd(8)} ${status}\n`);
	}
	const missing = Object.entries(tools)
		.filter(([, info]) => !info.available)
		.map(([name]) => name);
	if (missing.length > 0) {
		process.stdout.write(
			`\nMissing: ${missing.join(", ")}. Install only what the target repository needs; this skill does not install system packages.\n`,
		);
	}
}

// --- inspect ----------------------------------------------------------------

function walkRepository(root) {
	const extensionCounts = new Map();
	const configFiles = new Set();
	const stack = [root];
	const configNames = new Set([
		"tsconfig.json",
		".eslintrc",
		".eslintrc.js",
		".eslintrc.cjs",
		".eslintrc.json",
		"eslint.config.js",
		"eslint.config.mjs",
		".prettierrc",
		".prettierrc.json",
		"prettier.config.js",
		".editorconfig",
		"ruff.toml",
		".ruff.toml",
		"setup.cfg",
		"tox.ini",
		"pytest.ini",
		"jest.config.js",
		"jest.config.ts",
		"vitest.config.ts",
		"vitest.config.js",
		".flake8",
		".gitignore",
	]);
	while (stack.length > 0) {
		const current = stack.pop();
		let entries;
		try {
			entries = readdirSync(current, { withFileTypes: true });
		} catch {
			continue;
		}
		for (const entry of entries) {
			if (entry.isSymbolicLink()) continue;
			const fullPath = join(current, entry.name);
			if (entry.isDirectory()) {
				if (IGNORED_DIRECTORIES.has(entry.name) || entry.name.startsWith(".")) continue;
				stack.push(fullPath);
				continue;
			}
			if (!entry.isFile()) continue;
			if (configNames.has(entry.name)) configFiles.add(entry.name);
			const dotIndex = entry.name.lastIndexOf(".");
			if (dotIndex > 0) {
				const extension = entry.name.slice(dotIndex).toLowerCase();
				if (LANGUAGE_BY_EXTENSION.has(extension)) {
					extensionCounts.set(extension, (extensionCounts.get(extension) ?? 0) + 1);
				}
			}
		}
	}
	return { extensionCounts, configFiles };
}

function detectLanguages(extensionCounts) {
	const byLanguage = new Map();
	for (const [extension, count] of extensionCounts) {
		const language = LANGUAGE_BY_EXTENSION.get(extension);
		byLanguage.set(language, (byLanguage.get(language) ?? 0) + count);
	}
	return [...byLanguage.entries()]
		.sort((left, right) => right[1] - left[1])
		.map(([language, fileCount]) => ({ language, fileCount }));
}

function detectPackageManager(root) {
	if (existsSync(join(root, "pnpm-lock.yaml"))) return "pnpm";
	if (existsSync(join(root, "yarn.lock"))) return "yarn";
	if (existsSync(join(root, "bun.lockb"))) return "bun";
	if (existsSync(join(root, "package-lock.json"))) return "npm";
	if (existsSync(join(root, "package.json"))) return "npm (no lockfile)";
	return null;
}

function detectBuildSystems(root) {
	const found = [];
	const candidates = [
		["package.json", "Node.js / npm scripts"],
		["pyproject.toml", "Python (pyproject)"],
		["setup.py", "Python (setuptools)"],
		["requirements.txt", "Python (requirements.txt)"],
		["Cargo.toml", "Rust / Cargo"],
		["go.mod", "Go modules"],
		["Makefile", "Make"],
		["pom.xml", "Maven"],
		["build.gradle", "Gradle"],
		["Gemfile", "Ruby / Bundler"],
	];
	for (const [file, label] of candidates) {
		if (existsSync(join(root, file))) found.push({ file, label });
	}
	return found;
}

function detectNpmScripts(root) {
	const manifest = readJson(join(root, "package.json"));
	if (!manifest || typeof manifest.scripts !== "object" || manifest.scripts === null) return {};
	return manifest.scripts;
}

function classifyCommands(scripts) {
	const test = [];
	const lint = [];
	const build = [];
	const typecheck = [];
	for (const name of Object.keys(scripts)) {
		const lowered = name.toLowerCase();
		if (lowered.includes("test")) test.push(`npm run ${name}`);
		if (lowered.includes("lint")) lint.push(`npm run ${name}`);
		if (lowered.includes("build")) build.push(`npm run ${name}`);
		if (lowered.includes("typecheck") || lowered.includes("type-check") || lowered === "tsc") {
			typecheck.push(`npm run ${name}`);
		}
	}
	return { test, lint, build, typecheck };
}

function gitSnapshot(root) {
	const insideResult = run("git", ["-C", root, "rev-parse", "--is-inside-work-tree"]);
	if (insideResult.status !== 0 || insideResult.stdout.trim() !== "true") {
		return { repository: false, branch: null, head: null, dirtyFileCount: 0, status: [] };
	}
	const branch = run("git", ["-C", root, "rev-parse", "--abbrev-ref", "HEAD"]).stdout.trim() || null;
	const head = run("git", ["-C", root, "rev-parse", "HEAD"]).stdout.trim() || null;
	const porcelain = run("git", ["-C", root, "status", "--porcelain"]).stdout;
	const status = porcelain.split(/\r?\n/).filter((line) => line.length > 0);
	return { repository: true, branch, head, dirtyFileCount: status.length, status };
}

function buildProfileMarkdown(profile) {
	const lines = ["# Repository Profile", ""];
	lines.push(`- Repository: \`${profile.repository}\``);
	lines.push(`- Inspected: ${profile.inspectedAt}`);
	lines.push("");
	lines.push("## Languages", "");
	if (profile.languages.length === 0) {
		lines.push("No recognized source files detected.");
	} else {
		lines.push("| Language | Files |", "|---|---:|");
		for (const entry of profile.languages) lines.push(`| ${entry.language} | ${entry.fileCount} |`);
	}
	lines.push("", "## Build and Package Management", "");
	lines.push(`- Package manager: ${profile.packageManager ?? "none detected"}`);
	if (profile.buildSystems.length === 0) {
		lines.push("- Build systems: none detected");
	} else {
		lines.push("- Build systems:");
		for (const entry of profile.buildSystems) lines.push(`  - ${entry.label} (\`${entry.file}\`)`);
	}
	lines.push("", "## Suggested Commands", "");
	const labelByKey = { test: "Test", lint: "Lint", typecheck: "Type check", build: "Build" };
	let anyCommand = false;
	for (const key of ["test", "lint", "typecheck", "build"]) {
		const commands = profile.commands[key];
		if (commands.length > 0) {
			anyCommand = true;
			lines.push(`- ${labelByKey[key]}: ${commands.map((command) => `\`${command}\``).join(", ")}`);
		}
	}
	if (!anyCommand) lines.push("No npm scripts detected. Inspect build files manually before running checks.");
	lines.push("", "## Convention Signals", "");
	if (profile.configFiles.length === 0) {
		lines.push("No common config files detected.");
	} else {
		for (const file of profile.configFiles) lines.push(`- \`${file}\``);
	}
	lines.push("", "## Git State", "");
	if (!profile.git.repository) {
		lines.push("Not a git work tree. There is no version-control safety net for edits here.");
	} else {
		lines.push(`- Branch: ${profile.git.branch ?? "(detached)"}`);
		lines.push(`- HEAD: ${profile.git.head ?? "(unknown)"}`);
		lines.push(`- Uncommitted changes: ${profile.git.dirtyFileCount}`);
		if (profile.git.dirtyFileCount > 0) {
			lines.push("- Working tree is dirty before edits. Confirm with the user which changes are theirs.");
		}
	}
	lines.push("");
	return lines.join("\n");
}

function inspect(args) {
	const output = requireOption(args, "--output");
	if (!output) fail("inspect requires --output <new-directory>");
	const positional = args.filter((argument) => !argument.startsWith("--"));
	if (positional.length !== 1) fail("inspect requires exactly one <repo> path");
	const repository = resolve(positional[0]);
	if (!existsSync(repository) || !statSync(repository).isDirectory()) {
		fail(`repository path is not a directory: ${repository}`);
	}
	const outputDirectory = resolve(output);
	if (existsSync(outputDirectory)) fail(`output directory already exists: ${outputDirectory}`);

	const { extensionCounts, configFiles } = walkRepository(repository);
	const scripts = detectNpmScripts(repository);
	const profile = {
		repository,
		inspectedAt: new Date().toISOString(),
		languages: detectLanguages(extensionCounts),
		packageManager: detectPackageManager(repository),
		buildSystems: detectBuildSystems(repository),
		commands: classifyCommands(scripts),
		npmScripts: scripts,
		configFiles: [...configFiles].sort(),
		git: gitSnapshot(repository),
	};

	mkdirSync(outputDirectory, { recursive: true });
	writeFileSync(join(outputDirectory, "repo_profile.json"), `${JSON.stringify(profile, undefined, "\t")}\n`);
	writeFileSync(join(outputDirectory, "repo_profile.md"), buildProfileMarkdown(profile));

	if (profile.git.repository && profile.git.dirtyFileCount > 0) {
		warn(`repository has ${profile.git.dirtyFileCount} uncommitted change(s) before any edits`);
	}
	if (!profile.git.repository) warn("repository is not a git work tree; edits have no version-control safety net");
	process.stdout.write(
		`${JSON.stringify({
			repository,
			output: outputDirectory,
			languages: profile.languages,
			packageManager: profile.packageManager,
			git: { branch: profile.git.branch, dirtyFileCount: profile.git.dirtyFileCount },
		})}\n`,
	);
}

// --- validate ---------------------------------------------------------------

function validate(args) {
	const positional = args.filter((argument) => !argument.startsWith("--"));
	if (positional.length !== 1) fail("validate requires exactly one <output-directory>");
	const directory = resolve(positional[0]);
	if (!existsSync(directory) || !statSync(directory).isDirectory()) {
		fail(`output directory does not exist: ${directory}`);
	}

	const errors = [];
	const warnings = [];

	for (const name of ["change_summary.md", "run_log.md"]) {
		const path = join(directory, name);
		if (!existsSync(path)) {
			errors.push(`missing required artifact: ${name}`);
			continue;
		}
		if (statSync(path).size === 0) errors.push(`required artifact is empty: ${name}`);
	}

	const summaryPath = join(directory, "change_summary.md");
	if (existsSync(summaryPath)) {
		const summary = readFileSync(summaryPath, "utf8");
		for (const heading of REQUIRED_SUMMARY_HEADINGS) {
			if (!summary.includes(heading)) errors.push(`change_summary.md is missing heading: ${heading}`);
		}
		if (!summary.includes("## Follow-ups")) {
			warnings.push("change_summary.md has no '## Follow-ups & uncertainties' section");
		}
	}

	for (const warning of warnings) warn(warning);
	if (errors.length > 0) {
		for (const error of errors) process.stderr.write(`Error: ${error}\n`);
		process.exit(1);
	}
	process.stdout.write(`${JSON.stringify({ directory: basename(directory), valid: true, warnings })}\n`);
}

// --- entry ------------------------------------------------------------------

function printUsage(stream) {
	stream.write(
		[
			"Usage: coding.mjs <command> [options]",
			"",
			"Commands:",
			"  doctor [--json]                       Report the available toolchain.",
			"  inspect <repo> --output <new-dir>     Profile a repository before editing.",
			"  validate <output-dir>                 Check change_summary.md and run_log.md.",
			"",
		].join("\n"),
	);
}

const argv = process.argv.slice(2);
if (argv.length === 0 || argv[0] === "--help" || argv[0] === "-h") {
	printUsage(process.stdout);
	process.exit(0);
}

const [command, ...rest] = argv;
switch (command) {
	case "doctor":
		doctor(rest);
		break;
	case "inspect":
		inspect(rest);
		break;
	case "validate":
		validate(rest);
		break;
	default:
		printUsage(process.stderr);
		fail(`unknown command: ${command}`, 2);
}

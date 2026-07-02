import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { basename, dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { parse } from "yaml";
import { formatSkillsForPrompt, loadSkillsFromDir, type Skill } from "../packages/coding-agent/src/core/skills.ts";
import { parseFrontmatter } from "../packages/coding-agent/src/utils/frontmatter.ts";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const skillsDirectory = join(repositoryRoot, "forge", "skills");
const reportPath = join(repositoryRoot, "FORGE_SKILLS.md");
const checkOnly = process.argv.slice(2).includes("--check");
const unknownOptions = process.argv.slice(2).filter((argument) => argument !== "--check");
const skillNamePattern = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const maxSkillNameLength = 64;
const maxDescriptionLength = 1024;

if (unknownOptions.length > 0) {
	console.error(`Unknown option: ${unknownOptions[0]}`);
	process.exit(2);
}

function estimateTokens(text: string): number {
	return Math.ceil(text.length / 4);
}

function repositoryPath(path: string): string {
	return relative(repositoryRoot, path).split(sep).join("/");
}

function escapeMarkdown(value: string): string {
	return value.replace(/\|/g, "\\|").replace(/\r?\n/g, " ").trim();
}

function fallbackSummary(description: string): string {
	const firstSentence = description.match(/^.*?[.!?](?:\s|$)/)?.[0]?.trim() ?? description.trim();
	return firstSentence.length <= 96 ? firstSentence : `${firstSentence.slice(0, 93).trimEnd()}...`;
}

function loadSummary(skill: Skill): string {
	const metadataPath = join(skill.baseDir, "agents", "openai.yaml");
	if (!existsSync(metadataPath)) return fallbackSummary(skill.description);
	const metadata = parse(readFileSync(metadataPath, "utf8")) as unknown;
	if (!metadata || typeof metadata !== "object" || !("interface" in metadata)) {
		return fallbackSummary(skill.description);
	}
	const interfaceValue = metadata.interface;
	if (!interfaceValue || typeof interfaceValue !== "object" || !("short_description" in interfaceValue)) {
		return fallbackSummary(skill.description);
	}
	return typeof interfaceValue.short_description === "string"
		? interfaceValue.short_description
		: fallbackSummary(skill.description);
}

function withStablePath(skill: Skill): Skill {
	return { ...skill, filePath: repositoryPath(skill.filePath) };
}

function standardDiagnostics(skill: Skill): string[] {
	const diagnostics: string[] = [];
	const directoryName = basename(skill.baseDir);
	if (skill.name !== directoryName) {
		diagnostics.push(`${repositoryPath(skill.filePath)}: skill name "${skill.name}" must match directory "${directoryName}"`);
	}
	if (skill.name.length < 1 || skill.name.length > maxSkillNameLength) {
		diagnostics.push(`${repositoryPath(skill.filePath)}: skill name must be 1-${maxSkillNameLength} characters`);
	}
	if (!skillNamePattern.test(skill.name)) {
		diagnostics.push(`${repositoryPath(skill.filePath)}: skill name must use lowercase letters, numbers, and single hyphens`);
	}
	if (skill.description.trim().length === 0) {
		diagnostics.push(`${repositoryPath(skill.filePath)}: skill description is required`);
	}
	if (skill.description.length > maxDescriptionLength) {
		diagnostics.push(`${repositoryPath(skill.filePath)}: skill description must be at most ${maxDescriptionLength} characters`);
	}
	return diagnostics;
}

function marginalLaunchCharacters(skill: Skill): number {
	if (skill.disableModelInvocation) return 0;
	const synthetic: Skill = {
		...skill,
		name: "report-measurement-sentinel",
		description: "Synthetic entry used only to isolate skill prompt overhead.",
		filePath: "forge/skills/report-measurement-sentinel/SKILL.md",
	};
	return formatSkillsForPrompt([synthetic, skill]).length - formatSkillsForPrompt([synthetic]).length;
}

const loaded = loadSkillsFromDir({ dir: skillsDirectory, source: "forge-profile" });
if (loaded.diagnostics.length > 0) {
	for (const diagnostic of loaded.diagnostics) {
		console.error(`${diagnostic.path}: ${diagnostic.message}`);
	}
	process.exit(1);
}
const skillStandardDiagnostics = loaded.skills.flatMap(standardDiagnostics);
if (skillStandardDiagnostics.length > 0) {
	for (const diagnostic of skillStandardDiagnostics) {
		console.error(diagnostic);
	}
	process.exit(1);
}

const skills = loaded.skills.map(withStablePath).sort((left, right) => left.name.localeCompare(right.name));
if (skills.length === 0) {
	console.error(`No skills found under ${skillsDirectory}`);
	process.exit(1);
}

const prompt = formatSkillsForPrompt(skills);
const launchPromptCharacters = prompt.length;
const launchPromptTokens = estimateTokens(prompt);
const entries = skills.map((skill) => {
	const absolutePath = resolve(repositoryRoot, skill.filePath);
	const raw = readFileSync(absolutePath, "utf8");
	const { body } = parseFrontmatter(raw);
	const launchCharacters = marginalLaunchCharacters(skill);
	return {
		name: skill.name,
		summary: loadSummary({ ...skill, baseDir: dirname(absolutePath) }),
		location: skill.filePath,
		modelVisible: !skill.disableModelInvocation,
		launchCharacters,
		launchTokens: Math.ceil(launchCharacters / 4),
		bodyTokens: estimateTokens(body),
		fileTokens: estimateTokens(raw),
	};
});
const visibleEntries = entries.filter((entry) => entry.modelVisible);
const sharedCharacters = launchPromptCharacters - visibleEntries.reduce((sum, entry) => sum + entry.launchCharacters, 0);
const sharedTokens = Math.ceil(sharedCharacters / 4);
const allFilesTokens = entries.reduce((sum, entry) => sum + entry.fileTokens, 0);

// AGENTS.md is fed at launch inside buildSystemPrompt's <project_context> wrapper
// (see packages/coding-agent/src/core/system-prompt.ts). Replicate the wrapper exactly
// so the count matches what the model actually processes. The path is kept repository-
// relative for a stable, machine-independent report.
const agentsPath = join(repositoryRoot, "forge", "AGENTS.md");
const agentsRepoPath = "forge/AGENTS.md";
const agentsRaw = existsSync(agentsPath) ? readFileSync(agentsPath, "utf8") : "";
const projectContextBlock = agentsRaw
	? `\n\n<project_context>\n\nProject-specific instructions and guidelines:\n\n<project_instructions path="${agentsRepoPath}">\n${agentsRaw}\n</project_instructions>\n\n</project_context>\n`
	: "";
const agentsCharacters = projectContextBlock.length;
const agentsTokens = estimateTokens(projectContextBlock);

// Everything the forge profile feeds at launch: managed instructions + the skills menu.
const totalLaunchTokens = Math.ceil((agentsCharacters + launchPromptCharacters) / 4);
// Worst case: the launch menu stays in context and every SKILL.md is also fully read.
const maxAllLoadedTokens = totalLaunchTokens + allFilesTokens;

const lines = [
	"# Forge Skills Context Report",
	"",
	"> Generated by `npm run forge:skills-report`. Do not edit token counts manually.",
	"",
	"## Launch Context Summary",
	"",
	`- Available skills: ${entries.length}`,
	`- Model-visible skills at launch: ${visibleEntries.length}`,
	`- Managed instructions (\`AGENTS.md\` with its \`<project_context>\` wrapper): ${agentsTokens} tokens`,
	`- Skills menu (metadata for all skills): ${launchPromptTokens} tokens`,
	`- **Total forge launch context (always processed): ${totalLaunchTokens} tokens**`,
	`- **Maximum if every \`SKILL.md\` body is also loaded at once: ${maxAllLoadedTokens} tokens**`,
	"",
	"Of the skills menu above, the shared wrapper (instructions and XML envelope, independent of skill count) is ~" +
		`${sharedTokens} tokens; the rest scales with the number of skills.`,
	"",
	"This counts everything the forge profile itself feeds at launch: the managed `AGENTS.md` instructions and the skills menu (name, description, and location for every model-visible skill). The maximum adds every complete `SKILL.md` on top of the launch menu — the ceiling if every skill is triggered and read in one session.",
	"",
	"Still excluded, because they are owned by the Pi harness rather than this profile and vary by machine and tool selection: Pi's base system prompt, the tool JSON schemas, conversation history, and any non-skill files the model reads on demand.",
	"",
	"## Skills",
	"",
	"| Skill | Summary | Launch metadata tokens | On-demand body tokens | Complete file tokens | Launch visibility |",
	"|---|---|---:|---:|---:|---|",
];

for (const entry of entries) {
	lines.push(
		`| [\`${escapeMarkdown(entry.name)}\`](${entry.location}) | ${escapeMarkdown(entry.summary)} | ${entry.launchTokens} | ${entry.bodyTokens} | ${entry.fileTokens} | ${entry.modelVisible ? "Model-visible" : "Manual invocation only"} |`,
	);
}

lines.push(
	"",
	"## Counting Method",
	"",
	"- The skills menu is the exact text produced by Pi's `formatSkillsForPrompt` (name, description, and location per model-visible skill, plus shared instructions and XML envelope).",
	"- The `AGENTS.md` figure replicates Pi's `<project_context>` wrapper from `buildSystemPrompt` (`packages/coding-agent/src/core/system-prompt.ts`) around the current `forge/AGENTS.md`.",
	"- Total forge launch context = `AGENTS.md` (wrapped) + skills menu. The maximum adds every complete `SKILL.md` (frontmatter + body) on top, the ceiling when all skills are read in one session.",
	"- Token estimates use Pi's conservative `ceil(characters / 4)` heuristic. Provider tokenizers produce different exact counts.",
	"- Repository-relative locations keep this report stable across machines. Installed absolute paths can change the real launch count slightly.",
	"- On-demand body tokens exclude YAML frontmatter; complete file tokens include it and approximate reading the entire file through the read tool.",
	"",
	"Regenerate after adding a skill or changing a skill name, description, location, body, or launch visibility:",
	"",
	"```bash",
	"npm run forge:skills-report",
	"```",
	"",
);

const report = `${lines.join("\n")}\n`;
if (checkOnly) {
	if (!existsSync(reportPath) || readFileSync(reportPath, "utf8") !== report) {
		console.error("FORGE_SKILLS.md is stale. Run: npm run forge:skills-report");
		process.exit(1);
	}
	console.log("FORGE_SKILLS.md is up to date.");
} else {
	writeFileSync(reportPath, report, "utf8");
	console.log(`Updated ${repositoryPath(reportPath)}`);
}

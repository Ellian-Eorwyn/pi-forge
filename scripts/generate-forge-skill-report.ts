import { existsSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { basename, dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { parse } from "yaml";
import { formatConnectedServices } from "../packages/coding-agent/src/core/resource-loader.ts";
import { DEFAULT_CONNECTED_SERVICES } from "../packages/coding-agent/src/core/settings-manager.ts";
import { formatSkillsForPrompt, loadSkillsFromDir, type Skill } from "../packages/coding-agent/src/core/skills.ts";
import { buildSystemPrompt } from "../packages/coding-agent/src/core/system-prompt.ts";
import { allToolNames, createToolDefinition, type ToolName } from "../packages/coding-agent/src/core/tools/index.ts";
import { parseFrontmatter } from "../packages/coding-agent/src/utils/frontmatter.ts";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const skillsDirectory = join(repositoryRoot, "forge", "skills");
const extensionsDirectory = join(repositoryRoot, "forge", "extensions");
const reportPath = join(repositoryRoot, "FORGE_SKILLS.md");
const checkOnly = process.argv.slice(2).includes("--check");
const unknownOptions = process.argv.slice(2).filter((argument) => argument !== "--check");
const skillNamePattern = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const maxSkillNameLength = 64;
const maxDescriptionLength = 1024;
/**
 * Every description is read on every turn of every session, so the menu is the
 * profile's largest fixed cost. The Agent Skills spec allows 1024 characters;
 * this budget is the tighter house limit. A description over it is not
 * automatically wrong — but it has to earn the space, so it fails `--check`
 * until it is trimmed or the budget is deliberately raised.
 */
const descriptionBudget = 450;

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
	if (skill.description.length > descriptionBudget) {
		diagnostics.push(
			`${repositoryPath(skill.filePath)}: skill description is ${skill.description.length} characters, over the ${descriptionBudget}-character launch budget. Trim it, or raise descriptionBudget in scripts/generate-forge-skill-report.ts deliberately.`,
		);
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

interface MeasuredTool {
	name: string;
	source: "built-in" | "forge extension";
	schemaTokens: number;
	parameterCount: number;
	promptTokens: number;
}

/**
 * Every tool the model sees at launch, with the JSON the provider actually
 * receives. This is the half of launch context the report used to disclaim as
 * harness-owned — which is exactly why an oversized schema could grow unnoticed.
 */
async function measureTools(): Promise<{ tools: MeasuredTool[]; snippets: Record<string, string>; guidelines: string[] }> {
	const snippets: Record<string, string> = {};
	const guidelines: string[] = [];
	const tools: MeasuredTool[] = [];

	const record = (
		name: string,
		source: MeasuredTool["source"],
		definition: { description?: string; parameters?: unknown; promptSnippet?: string; promptGuidelines?: string[] },
	): void => {
		const schema = JSON.stringify({ name, description: definition.description, input_schema: definition.parameters });
		const properties = (definition.parameters as { properties?: Record<string, unknown> } | undefined)?.properties ?? {};
		let promptCharacters = 0;
		if (definition.promptSnippet) {
			snippets[name] = definition.promptSnippet;
			promptCharacters += `- ${name}: ${definition.promptSnippet}\n`.length;
		}
		for (const guideline of definition.promptGuidelines ?? []) {
			guidelines.push(guideline);
			promptCharacters += `- ${guideline}\n`.length;
		}
		tools.push({
			name,
			source,
			schemaTokens: estimateTokens(schema),
			parameterCount: Object.keys(properties).length,
			promptTokens: Math.ceil(promptCharacters / 4),
		});
	};

	for (const name of [...allToolNames].sort()) {
		record(name, "built-in", createToolDefinition(name as ToolName, repositoryRoot));
	}

	// Extensions register their tools through the ExtensionAPI; a stub captures the
	// registrations without starting a session.
	const noop = (): void => {};
	const captured: Array<Record<string, unknown>> = [];
	const stub = new Proxy(
		{},
		{
			get: (_target, property) =>
				property === "registerTool" ? (definition: Record<string, unknown>) => captured.push(definition) : noop,
		},
	);
	const extensionFiles = readdirSync(extensionsDirectory)
		.filter((file) => file.endsWith(".ts"))
		.sort();
	for (const file of extensionFiles) {
		const module = (await import(join(extensionsDirectory, file))) as { default?: (api: unknown) => unknown };
		if (typeof module.default === "function") await module.default(stub);
	}
	for (const definition of captured.sort((left, right) => String(left.name).localeCompare(String(right.name)))) {
		record(String(definition.name), "forge extension", definition as Parameters<typeof record>[2]);
	}

	return { tools, snippets, guidelines };
}

const { tools: measuredTools, snippets: toolSnippets, promptGuidelines } = await measureTools();
const toolSchemaTokens = measuredTools.reduce((sum, tool) => sum + tool.schemaTokens, 0);

// The harness skeleton: intro, the tools list, the guidelines, the pi documentation
// block, and the connected-services append — everything buildSystemPrompt emits before
// any project context or skills are added.
const connectedServices = formatConnectedServices(DEFAULT_CONNECTED_SERVICES);
const basePrompt = buildSystemPrompt({
	selectedTools: measuredTools.map((tool) => tool.name),
	toolSnippets,
	promptGuidelines,
	appendSystemPrompt: connectedServices,
	cwd: repositoryRoot,
	// Mirrors what configure-pi-forge.mjs writes into the forge profile's settings.
	includePiDocs: false,
});
const basePromptTokens = estimateTokens(basePrompt);
const connectedServicesTokens = estimateTokens(connectedServices);

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
const profileLaunchTokens = Math.ceil((agentsCharacters + launchPromptCharacters) / 4);
// The whole first-turn payload, including the harness skeleton and every tool schema.
const totalLaunchTokens = profileLaunchTokens + basePromptTokens + toolSchemaTokens;
// Worst case: the launch payload stays in context and every SKILL.md is also fully read.
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
	`- Tools offered at launch: ${measuredTools.length}`,
	"",
	"| Launch context block | Tokens |",
	"|---|---:|",
	`| Base system prompt (intro, tools list, guidelines, connected services) | ${basePromptTokens} |`,
	`| Tool JSON schemas (${measuredTools.length} tools) | ${toolSchemaTokens} |`,
	`| Managed instructions (\`AGENTS.md\` with its \`<project_context>\` wrapper) | ${agentsTokens} |`,
	`| Skills menu (metadata for all model-visible skills) | ${launchPromptTokens} |`,
	`| **Total launch context (always processed)** | **${totalLaunchTokens}** |`,
	`| Maximum if every \`SKILL.md\` body is also loaded at once | ${maxAllLoadedTokens} |`,
	"",
	`Of that total, the forge profile itself owns ${profileLaunchTokens} tokens (\`AGENTS.md\` plus the skills menu); the rest is the harness skeleton and the tool schemas, which this repository also controls.`,
	"",
	"Of the skills menu above, the shared wrapper (instructions and XML envelope, independent of skill count) is ~" +
		`${sharedTokens} tokens; the rest scales with the number of skills.`,
	"",
	`The connected-services append inside the base prompt is ${connectedServicesTokens} tokens, measured with both services enabled.`,
	"",
	"This is the whole first-turn payload the model processes before the user's first word. The maximum adds every complete `SKILL.md` on top — the ceiling if every skill is triggered and read in one session.",
	"",
	"Still excluded, because they are not fixed launch cost: conversation history, the once-per-session vault coordinates the `vault-context` extension injects inside an Obsidian vault, any `AGENTS.md` in the working directory's own ancestry, and non-skill files the model reads on demand.",
	"",
	"## Tools",
	"",
	"Sorted by launch cost. `Prompt lines` counts a tool's `promptSnippet` and `promptGuidelines` contribution to the base system prompt, and is already included in the base prompt figure above.",
	"",
	"| Tool | Source | Parameters | Schema tokens | Prompt lines |",
	"|---|---|---:|---:|---:|",
	...[...measuredTools]
		.sort((left, right) => right.schemaTokens - left.schemaTokens || left.name.localeCompare(right.name))
		.map(
			(tool) =>
				`| \`${escapeMarkdown(tool.name)}\` | ${tool.source} | ${tool.parameterCount} | ${tool.schemaTokens} | ${tool.promptTokens} |`,
		),
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
	"- The base system prompt calls the real `buildSystemPrompt` with every tool's `promptSnippet` and `promptGuidelines`, plus the real `formatConnectedServices` output with both services enabled.",
	"- Tool schemas count `JSON.stringify({name, description, input_schema})` per tool: built-ins from `createToolDefinition`, extension tools captured by loading each `forge/extensions/*.ts` against a stub `ExtensionAPI`.",
	"- Total launch context = base system prompt + tool schemas + `AGENTS.md` (wrapped) + skills menu. The maximum adds every complete `SKILL.md` (frontmatter + body) on top, the ceiling when all skills are read in one session.",
	"- Token estimates use Pi's conservative `ceil(characters / 4)` heuristic. Provider tokenizers produce different exact counts.",
	"- Repository-relative locations keep this report stable across machines. Installed absolute paths can change the real launch count slightly.",
	"- On-demand body tokens exclude YAML frontmatter; complete file tokens include it and approximate reading the entire file through the read tool.",
	"",
	"Regenerate after adding a skill or tool, or changing a skill name, description, location, body, or launch visibility, a tool schema, or the base prompt text:",
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

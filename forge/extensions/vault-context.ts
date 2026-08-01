/**
 * Vault Context Extension
 *
 * Recognizes when pi-forge is launched inside an Obsidian vault and tells the
 * model so, once per session, with the vault's actual coordinates.
 *
 * The forge skills menu already carries the descriptions of `vault-organizer`
 * and `vault-connections`, so the model can always find them. What it cannot
 * know without looking is that the working directory *is* a vault, where the
 * schema note lives, whether an embedding index exists yet, and which skill
 * answers which kind of question. Re-deriving that costs several tool calls at
 * the start of every session, and the model often skips it and greps instead.
 *
 * The same is true of who the vault belongs to. The personal-context register
 * may declare an owner, and a session that reads it can use the person's name
 * instead of calling them "the user" for an hour.
 *
 * Detection is filesystem-only and cheap: walk up for `.obsidian/`, then read a
 * few known paths. Outside a vault this extension does nothing at all.
 */

import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { basename, delimiter, dirname, join, relative, resolve } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const CONTEXT_CUSTOM_TYPE = "vault-context";
const DEFAULT_SCHEMA_RELATIVE = join("99 Meta", "99.02 Schemas", "0.00 Vault Schema.md");
const DEFAULT_PROFILE_RELATIVE = join("99 Meta", "99.02 Schemas", "0.03 Personal Context.md");
/** Matches `MAX_OWNER_FIELD_CHARS` in `forge/lib/vault_profile.py`: a name, not a biography. */
const MAX_OWNER_FIELD_CHARS = 40;
const SKIPPED_DIRECTORIES = new Set([".obsidian", ".git", ".vault-organizer", ".vault-connections", "node_modules"]);
// Bounds so a pathological directory can never make startup feel slow.
const MAX_ASCEND = 24;
const MAX_NOTES_COUNTED = 50000;
const MAX_SCHEMA_SEARCH_DEPTH = 3;

// The schema's registry values for the folder that holds generated run
// directories. The folder *name* is compiled from the schema's numbers and
// labels, never hardcoded: renumbering `meta` renames every path beneath it.
const META_DOMAIN_VALUE = "meta";
const WORKFLOWS_SUBDOMAIN_VALUE = "workflows";
const WORKFLOWS_FOLDER_PATTERN = /^\d{1,2}\.\d{2} Workflows$/;
const CATEGORY_MAP_URL = new URL("../lib/workflow-categories.json", import.meta.url);
/** Marks a directory whose contents are machine artifacts, not vault notes. */
const WORKSPACE_MARKER = ".forge-workspace";
const WORKSPACE_MARKER_CONTENT = [
	"pi-forge workspace. Generated run directories live here.",
	"vault-organizer and vault-connections skip any directory containing this file.",
	"",
].join("\n");

interface VaultOwner {
	name: string;
	pronouns?: string;
}

/** What the Obsidian CLI can do for this vault, as far as the filesystem shows. */
interface ObsidianCli {
	binary: string;
	/** The name `vault=` accepts. Undefined when unregistered or when two vaults share a basename. */
	vaultName?: string;
	/** Settings -> General -> Command line interface. */
	enabled: boolean;
	/** Settings -> Files and links -> Automatically update internal links. */
	linkUpdates: "always" | "unset" | "off";
}

interface VaultInfo {
	root: string;
	name: string;
	noteCount: number;
	truncated: boolean;
	schemaNote?: string;
	/** Declared by the personal-context register's `## Owner` section, when it has one. */
	owner?: VaultOwner;
	wikiDomain: boolean;
	organizerState: boolean;
	indexedNotes?: number;
	/** Vault-relative folder holding generated run directories, when resolvable. */
	workflowsFolder?: string;
	/** Present only when an `obsidian` binary is on PATH. */
	obsidianCli?: ObsidianCli;
}

/**
 * Obsidian's own registry of vaults, read rather than asked for.
 *
 * The CLI's `vault=` takes a registered vault *name*, and naming an unopened
 * vault opens its window and switches the user's active vault — far too much to
 * do at session start just to learn a name. The registry is a JSON file holding
 * the same information, and the name is simply the registered path's basename.
 * So detection here stays what this file promises: filesystem-only and cheap.
 */
function obsidianConfigDirectory(): string | undefined {
	const override = process.env.FORGE_OBSIDIAN_CONFIG_DIR;
	if (override) return override;
	const home = process.env.HOME || process.env.USERPROFILE;
	if (process.platform === "darwin") {
		return home ? join(home, "Library", "Application Support", "obsidian") : undefined;
	}
	if (process.platform === "win32") {
		const appData = process.env.APPDATA;
		return appData ? join(appData, "obsidian") : home ? join(home, "AppData", "Roaming", "obsidian") : undefined;
	}
	const configHome = process.env.XDG_CONFIG_HOME;
	if (configHome) return join(configHome, "obsidian");
	return home ? join(home, ".config", "obsidian") : undefined;
}

function findObsidianBinary(): string | undefined {
	if ((process.env.FORGE_OBSIDIAN_CLI || "").trim().toLowerCase() === "off") return undefined;
	const explicit = process.env.FORGE_OBSIDIAN_CLI;
	if (explicit) return existsSync(explicit) ? explicit : undefined;
	const names = process.platform === "win32" ? ["obsidian.exe", "obsidian"] : ["obsidian"];
	for (const directory of (process.env.PATH || "").split(delimiter)) {
		if (!directory) continue;
		for (const name of names) {
			const candidate = join(directory, name);
			try {
				if (statSync(candidate).isFile()) return candidate;
			} catch {
				// keep looking
			}
		}
	}
	return undefined;
}

function readJsonFile(path: string): Record<string, unknown> | undefined {
	try {
		const parsed = JSON.parse(readFileSync(path, "utf8"));
		return parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : undefined;
	} catch {
		return undefined;
	}
}

function inspectObsidianCli(root: string): ObsidianCli | undefined {
	const binary = findObsidianBinary();
	if (!binary) return undefined;
	const configDirectory = obsidianConfigDirectory();
	const registry = configDirectory ? readJsonFile(join(configDirectory, "obsidian.json")) : undefined;
	const vaults = (registry?.vaults ?? {}) as Record<string, { path?: unknown }>;
	const names = new Map<string, string>();
	for (const entry of Object.values(vaults)) {
		if (typeof entry?.path !== "string" || !entry.path) continue;
		names.set(resolve(entry.path), basename(entry.path));
	}
	const name = names.get(resolve(root));
	// Two registered vaults with the same folder name make `vault=` ambiguous,
	// and guessing which one Obsidian picks is not a guess to make about notes.
	const ambiguous = name !== undefined && [...names.values()].filter((value) => value === name).length > 1;
	const app = readJsonFile(join(root, ".obsidian", "app.json"));
	const linkUpdates =
		app === undefined || !("alwaysUpdateLinks" in app)
			? "unset"
			: app.alwaysUpdateLinks === true
				? "always"
				: "off";
	return {
		binary,
		vaultName: ambiguous ? undefined : name,
		enabled: registry?.cli !== false,
		linkUpdates,
	};
}

/** Nearest ancestor of `from` (inclusive) that contains a `.obsidian` directory. */
function findVaultRoot(from: string): string | undefined {
	let current = resolve(from);
	for (let depth = 0; depth < MAX_ASCEND; depth += 1) {
		try {
			if (statSync(join(current, ".obsidian")).isDirectory()) return current;
		} catch {
			// not a vault at this level; keep walking up
		}
		const parent = dirname(current);
		if (parent === current) break;
		current = parent;
	}
	return undefined;
}

function countNotes(root: string): { count: number; truncated: boolean } {
	let count = 0;
	const queue = [root];
	while (queue.length > 0) {
		const directory = queue.pop() as string;
		let entries: ReturnType<typeof readdirSync>;
		try {
			entries = readdirSync(directory, { withFileTypes: true });
		} catch {
			continue;
		}
		for (const entry of entries) {
			if (entry.isSymbolicLink()) continue;
			if (entry.isDirectory()) {
				if (entry.name.startsWith(".") || SKIPPED_DIRECTORIES.has(entry.name)) continue;
				// Marked workspaces hold run artifacts, which the skills do not treat
				// as notes; counting them would disagree with what discovery selects.
				if (existsSync(join(directory, entry.name, WORKSPACE_MARKER))) continue;
				queue.push(join(directory, entry.name));
				continue;
			}
			if (entry.isFile() && entry.name.toLowerCase().endsWith(".md")) {
				count += 1;
				if (count >= MAX_NOTES_COUNTED) return { count, truncated: true };
			}
		}
	}
	return { count, truncated: false };
}

/** A config note at its canonical path, else a shallow search for its basename. */
function findConfigNote(root: string, canonicalRelative: string): string | undefined {
	if (existsSync(join(root, canonicalRelative))) return canonicalRelative;
	const target = basename(canonicalRelative);
	const queue: { directory: string; depth: number }[] = [{ directory: root, depth: 0 }];
	while (queue.length > 0) {
		const { directory, depth } = queue.shift() as { directory: string; depth: number };
		let entries: ReturnType<typeof readdirSync>;
		try {
			entries = readdirSync(directory, { withFileTypes: true });
		} catch {
			continue;
		}
		for (const entry of entries) {
			const full = join(directory, entry.name);
			if (entry.isFile() && entry.name === target) return relative(root, full);
			if (entry.isDirectory() && depth < MAX_SCHEMA_SEARCH_DEPTH) {
				if (entry.name.startsWith(".") || SKIPPED_DIRECTORIES.has(entry.name)) continue;
				queue.push({ directory: full, depth: depth + 1 });
			}
		}
	}
	return undefined;
}

function findSchemaNote(root: string): string | undefined {
	return findConfigNote(root, DEFAULT_SCHEMA_RELATIVE);
}

/** The `## Owner` section only, so a same-named row elsewhere in the note cannot match. */
function ownerSection(text: string): string {
	const start = /^##\s+Owner\s*$/m.exec(text);
	if (!start) return "";
	const rest = text.slice(start.index + start[0].length);
	const end = /^#{1,2}\s+\S/m.exec(rest);
	return end ? rest.slice(0, end.index) : rest;
}

function unwrapInlineCode(cell: string): string {
	const text = cell.trim();
	return text.length >= 2 && text.startsWith("`") && text.endsWith("`") ? text.slice(1, -1).trim() : text;
}

/** One `| \`field\` | \`value\` |` row from the owner table. */
function ownerField(section: string, field: string): string | undefined {
	for (const line of section.split("\n")) {
		const trimmed = line.trim();
		if (!trimmed.startsWith("|") || !trimmed.endsWith("|")) continue;
		const cells = trimmed.slice(1, -1).split("|").map(unwrapInlineCode);
		if (cells.length !== 2 || cells[0]?.toLowerCase() !== field) continue;
		const value = cells[1] as string;
		return value.length > 0 && value.length <= MAX_OWNER_FIELD_CHARS ? value : undefined;
	}
	return undefined;
}

/**
 * Who the vault belongs to, as its personal-context register declares them.
 *
 * A deliberately partial read of the same section `vault_profile.py` parses:
 * the name and pronouns are what a conversation needs, and the cards below them
 * are private material this extension has no business putting into context. A
 * register with no owner row returns nothing and the session says nothing.
 */
function readOwner(root: string): VaultOwner | undefined {
	const note = findConfigNote(root, DEFAULT_PROFILE_RELATIVE);
	if (!note) return undefined;
	let text: string;
	try {
		text = readFileSync(join(root, note), "utf8");
	} catch {
		return undefined;
	}
	const section = ownerSection(text);
	const name = ownerField(section, "name");
	if (!name) return undefined;
	const pronouns = ownerField(section, "pronouns");
	return pronouns ? { name, pronouns } : { name };
}

function readIndexedNoteCount(root: string): number | undefined {
	try {
		const meta = JSON.parse(readFileSync(join(root, ".vault-connections", "cache", "vectors.json"), "utf8"));
		const rows = meta?.rows;
		if (rows && typeof rows === "object") return Object.keys(rows).length;
	} catch {
		// no index yet, or unreadable — both mean "not indexed"
	}
	return undefined;
}

/** Whether the schema note declares a `wiki` domain row, which the wiki command requires. */
function hasWikiDomain(root: string, schemaNote: string | undefined): boolean {
	if (!schemaNote) return false;
	try {
		return /^\|\s*`wiki`\s*\|/m.test(readFileSync(join(root, schemaNote), "utf8"));
	} catch {
		return false;
	}
}

let categoryCache: Record<string, string> | undefined;

/** Skill name -> folder label under the workflows folder. Absent means "no vault route". */
function workflowCategories(): Record<string, string> {
	if (categoryCache) return categoryCache;
	try {
		const parsed = JSON.parse(readFileSync(CATEGORY_MAP_URL, "utf8")) as { categories?: unknown };
		categoryCache =
			parsed.categories && typeof parsed.categories === "object" ? (parsed.categories as Record<string, string>) : {};
	} catch {
		// A missing or malformed map is not fatal; every skill falls back to forge-output/.
		categoryCache = {};
	}
	return categoryCache;
}

function pad2(value: number): string {
	return value < 10 ? `0${value}` : String(value);
}

/** One `| \`value\` | \`number\` | \`Label\` | …` row from a schema registry table. */
function registryRow(text: string, value: string): { number: number; label: string } | undefined {
	const match = new RegExp(`^\\|\\s*\`${value}\`\\s*\\|\\s*\`(\\d{1,2})\`\\s*\\|\\s*\`([^\`|]+)\`\\s*\\|`, "m").exec(text);
	if (!match) return undefined;
	const number = Number.parseInt(match[1] as string, 10);
	const label = (match[2] as string).trim();
	if (!Number.isInteger(number) || number < 1 || number > 99 || label.length === 0) return undefined;
	return { number, label };
}

/** The `### <domain>` subdomain table only, so a same-named row elsewhere cannot match. */
function subdomainSection(text: string, domain: string): string {
	const start = new RegExp(`^###\\s+${domain}\\s*$`, "m").exec(text);
	if (!start) return "";
	const rest = text.slice(start.index + start[0].length);
	const end = /^#{2,3}\s+\S/m.exec(rest);
	return end ? rest.slice(0, end.index) : rest;
}

/** Compile `<pad2(domain)> <Domain>/<domain>.<pad2(sub)> <Sub>` from the schema's registries. */
function workflowsFolderFromSchema(root: string, schemaNote: string | undefined): string | undefined {
	if (!schemaNote) return undefined;
	let text: string;
	try {
		text = readFileSync(join(root, schemaNote), "utf8");
	} catch {
		return undefined;
	}
	const domain = registryRow(text, META_DOMAIN_VALUE);
	if (!domain) return undefined;
	const subdomain = registryRow(subdomainSection(text, META_DOMAIN_VALUE), WORKFLOWS_SUBDOMAIN_VALUE);
	if (!subdomain) return undefined;
	return join(
		`${pad2(domain.number)} ${domain.label}`,
		`${domain.number}.${pad2(subdomain.number)} ${subdomain.label}`,
	);
}

/** Fallback for a vault with no readable schema: an existing `NN.MM Workflows` folder. */
function workflowsFolderOnDisk(root: string): string | undefined {
	let entries: ReturnType<typeof readdirSync>;
	try {
		entries = readdirSync(root, { withFileTypes: true });
	} catch {
		return undefined;
	}
	for (const entry of entries) {
		if (!entry.isDirectory() || entry.isSymbolicLink()) continue;
		if (entry.name.startsWith(".") || SKIPPED_DIRECTORIES.has(entry.name)) continue;
		let children: ReturnType<typeof readdirSync>;
		try {
			children = readdirSync(join(root, entry.name), { withFileTypes: true });
		} catch {
			continue;
		}
		for (const child of children) {
			if (child.isDirectory() && WORKFLOWS_FOLDER_PATTERN.test(child.name)) return join(entry.name, child.name);
		}
	}
	return undefined;
}

function findWorkflowsFolder(root: string, schemaNote: string | undefined): string | undefined {
	return workflowsFolderFromSchema(root, schemaNote) ?? workflowsFolderOnDisk(root);
}

/**
 * Where `skill` should write its generated run directories.
 *
 * Inside a vault whose workflows folder resolves, that is a per-skill category
 * folder under it, marked so the organizer and the index leave the artifacts
 * alone. Everywhere else it is the unchanged `forge-output/<skill>/`.
 */
export function resolveWorkflowRoot(cwd: string, skill: string): string {
	const category = workflowCategories()[skill];
	const vaultRoot = category ? findVaultRoot(cwd) : undefined;
	const workflowsFolder = vaultRoot ? findWorkflowsFolder(vaultRoot, findSchemaNote(vaultRoot)) : undefined;
	if (!vaultRoot || !workflowsFolder || !category) {
		const fallback = join(cwd, "forge-output", skill);
		mkdirSync(fallback, { recursive: true });
		return fallback;
	}
	const directory = join(vaultRoot, workflowsFolder, category);
	mkdirSync(directory, { recursive: true });
	const marker = join(directory, WORKSPACE_MARKER);
	if (!existsSync(marker)) writeFileSync(marker, WORKSPACE_MARKER_CONTENT);
	return directory;
}

export function inspectVault(cwd: string): VaultInfo | undefined {
	const root = findVaultRoot(cwd);
	if (!root) return undefined;
	const schemaNote = findSchemaNote(root);
	const { count, truncated } = countNotes(root);
	return {
		root,
		name: basename(root),
		noteCount: count,
		truncated,
		schemaNote,
		owner: readOwner(root),
		wikiDomain: hasWikiDomain(root, schemaNote),
		organizerState: existsSync(join(root, ".vault-organizer")),
		indexedNotes: readIndexedNoteCount(root),
		workflowsFolder: findWorkflowsFolder(root, schemaNote),
		obsidianCli: inspectObsidianCli(root),
	};
}

/**
 * What to say about the Obsidian CLI, which is usually nothing.
 *
 * This message is injected once per session and every line costs tokens, so an
 * absent accelerator does not get a line explaining that it is absent. Two cases
 * do earn one: the CLI is in use, which the model needs warning about; and the
 * CLI is one setting away from working, which is worth fixing.
 */
function obsidianCliLines(cli: ObsidianCli | undefined): string[] {
	if (!cli) return [];
	if (!cli.enabled) {
		return [
			"- Obsidian is installed but its command line interface is turned off (Settings -> General). Turning it on lets the vault skills verify their own work and move notes without breaking links.",
		];
	}
	if (!cli.vaultName) {
		return [
			"- Obsidian's CLI is installed but this vault is not registered with it under a unique name, so the skills cannot use it. Everything still works; link-safe moves and the property-vocabulary check do not.",
		];
	}
	if (cli.linkUpdates !== "always") {
		return [
			`- Obsidian's CLI is available for vault \`${cli.vaultName}\`, but "Automatically update internal links" is off (Settings -> Files and links), so renames fall back to a plain rename and inbound links are left behind. Turning it on is the single change that makes moves link-safe.`,
		];
	}
	return [
		`- Obsidian CLI: available (vault name \`${cli.vaultName}\`). The vault skills use it as an optional verifier and for link-safe moves. It is never required; every skill works without it.`,
		"- Do not run mutating `obsidian` subcommands yourself — rename, move, delete, property:set, create, append, eval. It exits 0 whether it succeeded or failed, has no dry run, and rewrites links across the whole vault. The skills own those calls: they back up every affected note, verify hashes, and journal what they did.",
	];
}

export function vaultContextMessage(vault: VaultInfo): string {
	const lines = [
		"[OBSIDIAN VAULT DETECTED]",
		"The working directory is inside an Obsidian vault. Prefer the vault skills over ad-hoc file reading.",
	];

	if (vault.owner) {
		const who = vault.owner.pronouns ? `${vault.owner.name} (${vault.owner.pronouns})` : vault.owner.name;
		lines.push(
			"",
			`You are working with ${who}. Address them by name; do not call them "the user" or "the owner".`,
		);
	}

	lines.push(
		"",
		`- Vault root: ${vault.root}`,
		`- Notes: ${vault.truncated ? `${vault.noteCount}+` : vault.noteCount} Markdown files`,
	);

	if (vault.schemaNote) {
		lines.push(`- Schema note (sole source of truth for folders and frontmatter): ${vault.schemaNote}`);
	} else {
		lines.push("- Schema note: NOT FOUND. vault-organizer cannot file notes until one exists; say so before attempting it.");
	}

	lines.push(
		vault.indexedNotes === undefined
			? "- vault-connections index: not built yet. Run its `index` command once before `search` or `propose`."
			: `- vault-connections index: built, ${vault.indexedNotes} notes embedded.`,
	);
	if (vault.schemaNote && !vault.wikiDomain) {
		lines.push("- No `wiki` domain in the schema, so vault-connections `wiki` will fail closed until one is added.");
	}
	if (vault.schemaNote && !vault.organizerState) {
		lines.push("- vault-organizer has never run here, so notes are not yet guaranteed to match the schema. Dry-run before proposing any apply.");
	}
	lines.push(...obsidianCliLines(vault.obsidianCli));

	if (vault.workflowsFolder) {
		lines.push(
			"",
			"Where generated output goes:",
			`- Workflow root: ${join(vault.root, vault.workflowsFolder)}`,
			"- Every skill that would otherwise write to `forge-output/<skill>/` writes to `<workflow root>/<Category>/<stem>/` instead. Each SKILL.md names its own category; do not invent one.",
			"- This wins over a skill's `<source-folder>/Generated/…` convention inside the vault, so no run directory lands in a domain folder.",
			"- Those category folders carry a `.forge-workspace` marker and are skipped by vault-organizer and vault-connections. Run artifacts are not notes.",
			"- To make a finished report an actual note, use vault-connections `import-run`. Never hand-copy a run artifact into a domain folder.",
		);
	} else {
		lines.push(
			"",
			`- No workflows folder resolved from the schema, so generated output stays in \`forge-output/<skill>/\`. Say so if the user expects it in the vault.`,
		);
	}

	lines.push(
		"",
		"Which skill to load:",
		"- Finding notes, or answering a question about what is in the vault -> skills/vault-connections/SKILL.md, `search`. Use it before grep; it ranks by meaning, and grep misses notes that never use the query's words.",
		"- Proposing links between notes, filling `related`, or maintaining the wiki layer -> skills/vault-connections/SKILL.md.",
		"- Classifying, filing, de-duplicating, or processing the inbox -> skills/vault-organizer/SKILL.md.",
		"- Raw voice-note or meeting transcripts in the inbox -> skills/vault-transcripts/SKILL.md **before** vault-organizer processes them: it names, cleans, and summarizes each recording and writes advisory frontmatter, and the organizer then classifies and files the result.",
		"",
		"Both skills dry-run by default and need explicit approval before `--apply`. Never hand-edit the schema note or note frontmatter; let the skills write them.",
	);
	return lines.join("\n");
}

function summaryLine(vault: VaultInfo): string {
	const parts = [`${vault.truncated ? `${vault.noteCount}+` : vault.noteCount} notes`];
	parts.push(vault.schemaNote ? "schema ok" : "no schema note");
	parts.push(vault.indexedNotes === undefined ? "not indexed" : `${vault.indexedNotes} indexed`);
	if (vault.obsidianCli?.vaultName && vault.obsidianCli.enabled) parts.push("cli");
	return `Obsidian vault: ${vault.name} (${parts.join(", ")})`;
}

export default function vaultContextExtension(pi: ExtensionAPI): void {
	let vault: VaultInfo | undefined;
	let injected = false;
	// Tracked separately from `vault`: "we looked and found nothing" must not
	// re-walk the filesystem on every turn outside a vault.
	let scanned = false;

	function scan(ctx: ExtensionContext): VaultInfo | undefined {
		try {
			vault = inspectVault(ctx.cwd);
		} catch {
			vault = undefined;
		}
		scanned = true;
		return vault;
	}

	function updateStatus(ctx: ExtensionContext): void {
		if (!vault) {
			ctx.ui.setStatus("vault-context", undefined);
			return;
		}
		ctx.ui.setStatus("vault-context", ctx.ui.theme.fg("accent", `🗂 ${vault.name}`));
	}

	pi.on("session_start", async (_event, ctx) => {
		injected = false;
		scan(ctx);
		updateStatus(ctx);
	});

	// Inject once per session: the facts are stable, and repeating them every
	// turn would spend tokens on something the model already has in context.
	pi.on("before_agent_start", async (_event, ctx) => {
		if (!scanned) {
			// The first turn can precede session_start in some run modes.
			scan(ctx);
			updateStatus(ctx);
		}
		if (!vault || injected) return undefined;
		injected = true;
		return { message: { customType: CONTEXT_CUSTOM_TYPE, content: vaultContextMessage(vault), display: false } };
	});

	// Compaction can summarize the injected context away; re-arm so the next
	// turn restates the vault coordinates.
	pi.on("session_compact", async () => {
		injected = false;
	});

	pi.registerCommand("vault", {
		description: "Show the detected Obsidian vault and which vault skills apply",
		handler: async (_args, ctx) => {
			scan(ctx);
			updateStatus(ctx);
			if (!vault) {
				ctx.ui.notify(`No Obsidian vault found at or above ${ctx.cwd} (looking for a .obsidian directory).`, "info");
				return;
			}
			injected = false;
			ctx.ui.notify(`${summaryLine(vault)}\n${vault.root}`, "info");
		},
	});
}

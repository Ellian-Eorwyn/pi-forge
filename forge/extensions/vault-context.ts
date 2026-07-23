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
 * Detection is filesystem-only and cheap: walk up for `.obsidian/`, then read a
 * few known paths. Outside a vault this extension does nothing at all.
 */

import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { basename, dirname, join, relative, resolve } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const CONTEXT_CUSTOM_TYPE = "vault-context";
const DEFAULT_SCHEMA_RELATIVE = join("99 Meta", "99.02 Schemas", "0.00 Vault Schema.md");
const SCHEMA_BASENAME = "0.00 Vault Schema.md";
const SKIPPED_DIRECTORIES = new Set([".obsidian", ".git", ".vault-organizer", ".vault-connections", "node_modules"]);
// Bounds so a pathological directory can never make startup feel slow.
const MAX_ASCEND = 24;
const MAX_NOTES_COUNTED = 50000;
const MAX_SCHEMA_SEARCH_DEPTH = 3;

interface VaultInfo {
	root: string;
	name: string;
	noteCount: number;
	truncated: boolean;
	schemaNote?: string;
	wikiDomain: boolean;
	organizerState: boolean;
	indexedNotes?: number;
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

/** The schema note at its canonical path, else a shallow search for its basename. */
function findSchemaNote(root: string): string | undefined {
	const canonical = join(root, DEFAULT_SCHEMA_RELATIVE);
	if (existsSync(canonical)) return DEFAULT_SCHEMA_RELATIVE;
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
			if (entry.isFile() && entry.name === SCHEMA_BASENAME) return relative(root, full);
			if (entry.isDirectory() && depth < MAX_SCHEMA_SEARCH_DEPTH) {
				if (entry.name.startsWith(".") || SKIPPED_DIRECTORIES.has(entry.name)) continue;
				queue.push({ directory: full, depth: depth + 1 });
			}
		}
	}
	return undefined;
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
		wikiDomain: hasWikiDomain(root, schemaNote),
		organizerState: existsSync(join(root, ".vault-organizer")),
		indexedNotes: readIndexedNoteCount(root),
	};
}

export function vaultContextMessage(vault: VaultInfo): string {
	const lines = [
		"[OBSIDIAN VAULT DETECTED]",
		"The working directory is inside an Obsidian vault. Prefer the vault skills over ad-hoc file reading.",
		"",
		`- Vault root: ${vault.root}`,
		`- Notes: ${vault.truncated ? `${vault.noteCount}+` : vault.noteCount} Markdown files`,
	];

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
		lines.push("- No `wiki` domain in the schema, so vault-connections `wiki` will fail closed until the user adds one.");
	}
	if (vault.schemaNote && !vault.organizerState) {
		lines.push("- vault-organizer has never run here, so notes are not yet guaranteed to match the schema. Dry-run before proposing any apply.");
	}

	lines.push(
		"",
		"Which skill to load:",
		"- Finding notes, or answering a question about what is in the vault -> skills/vault-connections/SKILL.md, `search`. Use it before grep; it ranks by meaning, and grep misses notes that never use the query's words.",
		"- Proposing links between notes, filling `related`, or maintaining the wiki layer -> skills/vault-connections/SKILL.md.",
		"- Classifying, filing, de-duplicating, or processing the inbox -> skills/vault-organizer/SKILL.md.",
		"",
		"Both skills dry-run by default and need explicit approval before `--apply`. Never hand-edit the schema note or note frontmatter; let the skills write them.",
	);
	return lines.join("\n");
}

function summaryLine(vault: VaultInfo): string {
	const parts = [`${vault.truncated ? `${vault.noteCount}+` : vault.noteCount} notes`];
	parts.push(vault.schemaNote ? "schema ok" : "no schema note");
	parts.push(vault.indexedNotes === undefined ? "not indexed" : `${vault.indexedNotes} indexed`);
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

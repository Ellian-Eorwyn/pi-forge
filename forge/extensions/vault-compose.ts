/**
 * Vault Compose Extension
 *
 * Lets a session turn what it is holding — this conversation, notes it has read,
 * a research run — into a vault note, without ever being the thing that writes
 * the note.
 *
 * The division is the one `web-research.ts` already draws: the extension is the
 * safe surface and the skill is the judgment. Two tools are registered and
 * neither has a field that can carry note prose. `forge_vault_capture_source`
 * takes a *reference* — session entry ids, or a path — and the harness reads the
 * text itself; `forge_vault_compose` hands the collected set to the Python CLI,
 * which drafts under the vault's voice, checks every specific against the
 * sources, and proposes.
 *
 * That is the whole point of the entry-id form. A note composed from a
 * conversation has to quote the conversation, and a model asked to reproduce
 * what was said will paraphrase some of it — at which point the grounding check
 * is checking the model against itself. Passing ids and letting the harness copy
 * the bytes makes the excerpt verbatim by construction. A model that quotes
 * wrong is caught by the check; a model that *cannot* quote is better.
 *
 * The real hole this closes is not the compose tool but a heredoc: nothing
 * stopped a session writing a note into the vault with `write` and calling it
 * composed. A `tool_call` guard blocks writes inside the detected vault root
 * that are not run artifacts.
 */

import { spawn } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { inspectVault, resolveWorkflowRoot } from "./vault-context.ts";

const SKILL_SCRIPT = join(dirname(fileURLToPath(import.meta.url)), "..", "skills", "vault-compose", "scripts", "vault-compose.py");
/** Marks a directory whose contents are machine artifacts, not vault notes. */
const WORKSPACE_MARKER = ".forge-workspace";
/** Bound on one excerpt, so a pasted book cannot become a "source". */
const MAX_UNIT_CHARS = 60000;

type SourceKind = "chat" | "vault-note" | "file";

interface PendingUnit {
	kind: SourceKind;
	label: string;
	text: string;
	wikilink?: string;
	origin: Record<string, unknown>;
	why?: string;
}

interface CaptureSourceParams {
	kind: SourceKind;
	label: string;
	path?: string;
	entryIds?: string[];
	why?: string;
}

interface ComposeParams {
	intent: "synthesis" | "conversation" | "research";
	request: string;
	noteType?: string;
	titleHint?: string;
	date?: string;
	maxNotes?: number;
	sourceIds?: string[];
}

/** Plain text from a message's content, which may be a string or a block array. */
function extractText(content: unknown): string {
	if (typeof content === "string") return content;
	if (!Array.isArray(content)) return "";
	return content
		.map((block) => (block && typeof block === "object" && "text" in block ? String((block as { text?: string }).text ?? "") : ""))
		.filter(Boolean)
		.join("\n");
}

function runPython(args: string[], signal?: AbortSignal): Promise<{ stdout: string; stderr: string; code: number }> {
	return new Promise((resolvePromise, reject) => {
		const child = spawn("python3", [SKILL_SCRIPT, ...args], { signal });
		let stdout = "";
		let stderr = "";
		child.stdout.on("data", (chunk) => {
			stdout += String(chunk);
		});
		child.stderr.on("data", (chunk) => {
			stderr += String(chunk);
		});
		child.on("error", reject);
		child.on("close", (code) => resolvePromise({ stdout, stderr, code: code ?? 0 }));
	});
}

/** Whether a path is a run artifact rather than a note. */
function insideWorkspace(vaultRoot: string, target: string): boolean {
	let current = dirname(resolve(target));
	const root = resolve(vaultRoot);
	while (current.startsWith(root)) {
		if (existsSync(join(current, WORKSPACE_MARKER))) return true;
		const parent = dirname(current);
		if (parent === current) break;
		current = parent;
	}
	return false;
}

function isInside(root: string, target: string): boolean {
	const resolvedRoot = resolve(root);
	const resolvedTarget = resolve(target);
	return resolvedTarget === resolvedRoot || resolvedTarget.startsWith(`${resolvedRoot}/`);
}

export default function vaultComposeExtension(pi: ExtensionAPI) {
	const pending: PendingUnit[] = [];

	// A session writing a note into the vault by hand is the hole the compose
	// tool cannot close on its own: nothing checks that note against anything.
	// Run artifacts under a `.forge-workspace` marker are exempt, because that is
	// where every skill legitimately writes.
	pi.on("tool_call", async (event, ctx) => {
		if (event.toolName !== "write" && event.toolName !== "edit") return undefined;
		const vault = inspectVault(ctx.cwd);
		if (!vault) return undefined;
		const raw = (event.args as { file_path?: string; path?: string } | undefined) ?? {};
		const target = raw.file_path ?? raw.path;
		if (!target) return undefined;
		const absolute = resolve(ctx.cwd, target);
		if (!isInside(vault.root, absolute)) return undefined;
		if (insideWorkspace(vault.root, absolute)) return undefined;
		return {
			block: true,
			reason:
				`${target} is inside the Obsidian vault at ${vault.root}. Notes are not written by hand: use ` +
				"forge_vault_capture_source to collect what the note should rest on, then forge_vault_compose, " +
				"which drafts under the vault's voice, checks every name and link against those sources, and " +
				"proposes the note for approval. Run artifacts under a .forge-workspace directory are exempt.",
		};
	});

	pi.registerTool({
		name: "forge_vault_capture_source",
		label: "Collect a source for a vault note",
		description:
			"Add one piece of material to the set a vault note will be composed from. You never supply the text: " +
			"for a conversation give the session entry ids and the harness copies what was actually said, and for a " +
			"note or file give the path and the harness reads it. Call once per source, then forge_vault_compose.",
		promptSnippet: "Collect sources for a vault note",
		promptGuidelines: [
			"To turn a conversation, some notes, or a research run into a vault note, collect its sources with forge_vault_capture_source and then call forge_vault_compose. Never write a note into the vault with the write tool.",
		],
		parameters: Type.Object({
			kind: Type.Union([Type.Literal("chat"), Type.Literal("vault-note"), Type.Literal("file")], {
				description: "Where this material comes from.",
			}),
			label: Type.String({ description: "What a citation from this material should be credited to." }),
			path: Type.Optional(
				Type.String({ description: "vault-note or file: the path. Read by the harness, not by you." }),
			),
			entryIds: Type.Optional(
				Type.Array(Type.String(), {
					description:
						"chat: the session entry ids whose text belongs in the note. The harness copies the text; do not retype it.",
				}),
			),
			why: Type.Optional(Type.String({ description: "Why this bears on the note. Recorded, never quoted." })),
		}),
		executionMode: "sequential",
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const input = params as CaptureSourceParams;
			let text = "";
			const origin: Record<string, unknown> = {};
			let wikilink: string | undefined;

			if (input.kind === "chat") {
				const wanted = new Set(input.entryIds ?? []);
				if (wanted.size === 0) throw new Error("kind 'chat' needs entryIds; the harness copies the text, you do not supply it.");
				const collected: string[] = [];
				const found: string[] = [];
				for (const entry of ctx.sessionManager.getEntries()) {
					if (!wanted.has(entry.id) || entry.type !== "message") continue;
					const message = (entry as { message?: { role?: string; content?: unknown } }).message;
					if (!message || (message.role !== "user" && message.role !== "assistant")) continue;
					const body = extractText(message.content);
					if (!body.trim()) continue;
					collected.push(`[${String(message.role).toUpperCase()}]\n${body}`);
					found.push(entry.id);
				}
				if (collected.length === 0) throw new Error(`no message entries matched: ${[...wanted].join(", ")}`);
				text = collected.join("\n\n");
				origin.sessionFile = ctx.sessionManager.getSessionFile();
				origin.sessionId = ctx.sessionManager.getSessionId();
				origin.entryIds = found;
			} else {
				if (!input.path) throw new Error(`kind '${input.kind}' needs a path.`);
				const absolute = resolve(ctx.cwd, input.path);
				text = readFileSync(absolute, "utf8");
				origin.path = input.path;
				origin.bytes = statSync(absolute).size;
				if (input.kind === "vault-note") {
					// Frontmatter is the vault's own metadata, not the note's
					// content, and leaving it in makes a schema value look like
					// something the note said.
					const stripped = /^---\n[\s\S]*?\n---\n/.exec(text);
					if (stripped) text = text.slice(stripped[0].length);
					const stem = absolute.split("/").pop()?.replace(/\.md$/i, "") ?? "";
					if (stem) wikilink = `[[${stem}]]`;
				}
			}

			if (text.length > MAX_UNIT_CHARS) {
				throw new Error(`source is ${text.length} characters, over the ${MAX_UNIT_CHARS} limit for one unit.`);
			}
			if (!text.trim()) throw new Error("that source has no text; a unit that says nothing cannot ground anything.");

			pending.push({ kind: input.kind, label: input.label, text, wikilink, origin, why: input.why });
			const details = {
				id: `s-${String(pending.length).padStart(4, "0")}`,
				kind: input.kind,
				label: input.label,
				characters: text.length,
				collected: pending.length,
			};
			return { content: [{ type: "text", text: JSON.stringify(details, null, 2) }], details };
		},
	});

	pi.registerTool({
		name: "forge_vault_compose",
		label: "Compose a vault note",
		description:
			"Compose a note from the sources collected so far, in the block order and voice the vault declares. " +
			"Every name, link, and wikilink is checked against the sources it cites. Nothing is written: this " +
			"proposes notes with ids, and the user accepts one before it reaches the vault.",
		promptSnippet: "Compose a vault note from collected sources",
		parameters: Type.Object({
			intent: Type.Union([Type.Literal("synthesis"), Type.Literal("conversation"), Type.Literal("research")], {
				description: "synthesis: from vault notes. conversation: from this chat. research: from a research run.",
			}),
			request: Type.String({ description: "What the user asked for, in their words." }),
			noteType: Type.Optional(Type.String({ description: "The schema `type` for the note. Defaults to note." })),
			titleHint: Type.Optional(Type.String({ description: "A title the user already suggested." })),
			date: Type.Optional(Type.String({ description: "The date the note is about, YYYY-MM-DD. Defaults to today." })),
			maxNotes: Type.Optional(Type.Integer({ minimum: 1, description: "Ceiling on how many notes to propose." })),
			sourceIds: Type.Optional(
				Type.Array(Type.String(), { description: "Which collected sources to use. Defaults to all of them." }),
			),
		}),
		executionMode: "sequential",
		async execute(_toolCallId, params, signal, _onUpdate, ctx) {
			const input = params as ComposeParams;
			const vault = inspectVault(ctx.cwd);
			if (!vault) throw new Error("the working directory is not inside an Obsidian vault.");
			if (pending.length === 0) {
				throw new Error("no sources collected. Call forge_vault_capture_source first, once per source.");
			}
			const wanted = new Set(input.sourceIds ?? []);
			const chosen = pending.filter((_unit, index) => wanted.size === 0 || wanted.has(`s-${String(index + 1).padStart(4, "0")}`));
			if (chosen.length === 0) throw new Error(`no collected source matched: ${[...wanted].join(", ")}`);

			const runRoot = resolveWorkflowRoot(ctx.cwd, "vault-compose");
			const stem = `${new Date().toISOString().replace(/[:.]/g, "-")}-${input.intent}`;
			const directory = join(runRoot, stem);
			mkdirSync(directory, { recursive: true });
			const specPath = join(directory, "run-spec.json");
			writeFileSync(
				specPath,
				JSON.stringify(
					{
						version: 1,
						intent: input.intent,
						request: input.request,
						noteType: input.noteType ?? "note",
						titleHint: input.titleHint ?? null,
						date: input.date ?? new Date().toISOString().slice(0, 10),
						maxNotes: input.maxNotes ?? 3,
						sources: chosen.map((unit) => ({
							kind: unit.kind,
							label: unit.label,
							text: unit.text,
							wikilink: unit.wikilink,
							origin: unit.origin,
						})),
					},
					null,
					2,
				),
				"utf8",
			);

			const result = await runPython(["compose", "--vault", vault.root, "--spec", specPath], signal);
			let parsed: unknown;
			try {
				parsed = JSON.parse(result.stdout);
			} catch {
				throw new Error(`vault-compose did not return JSON (exit ${result.code}): ${result.stderr.slice(-800)}`);
			}
			return { content: [{ type: "text", text: JSON.stringify(parsed, null, 2) }], details: parsed as object };
		},
	});

	pi.registerCommand("compose", {
		description: "Show the sources collected for a vault note so far",
		handler: async (_args: string, ctx: ExtensionContext) => {
			if (pending.length === 0) {
				ctx.ui?.notify?.("No sources collected yet.");
				return;
			}
			const lines = pending.map(
				(unit, index) => `s-${String(index + 1).padStart(4, "0")}  ${unit.kind.padEnd(11)} ${unit.label} (${unit.text.length} chars)`,
			);
			ctx.ui?.notify?.([`${pending.length} source(s) collected:`, ...lines].join("\n"));
		},
	});
}

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
import { WORKSPACE_MARKER } from "../lib/vault-workspace.mjs";
import { inspectVault, resolveWorkflowRoot } from "./vault-context.ts";

const SKILL_SCRIPT = join(
	dirname(fileURLToPath(import.meta.url)),
	"..",
	"skills",
	"vault-compose",
	"scripts",
	"vault-compose.py",
);
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

interface ApplyParams {
	runDirectory: string;
	accept: string[];
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
		.map((block) =>
			block && typeof block === "object" && "text" in block ? String((block as { text?: string }).text ?? "") : "",
		)
		.filter(Boolean)
		.join("\n");
}

/**
 * The CLI, with its progress reaching the caller while it still means something.
 *
 * The skill already writes a line per note to stderr. Buffering that to the end
 * turns a run that is working into a run that looks hung, which is how a
 * multi-minute call comes to be interrupted or retried.
 */
function runPython(
	args: string[],
	signal?: AbortSignal,
	onProgress?: (line: string) => void,
): Promise<{ stdout: string; stderr: string; code: number }> {
	return new Promise((resolvePromise, reject) => {
		const child = spawn("python3", [SKILL_SCRIPT, ...args], { signal });
		let stdout = "";
		let stderr = "";
		let partial = "";
		child.stdout.on("data", (chunk) => {
			stdout += String(chunk);
		});
		child.stderr.on("data", (chunk) => {
			const text = String(chunk);
			stderr += text;
			if (!onProgress) return;
			// Held back to whole lines: half a progress line is noise, not news.
			partial += text;
			const lines = partial.split("\n");
			partial = lines.pop() ?? "";
			for (const line of lines) if (line.trim()) onProgress(line.trim());
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

/**
 * What to do with the proposals a compose run just made.
 *
 * The skill computes this and files it in `run_state.json`, which nothing reads.
 * Saying it in the result is the difference between a session that names ids to
 * the user and one that starts inventing `forge compose accept`.
 */
function nextStep(parsed: unknown): string {
	const data = (parsed as { data?: { run_directory?: unknown; proposals?: unknown[] } } | undefined)?.data;
	const proposals = Array.isArray(data?.proposals)
		? (data.proposals as { id?: unknown; needs_review?: unknown }[])
		: [];
	if (proposals.length === 0) return "Nothing was proposed. Relay the warnings; do not retry with different options.";
	const held = proposals.filter((entry) => entry.needs_review).map((entry) => String(entry.id));
	const ready = proposals.filter((entry) => !entry.needs_review).map((entry) => String(entry.id));
	const lines = [
		"Nothing is in the vault yet. Show the user each proposal and let them name the ids they want.",
		`Then call forge_vault_compose_apply with runDirectory ${String(data?.run_directory ?? "")} and those ids.`,
	];
	if (ready.length > 0) lines.push(`Ready to write: ${ready.join(", ")}.`);
	if (held.length > 0) {
		lines.push(
			`Held for review: ${held.join(", ")}. Relay the reasons. A hold can be fixed by editing the note in ` +
				"the run's `proposed/` directory -- the checks run again over the file as it then stands -- but a " +
				"reviewer's objection needs a fresh compose, not an edit.",
		);
	}
	return lines.join(" ");
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
		// The event carries the tool's arguments under `input`, and both `write` and
		// `edit` name the file `path`. This read was `event.args.file_path`, which is
		// neither field nor property: it resolved to undefined on every call, so the
		// guard returned early and never blocked a single write. Two things hid it --
		// the file was outside the typechecker, and the test built its own
		// `{ toolName, args }` event, so it was asserting against a shape the runtime
		// does not send.
		const requested: unknown = event.input?.path;
		const target = typeof requested === "string" && requested.length > 0 ? requested : undefined;
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
			"To turn a conversation, some notes, or a research run into a vault note: collect its sources with forge_vault_capture_source, call forge_vault_compose, show the user what it proposed, then call forge_vault_compose_apply with the ids they name. Never write a note into the vault with the write tool.",
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
				if (wanted.size === 0)
					throw new Error("kind 'chat' needs entryIds; the harness copies the text, you do not supply it.");
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
			date: Type.Optional(
				Type.String({ description: "The date the note is about, YYYY-MM-DD. Defaults to today." }),
			),
			maxNotes: Type.Optional(Type.Integer({ minimum: 1, description: "Ceiling on how many notes to propose." })),
			sourceIds: Type.Optional(
				Type.Array(Type.String(), { description: "Which collected sources to use. Defaults to all of them." }),
			),
		}),
		executionMode: "sequential",
		async execute(_toolCallId, params, signal, onUpdate, ctx) {
			const input = params as ComposeParams;
			const vault = inspectVault(ctx.cwd);
			if (!vault) throw new Error("the working directory is not inside an Obsidian vault.");
			if (pending.length === 0) {
				throw new Error("no sources collected. Call forge_vault_capture_source first, once per source.");
			}
			const wanted = new Set(input.sourceIds ?? []);
			const chosen = pending.filter(
				(_unit, index) => wanted.size === 0 || wanted.has(`s-${String(index + 1).padStart(4, "0")}`),
			);
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

			const result = await runPython(["compose", "--vault", vault.root, "--spec", specPath], signal, (line) =>
				onUpdate?.({ content: [{ type: "text", text: line }], details: {} }),
			);
			let parsed: unknown;
			try {
				parsed = JSON.parse(result.stdout);
			} catch {
				throw new Error(`vault-compose did not return JSON (exit ${result.code}): ${result.stderr.slice(-800)}`);
			}
			// Without this a session that has just been handed proposal ids has
			// nowhere to go, and starts guessing at CLIs that do not exist.
			const payload = { ...(parsed as Record<string, unknown>), nextStep: nextStep(parsed) };
			return { content: [{ type: "text", text: JSON.stringify(payload, null, 2) }], details: payload };
		},
	});

	pi.registerTool({
		name: "forge_vault_compose_apply",
		label: "Write an accepted composed note",
		description:
			"Write proposals from a forge_vault_compose run into the vault, by id. Only ids the user named: " +
			"this is the step that puts a note in the vault. Every accepted note is checked against its sources " +
			"again here, over the file as it currently stands, so a note held for review stays held until the " +
			"reason is actually fixed.",
		promptSnippet: "Write an accepted composed note into the vault",
		parameters: Type.Object({
			runDirectory: Type.String({ description: "The run directory forge_vault_compose reported." }),
			accept: Type.Array(Type.String(), {
				description: "Proposal ids the user named, such as n-001. Nothing else is written.",
			}),
		}),
		executionMode: "sequential",
		async execute(_toolCallId, params, signal, onUpdate, ctx) {
			const input = params as ApplyParams;
			const vault = inspectVault(ctx.cwd);
			if (!vault) throw new Error("the working directory is not inside an Obsidian vault.");
			if (input.accept.length === 0) throw new Error("accept is empty; nothing is written without a proposal id.");
			// A run directory is somewhere this extension put one. Anywhere else is
			// either a mistake or a way to point apply at a hand-built manifest.
			const runDirectory = resolve(ctx.cwd, input.runDirectory);
			const runRoot = resolveWorkflowRoot(ctx.cwd, "vault-compose");
			if (!isInside(runRoot, runDirectory)) {
				throw new Error(`${input.runDirectory} is not a vault-compose run directory under ${runRoot}.`);
			}
			const result = await runPython(
				["apply", "--vault", vault.root, "--run", runDirectory, "--accept", input.accept.join(",")],
				signal,
				(line) => onUpdate?.({ content: [{ type: "text", text: line }], details: {} }),
			);
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
				(unit, index) =>
					`s-${String(index + 1).padStart(4, "0")}  ${unit.kind.padEnd(11)} ${unit.label} (${unit.text.length} chars)`,
			);
			ctx.ui?.notify?.([`${pending.length} source(s) collected:`, ...lines].join("\n"));
		},
	});
}

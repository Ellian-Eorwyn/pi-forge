/**
 * The `.forge-workspace` marker: one definition for every JavaScript and
 * TypeScript caller.
 *
 * A directory holding this file contains machine artifacts, not vault notes.
 * `vault_schema.is_workspace_dir` (Python) and `countNotes` (TypeScript) skip
 * its whole tree, so a run's Markdown is never classified, refiled, counted, or
 * embedded -- refiling a run's own files would break the path references inside
 * them.
 *
 * The marker used to be written in exactly one place, `resolveWorkflowRoot`,
 * which only the web-research and vault-compose extensions call. Every other
 * skill creates its run directory with a plain mkdir, so its category folder
 * came out unmarked and its artifacts were counted as notes. The rule that
 * replaces that: whatever creates a directory for generated output marks it,
 * and it does so from here rather than from a fourth copy of the text.
 *
 * `forge/lib/vault_schema.py` carries the same two constants for the Python
 * skills. The strings must stay byte-identical: a vault ends up with markers
 * written by both, and a reader comparing two of them should not have to
 * wonder whether the difference means anything.
 */

import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

/** Marks a directory whose contents are machine artifacts, not vault notes. */
export const WORKSPACE_MARKER = ".forge-workspace";

export const WORKSPACE_MARKER_CONTENT = [
	"pi-forge workspace. Generated run directories live here.",
	"vault-organizer and vault-connections skip any directory containing this file.",
	"",
].join("\n");

/**
 * Mark `directory` as holding run artifacts, creating it if it does not exist.
 *
 * Idempotent, and never rewrites a marker that is already there: the three
 * markers already in the owner's vault carry hand-written wording, and
 * normalizing them would be a vault edit nobody asked for. Returns the marker
 * path.
 */
export function ensureWorkspaceMarker(directory) {
	mkdirSync(directory, { recursive: true });
	const marker = join(directory, WORKSPACE_MARKER);
	if (!existsSync(marker)) writeFileSync(marker, WORKSPACE_MARKER_CONTENT);
	return marker;
}

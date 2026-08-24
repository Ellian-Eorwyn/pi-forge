# pi-forge script/tool contract

Mechanical operations should be implemented as scripts/tools rather than encoded only as prose in `SKILL.md`.

Preferred contract:

- Accept structured JSON input via stdin or an explicit `--input` file.
- Return structured JSON output via stdout or an explicit `--output` file.
- Write logs/artifacts to declared paths.
- Avoid hidden global state.
- Validate inputs when practical.
- Return machine-readable errors.
- Support dry-run mode for filesystem-changing operations where practical.
- Preserve source files unless the user explicitly requests destructive changes.
- Record provenance for research, document, literature, web, and data workflows.

Scripts that process multiple files, rows, URLs, chunks, packets, pages, or
deliverables must also follow `RUN_STATE_CONTRACT.md`. Keep current state in
`run_state.json`, transitions in an append-only fsynced `run_events.jsonl`, and
domain manifests as atomic projections. Expose `status`, `refresh`, and `retry`
where applicable. A `next` call returns one bounded unit and `record` commits it
atomically so an unrecorded unit is returned again after interruption.

Scripts may also expose command-oriented CLIs when that is already the local
convention, but new tool extraction should move toward this structured result
shape so agents and MCP callers do not need to parse prose logs.

Successful result shape:

```json
{
	"status": "ok",
	"artifacts": [],
	"warnings": [],
	"errors": [],
	"data": null
}
```

Failure result shape:

```json
{
	"status": "error",
	"artifacts": [],
	"warnings": [],
	"errors": [
		{
			"code": "short_error_code",
			"message": "Human-readable error message"
		}
	],
	"data": null
}
```

`data` is tool-specific structured payload. Keep core execution facts, machine
readable summaries, parsed metadata, and counts there instead of requiring
agents to parse logs or Markdown reports.

## Bound what you return

A script's stdout is spent directly out of the calling agent's context window,
so size is part of the contract. Return counts, ids, artifact paths, warnings,
and the exceptions — the items that failed, were skipped, or need review. Write
the full material to a declared artifact and name its path in `artifacts`.

Per-item detail belongs in the run directory, not in the session. A script that
prints one summary per processed file spends the agent's window on material it
can read back on demand, and a large batch can exhaust that window before the
agent gets to act on any of it. Where a caller genuinely needs the whole set,
give it an explicit flag rather than making it the default.

## Keep stdout to the payload

In `--json` mode nothing may precede the JSON document on stdout -- not a
progress line, not a banner, not a warning. Callers parse the whole stream, so a
single extra byte is a crash rather than noise, and the traceback names the
caller rather than whatever printed.

An import counts. PyMuPDF's `fitz` shim prints its deprecation notice to stdout,
not stderr, which put a line of prose in front of two skills' payloads simply
because they imported it; `forge/lib/pymupdf_compat.py` exists to import that
library under a name that stays quiet. Progress and diagnostics go to stderr --
`literature-library.py` has the pattern as a one-line `progress()` helper.

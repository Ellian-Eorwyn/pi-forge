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

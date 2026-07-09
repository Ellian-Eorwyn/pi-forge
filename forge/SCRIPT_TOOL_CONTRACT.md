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

A tool result should include at least:

```json
{
	"status": "ok",
	"artifacts": [],
	"warnings": [],
	"errors": []
}
```

For failure:

```json
{
	"status": "error",
	"errors": [
		{
			"code": "short_error_code",
			"message": "Human-readable error message"
		}
	]
}
```

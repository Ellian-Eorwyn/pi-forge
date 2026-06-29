# Pi-forge MCP bridge

`pi-forge-mcp` exposes deterministic pi-forge transcription and file conversion
as local MCP tools. It uses stdio, processes one request at a time, and never
starts another language-model session.

## Configure a client

Install or update pi-forge, then register the launcher with explicit roots. The
exact outer configuration key varies by MCP client:

```json
{
  "mcpServers": {
    "pi-forge": {
      "command": "/Users/you/.local/share/pi-vault/bin/pi-forge-mcp",
      "args": [
        "--read-root",
        "/Users/you/Documents/Obsidian/My Vault",
        "--write-root",
        "/Users/you/Documents/Obsidian/My Vault/Forge Output"
      ]
    }
  }
}
```

Both options may be repeated. Roots must be existing absolute directories. The
server resolves real paths and rejects paths outside them, including symlink
escapes. `outputRoot` must already exist under a write root. Each operation
creates a new numbered run directory below it.

The portable caller instructions are in
`integrations/pi-forge-delegation/SKILL.md`. Register or copy that skill into the
calling harness according to its skill-discovery rules.

## Tools

`forge_transcribe` requires `inputPath`, `outputRoot`, and `recordingType`.
`projectDictionaryPath` is optional. It runs transcription `doctor` first;
missing dependencies return `dependency_not_ready` without installing anything.

`forge_convert_files` requires `inputPaths`, `target`, and `outputRoot`. Optional
fields are `sourceFormat`, `coverPath`, `title`, `author`, `language`, and
`date`. Conversion validation runs before the tool returns.

## Result contract

Every accepted operation returns schema version 1 with a task ID, operation,
status, timestamps, run directory, artifact list, counts, warnings, and a
structured error when applicable. Status is `success`, `needs_review`, `failed`,
or `canceled`. Invalid requests and disallowed paths are MCP errors.

Worker stderr is forwarded to server stderr. Stdout remains reserved for MCP
messages. Cancellation terminates the active worker; queued requests stay
serialized.

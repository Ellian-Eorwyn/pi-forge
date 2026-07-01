# pi-forge and pi-vault bidirectional integration handoff

This document is an implementation brief for a Codex session working across:

- pi-forge: `/path/to/pi-forge`
- pi-vault: `/path/to/pi-vault`

The first direction is implemented in pi-forge. The remaining work belongs
primarily in pi-vault, plus one small MCP-client extension in pi-forge for the
reverse direction.

## Existing pi-forge interface

Install or update pi-forge to expose:

```text
~/.pi-forge/bin/pi-forge-mcp
```

The server uses local stdio MCP and requires explicit roots:

```bash
pi-forge-mcp \
  --read-root "/path/to/allowed/input" \
  --write-root "/path/to/forge-output"
```

Options may be repeated. Real paths are checked, and traversal and symlink
escapes are rejected.

### `forge_transcribe`

Input:

```json
{
  "inputPath": "/absolute/path/recording.m4a",
  "outputRoot": "/absolute/path/forge-output",
  "recordingType": "lecture",
  "projectDictionaryPath": "/optional/absolute/dictionary.json"
}
```

`recordingType` is `lecture`, `interview`, `meeting`, `call`, `voice-note`, or
`other`. The tool runs the transcription doctor first. It never installs the
runtime or downloads the model. Output includes raw recognition, corrected
Markdown/text, timestamps, subtitles, corrections, warnings, and a manifest.
It does not run model-based transcript cleanup.

### `forge_convert_files`

Input:

```json
{
  "inputPaths": ["/absolute/path/source.docx"],
  "target": "md",
  "outputRoot": "/absolute/path/forge-output"
}
```

Targets are `md`, `docx`, `html`, `txt`, `epub`, `csv`, and `xlsx`. Optional
fields are `sourceFormat`, `coverPath`, `title`, `author`, `language`, and
`date`. Conversion validation runs before completion.

Both tools return schema version 1 with `taskId`, `operation`, `status`,
timestamps, `runDirectory`, `artifacts`, `counts`, `warnings`, and `error`.
Status is `success`, `needs_review`, `failed`, or `canceled`. Determine success
from `status`, never from prose or file existence.

## Required architecture

Use deterministic MCP tools, not agent-to-agent recursive model calls:

```text
pi-vault interactive agent
  -> pi-forge MCP server
  -> deterministic skill script
  -> structured artifact result
  -> pi-vault local import-proposal tool

pi-forge interactive agent
  -> pi-vault MCP server
  -> deterministic vault engine
  -> pending validated import proposal
```

Neither MCP server may launch the other interactive harness or request an LLM
completion. This is required because both harnesses share one local model. The
calling agent yields during a tool call; the MCP worker performs deterministic
work and returns before the caller resumes.

Do not make the deterministic pi-forge MCP server call back into pi-vault. When
pi-vault initiated a pi-forge job, pi-vault already owns the result and should
submit the artifact through its local vault tool. Reverse MCP is only needed
when the user is working interactively in pi-forge.

## pi-vault implementation

Start by following pi-vault's root `AGENTS.md`, `PROJECT_STATUS.md`,
`NEXT_ACTIONS.md`, and `DECISIONS.md`. Preserve its proposal/review/apply and
Git-backed rollback boundaries.

### 1. Add pi-forge client tools

Add an MCP client module under
`packages/coding-agent/src/pi-vault/`. Use the official
`@modelcontextprotocol/sdk` `StdioClientTransport` and `Client` APIs. Pin direct
dependencies exactly and follow pi-vault's lockfile and shrinkwrap rules.

Load configuration from vault-local `.pi-vault/config.yaml`:

```yaml
integrations:
  pi_forge:
    command: "/Users/you/.pi-forge/bin/pi-forge-mcp"
    read_roots:
      - "/Users/you/Documents/Recordings"
      - "/Users/you/Documents/Obsidian/My Vault"
    output_root: "/Users/you/Documents/Forge Output"
```

Requirements:

- Resolve and validate configured absolute paths before spawning.
- Connect lazily on the first tool call.
- Keep one client subprocess per pi-vault session.
- Close it idempotently on `session_shutdown`.
- Pass the pi tool's `AbortSignal` into the MCP call.
- Mark both registered tools `executionMode: "sequential"`.
- Forward structured MCP results without reducing them to unparseable prose.

Register native pi tools that mirror the MCP operations:

- `forge_transcribe`
- `forge_convert_files`

Add `packages/coding-agent/src/pi-vault/skills/vault-forge-delegation/SKILL.md`.
It should use the portable instructions in
pi-forge's `integrations/pi-forge-delegation/SKILL.md`, ask for missing required
inputs, wait for the synchronous result, and report artifacts and warnings.
Update pi-vault's asset-copy and skill-discovery tests so the skill is present
in source and built distributions.

### 2. Add a proposal-first artifact import service

Create one shared deterministic service used by both pi-vault's built-in tool
and its MCP server. Do not duplicate vault mutation logic in the MCP layer.

Version 1 accepts `.md` and `.txt` artifacts only. Reject DOCX, EPUB, PDF, media,
and other binary formats with `unsupported_artifact_format`; those need a later
reviewed attachment-import operation. Convert them to Markdown through
pi-forge first when appropriate.

Service input:

```json
{
  "sourcePath": "/absolute/path/forge-output/transcription/example/corrected_transcript.md",
  "suggestedName": "Example transcript.md",
  "title": "Example transcript",
  "sourceTaskId": "pi-forge-task-uuid",
  "sourceOperation": "transcribe"
}
```

Behavior:

1. Resolve the source real path and require it to be under an explicitly allowed
   import root.
2. Require an initialized vault and read the configured inbox directory.
3. Hash the source with SHA-256 and read it without modifying it.
4. Normalize `.txt` to a Markdown file without changing the source text.
5. Choose a filesystem-safe destination under the configured inbox only. An
   optional `suggestedName` may change the filename, never the destination root.
6. Create a normal pending pi-vault proposal containing a `write_file`
   operation with `if_exists: "fail"`. Embed the content in the proposal so its
   application does not depend on the external source remaining available.
7. Include the source path, SHA-256, pi-forge task ID, and operation in the
   proposal summary or an explicitly validated provenance field. Do not add new
   note frontmatter properties.
8. Run the existing proposal validator and `review-proposals --dry-run` before
   returning.
9. Never approve or apply the proposal automatically.

Return:

```json
{
  "schemaVersion": 1,
  "status": "pending_review",
  "sourcePath": "/absolute/source.md",
  "sourceSha256": "...",
  "destinationPath": "01 Inbox/Example transcript.md",
  "proposalPath": "00 System/0.01 agent/review/proposals/import-....json",
  "reviewValid": true,
  "warnings": []
}
```

Expose the service inside interactive pi-vault as `vault_submit_artifact`. This
lets pi-vault call pi-forge, inspect the returned artifact, and submit it without
looping through another process.

### 3. Add the pi-vault MCP server

Implement a local stdio server using the same shared service. Recommended source
locations:

- `packages/coding-agent/src/pi-vault/mcp-server.ts`
- `packages/coding-agent/src/pi-vault/mcp-cli.ts`

Install a `pi-vault-mcp` launcher alongside `pi-vault`:

```bash
pi-vault-mcp \
  --vault-root "/Users/you/Documents/Obsidian/My Vault" \
  --read-root "/Users/you/Documents/Forge Output"
```

Require exactly one initialized vault root and at least one repeated read root.
Resolve real paths and reject traversal and symlink escapes. Keep stdout strictly
MCP JSON-RPC; worker and diagnostic logs go to stderr.

Expose two tools:

- `vault_status`: read-only machine status for the configured vault.
- `vault_submit_artifact`: invoke the proposal-first service above.

Do not expose arbitrary `vault-agent` commands, arbitrary shell execution,
approval, `apply-approved`, or undo through this server. The remote pi-forge
agent may submit work for review; it may not approve or apply vault mutations.

### 4. Add the reverse pi-forge client

In pi-forge, add `forge/extensions/pi-vault-client.ts` and a visible
`vault-handoff` skill. Pi has no built-in MCP client, so the extension must use
the already pinned MCP SDK and register one native pi tool:

- `pi_vault_submit_artifact`

Use an explicit JSON configuration file, defaulting to:

```text
${PI_FORGE_HOME:-~/.pi-forge}/agent/vault-bridge.json
```

Schema:

```json
{
  "command": "/Users/you/.local/bin/pi-vault-mcp",
  "vaultRoot": "/Users/you/Documents/Obsidian/My Vault",
  "readRoots": ["/Users/you/Documents/Forge Output"]
}
```

The extension should:

- Validate the configuration before spawning.
- Connect lazily and close on `session_shutdown`.
- Register the tool with sequential execution.
- Pass cancellation through to MCP.
- Return pi-vault's structured proposal result.
- Never invoke `apply-approved`.

The skill should trigger when the user says “send this to my vault”, “add the
result to Obsidian”, or equivalent. It must:

1. Select a completed `.md` or `.txt` artifact from the current pi-forge result.
2. Report outstanding pi-forge warnings before submission.
3. Call `pi_vault_submit_artifact` with the pi-forge task ID and operation.
4. Report the pending proposal and destination.
5. Tell the user to review/apply it from pi-vault; never claim the note was
   integrated while status is `pending_review`.

## Test requirements

In pi-vault:

- MCP initialization, tool discovery, stdio framing, and shutdown.
- pi-forge client tool discovery and structured result forwarding with a fake
  MCP server.
- Cancellation and `session_shutdown` terminate subprocesses.
- Concurrent forge calls serialize.
- Missing or malformed integration configuration fails clearly.
- Artifact paths outside read roots and symlink escapes fail.
- `.md` and `.txt` produce pending proposals under the configured review path.
- Binary formats are rejected.
- Destination collisions remain pending failures; existing notes are untouched.
- Dry-run validation failure returns an error and creates no applicable
  proposal.
- No tool can approve or apply a proposal.
- Asset-copy/build tests include the new skill and MCP entrypoint.

In pi-forge:

- Reverse client connects to a fake pi-vault MCP server.
- The registered tool forwards structured proposal results and cancellation.
- Missing configuration is non-destructive and actionable.
- The vault-handoff skill is model-visible and included in
  `FORGE_SKILLS.md` accounting.

Run each repository's required targeted tests and full `npm run check`. Run
pi-vault's Python suite because the proposal service changes its engine:

```bash
cd /path/to/pi-vault/vault-manager
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
```

## End-to-end acceptance

1. Install/update both repositories.
2. Configure pi-vault's pi-forge client and pi-forge's vault bridge.
3. From pi-vault, transcribe a small fixture through `forge_transcribe` and
   verify the local model receives no competing request.
4. Submit `corrected_transcript.md` through the local
   `vault_submit_artifact` tool.
5. Confirm a valid pending proposal exists and no inbox note exists yet.
6. Review and explicitly approve/apply through pi-vault.
7. Confirm the inbox note preserves the artifact and the version log reports a
   rollback command.
8. From interactive pi-forge, ask to send a completed Markdown artifact to the
   vault.
9. Confirm pi-forge calls `pi-vault-mcp`, receives `pending_review`, and does
   not claim final integration.
10. Review/apply from pi-vault and run `obsidian-check --json`.

## Handoff prompt

Use this prompt in the next Codex session:

> Implement the bidirectional pi-forge/pi-vault MCP integration described in
> `/path/to/pi-forge/docs/pi-vault-bidirectional-integration-handoff.md`.
> Work primarily in `/path/to/pi-vault`, starting with its
> `AGENTS.md`, `PROJECT_STATUS.md`, `NEXT_ACTIONS.md`, and `DECISIONS.md`.
> Preserve pi-vault's proposal/review/apply boundary: external artifacts may
> create validated pending proposals but may never be auto-approved or applied.
> Then add the documented reverse MCP-client extension and vault-handoff skill
> in `/path/to/pi-forge`. Use one local model sequentially;
> neither MCP server may launch another agent or model request. Run all targeted
> tests and each repository's required full checks. Do not commit unless asked.

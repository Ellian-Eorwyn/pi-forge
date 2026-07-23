# Vault Organizer Schema Contract

`vault-organizer` reads the vault's canonical Markdown schema note, defaulting
to `99 Meta/99.02 Schemas/0.00 Vault Schema.md` and falling back to the unique
`0.00 Vault Schema.md` anywhere in the vault outside `00 Inbox`. The schema
note is authoritative; cached JSON under `.vault-organizer/cache/` is generated
state.

## Parsed Sections

The compiler parses these exact Markdown sections and fails closed when a
required section, required table column, or row is malformed:

- `Approved properties`
- `Note types`
- `Status values`
- `Domains`
- `Subdomains`, with one `### <domain>` subsection per domain that has
  subdomains
- `Project registry`
- `Source kinds`
- `Capture types`
- `Legacy normalization map`
- `Folder routing`

The parser uses stable headings and table columns. It does not use an LLM to
parse the schema and does not reconstruct routes from prose examples.

## Routing

Only `domain`, `subdomain`, and `project` determine the destination folder.
Folder names are derived by code:

- domain: `<pad2(domain.number)> <domain.label>`
- subdomain: `<domain.number>.<pad2(subdomain.number)> <subdomain.label>`
- domain project: `<domain.number>.<pad2(project.number)> <project-name>`
- subdomain project:
  `<domain.number>.<pad2(subdomain.number)>.<pad2(project.number)> <project-name>`

The script refuses unregistered values, absolute paths, `..` traversal, unsafe
labels, duplicate derived destinations, and destination collisions.

## Frontmatter

Existing YAML frontmatter is untrusted input. The script discards it and emits a
new canonical block using the schema property order. List-valued properties are
always block lists. Wikilinks are quoted. Empty optional values are omitted.

Malformed opening frontmatter delimiters without a closing `---` are not guessed
or repaired. The note is left unchanged and added to the review queue.

## Run State and Resume

Every invocation is a durable run under `.vault-organizer/runs/<timestamp>/`
following the repository run-state contract: `run_state.json` (options
fingerprint, phase, per-note statuses), `run_events.jsonl` (fsynced phase
journal), `scan.json` (input snapshot with content hashes), `dedupe.json`
(duplicate plan), `classified.jsonl` (fsynced per-note classification
journal), `plan.json`, `report.md`, `review-queue.jsonl`, and
`apply-log.jsonl`.

- `--run <dir>` resumes: journaled classifications are reused, apply
  operations already logged `ok` are skipped, and input drift since the scan
  is reported as warnings. Files changed after planning are refused at apply
  by SHA-256 re-check.
- Resuming with different options (model, endpoints, thresholds, limit,
  schema hash) is refused via the options fingerprint; start a new run.
- A vault-level lock (`.vault-organizer/.run.lock`) serializes runs; a stale
  lock from a dead process is reclaimed automatically.
- The whole run is idempotent: re-running a completed run re-derives the same
  plan and applies nothing new.

## De-duplication

Dedupe runs before classification so duplicate losers never consume model
calls.

- The dedupe identity is the SHA-256 of the body after stripping frontmatter,
  normalizing line endings, right-trimming lines, and trimming blank edges.
  Empty bodies never form duplicate groups.
- Exact groups pick one canonical winner: filed-outside-inbox beats inbox,
  then larger raw file (richer frontmatter), then non-temporary basename,
  then earlier mtime, then lexicographic path.
- Near-duplicate candidates are blocked on shared normalized basename stem,
  shared title, or shared first line (renamed near-duplicates are therefore
  not detected; this is stated in the report). Candidate pairs are scored
  with local embeddings; auto-resolution requires cosine at or above the auto
  threshold (default 0.97) and line containment at or above 0.90, keeping the
  copy with the richer body. Pairs scoring between the review threshold
  (default 0.90) and auto, or failing containment, are reported for review
  and both copies proceed to classification.
- Losers are moved (never deleted) to `.vault-organizer/duplicates/<original
  path>` with numeric suffixes on collisions, only during `--apply`, after
  SHA-256 re-verification, and each move is journaled in `apply-log.jsonl`.
- Embedding vectors are cached in `.vault-organizer/cache/embeddings.jsonl`
  keyed by body hash and model. If the embeddings endpoint is unavailable the
  run degrades to exact-only dedupe with a warning.
- Inbox mode also compares against `.vault-organizer/cache/vault-index.json`,
  a content index of filed notes refreshed lazily by size and mtime and after
  every apply. A filed copy always wins auto-resolution; an inbox copy with a
  richer body is reported for review instead. The schema note is part of the
  index, so stray copies of it resolve as ordinary duplicates.

## Prompt Caching and Reasoning Suppression

The compiled schema is serialized canonically into a byte-stable system
message shared by every request, and requests set `cache_prompt: true`
(disable with `--no-cache-prompt` for servers that reject it). The per-note
user message carries only the title, current path, the previous frontmatter
as untrusted advisory context (capped), and the body excerpt. Repair requests
append to the user message so the cached prefix survives.

The default chat endpoint (`http://llms:8004/v1/chat/completions`) is a
non-thinking configuration, so no reasoning-suppression trick is needed and
classification runs directly. Pointing at a thinking backend instead (for
example `--base-url http://llms:8008/v1/chat/completions`) wastes thousands of
hidden thinking tokens per note; add `--think-prefill` in that case to end
each request with a closed empty `<think></think>` assistant turn that
llama.cpp-style servers continue from, skipping reasoning (observed ~10x
speedup). Either way the response parser strips a leading think block and code
fences before JSON parsing, so a thinking backend used without the flag still
produces valid output (just slowly). `--think-prefill` is part of the
classification cache key and the run options fingerprint.

## Model Output

The model returns JSON only:

```json
{
  "metadata": {
    "type": "note",
    "status": "active",
    "domain": "technology",
    "subdomain": "obsidian",
    "project": "[[Pi Forge]]",
    "parent": "[[Vault Organization]]",
    "people": [],
    "organization": null,
    "related": [],
    "source_kind": null,
    "capture_type": "manual"
  },
  "needs_review": false,
  "review_reason": null,
  "suggestions": []
}
```

The validator treats model output as untrusted. It validates keys, shapes,
controlled values, wikilink syntax, project inheritance, conditional
`source_kind`, and control characters. One repair request is allowed after an
invalid response; if the repair fails, the note remains unchanged and enters the
review queue. Transient endpoint failures retry up to three times.

`suggestions` is an optional list of short strings proposing schema additions.
Suggestions are aggregated into the report's Schema Suggestions section for
the human maintainer and are never applied to the schema or to any note.

## Review Routing

Notes that cannot be confidently classified (model review, validation
failure after repair, empty body, malformed frontmatter, or a destination
collision) follow the schema's own inbox contract:

- In `vault` mode they are moved byte-intact into `00 Inbox/` (numeric suffix
  on name collisions) with the reason recorded in `review-queue.jsonl` and
  the report. A whole-vault run is the schema's "explicit migration command",
  which is what permits moving filed notes at all.
- In `inbox` mode they stay exactly where they are with the reason recorded.
- Notes that failed to read at all are left in place and reported.

## Apply

Dry run is the default. With `--apply`, the script executes quarantines, then
inbox review moves, then rewrites. For every operation it re-reads the
source, verifies the recorded SHA-256, backs up the original under the run
directory, and either renames (moves) or writes through a temporary file with
fsync (rewrites). Every operation is journaled in `apply-log.jsonl`, which is
never truncated; on resume, operations already logged `ok` are skipped. It
never overwrites existing files and does not delete backups automatically.
After a successful apply the vault content index is refreshed. `.base` files
are never modified; the report lists `.base` files that reference moved
notes.

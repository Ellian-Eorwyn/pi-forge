# Vault Connections Contract

`vault-connections` reads the same human-maintained Markdown schema note as
`vault-organizer`, through the shared `forge/lib/vault_schema.py` compiler, so
both tools derive identical folder paths and property order. The schema note is
authoritative; everything under `.vault-connections/` is generated state.

## Storage layout

All state lives inside the vault, in a dot-directory that Obsidian ignores and
that `selected_notes()` excludes from every scan.

```
<vault>/.vault-connections/
  cache/compiled-schema.json     compiled schema, keyed by the schema note's SHA-256
  cache/notes.json               note index, refreshed by size + mtime
  cache/vectors.json             {model, dims, rows: {body_hash: row_index}}
  cache/vectors.f32              concatenated float32 rows, one per body hash
  decisions.jsonl                every accepted and rejected pair, appended forever
  runs/<timestamp>/              run_state.json, run_events.jsonl, candidates.json,
                                 judged.jsonl, proposals.jsonl, report.md,
                                 apply-log.jsonl, backup/
```

Vectors are stored as raw `array('f')` rows rather than JSON numbers: ~10 KB per
note instead of ~26 KB, loaded with one `frombytes` call instead of parsing
millions of JSON floats. This matters because `search` is interactive. The
sidecar and the binary are cross-checked on load (`len(data) == dims * len(rows)`);
any mismatch, model change, or dimension change discards the store and re-embeds
rather than returning wrong vectors. The store is compacted when fewer than 80%
of its rows are still live.

These caches are derivable from the notes and should be git-ignored. The
decisions ledger and the run reports are small and worth versioning.

`vault-organizer`'s own `.vault-organizer/cache/embeddings.jsonl` is untouched.
The two stores hold different text — the organizer embeds title + body for
duplicate detection, this embeds title + heading outline + body for topical
similarity — so they are deliberately not shared.

## Index

One entry per Markdown note outside the protected directories, excluding the
schema note. Entries carry the title, frontmatter `type`/`domain`/`subdomain`,
the heading outline, every wikilink target found in frontmatter and body, a
truncated search text, the body hash, and the file SHA-256. Unchanged files are
reused by size and mtime, so a refresh only re-reads what moved.

Notes with fewer than 80 normalized body characters are indexed but never
embedded; they are too short to place topically and would generate noise.

## Search

Reciprocal-rank fusion of two rankings, `1/(60 + rank)` each:

- lexical: BM25-shaped scoring over title, headings, and search text;
- semantic: cosine against the query embedding.

An exact filename match adds 1.0; a title substring match adds 0.25. If the
embeddings endpoint is unreachable the command still returns lexical results and
reports `"ranking": "lexical"` with a warning. It never fails hard.

## Candidate selection

Candidates are chosen primarily by **rank**, because the embedding model's
absolute scores are compressed into a narrow high band on a personal vault. On a
1,051-note vault, Qwen3-Embedding-4B produced this whole-corpus distribution:

| bucket | ≥0.95 | ≥0.90 | ≥0.85 | ≥0.80 | ≥0.75 | ≥0.65 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| pairs | 289 | 2,014 | 11,179 | 40,316 | 108,455 | 342,535 |

The mode sits near 0.65, so a low floor admits most of the corpus and lets top-K
do all the work. Hence the defaults:

1. Score all pairs. Each note keeps its top `--per-note` (default 5) neighbors
   within `[--min-similarity, --max-similarity)` — default `[0.75, 0.97)` — held
   in a bounded min-heap so a hub note similar to hundreds of others still costs
   O(per-note) memory.
2. Union the per-note neighbor sets, dedupe, sort by similarity, cap at
   `--max-candidates` (default 400).
3. Drop pairs that are already linked in either direction — via any frontmatter
   wikilink or any body wikilink — pairs already in `decisions.jsonl`, and pairs
   involving `00 Inbox`.

The upper bound matters: a pair at or above `--max-similarity` is a near-duplicate,
not a connection. Those are counted separately, listed in `candidates.json` under
`nearDuplicates`, and reported as a warning pointing at `vault-organizer`, which is
the tool that de-duplicates. The default 0.97 matches its near-duplicate threshold.

The report prints the full similarity histogram, so both bounds can be retuned
against real data after a first run.

The all-pairs pass is O(n²) in pure Python: about 45 seconds for 1,000 notes at
2560 dimensions. No approximate index is needed at personal-vault scale.

## Judgment

One chat call per surviving pair, with a byte-stable system message and
`cache_prompt: true` so the prefix stays cached. The model returns JSON only:

```json
{"connect": true, "strength": "strong", "kind": "generalization", "reason": "…"}
```

`strength` is `strong`, `moderate`, or `weak`. `kind` is `same-topic`,
`generalization`, `application`, `contrast`, or `shared-entity`. Output is
untrusted: keys, enum membership, control characters, and reason length are all
validated, and an invalid or failed judgment drops the pair with a warning
rather than guessing. Transient endpoint failures retry up to three times.

Every judgment, including rejections, is journaled to `judged.jsonl`.

## Frontmatter merge

The only mutation to an existing note. It is textual, not a YAML round-trip:

1. Locate the opening and closing `---` lines; refuse if either is missing.
2. Parse the block only to learn which wikilinks are already present anywhere in
   the frontmatter, so nothing is added twice.
3. Find the `related:` key. If it exists as a block list, append the missing
   items using the list's own indentation. If it is empty or `[]`, start a block
   list. If it is an inline list with content, refuse — that shape is reported,
   never rewritten.
4. If `related:` is absent, insert it after the last present property that
   precedes it in the schema's `property_order`.
5. Reassemble the delimiters, the modified block, and the original body.

The body, the BOM, the line endings (`\r\n` is preserved and used for inserted
lines), and every other key — including keys the schema would not approve — come
through byte-identical. A note with no frontmatter is refused with a reason, not
given one: that is `vault-organizer`'s job.

## Wiki layer

Requires a `wiki` domain in the schema note with subdomains `concepts`,
`practices`, `places`, `events`, `terms`, `works`, and `figures`. Without them the command
fails closed and names what is missing; the skill never edits the schema itself.

The schema mappings are fixed:

| Wiki kind | Frontmatter type | Schema route |
| --- | --- | --- |
| concept | `concept` | `wiki/concepts` |
| practice | `concept` | `wiki/practices` |
| place | `place` | `wiki/places` |
| event | `event` | `wiki/events` |
| term | `concept` | `wiki/terms` |
| work | `work` | `wiki/works` |
| figure | `person` | `wiki/figures` |

**Stub candidates.** Wikilink targets with no note of that basename, appearing in
at least `--min-mentions` (default 2) notes. The model classifies each target as
one of the six wiki kinds, `person`, `organization`, or `skip`. Wiki kinds get a
proposed stub at the compiled path for their subdomain, with a generated
frontmatter block, a one-paragraph summary, up to eight mentioning notes in
`related`, and a `## Mentioned in` list of all of them. Because the mentioning
notes already contain the wikilink in their bodies, no edit to them is needed.

Two classes of target are diverted before the model ever sees them or before a
stub is proposed, because they belong to `vault-organizer`'s routing rules:

- **Registered projects.** A target matching a wikilink in the schema's project
  registry is never made a wiki note. Its link resolving to nothing means the
  project note itself is missing — a gap in the project tree, reported under
  "Registered projects whose project note is missing". Without this guard the
model, seeing only mention context, invents a plausible definition for a
project name and files it under Works.
- **People and organizations.** Reported as `08 Directory` candidates.

**Collision guard.** A proposed stub whose title matches any existing note
basename, case-insensitively, anywhere in the vault, is blocked and reported.
Obsidian resolves `[[Name]]` by basename, so a duplicate basename makes link
resolution ambiguous. This matters most when the model canonicalizes a target
into a name that already exists.

**Backfill.** For each note already filed in the wiki domain, notes that either
name it literally in their text or exceed `--min-similarity` become link
proposals, capped at `--per-note` each. Literal mentions rank first and are
labeled `strong`.

## Research-to-vault import

`import-run` recognizes completed literature, meta-literature, and deep-research
runs from their durable artifacts. It invokes the owning workflow's validator in
read-only mode before proposing anything:

- literature defaults: `literature_summary.md`, `key_terms.md`;
- meta-literature defaults: `meta_synthesis.md`, `concept_register.md`;
- deep-research default: `deep_research_report.md`.

`--include-artifact` adds another run-relative Markdown file. Absolute paths,
parent traversal, non-Markdown files, and symlink escapes are refused. Inbox
filenames combine the input/corpus name or research question with the artifact
role. Unsafe names and case-insensitive basename collisions anywhere in the
vault are blocked.

For each inbox proposal, existing frontmatter is discarded and the body after
its closing delimiter is preserved exactly. The shared schema-classification
helper chooses metadata, then `status: complete` and
`capture_type: generated` are forced. A classified `type: source` also receives
`source_kind: generated`. Frontmatter is serialized in schema order.

Wiki candidates are harvested from structured item, claim, evidence, and source
records. Model output must cite real upstream identifiers; unsupported
candidates are discarded. Candidates are deduplicated by canonical
case-insensitive title and their evidence is merged before proposal creation.
Defaults are `concept,term`; all seven kinds in the table above may be selected.

### Vault-owned templates

The template directory is compiled from the schema's `meta/templates` route.
These exact files are required only for selected import kinds:

```text
Wiki Concept.md
Wiki Practice.md
Wiki Place.md
Wiki Event.md
Wiki Term.md
Wiki Work.md
Wiki Figure.md
```

Every template must classify as:

```yaml
---
type: template
status: active
domain: meta
subdomain: templates
capture_type: manual
---
```

Its body must contain each placeholder exactly as a replaceable token:

```markdown
# {{title}}

{{summary}}

## Evidence

{{evidence}}

## Sources

{{sources}}

## Provenance

{{provenance}}
```

Additional prose is allowed, but unknown placeholders are refused. Generated
entity frontmatter is always produced from the schema, never copied from the
template. `doctor` reports readiness without making missing templates a
non-import failure. Pi-Forge never creates or edits these vault-owned files.

Proposal generation writes only under `.vault-connections/runs`. The source
run's structured inputs and selected artifacts, the schema, every selected
template, import options, and the completed proposal manifest are hashed into
run state. Rejections are keyed by the source-run fingerprint. At apply time
the full accepted creation batch is bound to the originating vault and
preflighted before any write; proposal tampering, schema, source, or template
drift, an invalid schema-compiled route, and new collisions refuse the batch.

## Apply

`apply` needs `--accept` and/or `--reject`; unknown proposal ids are refused
before anything is written. `--dry-run` reports the identical operation list
without touching the vault.

- Link edits are grouped by file, so a note named by several accepted proposals
  is read once, merged once, and written once.
- Link-source hashes are checked before any accepted link edit. A changed note
  refuses the batch unless it already contains the reviewed link.
- Every rewritten note is copied to `<run>/backup/<relative-path>` first.
- Writes go through a temporary file with `fsync` and an atomic rename, as raw
  bytes so a BOM survives.
- New notes use an atomic no-overwrite create. Import batches roll back files
  created by that invocation if a later create fails.
- Every operation is journaled to `apply-log.jsonl`.
- Accepted and rejected ids are appended to `decisions.jsonl` keyed by the note
  pair, so a rejected pair never returns in a later `propose` run.
- The note index is refreshed afterward.

Re-applying the same ids adds nothing: link merges report `already linked`, and
an imported destination with byte-identical content is treated as already
applied. Nothing is ever deleted, moved, or renamed.

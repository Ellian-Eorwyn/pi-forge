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

### The sources tree

A schema that declares a **Sources root** section files every `type: source`
note by its `source_kind` instead, under one top-level tree:

- sources root: `<pad2(root.number)> <root.label>` — `10 Sources`
- source kind: `<root.number>.<pad2(kind.number)> <kind.label>` — `10.01 Book`
- then the note's `domain` and `subdomain` **labels**, unnumbered:
  `10 Sources/10.01 Book/Academic/Dissertation`

`project` is deliberately not read for a source: a source belongs to its kind
whichever projects happen to cite it. `domain` stays required and keeps its
meaning; only the derivation changes. The unnumbered tail is what lets the tree
grow a folder per domain without ninety more registry rows — drift checking
treats anything unnumbered below a declared route as detail (see Schema Drift),
while the numbered kind folders are declared routes and are checked normally.

Declaring the root makes the table form of **Source kinds** mandatory (`Value`,
`Number`, `Label`, `Definition`); a bullet list with a root declared is a parse
error rather than a half-routed vault. Omitting the section restores filing by
domain, so the switch is one section in the schema note. The root's number is
reserved against domains the way `0` is reserved for the inbox.

Because `source_kind` now selects a tree rather than only describing a note,
`carry_forward_provenance` pins `type: source` and a schema-valid `source_kind`
from the note's previous frontmatter over the classifier's answer, and carries
`parent` with them. The writers of those values are scripts that knew what they
were making — a transcript's recording half, an imported artifact — and a
classifier reading a wall of timestamped speech has been seen to call it a
meeting, which under kind routing moves the note rather than just mislabelling
it.

## Schema Drift

`validate_derived_paths` only catches two *declared* routes colliding with each
other. Nothing else checks the compiled routes against the folders that exist,
and `apply_move_operation`/`apply_rewrite_operation` create a destination on
demand — so a route naming a folder that is not there silently creates a second
folder beside the one the notes are in, and Johnny Decimal's one-number-one-place
guarantee is lost with no error raised. `check_schema_drift` in
`forge/lib/vault_schema.py` closes that seam. It is pure filesystem plus the
compiled schema: no model, no embeddings, no network. It walks the vault with
the exclusions `selected_notes` uses (`PROTECTED_DIRS`, dotfiles, symlinks,
`.forge-workspace` trees), so a folder invisible to filing is invisible here.

Finding kinds, in severity order:

| kind | severity | Condition |
| --- | --- | --- |
| `number_collision` | high | A compiled route's number is held by a different folder under the same parent, or an undeclared folder shares a number with a declared one |
| `label_moved` | high | A folder carries a compiled route's exact label at a different number |
| `undeclared_with_notes` | medium | A folder no route names, holding `.md` files |
| `undeclared_empty` | low | The same, holding none |
| `declared_absent` | info | A compiled route with no folder — a reserved slot, created on first use |
| `property_unapproved` | medium | A property in use that **Approved properties** does not list |
| `property_obsidian_builtin` | low | `tags`, `aliases`, or `cssclasses` in use but undeclared |
| `property_type_mismatch` | medium | Obsidian's registered property type contradicts the schema's `shape` |
| `property_unused` | info | An approved property no note uses |

Two rules keep the output usable. **Substructure is never a finding**: any
folder whose ancestor chain reaches a compiled route is legitimate detail below
it (`99.05 Attachments/Images`), and only the topmost undeclared folder is
reported, so a whole undeclared tree is one finding and not five. The exception
is a folder carrying a Johnny Decimal number, which is claiming a slot rather
than adding detail, and is reported even directly below a declared route.
Without this ranking a real vault yields ~20 findings of which ~3 matter, and a
checker nobody reads is how a live collision survives.

Ids are derived from the finding's content, not its position, so an id copied
out of a report cannot come to address something else before it is applied.

The four `property_*` kinds are the exception to "pure filesystem". Comparing the
**Approved properties** table against the properties a vault actually holds needs
an index of every note's frontmatter, which Obsidian keeps in memory and pi-forge
would have to build by walking and parsing the whole vault. So
`check_property_drift` lives in the skill script rather than in
`forge/lib/vault_schema.py` — whose docstring commits to determinism and the
standard library, and which both skills depend on staying that way — and it
returns an empty list when the Obsidian CLI is unavailable. Everything else in
this section is checked identically with or without it; the report says which
happened. None of the four exceeds `medium`, by construction, so the `high`-only
gate on `--apply` cannot be tripped by vocabulary, and all four are
`fix_side: manual`, which `apply_schema_fixes` already refuses by name.

Each `high` finding names the cheaper side to change: **count the notes under
the existing folder and under the compiled route, and change whichever holds
less**, because a folder rename moves notes and a schema row edit moves none.
Fewer at the route means edit the schema; fewer on disk means rename the folder;
a tie proposes the schema edit and says the other direction is equally cheap. A
swap — where the wanted number already belongs to another row — has no
single-cell schema fix at all, so the folders are the only side that can move.

### `--fix-schema`

`drift --fix-schema <id>,<id>` is the only code in this workflow that writes the
schema note, and it is fenced accordingly:

- Bare `drift` is always read-only. Only `--fix-schema` writes.
- Explicit ids only. There is no `--all`, and an unknown id is refused with the
  ids the vault actually reports.
- Schema side only. It never renames, moves, or deletes a folder, and never adds
  a row — registering a new domain is the user's edit to make, not a definition
  for the tool to invent. Folder-side and manual findings are refused by name.
- The note is copied to `<run-dir>/backup/` before anything is written.
- The edit is surgical: the row is matched by its `Value` cell and only the
  `Number` cell changes, preserving the row's `Definition` prose and the cell's
  spacing and backtick style. Every other byte of the note is untouched.
- The result is written to a temp file, read back, re-parsed through
  `parse_schema_note` and `validate_derived_paths`, and re-checked for drift. If
  it does not parse, or a new `high` finding appears that was not there before,
  the temp file is discarded and nothing is written. Commit is `os.replace`.

The compiled-schema cache is keyed by the note's SHA-256, so it invalidates
itself after a write.

`doctor` reports the same findings under `checks.drift` and a `high` finding
makes it exit non-zero. `organize` runs the check before the run lock and before
any classification, and refuses `--apply` while a `high` finding stands unless
`--allow-schema-drift` is passed. Every finding at every severity goes into
`plan.json` and the report's `## Schema Drift` section, but only `high` and
`medium` become warnings — a run that warns about reserved slots every time
trains the reader to skip the section the real collisions appear in. That flag is deliberately not tracked
into resumed run state: an override granted for one apply must not persist.

## Frontmatter

Existing YAML frontmatter is untrusted input. The script discards it and emits a
new canonical block using the schema property order. List-valued properties are
always block lists. Wikilinks are quoted. Empty optional values are omitted.

Malformed opening frontmatter delimiters without a closing `---` are not guessed
or repaired. The note is left unchanged and added to the review queue.

## Date Backfill

`dates` fills a scalar human-owned property from evidence the notes and older
copies of them already carry. The extraction, matching, and write live in
`forge/lib/vault_dates.py`, which is pure standard library: no model, and no
embeddings unless `--near-match` is passed. A date written into a note is not a
proposal a later run revisits, so it has to be reproducible from the files
alone.

The property is read from the schema at runtime and refused if the schema does
not approve it, so the mode is correct whichever properties a given vault
declares. It is refused outright for a list-shaped property, and warns when the
property is not marked human-owned — filing replaces frontmatter wholesale, so
a value written to a classifier-owned property is gone on the next run.

### The two axes

Confidence is the weaker of them, and only `high` is written without a human
naming the proposal's id.

| Evidence | Meaning |
| --- | --- |
| `explicit` | A machine wrote it somewhere structural: a `created`/`date-created`/`createdAt`/`ctime` frontmatter key, a filename date, an Obsidian unique-note id, or a `YYYY/MM/DD.md` daily-note path |
| `stated` | A person typed it under a label in the first 20 lines, or linked a daily note there |
| `weak` | A bare date somewhere in the body, or a filesystem timestamp under `--include-file-times` |

| Match | Rule |
| --- | --- |
| `self` | The vault note's own evidence — there is no matching step to get wrong |
| `identical` | Equal body hash, the dedupe identity from De-duplication above |
| `named` | Equal normalized basename stem, unique on both sides |
| `titled` | Equal H1, unique on both sides |
| `similar` | `--near-match` only: cosine at or above `--near-dupe-auto` with line containment at or above 0.90 |

`high` is `explicit` evidence on a `self`, `identical`, `named`, or `titled`
match. `weak` evidence is `low` and never auto-applies whatever the match.
Everything between is `medium`.

Two demotions from `high` to `medium`. A `type: source` or `type: wiki` note,
because its subject date is the work's rather than the day the note was made.
And a file whose own filename and frontmatter give different explicit dates,
which means one of them is not describing that file.

### Calibrating Finder creation dates

`st_birthtime` is what Finder shows as Date Created. Whether an archive's copies
still carry a real one depends entirely on how the archive was made — a Finder
move preserves it, a copy resets it — and that is not guessable from the file,
so `calibrate_birthtime` measures it rather than assuming either way.

Files carrying both a creation date and an `explicit` date from their name or
frontmatter are labelled examples. The share of those where the two agree
estimates the accuracy of the creation date on the files that state nothing.
Reported alongside it is the largest single-day cluster: when a big share of the
archive was created on one day, that day is the copy, and the agreement rate is
being measured on the survivors of a different history.

The measurement is always computed and always reported. `--trust-birthtime`
acts on it by promoting a creation date from `weak` to `explicit`, which is what
makes it auto-appliable. A modification time is never promotable — nothing makes
it a creation date — so `--include-file-times` only ever adds `weak` evidence.

### Choosing between candidates

Evidence tier decides first; inside it the earliest date wins. Several archive
copies of one note is the normal case, not a conflict — the oldest copy is the
one that dates the note's creation, and a revision saved later under a newer
name must not overwrite it. Every candidate considered is listed in the report
with the exact substring it came from.

Ambiguity is dropped rather than resolved. `11-04-2023` is read only when one of
the two positions cannot be a month; where both readings are dates, no candidate
is produced. Name and title matching requires uniqueness on *both* sides, so an
archive holding two files called `Notes.md` contributes neither.

### Writing

Ids are derived from the note path and the chosen date, so an id copied out of a
report cannot come to address something else before it is applied. `--ids`
accepts only ids the current run proposes; an unknown one is refused with the
ids on offer.

The write is an additive frontmatter merge: the key is placed by the schema's
property order and exactly one line is added. Body, delimiters, BOM, line
endings, and every other property are preserved byte-for-byte, which is why it
goes through a bytes-level atomic write rather than a text one — text mode
would rewrite line endings on Windows. A note with malformed or absent
frontmatter is refused with a reason and never repaired, and an existing value
is never overwritten.

Dry run is the default. `date_report.md` and `date_report.json` are written
before any edit. Each note is re-read at apply, verified against the SHA-256 the
report was built on, and copied to `backup/` under the run directory before it
is touched. Archive roots are opened read-only; an archive kept inside the vault
is a source and is excluded from the notes being filled.

### Ordering against the schema

`parse_schema_note` refuses a human-owned property that is not `Required: no`,
because nothing could satisfy it: the classifier is never shown the property, so
it cannot supply one for a note that lacks it. The backfill therefore runs
*before* the property is made required, and warns when the requirement has
already landed.

## Run State and Resume

Every invocation is a durable run under `.vault-organizer/runs/<timestamp>/`
following the repository run-state contract: `run_state.json` (options
fingerprint, phase, per-note statuses), `run_events.jsonl` (fsynced phase
journal), `scan.json` (input snapshot with content hashes), `dedupe.json`
(duplicate plan), `classified.jsonl` (fsynced per-note classification
journal), `verified.jsonl` (fsynced per-note verdict journal), `plan.json`,
`report.md`, `review-queue.jsonl`, and `apply-log.jsonl`.

- `--run <dir>` resumes: journaled classifications and verdicts are reused,
  apply operations already logged `ok` are skipped, and input drift since the
  scan is reported as warnings. Files changed after planning are refused at
  apply by SHA-256 re-check.
- Resuming with different options (model, endpoints, thresholds, limit,
  schema hash) is refused via the options fingerprint; start a new run.
- A vault-level lock (`.vault-organizer/.run.lock`) serializes runs; a stale
  lock from a dead process is reclaimed automatically.
- The whole run is idempotent: re-running a completed run re-derives the same
  plan and applies nothing new.

## Verification

Classification runs on the non-thinking service, so every classification is
then reviewed by the thinking service (`connectedServices.think`, overridable
with `--think-url`/`--think-model`, skippable with `--no-verify`).

- Review is batched at ~20 notes per call, so coverage is total while the
  thinking cost stays proportional to the number of batches, not notes. The
  reviewer sees each note's title, current path, proposed destination,
  metadata, and a 1,000-character excerpt.
- The reviewer must return exactly one verdict per note it was given, and a
  flag must carry a reason. A malformed response gets one corrective retry.
- A flagged note is re-classified individually on the thinking service, told
  what the objection was. That result wins and is recorded with
  `classification_source: model-think`. If it fails validation, the note
  becomes `needs_review` for a human rather than shipping the filing the
  reviewer objected to.
- Verdicts are journaled per note, so a resumed run reviews nothing twice.
- If the thinking service is unreachable the run continues and `report.md`
  says **Not verified** with the reason. An absent reviewer never reads as
  approval.

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

Classification resolves its endpoint from `connectedServices.chat`, which is a
non-thinking configuration (`http://llms:8004/v1/chat/completions`, model
`chat`), so no reasoning-suppression trick is needed. Pointing at a thinking
backend instead costs hundreds of hidden tokens per note: measured on the
reference deployment, two inbox notes took 4.2s against :8004 and 56.9s against
:8008, for the same destinations.

That cost is invisible in the response — llama.cpp strips the think block
server-side and returns no `reasoning_content` — so `doctor` detects it from the
generated-token count instead and warns when the bulk endpoint is reasoning.

`--think-prefill` remains for pointing this at a thinking backend by hand: it
ends each request with a closed empty `<think></think>` assistant turn that
llama.cpp-style servers continue from. It is part of the classification cache
key and the run options fingerprint. The response parser strips a leading think
block and code fences regardless, so a thinking backend used without the flag
still produces valid output (just slowly).

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

## Machine Provenance

Two properties record what a machine did to a note, and neither is the
classifier's to decide. `capture_type: generated` means a skill created the
note. `processed_by` lists the workflows that substantially transformed a note
the user wrote — `vault-transcripts` sets it on every transcript it cleans.

Filing replaces frontmatter wholesale from the model's response, so both are
carried forward deterministically from the note's previous frontmatter before
routing: a `generated` capture type is restored (with a warning naming what the
classifier proposed instead), and `processed_by` is taken from the file and any
model-supplied value discarded. A note does not stop being machine-made because
a later pass read it as prose. Vaults whose schema note does not define
`processed_by` as a list property simply never carry the key, and the report
warns when an existing value has to be dropped for that reason.

## Renumbering

`--fix-schema` cannot express a cascade — `schema_side_fix` returns `None` when
the wanted number belongs to a sibling row, and `cheapest_fix_side` then reports
`folder` with "the registry cannot express this as a single row edit". `renumber`
is that folder-side move, done deliberately rather than as a drift repair.

`renumber_cascade` shifts the contiguous run of occupied numbers starting at the
requested one and **stops at the first gap**, which is what leaves a distant
`98 Archive` and `99 Meta` alone without a flag naming them. `renumber_mapping`
validates the *result* rather than each step: a transient collision is fine
because the rename order prevents it on disk, but two domains ending on one
number is refused before anything is touched. Neither adds a row; registering a
domain stays the owner's edit.

Only the `Domains` numbers move. `subdomain_folder` renders
`<domain.number>.<pad2(subdomain.number)>`, so every folder beneath a moved
domain follows from that one substitution and the Subdomains and Project registry
tables are not edited at all.

Two orderings in `renumber_folder_moves` point opposite ways and both are
load-bearing. *Between* domains, descending by destination: shifting a block up
by one means each target is the next domain's current folder, so `07 → 08`
precedes `06 → 07`. *Within* a domain, shallowest first, because renaming
`03 Craft` carries its children along under their old names. The function tracks
a live location per route rather than substituting over the original paths, since
by the time a project is renamed both its domain and its subdomain have moved out
from under it. `prune_renumber_moves` replays the same way when testing which
sources exist, so a nested route is not mistaken for a missing one.

Notes are not touched. Frontmatter names a domain by value and Obsidian resolves
a wikilink by basename, so nothing is refiled, reclassified, or relinked. What
does not follow a rename is reported by `find_path_references`: Markdown links
with explicit paths, `.base` views filtering on `file.path`, and `.obsidian`
bookmarks and workspace state. It searches the percent-encoded form too, and it
is the one walk in this module that opts `.obsidian` back in. Those references
are reported and never rewritten.

Apply order is folders first, schema note last, because the renames are the
reversible half and the note is the small atomic one. A failed rename rolls back
every earlier one and the note is never written; the note itself lands through
the same rails as `--fix-schema` — backup, surgical single-cell edits, and a temp
file that must re-parse and re-check clean before `os.replace`.

## Property Ownership

The `Shape` cell of an **Approved properties** row carries two optional markers,
and they answer the same question — who supplies this value — with different
answers. A row carrying both is a parse error.

| Marker | Supplier | May be `Required: yes` |
| --- | --- | --- |
| `human-owned` | the owner, by hand | no |
| `derived` | code, deterministically | yes |

Both are withheld from the classifier: a property in the prompt is a property the
model fills in, and neither of these is derivable from a note's text the way
`type` or `domain` is. Both are restored from the note's previous frontmatter by
`carry_forward_provenance`, which is why filing does not drop them on the first
pass.

The difference is the requirement. A required `human-owned` property is
unsatisfiable by construction — the classifier is never shown it, so it can never
supply one for a note that lacks it — and the parser refuses the row. A `derived`
property is exempt precisely because something always supplies it.

### `created`

The one derived property with a derivation rule. `stamp_created` runs after
carry-forward and fills it only when nothing carried one forward, so a value a
note already has always wins and filing the same note twice cannot change its
birthday. Evidence, best first:

| Tier | Source |
| --- | --- |
| `carried` | the note's previous frontmatter — nothing was derived |
| `filename` | a `YYYY-MM-DD` prefix on the basename, written by someone who meant it |
| `date_property` | the note's own subject `date`, the closest remaining proxy |
| `filesystem` | `st_birthtime`, else mtime |
| `run_date` | no evidence at all; the note is one the pipeline is seeing new |

The `filesystem` tier is last and named in the report because a bulk move or a
sync rewrites every timestamp at once, after which they mean "recently touched"
rather than "made on" — which is the failure this property exists to repair. The
report's **Created Dates** section counts notes per tier rather than presenting
every date as equally well known.

`type: template` notes are never stamped. A template's frontmatter is pinned by
the wiki template contract and the installed copy is compared byte-for-byte with
the shipped one, so a stamped template would be refused as owner-modified by
every later `template-install`.

`missing_required_properties` is the general check that `Required: yes` means
something for a property `validate_classification` does not hardcode. It runs
after carry-forward and stamping; a note still missing a required value goes to
review rather than being filed incomplete.

Recovering the property across a whole vault is
`scripts/backfill-vault-created.py`, which adds two tiers the filing path does
not reach for: the pre-migration copies under `.vault-organizer/runs/*/backup/`,
and the first commit adding a note when the vault is a git repository. It is dry
run by default and `--min-tier` defaults to excluding `file`.

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

`--reuse-frontmatter` files a note whose existing frontmatter already validates
without asking the model at all: the values are read from the note, pushed
through `validate_classification` unchanged, and recorded with
`classification_source: frontmatter`. Anything that fails to validate falls
through to the cache and the model exactly as it would have. Reused records are
excluded from verification — there is no model judgment in them to review.

This exists for schema migrations that move folders without changing what any
note *is*. Without it, editing the schema note changes `schema_hash`, which is
part of the classification cache key, so a whole-vault run re-derives every
classification from the model: slow, and lossy, because a note the model hedges
on lands in the review queue and gets pulled back into `00 Inbox`.

## Filenames

A note keeps its basename through filing, with one exception. A name containing
`#`, `^`, `[`, `]`, or `|` cannot be the target of a `[[wikilink]]` and will not
sync to mobile, so the note would arrive in its folder unreachable; names with
path-illegal characters are repaired for the same reason. Filing is the last
moment a name is cheap to change — afterwards a rename means rewriting every
link to it — so the repair happens there: `[` and `]` become `(` and `)`, `|`
becomes `-`, the rest are dropped. The original name is recorded on the record
and every repair is listed in the report under "Filenames Repaired". A name with
nothing usable left is never invented; the note goes to review instead.

The rule itself lives in `safe_title`/`safe_basename` in `forge/lib/vault_schema.py`
so that every vault skill names notes identically, and is documented for humans
under "Filename and collision rules" in the vault's schema note.

## Apply

Dry run is the default. With `--apply`, the script does **all content writes
first, then all moves**: quarantines, then every rewrite at the note's existing
path, then every relocation. The order is load-bearing. A move can rewrite links
inside other notes, so once moves begin no other note's planning hash can be
trusted; doing every content write beforehand means each one still verifies
against the hash the plan was built on.

A `rewrite` that also relocates is therefore two journalled steps — `rewrite` for
the content and `rewrite_move` for the move — so a resumed run can tell which
half already landed. Quarantines journal `quarantine`, plain moves `move_only`.
For every operation the script re-reads the source, verifies its expected
SHA-256, backs up the original under the run directory, and either renames or
writes through a temporary file with fsync. A live `expected_hash` map carries
each note's current hash forward, updated after each content write and after each
move's link rewriting, so a later operation verifies against reality rather than
against planning time. `apply-log.jsonl` is never truncated; on resume,
operations already logged `ok` are skipped. It never overwrites existing files
and does not delete backups automatically. After a successful apply the vault
content index is refreshed.

Backups use `backup_once`: the first copy of a file wins, because a note can now
be backed up twice in one run — once as a move source and once as a note some
other move is about to rewrite.

Moves themselves go through a mover (`forge/lib/vault_moves.py`). `PlainMover` is
`os.rename` and is what this workflow has always done. `ObsidianMover` routes the
move through the Obsidian CLI so inbound links follow the note, and pays for that
reach: it backs up every inbound linker before the call, verifies the moved
file's bytes, requires every rewritten note to differ only on lines carrying
links, restores everything from backup otherwise, and disables itself for the
rest of the run after one failure. `--link-rewrite {auto,off,require}` selects
the policy; `auto` falls back to `PlainMover` silently. A quarantine is always a
plain rename — it moves into a dot-directory Obsidian does not index, so
rewriting links to chase it there would point them at nothing.

`.base` files are never modified. The report lists the bases a run disturbs: when
the CLI is available each base's view is evaluated with `base:query` and reported
as "returns N notes, M of which this run moves"; otherwise the base file's text
is substring-matched against the moved paths, which is the weaker check it has
always been — a base selects by property, so it can hold a note whose path
appears nowhere in the file.

## Attachment Links

A move never rewrites an embed, so relative asset paths break whenever notes
are reorganized. The `attachments` mode is the separate, deterministic repair:
it resolves no model or embeddings service, and it classifies every markdown
(`![alt](target)`) and wikilink (`![[target]]`) embed whose target carries an
asset extension.

Classification is by resolution, not similarity. A target that exists is
`resolves`. Otherwise the basename is looked up across the vault: exactly one
match is `repairable` and is rewritten to `![[basename]]`; several matches are
`ambiguous` and are reported untouched, because choosing between them would be
a guess; no match is `missing`.

A `missing` embed is stripped. Non-empty alt text replaces the embed — for
Word-pasted images the alt text carries the content. An embed that was the
entire line, or the entire list item, takes that line with it rather than
leaving empty scaffolding behind.

Two invariants:

- Text inside a code span or fenced block is never matched. Schema project
  registries and Obsidian documentation both contain embed-shaped strings, and
  rewriting either is corruption.
- `attachment_report.json` and `attachment_report.md` are written before any
  edit. Stripping removes a filename from the note permanently, so the report
  is its only remaining record. Rewritten notes are copied to `backup/` under
  the run directory, as with any other apply.

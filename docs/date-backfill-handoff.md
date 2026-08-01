# Handoff: backfilling `date` across the Loom vault

For a local agent with filesystem access to Ellie's machine. The remote session
that wrote this tool could not reach either folder, so nothing below has been
run against real notes — every step is written to be verified locally before it
writes anything.

## Coordinates

| | |
| --- | --- |
| Vault | `/Users/ellie/Documents/Obsidian/Loom` |
| Archive | `/Users/ellie/Documents/Obsidian/Archive` (many versions of the vault, disorganized) |
| Branch | `claude/obsidian-notes-derive-dates-085ej5` in `pi-forge` |
| Tool | `forge/skills/vault-organizer/scripts/vault-organizer.py`, mode `dates` |

Check out the branch before starting; the `dates` mode does not exist on `main`.

## The goal, and the one ordering constraint

`date` is a **human-owned** property: the classifier is never shown it and can
never fill it. `parse_schema_note` therefore refuses a human-owned property that
is not `Required: no` — nothing could satisfy the requirement.

**Do the backfill before flipping `date` to required in the schema note.** If
the requirement has already landed, the schema note will not parse and every
vault skill fails closed until it is set back to `Required: no`. Check first:

```bash
grep -n '`date`' "/Users/ellie/Documents/Obsidian/Loom/99 Meta/99.02 Schemas/0.00 Vault Schema.md"
```

The row must read `| `date` | no | scalar, human-owned | ... |` for the run to
work. Flip it to `yes` only once the fill is done and Ellie is happy with it.

## How the tool decides

Confidence is the **weaker of two independent axes**, and only `high` is written
without Ellie naming the note.

**Match** — how sure we are an archive file is an older copy of a given note:
`identical` (same body hash) · `named` (same filename stem, unique on both
sides) · `titled` (same H1, unique on both sides) · `similar` (embeddings, opt
in) · `self` (the note's own name/path/text, no matching involved).

**Evidence** — how explicitly that file states a date: `explicit` (filename
date, `YYYY/MM/DD` daily-note path, a `created`/`date-created`/`createdAt`/
`ctime` frontmatter key, an Obsidian unique-note id, or a Finder creation date
once trusted) · `stated` (a labelled date in the opening lines) · `weak`
(a bare date in the body, or filesystem times).

`high` = an exact-ish match on explicit evidence. Everything else gets an id in
`date_report.md` and is written only when named with `--ids`.

Deliberate refusals, because the ask was to be *confident*: `11-04-2023` is
dropped unless one position cannot be a month; name and title matching require
uniqueness on both sides; `type: source` and `type: wiki` notes are demoted to
review even on perfect evidence, since a source's subject date is the work's and
not the day the note was written. Several archive copies of one note is not a
conflict — the earliest wins, because the oldest copy dates the creation — but
one file whose filename and frontmatter disagree is, and it is demoted.

## Step 1 — dry run, and read the calibration

```bash
cd /path/to/pi-forge
python3 forge/skills/vault-organizer/scripts/vault-organizer.py dates \
  --vault "/Users/ellie/Documents/Obsidian/Loom" \
  --archive "/Users/ellie/Documents/Obsidian/Archive"
```

Dry run is the default; this writes nothing to any note. It prints one JSON
result and writes `date_report.md` and `date_report.json` into a fresh run
directory under `Loom/.vault-organizer/runs/`.

**Ellie's expectation is that Finder dates carry most of the signal, so the
`## Finder creation dates` section is the first thing to read.** macOS records
`st_birthtime`, but a Finder *move* preserves it while a *copy* resets it to the
day of the copy — and most archive tools reset it too. The report measures which
happened rather than assuming:

- **Agreement** — of the archive files that carry both a creation date *and* a
  date stated in their name or frontmatter, how often the two match. That rate
  estimates how often the creation date is right on the files that state
  nothing, which is the population being relied on.
- **Largest single-day cluster** — the counter-signal. If a large share of the
  archive was "created" on one day, that day is when the copy happened and those
  files lost their original dates.

Report both numbers to Ellie in plain terms. Roughly:

- High agreement (say 90%+) and a small cluster → creation dates survived; go to
  step 2.
- Low agreement, or a cluster holding a large share of the archive → they did
  not; skip step 2, and treat the text evidence as the real yield.
- In between → say so, and let Ellie decide. Do not pick for them.

## Step 2 — trust the creation dates, if the calibration earned it

```bash
python3 forge/skills/vault-organizer/scripts/vault-organizer.py dates \
  --vault "/Users/ellie/Documents/Obsidian/Loom" \
  --archive "/Users/ellie/Documents/Obsidian/Archive" \
  --trust-birthtime
```

This promotes a Finder creation date from `weak` to `explicit`, so an exact
match carrying one becomes auto-appliable. Still a dry run. Compare the `high`
count against step 1 — the difference is exactly what trusting them buys.

`--include-file-times` is the weaker alternative: it surfaces creation *and*
modification times as `weak` evidence for the report without promoting anything.
A modification time is never promotable; nothing makes it a creation date.

## Step 3 — apply the confident ones

Show Ellie the counts first and get an explicit go-ahead. Then:

```bash
python3 forge/skills/vault-organizer/scripts/vault-organizer.py dates \
  --vault "/Users/ellie/Documents/Obsidian/Loom" \
  --archive "/Users/ellie/Documents/Obsidian/Archive" \
  --trust-birthtime --apply
```

Writes only the `high` tier. Each note is re-read, verified against the SHA-256
the report was built on, and copied to `backup/` under the run directory before
it is touched. The write adds exactly one line — body, delimiters, BOM, line
endings, and every other property survive byte-for-byte. An existing value is
never overwritten, though a bare `date:` key is treated as the empty slot to
fill rather than a value to protect.

Re-running is safe: notes that now carry a date drop out of the next run.

## Step 4 — work the review pile

`date_report.md` groups everything else under **Held for review** and **Weak
evidence**, each line carrying an id, the proposed date, the archive file it
came from, and the exact substring the date was read out of. Read them to Ellie
in batches. Apply only ids they have named:

```bash
python3 forge/skills/vault-organizer/scripts/vault-organizer.py dates \
  --vault "/Users/ellie/Documents/Obsidian/Loom" \
  --archive "/Users/ellie/Documents/Obsidian/Archive" \
  --trust-birthtime --apply --ids <id>,<id>,<id>
```

Ids are derived from the note path and the chosen date, so one copied out of an
old report cannot come to address a different note. An unknown id is refused
with the ids the current run actually proposes.

Two groups are worth handling as their own pass, since they are review-by-design
rather than uncertain: the `source` and `wiki` notes, where Ellie should decide
whether she wants creation date or subject date in `date` at all.

## Step 5 — the notes with nothing

The report's **No evidence** section lists notes where neither the vault nor the
archive offered anything. These need a date typed by hand or left empty. Two
things worth trying before giving up on them:

```bash
# The note's own name, path, and text, with no archive involved.
python3 forge/skills/vault-organizer/scripts/vault-organizer.py dates \
  --vault "/Users/ellie/Documents/Obsidian/Loom" --self-only --trust-birthtime

# Pair leftovers to archive copies by meaning rather than by name. Needs the
# embeddings service; results are review-only, never auto-applied.
python3 forge/skills/vault-organizer/scripts/vault-organizer.py dates \
  --vault "/Users/ellie/Documents/Obsidian/Loom" \
  --archive "/Users/ellie/Documents/Obsidian/Archive" --near-match
```

Give Ellie a final count of what is still undated — that number is what decides
whether `date` can become required yet.

## Safety properties worth knowing

- Dry run is the default; `--apply` is the only thing that writes.
- The archive is opened **read-only** and is never written. An archive folder
  kept *inside* the vault is treated as a source and excluded from the notes
  being filled.
- Reports are written **before** any edit, so the evidence behind every date
  outlives the run.
- Every modified note is backed up under the run directory.
- A note with malformed or absent frontmatter is refused with a reason, never
  repaired. Run `vault-organizer` on those first if Ellie wants them included.
- No model is involved. No network either, unless `--near-match` is passed.

## Sanity checks before and after

```bash
# Both folders are where this document says, and the archive is not empty.
ls -d "/Users/ellie/Documents/Obsidian/Loom" "/Users/ellie/Documents/Obsidian/Archive"
find "/Users/ellie/Documents/Obsidian/Archive" -name '*.md' | wc -l

# The tool's own tests pass on this machine (Python 3.9+, no endpoints needed).
python3 forge/skills/vault-organizer/tests/test_vault_organizer.py DateEvidenceTests DateBackfillTests

# Afterwards: the schema and the vault still agree.
python3 forge/skills/vault-organizer/scripts/vault-organizer.py drift \
  --vault "/Users/ellie/Documents/Obsidian/Loom"
```

A Time Machine snapshot or a `cp -a` of the vault before step 3 costs a minute
and makes the whole thing reversible in one move, which the per-note backups do
not quite give you.

## Report back to Ellie

- How many notes now carry a date, and by which evidence.
- What the calibration said about Finder dates, and whether they were trusted.
- How many are still held for review, and how many have no evidence at all.
- Whether `date` can now be flipped to `Required: yes`.

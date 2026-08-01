# Schema migration: `created` dates and the natural world

Two changes to the vault's schema note, and the order to make them in. Both are
edits to `99 Meta/99.02 Schemas/0.00 Vault Schema.md`, which is the owner's file:
no skill in this repo writes it, and every code path here fails closed until the
rows exist.

**Every number below is written as `<N>` because none of them can be chosen from
outside your vault.** Read the **Domains** table in the schema note and pick free
values. Numbers are 1–99, `0` is reserved for `00 Inbox`, and the sources root
reserves its own.

Two different collisions, with two different failures:

- **A number already held by another row in the same table** is a parse error --
  `Domains: duplicate or reserved number 6`. The schema note stops parsing and
  every vault skill fails closed on the next run. Nothing is moved, merged, or
  renamed; the existing domain keeps its number, its folder, and its notes.
- **A number no row holds but a folder on disk carries** compiles fine and is
  caught later, by `vault-organizer drift`, as `number_collision` at severity
  `high` -- which blocks `--apply` until it is resolved.

The first is the reason to read the table before pasting. The second is the
reason to run `drift` afterwards.

---

## 1. `created`

One row in **Approved properties**. Put it after `date` so the two dates read
together:

```markdown
| `created` | yes | scalar, derived | Date this note came into existence, YYYY-MM-DD. Set once by tooling and never rewritten. |
```

`date` and `created` are different facts and both are worth having: `date` is
what the note is *about* — the day of the meeting, the sighting, the entry —
and `created` is when the note came to exist. A 2019 photograph filed today has
a `date` of 2019 and a `created` of today.

`derived` is a new marker beside the existing `human-owned`. Both are withheld
from the classifier; only `derived` may be `Required: yes`, because code always
supplies it. The full contract is under **Property Ownership** in
`forge/skills/vault-organizer/references/vault-schema-contract.md`.

Nothing else in the schema note changes for this.

### Recovering the dates that were lost

The dates went missing because a bulk reorganization flattened every file
timestamp — `36302b0` records ~1,470 notes collapsed to a single day — and the
closed property list then stripped any `created` key a note still carried. The
backfill reads the evidence that survived, in confidence order, and says which
tier answered for each note:

| Tier | Source |
| --- | --- |
| `backup` | a pre-migration copy under `.vault-organizer/runs/*/backup/`, including keys like `Created:` that the canonical parser would have dropped |
| `git` | the first commit adding the note, `--follow` so a rename keeps the original date |
| `filename` | a `YYYY-MM-DD` prefix on the basename |
| `date` | the note's own subject date |
| `file` | birthtime or mtime — **the tier this vault's migration destroyed** |

```bash
# Dry run. Read the by_tier counts and the unresolved list before writing.
python3 scripts/backfill-vault-created.py --vault ~/Documents/Obsidian/Loom

# Highest-confidence tiers only.
python3 scripts/backfill-vault-created.py --vault ~/Documents/Obsidian/Loom \
    --min-tier filename --apply

# Then everything down to the subject date. --min-tier defaults to `date`,
# which excludes file timestamps on purpose.
python3 scripts/backfill-vault-created.py --vault ~/Documents/Obsidian/Loom --apply
```

Notes with no evidence at all are reported and left alone rather than stamped
with today — a thousand notes all created on the migration date records nothing.
`vault-organizer` gives them a date when it next files them, and the report says
it did.

---

## 2. The natural world

### Where the two halves go

**Species cards go under the existing `wiki` domain.** A species card is exactly
what the wiki contract describes — it defines a thing so other notes can link to
it — so three new subdomains need no routing code at all.

**Field records go under a new `nature` domain.** An observation is not a
reference card; it is a thing that happened once.

### Note types

Two rows in **Note types**:

```markdown
- `organism` — A species: an animal, plant, or fungus.
- `observation` — Something seen in the field, on a date, in a place.
```

All three species kinds file as `organism`, the way `concept`, `practice` and
`term` all file as `concept`. Kind resolves from `subdomain`, never from `type`.

### Domains

One row in **Domains**:

```markdown
| `nature` | `<N>` | `Nature` | Field records: what was seen, where, and when. |
```

### Subdomains

Three rows under `### wiki`:

```markdown
| `animals` | `<N>` | `Animals` | Animal reference cards. |
| `plants` | `<N+1>` | `Plants` | Plant reference cards. |
| `fungi` | `<N+2>` | `Fungi` | Fungus reference cards. |
```

And a new `### nature` subsection:

```markdown
### nature

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `observations` | `<N>` | `Observations` | One sighting each: species, date, place. |
| `field-notes` | `<N+1>` | `Field Notes` | Longer accounts of an outing. |
| `weather` | `<N+2>` | `Weather` | Station rollups and summaries. |
```

`weather` is declared now so the slot is reserved; it stays empty until the
station arrives. A declared route with no folder is `declared_absent` at severity
`info` in drift, which is the intended reading.

### Naming

Species cards follow the vault's existing wiki convention, `Canonical Name,
Gloss`, with the scientific name as the gloss:

```
Raccoon, Procyon lotor
Salmonberry, Rubus spectabilis
Golden Chanterelle, Cantharellus formosus
```

Source lookup matches on the text before the first comma, so this is also the
right form for finding the Wikipedia and GBIF entries.

### Home region

One row in the `## Owner` table of `99 Meta/99.02 Schemas/0.03 Personal
Context.md`:

```markdown
| `home region` | Puget Lowland |
```

The value names a wiki `place` note. It is the single value that decides which of
a species card's phenology rows apply here, so **moving house is this one row**.
Every other region's rows stay on their cards; a species with nothing for the new
region reports as missing local data rather than answering with the old region's
calendar.

It is deliberately not part of the owner sentence injected into prompts — where
someone lives has no business in a prompt summarizing a therapy session.

---

## 3. Order of work, and the one trap

1. Make the schema note edits above.
2. Add the `home region` row to the Personal Context note.
3. Run the `created` backfill, dry run first.
4. `python3 forge/skills/vault-wiki/scripts/vault-wiki.py template-install --vault <vault>`
   — three new templates. Existing templates are untouched unless `--force`.
5. Write or expand some species cards.
6. `python3 forge/skills/vault-naturalist/scripts/vault-naturalist.py doctor --vault <vault>`

**The trap:** editing the schema note changes `schema_hash`, which is part of the
classification cache key. A plain whole-vault organize run after step 1
re-derives all ~2,500 classifications through the model — slow, and lossy,
because every note the model hedges on lands in the review queue and is pulled
back into `00 Inbox`. Use `--reuse-frontmatter`, which validates existing values
with no model call:

```bash
python3 forge/skills/vault-organizer/scripts/vault-organizer.py vault \
    --vault ~/Documents/Obsidian/Loom --reuse-frontmatter
```

This is the same trap the sources-tree migration hit, and `--reuse-frontmatter`
is what was added for it.

---

## 4. Phenology

The region-varying half of a species card is a managed table in its body:

```markdown
## Phenology

| Event | Window | Region | Basis |
| --- | --- | --- | --- |
| mating | Jan-Mar | [[Puget Lowland]] | sourced[^2] |
| birth | Apr-May | [[Puget Lowland]] | sourced[^2] |
| present | year-round | [[Puget Lowland]] | observed |
```

It is a body table rather than frontmatter because the approved-property list is
closed and global: a `phenology` property would be inherited by every note type
in the vault, and a nested per-region structure would be stripped on the next
filing pass. A body table is footnote-cited like every other managed section,
renders in Obsidian, and is one row away from covering a new region.

- **Event** — controlled per kind, in
  `forge/skills/vault-wiki/references/phenology-events.json`.
- **Window** — a month, a month range that may wrap the new year (`Nov-Feb`), or
  `year-round`. Ranges keep their direction: `Nov-Feb` is four months.
- **Region** — a `[[wikilink]]`, or `global` for a range-wide fact. A wikilink
  inside backticks is not a link and is refused.
- **Basis** — `sourced`, `inferred`, or `observed`. `vault-wiki` may write the
  first two and is refused the third by its own kind spec, so a window you derived
  from your own records can never be confused with one a model drafted.

`vault-naturalist compile` turns every such table into
`.vault-naturalist/cache/phenology.json`, and `report --month 3` answers what is
due. A row it cannot read is reported with its reason rather than dropped: an
index that quietly omits a species reads exactly like a species with no seasons.

### Edibility

`Uses & Cautions` on plant cards and `Edibility & Toxicity` on fungus cards are
owner-owned in the kind specs — the same mechanism as `## Notes`, so the pipeline
never writes or reads them. Identification and Look-alikes stay model-written,
because those are sourced and checkable and are what field guides are good at.
The line about whether to eat something is one you write, from a specimen in
hand. Change `"owner": true` in
`forge/skills/vault-wiki/references/wiki-kinds.json` if you want that differently.

---

## 5. Still to come

Designed, not built:

- **Weather ingest.** An ambient station emits a reading every few minutes; a
  year is over 100,000 records against a 2,500-note vault. It belongs in a CSV or
  SQLite store under a `.forge-workspace`-marked directory, which the organizer
  already ignores by construction. A daily rollup note at most.
- **The dashboard.** It joins the compiled phenology index, the observation
  notes, and the weather store. Obsidian Bases can drive the note-level views —
  pi-forge evaluates `.base` files read-only and never writes them. Anything
  cross-cutting (a species-by-month grid with your observed dates and degree-day
  accumulation over it) is a generated HTML artifact: **this vault has no
  Dataview**, so nothing should be designed assuming it.

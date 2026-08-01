---
name: vault-naturalist
description: Compile the seasonal Phenology tables on animal, plant, and fungus wiki cards into a queryable index, report what is expected in the owner's region this month, and record field observations as notes. Use when the user asks what to look for this month, when raccoons breed or a plant blooms here, to log or record something they saw, to compile or check phenology, or to set up naturalism tracking in the vault.
---

# Vault Naturalist

The species cards in the wiki say what a raccoon is. This skill covers the other
half: what it is doing *here*, this month, and what the owner actually saw.

Everything here is deterministic. No model is called, nothing is fetched, and
`compile` and `report` never write to a note. `observe` writes exactly one note
and never overwrites an existing one.

## The three ideas

**Researched and observed knowledge share one table and stay distinguishable.**
Every phenology row carries a `Basis` cell — `sourced`, `inferred`, or
`observed`. `vault-wiki` may write the first two and is refused the third by its
own kind spec, so a window the owner derived from their own records can never be
confused with one a model drafted. A card can carry both without either
laundering the other.

**Region is a value, never an assumption.** Raccoons breed two months apart
across their range, so a window with no region attached is not a fact about
anywhere. Rows name a region by `[[wikilink]]`, or `global` for a range-wide
fact. The region a query is asked in comes from a `home region` row in the Owner
table of `99 Meta/99.02 Schemas/0.03 Personal Context.md`. **Moving house is that
one row.** Every other region's rows stay where they are, and a species with no
row for the active region is reported as missing local data rather than answered
with somewhere else's calendar.

**Observations are notes; measurements are not.** An observation is something
seen once, which is what a note is for, and it needs no property the vault schema
does not already have. A weather station emitting a reading every few minutes is
not that: a year of it would bury the vault, so it belongs in a store beside the
vault that a later mode will read.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path, and the vault
   path from the user or known configuration.
2. Check the vault has what this needs:

   ```bash
   python3 <skill-directory>/scripts/vault-naturalist.py doctor --vault <vault>
   ```

   This reports which schema rows are missing, whether the species templates are
   installed, whether a home region is declared, how many phenology rows failed
   to parse and why, and how many species cards carry nothing for the home
   region. **Relay missing schema rows to the user and stop.** Adding a domain,
   subdomain, or note type is the owner's edit; no skill in this repo writes the
   schema note.
3. Compile the index:

   ```bash
   python3 <skill-directory>/scripts/vault-naturalist.py compile --vault <vault>
   ```

   Writes `.vault-naturalist/cache/phenology.json`. `--dry-run` reports the
   counts and the unreadable rows without writing.
4. Ask what is due:

   ```bash
   python3 <skill-directory>/scripts/vault-naturalist.py report --vault <vault> --month 3
   ```

   Defaults to the current month and the declared home region. `--region "[[…]]"`
   asks about somewhere else — planning a trip, or checking what a card claims
   about the range it was researched in. `--rebuild` recompiles instead of
   reading the cache.
5. Record what was seen:

   ```bash
   python3 <skill-directory>/scripts/vault-naturalist.py observe --vault <vault> \
       --species "Raccoon, Procyon lotor" --place "Back fence" --count 3 \
       --behavior "Foraging under the feeder" --dry-run
   ```

   `--dry-run` returns the exact note text without writing. Show it to the user
   before writing when anything was inferred rather than stated.

## Adding phenology to a card

`vault-wiki expand --kind animal` drafts the `## Phenology` table from canonical
sources along with the rest of the card, and every row it writes is `sourced` or
`inferred`. To add a row from the owner's own records, edit the card directly and
mark it `observed`. The columns are fixed by
`vault-wiki/references/wiki-kinds.json` and the event vocabulary by
`vault-wiki/references/phenology-events.json`; a row using an event outside that
vocabulary is reported by `compile` and absent from the index.

```markdown
## Phenology

| Event | Window | Region | Basis |
| --- | --- | --- | --- |
| mating | Jan-Mar | [[Puget Lowland]] | sourced[^2] |
| birth | Apr-May | [[Puget Lowland]] | sourced[^2] |
| present | year-round | [[Puget Lowland]] | observed |
```

A window is a month, a month range that may wrap the new year (`Nov-Feb`), or
`year-round`. Ranges keep their direction: `Nov-Feb` is four months, not eight.

## What to relay to the user

- **Unreadable rows are the interesting output.** A row with an unknown event, an
  unparseable window, or a region that is not a wikilink is reported with its
  reason and left out of the index. Say which and why — an index that quietly
  drops a species reads exactly like a species with no seasons.
- **Cards with no local data are the work queue.** `doctor` and `report` both
  count species carrying nothing for the home region. That number is what the
  next `vault-wiki expand` run is for.
- **Never present another region's window as local.** If a species has rows only
  for elsewhere, say so. The region column exists because those are different
  claims.
- **An `observed` row is the owner's, and this skill never writes one.** Only the
  owner promotes their own observations into a card's phenology.

## Rules

- Never add a domain, subdomain, or note type to the schema note. Report what is
  missing and let the user decide.
- Never write `observed` in a row the pipeline derived, and never describe a
  `sourced` row as something the user saw.
- Never write an edibility or toxicity judgment. Those sections are owner-owned
  in the plant and fungus kind specs, and this skill has no business in them —
  the cost of being wrong is not the vault's to carry.
- Never overwrite an existing observation note. A second sighting on the same day
  in the same place is a second note or an edit the user makes.
- Never turn a weather feed into notes.
- Keep stdout machine-readable; diagnostics belong on stderr.

## Reference

The kind specs, the phenology columns, and the event vocabulary all live with the
wiki skill and are read from there rather than copied:
`../vault-wiki/references/wiki-kinds.json` and
`../vault-wiki/references/phenology-events.json`. The compiler is
`forge/lib/vault_phenology.py`.

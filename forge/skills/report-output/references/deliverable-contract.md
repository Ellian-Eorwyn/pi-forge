# Report Output Deliverable Contract

Turn processed forge outputs into polished, skimmable deliverables without
blending generated commentary into extracted source material and without burying
uncertainty.

## Run Layout

```text
<run-dir>/
  run_config.json            # detail level, deliverable set, title
  source_manifest.json       # registered inputs with hashes and tags
  sources.md                 # script-generated from the manifest
  executive_summary.md       # } model-authored, present per detail level
  report.md                  # }
  briefing.md                # }
  annotated_outline.md       # }
  slide_outline.md           # }
  review_notes.md            # }
  assumptions_and_limits.md  # model-authored (every level)
  tables.xlsx                # via `tables`, when CSV inputs + openpyxl
  converted/                 # via `render` (DOCX / HTML)
  conversion_log.md
  warnings.md
```

`run_config.json`, `source_manifest.json`, and `sources.md` are managed by the
script — do not hand-edit them. The authored deliverables are scaffolded with
required headings and a placeholder marker (`<!-- TODO: author this section -->`)
that must be removed once the section is written.

## Detail Levels

Selected with `--detail` at `init`. `sources.md` (generated) and
`assumptions_and_limits.md` (authored) are in every level.

- `brief`: `executive_summary.md` — the shortest skim.
- `memo`: `briefing.md` + `review_notes.md` — situational, action-oriented.
- `full`: `report.md` + `executive_summary.md` + `review_notes.md` — complete
  report with a standalone summary.
- `outline`: `annotated_outline.md` + `slide_outline.md` — planning and
  presentation scaffolds.

## Deliverable Structure

Honor the scaffolded headings; add subsections as needed. Keep prose concise and
use tables where they add value.

- `report.md`: Summary, Background, Findings, Discussion, Recommendations,
  Sources.
- `executive_summary.md`: Key Points, What This Means, Caveats.
- `briefing.md`: Situation, Key Points, Recommended Next Steps, Open Questions.
- `annotated_outline.md`: one bullet per planned section with intended content
  and the source ids it draws on.
- `slide_outline.md`: one `## Slide N: <title>` per slide with talking points.
- `review_notes.md`: Decisions Pending Review, Items Needing Verification, Known
  Gaps.
- `assumptions_and_limits.md`: Assumptions, Limitations, Unresolved Questions.

## Separation Rule

Keep generated synthesis, interpretation, and recommendations clearly marked as
such, and separate from quoted or extracted source content. Cite sources by
their `source_manifest.json` id, and carry through upstream locators where they
exist (for example, a `literature-extraction` evidence row's document id and
page/section locator). Do not present inference as if the sources stated it.

## Uncertainty Rule

Never bury uncertainty. Record assumptions, caveats, and unresolved questions in
`assumptions_and_limits.md`, and flag material uncertainty inline where it
affects a finding or recommendation. Carry forward `needs_review` / `unclear`
dispositions from upstream skills rather than silently resolving them.

## Source Manifest

`source_manifest.json` lists every registered input with `sourceId`, `path`,
`sha256`, `sizeBytes`, `sourceType`, and `producingSkill`. Recognized forge
artifacts are tagged via a known-artifact map (for example `evidence_table.csv`
→ `evidence_table` / `literature-extraction`); unrecognized files are `generic`
with a null producing skill. Inputs are referenced by path and hash and are
never copied into the run. Hashes recorded at `init` must still match at
`validate`; a changed source blocks completion.

## Tables and Conversion

`tables` assembles `tables.xlsx` with one worksheet per CSV input (auto-detected
from the manifest, or named with `--from`). Sheet names are derived from
filenames, sanitized for Excel, de-duplicated, and truncated to 31 characters.
Values are written as text and not coerced. When `openpyxl` is unavailable the
command reports that no workbook was built rather than failing.

`render` converts an authored Markdown deliverable (default `report.md`) to DOCX
or HTML via Pandoc, writing to `converted/`. It refuses files that still contain
the placeholder marker. Every render appends to `conversion_log.md` and records a
fidelity caveat in `warnings.md`: Pandoc preserves common structure, but complex
tables, embedded media, and advanced styling may render imperfectly. Disclose
this rather than implying perfect fidelity.

## Statuses

`validate` reports the run as valid only when `sources.md` and every detail-level
deliverable exist with all placeholders resolved and all source hashes still
match. Unbuilt `tables.xlsx` (with CSV inputs present) and the absence of any
rendered format are warnings for human review, not errors.

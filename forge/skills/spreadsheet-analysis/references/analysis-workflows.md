# Spreadsheet Analysis Workflows

## Inspection and Assumptions

Start from the deterministic profile. Confirm the sheet, header row, unit of
observation, likely keys, row count, duplicate definition, inferred types,
missingness, and suspicious values. Treat type inference as evidence, not a
schema. Preserve identifiers such as postal codes, account numbers, and values
with leading zeros as text unless the user confirms otherwise.

Record assumptions before transformation. Include the source path and SHA-256,
selected sheets or ranges, input and output row counts, transformation order,
missing-value policy, duplicate key, type conversions, filters, joins, and
software used.

## Cleaning and Transformation

- Normalize headers only in a new output and retain an explicit old-to-new map.
- Trim or normalize values only when the rule is stated. Keep raw values when
  normalization is ambiguous.
- Parse dates with an explicit locale and timezone policy. Report parse
  failures rather than dropping them.
- Deduplicate using stated key columns and a stated survivor rule. Export
  removed rows or their identifiers for review.
- Filter with explicit predicates and report before/after counts.
- Merge with declared keys and cardinality expectations. Report unmatched,
  multiply matched, and duplicated keys on both sides.
- Reshape with explicit identifier and measure columns. Check that the operation
  does not aggregate or duplicate values unexpectedly.
- Save reusable code or an operation specification in `working/`; do not rely
  on an unrecorded series of interactive edits.

## Fuzzy Grouping and Record Linkage

Exact-key dedup and declared-key merges (above) only match identical values. When
records differ in formatting, spelling, or word order, or when a free-text column
needs topical grouping, use the `cluster` command. It embeds the combined text of
the chosen columns and groups rows by cosine similarity into reviewable candidate
groups (`clusters.csv`, `cluster_groups.md`, `cluster_run.json`).

Treat the output as candidates, never as confirmed matches:

- For deduplication, use a high threshold, review each multi-row group, and then
  remove or merge through the normal stated-survivor-rule path, exporting removed
  rows for review. Do not collapse a group automatically.
- For categorization, use a lower threshold and assign a category per group,
  recording the mapping; the new column is the artifact, not a silent rewrite.
- Keep exact-key dedup and merges authoritative; fuzzy grouping only surfaces
  candidates those exact rules cannot see.
- The embeddings endpoint is required for this command and the run records the
  source SHA-256; the source is never modified.

## Summaries, Tables, and Charts

Use frequency tables for categorical fields and bounded descriptive statistics
for numeric fields. State denominators and missing-value treatment. For grouped
comparisons, show group sizes alongside percentages, rates, or averages.

Use pivot tables only when the row, column, value, and aggregation fields are
explicit. Verify totals against the cleaned dataset. Prefer a separate
`summary_tables.xlsx` rather than modifying the source workbook.

Create a chart only when it improves interpretation. Use bar charts for
category comparisons, line charts for ordered time, scatter plots for numeric
relationships, and histograms for distributions. Avoid misleading truncated
axes, unlabeled units, excessive categories, and charts based on silently
excluded rows.

## Output Contract

Typical artifacts are:

```text
data_profile.md
data_profile.json
cleaned.csv or cleaned.xlsx
summary_tables.xlsx
analysis.md
transform_log.md
working/
```

`analysis.md` must distinguish source-backed observations, generated
interpretation, assumptions, and unresolved questions. Machine-readable files
must use stable headers and explicit nulls. Report every processed, skipped,
failed, and review-needed file or row in batch work.

For XLSX round-trips, preserve the original workbook as the source and write a
new workbook. `openpyxl` preserves common cells, formulas, sheets, and styles,
but it is not a complete Excel renderer. Always disclose possible loss of
macros, external links, advanced charts, embedded objects, and unsupported
features.

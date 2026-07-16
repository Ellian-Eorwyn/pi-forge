---
name: spreadsheet-analysis
description: Inspect, profile, clean, transform, summarize, compare, chart, and enrich CSV, TSV, and XLSX tabular data. Use for spreadsheet quality reviews, missing values, duplicates, unusual values, filtering, merging, reshaping, pivot or frequency tables, cleaned exports, embedding-based fuzzy duplicate detection and semantic record linkage or categorization of a text column into reviewable candidate groups, and resumable model-generated output processed one row at a time into a new column.
---

# Spreadsheet Analysis

Analyze tabular data reproducibly while preserving every source file.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path. Check local
   capabilities when XLSX support is uncertain:

   ```bash
   python3 <skill-directory>/scripts/spreadsheet-analysis.py doctor
   ```

2. Create a new output directory under
   `forge-output/spreadsheet-analysis/<source-stem>/`. If it exists, use a
   numbered suffix. Never overwrite a source or existing output.
3. Profile the input before interpreting or transforming it:

   ```bash
   python3 <skill-directory>/scripts/spreadsheet-analysis.py inspect <input> --output <new-directory>
   ```

   Use `--sheet <name>` to restrict an XLSX profile. Review both
   `data_profile.md` and `data_profile.json` before choosing transformations.
4. Read [references/analysis-workflows.md](references/analysis-workflows.md).
   State assumptions about headers, identifiers, missing-value tokens, types,
   units, dates, categories, duplicates, and filters before changing data.
5. Write cleaned data, summary tables, charts, and analysis to new files. Keep
   extracted values separate from generated interpretation. Save task-specific
   transformation code or a machine-readable operation specification under
   `working/`, and record inputs, outputs, row counts, and assumptions in
   `transform_log.md`.
6. Validate row-enrichment runs with the helper and report processed, skipped,
   failed, and review-needed rows. Never conceal incomplete coverage.

## Mechanical Tools

For lower-level execution, the manifest also exposes `load_table`,
`profile_columns`, `clean_columns`, and `export_table`. These tools accept
structured JSON input and return structured JSON results. `clean_columns`
applies only explicit operations supplied by the caller; it does not infer or
guess cleanup rules.

## Fuzzy Grouping and Record Linkage

Use the `cluster` command when exact key matching is not enough: detecting
near-duplicate records that differ in formatting or wording (entity resolution,
e.g. "Acme Inc" vs "Acme Incorporated"), or grouping a free-text column into
topical categories at scale. It embeds the combined text of one or more columns
through the shared forge embeddings endpoint (`FORGE_EMBEDDINGS_URL`, default
`http://llms:8005/v1/embeddings`) and groups rows by cosine similarity. Unlike
exact deduplication this finds matches that differ in spelling or order, and
unlike the one-row-at-a-time loop it categorizes in a single embedding pass.

```bash
python3 <skill-directory>/scripts/spreadsheet-analysis.py cluster <input> \
  --output <new-run-directory> \
  --columns "Company" ["City"] \
  --sheet <sheet-name> \
  --threshold 0.85
```

Raise `--threshold` (around `0.92` or higher) for tight duplicate detection;
lower it (around `0.6`-`0.75`) for broader topical grouping. The run writes
`clusters.csv` (every grouped row with its cluster id, representative, and
similarity), `cluster_groups.md` (multi-row groups for review), and
`cluster_run.json` (provenance and config). The endpoint is required for this
command; it fails with remediation when unreachable. The results are advisory
candidate groups only: nothing is merged, deduplicated, or modified. Review the
groups, then act through the normal cleaning steps (a stated survivor rule for
dedup, or a new column for categories) so every change stays explicit and
reversible.

## One-Row-at-a-Time Enrichment

Use this mode when each source row requires model judgment, extraction,
classification, summarization, drafting, or another requested output in one new
column. A run targets one sheet and stages results before writing a new file.

Initialize a run. The output column must not already exist. Repeating the same
command and output resumes a compatible marked run:

```bash
python3 <skill-directory>/scripts/spreadsheet-analysis.py row-init <input> \
  --output <new-run-directory> \
  --column "Requested output" \
  --sheet <sheet-name> \
  --input-columns "Column A" "Column B"
```

Omit `--sheet` for CSV/TSV or to use the active XLSX sheet. Omit
`--input-columns` to pass every column to the model. Use `--start-row` and
`--end-row` only for an explicitly bounded source-row range.

Then repeat sequentially:

1. Request exactly one pending row:

   ```bash
   python3 <skill-directory>/scripts/spreadsheet-analysis.py row-next <run-directory>
   ```

2. Generate only the requested value from that row. Do not infer facts absent
   from it. Put the complete value in a temporary UTF-8 file, including any
   required multiline text.
3. Record the value using the returned `rowId`:

   ```bash
   python3 <skill-directory>/scripts/spreadsheet-analysis.py row-record <run-directory> \
     --row-id <row-id> --value-file <temporary-file>
   ```

   If no defensible value can be generated, record an explicit disposition
   with `--status needs_review --note "<reason>"`,
   `--status skipped --note "<reason>"`, or
   `--status failed --note "<error>"`. Use `failed` for processing errors and
   `needs_review` for unresolved judgment. Do not use an empty generated value
   to hide a failure.

Resume safely by calling `row-next` again. It derives progress from the
durable run state and `row_results.jsonl`, and never returns several rows at
once. Use `row-status <run-directory> --json` to inspect progress and source
drift, or `row-retry <run-directory> --item <row-id>|--all-failed` to explicitly
retry permanent failures.
After every eligible row has a disposition, finalize and validate:

```bash
python3 <skill-directory>/scripts/spreadsheet-analysis.py row-finalize <run-directory>
python3 <skill-directory>/scripts/spreadsheet-analysis.py validate <run-directory>
```

Finalization verifies the source hash, refuses incomplete runs or output
collisions, and writes `enriched.csv`, `enriched.tsv`, or `enriched.xlsx`.
Skipped, failed, and review-needed rows remain blank in the new column and are
listed in `review_report.md`.

## Safety and Output Rules

- Treat the first row as the header row unless the user specifies otherwise.
- Preserve source row numbers and sheet names. Reject duplicate or blank
  headers for row enrichment rather than guessing which column was intended.
- Skip entirely blank source rows by default and record their row numbers in
  the run configuration.
- Preserve XLSX sheets, formulas, and basic formatting in a copied workbook.
  Warn that macros, external links, advanced charts, embedded objects, and
  unsupported Excel features may not survive `openpyxl` round-tripping.
- Do not silently coerce ambiguous identifiers, dates, currencies, percentages,
  or leading-zero values.
- Bound profiles and previews; do not dump an entire sensitive dataset into the
  conversation.
- Create charts only when they clarify a requested relationship. Label units,
  populations, filters, and missing-data treatment.
- Keep `analysis.md` interpretive. Keep profiles, cleaned data, summary tables,
  and staged row results machine-readable and traceable to the source.

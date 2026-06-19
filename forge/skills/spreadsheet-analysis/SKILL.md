---
name: spreadsheet-analysis
description: Inspect, profile, clean, transform, summarize, compare, chart, and enrich CSV, TSV, and XLSX tabular data. Use for spreadsheet quality reviews, missing values, duplicates, unusual values, filtering, merging, reshaping, pivot or frequency tables, cleaned exports, and resumable model-generated output processed one row at a time into a new column.
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

## One-Row-at-a-Time Enrichment

Use this mode when each source row requires model judgment, extraction,
classification, summarization, drafting, or another requested output in one new
column. A run targets one sheet and stages results before writing a new file.

Initialize a new run. The output column must not already exist:

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
append-only `row_results.jsonl` file and never returns several rows at once.
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

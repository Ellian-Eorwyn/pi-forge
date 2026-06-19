---
name: report-output
description: Turn processed forge outputs into polished, skimmable deliverables. Use to assemble reports, briefings, executive summaries, memos, annotated and slide outlines, review notes, source lists, and assumptions/limits from upstream run directories (literature-extraction evidence tables, spreadsheet-analysis analyses, transcript-cleanup summaries, document-ingest documents, web-collection manifests), at a chosen level of detail, with XLSX table assembly and DOCX/HTML rendering, keeping generated commentary separate from extracted source material and never burying uncertainty.
---

# Report Output

Assemble final deliverables from already-processed information. Keep generated
synthesis separate from extracted source content, and make uncertainty visible.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path. Check local
   capabilities when XLSX or DOCX/HTML output is uncertain:

   ```bash
   python3 <skill-directory>/scripts/report-output.py doctor
   ```

2. Create a new output directory under
   `forge-output/report-output/<title-or-stem>/`. If it exists, use a numbered
   suffix. Register inputs and scaffold deliverables for a detail level:

   ```bash
   python3 <skill-directory>/scripts/report-output.py init <inputs...> \
     --output <new-directory> --detail full --title "<report title>"
   ```

   `<inputs>` are files and/or directories, including upstream run directories
   such as a `literature-extraction` run. Folders are discovered recursively,
   skipping hidden paths, symlinks, and run-internal machinery. Detail levels are
   `brief`, `memo`, `full`, and `outline`.
3. Read [references/deliverable-contract.md](references/deliverable-contract.md).
   Read the registered inputs (`sources.md` lists them with ids) and author each
   scaffolded deliverable, removing its placeholder marker. Cite sources by
   manifest id, keep generated commentary separate from quoted source material,
   and record caveats in `assumptions_and_limits.md`.
4. Assemble tables and render document formats as needed:

   ```bash
   python3 <skill-directory>/scripts/report-output.py tables <run-directory>
   python3 <skill-directory>/scripts/report-output.py render <run-directory> --format html
   ```

   `tables` builds `tables.xlsx` (one sheet per CSV input). `render` converts an
   authored Markdown deliverable (default `report.md`) to DOCX or HTML via
   Pandoc and records a fidelity caveat in `warnings.md`.
5. Validate and report outcomes:

   ```bash
   python3 <skill-directory>/scripts/report-output.py validate <run-directory>
   ```

   Resolve every error (missing deliverable, unresolved placeholder, changed
   source) before completion. Report produced artifacts and any warnings.

## Safety and Output Rules

- Preserve sources. Inputs are referenced by path and SHA-256, never copied;
  hashes recorded at `init` must still match at `validate`.
- Keep generated synthesis, interpretation, and recommendations clearly marked
  and separate from extracted or quoted source content. Cite by manifest id and
  carry through upstream locators.
- Never bury uncertainty. Surface assumptions, caveats, and unresolved questions
  in `assumptions_and_limits.md` and inline where they affect a conclusion. Carry
  forward upstream `needs_review` / `unclear` dispositions.
- Disclose conversion losses; do not imply Pandoc preserved complex formatting
  it did not.
- Do not assume Obsidian schemas or frontmatter unless the user requests them.

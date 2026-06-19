---
name: document-ingest
description: Ingest and normalize PDF, DOCX, TXT, Markdown, HTML, and RTF documents into complete Markdown, evidence-backed metadata, extraction reports, manifests, and source maps. Use for single files or folders that need preservation, OCR detection and fallback, provenance, structural cleanup, or preparation for later summarization, extraction, analysis, and conversion.
---

# Document Ingest

Create faithful, reviewable document representations without summarizing or
changing source files.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path. Run the
   capability check when the input formats or local tools are uncertain:

   ```bash
   node <skill-directory>/scripts/document-ingest.mjs doctor
   ```

2. Choose a new output directory and prepare the input:

   ```bash
   node <skill-directory>/scripts/document-ingest.mjs prepare <input> --output <new-directory>
   ```

   Preparation is recursive for folders, skips hidden paths and symlinks,
   defaults to automatic OCR, and defaults to 150,000-character chunks. Use
   `--ocr never` or `--chunk-chars <positive-integer>` only when requested.
3. Read [references/output-contract.md](references/output-contract.md). Review
   every prepared document sequentially. Never review several documents in one
   model pass.
4. For one chunk, review the complete `document.md` in one pass. For several
   chunks, read `working/chunks/*.md` in order without overlap, write reviewed
   versions under `working/reviewed-chunks/`, concatenate them exactly into
   `document.md`, then perform a final seam and metadata review. Do not omit,
   summarize, deduplicate, or reorder source content.
5. Improve only structure supported by the source: headings, paragraphs,
   lists, tables, citations, footnotes, and appendices. Keep uncertain or
   damaged text visible and record the problem in `extraction_report.md`.
6. Enrich `metadata.json` only with evidence-backed values. Every title,
   author, date, or source value must include its origin, confidence, and a
   locator. Leave unsupported values null.
7. Update `source_map.json`, `extraction_report.md`, and the run-level
   `manifest.csv` after review. Keep generated interpretation out of
   `document.md`.
8. Validate the completed run:

   ```bash
   node <skill-directory>/scripts/document-ingest.mjs validate <run-directory>
   ```

   Resolve every validation error before completion. Report warnings that
   require human inspection.

## Safety and Failure Handling

- Never overwrite an input or existing output directory.
- Keep automatically generated OCR PDFs under `derived/ocr.pdf`.
- Continue batch preparation after individual failures and preserve each
  failure in `manifest.csv`.
- Treat missing tools, encrypted or corrupt files, unresolved low-text pages,
  invalid encoding, and suspicious content coverage as explicit failures or
  review warnings.
- Do not install system packages. Report missing capabilities and the commands
  that require them.
- Do not create summaries, analysis, conclusions, or Obsidian-specific
  frontmatter during ingestion.

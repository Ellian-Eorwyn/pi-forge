---
name: file-conversion
description: Convert files between formats while preserving originals. Use to transform DOCX, Markdown, PDF, HTML, CSV, TSV, XLSX, and text files — DOCX to Markdown, Markdown to DOCX or HTML, PDF to text or Markdown, HTML to Markdown, CSV to XLSX, XLSX to CSV, and text cleanup — for single files or whole folders, with conversion logs and manifests, traceable output filenames, and explicit warnings for lossy conversions, dropped or extracted media, and unstructured PDF text. This is the general-purpose converter for the forge profile.
---

# File Conversion

Convert files between formats deterministically, preserve every original, and
disclose lossy conversions. This is the profile's general-purpose converter.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path. Check tool
   availability when uncertain:

   ```bash
   python3 <skill-directory>/scripts/file-conversion.py doctor
   ```

   Pandoc handles DOCX/Markdown/HTML; `pdftotext` (Poppler) handles PDF;
   `openpyxl` handles CSV↔XLSX.
2. Create a new output directory under
   `forge-output/file-conversion/<input-stem>/`. If it exists, use a numbered
   suffix. Convert files or folders to one target format:

   ```bash
   python3 <skill-directory>/scripts/file-conversion.py convert <input...> \
     --to <target> --output <new-directory>
   ```

   `<input>` is files and/or directories; folders are discovered recursively,
   skipping hidden paths and symlinks. Targets are `md`, `docx`, `html`, `txt`,
   `csv`, and `xlsx`. Use `--from <ext>` to restrict a batch to one source
   extension. Sources that cannot produce the target are skipped, not failed.
3. Read [references/conversion-contract.md](references/conversion-contract.md).
   Review `conversion_manifest.csv`, `warnings.md`, and `conversion_log.md`.
   Report success, needs-review, skipped, and failed counts, and disclose every
   lossy conversion rather than implying perfect fidelity.
4. Validate the run:

   ```bash
   python3 <skill-directory>/scripts/file-conversion.py validate <run-directory>
   ```

   Resolve errors (missing output, changed source) before completion.

## Safety and Output Rules

- Preserve originals. Never modify a source; all outputs go under `converted/`.
- Keep output filenames traceable to their sources via the manifest.
- Never claim complex formatting survived when it did not. Flag formatting loss,
  dropped or extracted media, missing tables, and unstructured PDF text.
- Route scanned or low-text PDFs to `document-ingest` for OCR rather than
  pretending the extracted text is complete.
- Continue batches past individual failures and record each in the manifest.
- Do not install system packages; report missing tools and their remediation.
- Do not assume Obsidian schemas or frontmatter unless the user requests them.

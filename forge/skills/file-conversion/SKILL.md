---
name: file-conversion
description: Convert files between formats while preserving originals. Use to transform DOCX, Markdown, EPUB, PDF, HTML, CSV, TSV, XLSX, and text files — including Markdown to reflowable EPUB 3 with chapter navigation, a table of contents, metadata, and an optional cover, and EPUB back to clean Markdown with extracted media — for single files or whole folders, with conversion logs and manifests, traceable output filenames, and explicit warnings for lossy conversions, dropped or extracted media, and unstructured PDF text. This is the general-purpose converter for the forge profile.
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

   Pandoc handles DOCX/Markdown/HTML/EPUB; `pdftotext` (Poppler) handles
   PDF; `openpyxl` handles CSV↔XLSX. EPUBCheck is optional and provides a
   stronger EPUB conformance check when installed. If Java is available but
   EPUBCheck is missing, install the pinned managed copy explicitly:

   ```bash
   python3 <skill-directory>/scripts/file-conversion.py install-epubcheck
   ```

   This writes under pi-forge's isolated agent state; it never installs Java or
   another system package.
2. Create a new output directory under
   `forge-output/file-conversion/<input-stem>/`. If it exists, use a numbered
   suffix. Convert files or folders to one target format:

   ```bash
   python3 <skill-directory>/scripts/file-conversion.py convert <input...> \
     --to <target> --output <new-directory>
   ```

   `<input>` is files and/or directories; folders are discovered recursively,
   skipping hidden paths and symlinks. Targets are `md`, `docx`, `html`, `txt`,
   `epub`, `csv`, and `xlsx`. Use `--from <ext>` to restrict a batch to one source
   extension. Sources that cannot produce the target are skipped, not failed.
   For a single Markdown book, optional metadata and a cover can be supplied:

   ```bash
   python3 <skill-directory>/scripts/file-conversion.py convert book.md \
     --to epub --output <new-directory> --cover cover.jpg \
     --title "Book Title" --author "Author" --language en-US
   ```

   EPUB metadata flags override YAML frontmatter. If absent, the title falls
   back to the filename and language to `en-US`. Treat level-one headings as
   chapters; keep lower headings within their chapter. Ask for the cover path
   before running when the user wants a cover.

   Convert EPUB back to one clean Markdown file plus extracted media with:

   ```bash
   python3 <skill-directory>/scripts/file-conversion.py convert book.epub \
     --to md --output <new-directory>
   ```

   EPUB navigation labels become level-one headings when a chapter lacks one.
   Review every reverse-conversion warning because EPUB styling, pagination,
   fixed layout, scripts, and other reading-system behavior are not Markdown.
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
- EPUB output is reflowable EPUB 3. Keep semantic tables and review warnings for
  wide tables, spanning cells, missing image alt text, remote resources, and raw
  HTML. Kindle Oasis delivery uses Send to Kindle rather than direct USB EPUB.
- EPUB input produces consolidated Markdown with YAML metadata and media under
  `converted/media/<stem>/`. Reject DRM or unsupported encryption; never claim
  that CSS, typography, pagination, fixed layout, or scripts were preserved.
- Route scanned or low-text PDFs to `document-ingest` for OCR rather than
  pretending the extracted text is complete.
- Continue batches past individual failures and record each in the manifest.
- Do not install system packages; report missing tools and their remediation.
- Do not assume Obsidian schemas or frontmatter unless the user requests them.

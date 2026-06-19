# Document Ingest Output Contract

## Run Layout

Use one directory per source named `<source-stem>-<first-12-sha256-chars>`.
Write `manifest.csv` at the run root. A document directory contains:

```text
document.md
metadata.json
extraction_report.md
source_map.json
working/
  extracted.md
  chunks/
  reviewed-chunks/ # model-reviewed chunks, only when splitting was required
derived/
  ocr.pdf        # only when OCR ran
```

`working/extracted.md` and `working/chunks/` contain deterministic intermediate
material and must not be edited. `working/reviewed-chunks/` contains generated
review outputs. Do not treat working files as final artifacts. Other derived
media may appear when Pandoc extracts embedded assets.

## Normalization Rules

- Preserve all readable source content and its order.
- Repair structure only when supported by layout, typography, numbering, or
  explicit source labels.
- Do not summarize, interpret, modernize wording, or silently repair uncertain
  text.
- Preserve citations, footnotes, appendices, tables, and meaningful page
  boundaries where extraction permits.
- Keep extraction warnings outside `document.md`.

## Metadata

Keep `schemaVersion` equal to `1`. Preserve these top-level objects:

- `documentId`: `sha256:<full source hash>`.
- `source`: absolute path, basename, extension, format, byte size, modification
  time, and SHA-256.
- `extraction`: status, method, tool versions, warnings, chunk details, and OCR
  details.
- `fields`: `title`, `author`, `date`, and `source` evidence objects.
- `structure`: detected headings, tables, citations, footnotes, and appendices.
- `review`: whether model normalization is complete and any review notes.

Each evidence object has exactly:

```json
{
  "value": null,
  "origin": null,
  "confidence": null,
  "locator": null
}
```

Allowed origins are `embedded-metadata`, `document-text`, `filename`, and
`user-provided`. Allowed confidence values are `high`, `medium`, and `low`.
Use nulls when no defensible value exists. Preserve an embedded raw date when
its normalized interpretation is uncertain.

## Source Map

Keep `schemaVersion` equal to `1`, identify `document.md`, and provide ordered
entries with:

- `markdownStartLine` and `markdownEndLine`, or null for an empty source page.
- `sourceLocator`, using a PDF page number or the best available document,
  heading, or block locator.
- `method`: `page-extraction`, `document-conversion`, or `model-alignment`.
- `confidence`: `high`, `medium`, or `low`.

Update line ranges after model normalization. Do not claim page-level precision
for formats that do not expose pages.

## Extraction Report

Retain these sections:

```markdown
# Extraction Report
## Status
## Source
## Methods and Tools
## Coverage and OCR
## Structure and Encoding
## Warnings
## Review
```

Separate deterministic extraction facts from model review notes. State whether
OCR ran, why it ran, which pages remained low-text, and whether the derived PDF
was retained.

## Manifest

Use exactly these columns:

```text
document_id,source_path,source_sha256,source_format,status,output_directory,title,author,document_date,page_count,extraction_method,ocr_used,warning_count,error
```

Allowed statuses are `success`, `needs_review`, `failed`, and `skipped`.
Quote CSV values correctly. Keep paths absolute for sources and relative to the
run root for outputs.

## Model Review and Chunks

Review documents sequentially. The default threshold is 150,000 Unicode
characters. Preparation splits only at paragraph boundaries; headings and PDF
page transitions naturally qualify. A single unusually long paragraph may
exceed the threshold rather than being split.

For multiple chunks:

1. Review each complete chunk independently and write the result to the matching
   filename under `working/reviewed-chunks/`, without adding introductions or
   conclusions.
2. Preserve chunk order and every source passage.
3. Concatenate reviewed chunks without overlap.
4. Review seams, heading hierarchy, metadata evidence, and source mappings
   against `working/extracted.md`.
5. Mark `review.completed` true only after the final complete-document check.

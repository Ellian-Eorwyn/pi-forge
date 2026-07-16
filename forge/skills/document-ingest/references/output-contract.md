# Document Ingest Output Contract

## Run Layout

Use one directory per source named `<source-stem>-<first-12-sha256-chars>`.
For folder ingestion, the run root is the source folder's `Ingest/` directory.
Write `run_state.json`, `run_events.jsonl`, and `manifest.csv` at the run root
before extraction starts. Stage an active document under `.partial/` and rename
it into place only after its artifacts are durable. A document directory contains:

```text
document.md
metadata.json
extraction_report.md
source_map.json
working/
  extracted.md
  chunks/
  reviewed-chunks/ # model-reviewed chunks, only when splitting was required
  vision-pages/    # page-by-page model transcripts, only when vision ran
derived/
  ocr.pdf        # only when OCR ran
  glmocr-response.json # only when GLM-OCR SDK OCR ran
  glmocr-layout.json   # only when GLM-OCR SDK returned layout JSON
  vision-pages/  # unresolved PDF pages rendered as page-NNNN.png
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
- `extraction`: status, method, tool versions, warnings, chunk details, OCR
  quality comparisons, and vision fallback details.
- `fields`: `title`, `author`, `date`, and `source` evidence objects.
- `structure`: detected headings, tables, citations, footnotes, and appendices.
- `review`: whether model normalization is complete and any review notes.
- `finalOutput`: the final Markdown filename and the reason for that name.

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

## Final Output Filename

Set `metadata.finalOutput` before marking review complete:

```json
{
  "filename": "2026-05-03 Insurance Claim - Diagnosis - Procedure - Facility.md",
  "namingReason": "Uses the claim date, diagnosis, procedure, and facility stated in the source."
}
```

`filename` must be a filename only, not a path, and must end in `.md`.
`finalize` uses this filename for the final cleaned Markdown copy. If it is
missing, `finalize` falls back to a safe title/source-derived filename.

Choose names from the cleaned content and useful browsing cues, not merely the
original source filename. For lecture transcripts, include `Lecture Transcript`
or another content-supported transcript label. For administrative materials with
dates, start with `YYYY-MM-DD`. For insurance claims, include the date,
diagnosis, procedure, and facility when present. Do not invent missing details.

## Source Map

Keep `schemaVersion` equal to `1`, identify `document.md`, and provide ordered
entries with:

- `markdownStartLine` and `markdownEndLine`, or null for an empty source page.
- `sourceLocator`, using a PDF page number or the best available document,
  heading, or block locator.
- `method`: `page-extraction`, `document-conversion`, `model-alignment`, or
  `vision-transcription`.
- `confidence`: `high`, `medium`, or `low`.

Update line ranges after model normalization. Do not claim page-level precision
for formats that do not expose pages.

For PPTX, use ordered slide locators. Extract slide titles, body text, tables,
speaker notes, and image alt text from OOXML. Warn on visual-only slides,
charts, images whose meaning cannot be verified from alt text, and unsupported
embedded objects. Do not claim that deterministic XML extraction interpreted a
chart or visual composition.

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
OCR ran, why it ran, which pages remained suspicious, which pages selected OCR
output, whether vision ran, and whether derived files were retained.

## Vision Fallback

When automatic or forced OCR still leaves suspicious pages, preparation renders
those pages at 180 DPI under `derived/vision-pages/`. Read and transcribe one
page at a time. Store each transcript at the matching
`working/vision-pages/page-NNNN.md`, replace only that page's damaged text in
`document.md`, and add a `vision-transcription` source-map entry. Set
`vision.used` only after every candidate page is represented in
`vision.completedPages`. If the current model cannot read images, retain the
best local extraction and record `vision.unavailableReason`.

## GLM-OCR SDK Backend

The default backend is `auto`. With `auto` or `glmocr`, GLM-OCR is the primary
extractor for every PDF and image, even when a PDF already has a usable text
layer, because direct PDF text is unreliable for multi-column pages, charts,
and tables. Send PDFs and image inputs to the configured GLM-OCR SDK JSON
endpoint as base64 data URLs. Use `markdown_result`, then `md_results`, then
`text` as the extracted Markdown. Retain the full response at
`derived/glmocr-response.json`; retain `json_result` or `layout_details` at
`derived/glmocr-layout.json` when present. Set `metadata.extraction.method` to
`glm-ocr-sdk`, set `metadata.extraction.ocr.backend` to `glmocr`, and include
hashes for retained derived artifacts.

With `--ocr-backend auto`, record a warning and use local OCR if GLM-OCR
fails; with `--ocr-backend glmocr`, a GLM-OCR failure fails the document. When
GLM-OCR output is itself low quality, preparation renders the PDF pages under
`derived/vision-pages/`, sets `extraction.vision.required` true with every page
as a candidate, and the active model must complete the vision fallback (or
record `vision.unavailableReason`) before review is marked complete.

## Manifest

Use exactly these columns:

```text
document_id,source_path,source_sha256,source_format,status,suggested_pipeline,output_directory,title,author,document_date,page_count,extraction_method,ocr_used,warning_count,error
```

Allowed statuses are `pending`, `in_progress`, `success`, `needs_review`,
`failed`, and `skipped`.
Quote CSV values correctly. Keep paths absolute for sources and relative to the
run root for outputs. `suggested_pipeline` is advisory and may contain values
such as `basic-markdown`, `personal-admin`, `literature`,
`project-extraction`, `transcription,transcript-cleanup`, or
`transcription,transcript-cleanup,project-extraction`.

## Final Folder Layout

After every ingested file validates, run:

```bash
document-ingest.mjs finalize <source-folder>/Ingest --destination <source-folder>
```

`finalize` validates before moving or publishing anything, then preflights every
destination into `finalize_plan.json`. Each hash-bound operation is committed
and journaled independently. On restart it recognizes an already moved or
published expected hash and continues; it refuses overwrite conflicts or
mismatched filesystem state. The source folder uses:

```text
<source-folder>/
  Ingest/       # run state, manifests, extraction reports, source maps, derived files
  Originals/    # moved original source files, preserving relative paths
  Generated/    # user-facing generated synthesis and tables
  *.md          # final cleaned Markdown for flat folders
```

For structured folders, final cleaned Markdown preserves the source-relative
subfolder path. `Ingest/`, `Originals/`, and `Generated/` are reserved and are
ignored during future source discovery.

`artifact_manifest.csv` is written under `Ingest/` with:

```text
role,document_id,source_path,destination_path,sha256,created_at
```

Roles are `original`, `final_markdown`, and `generated_artifact`.
`destination_path` is relative to the finalized source folder. Generated
artifacts include user-facing files such as `evidence_table.csv`,
`methods_matrix.csv`, `claims_matrix.md`, `key_terms.md`,
`literature_summary.md`, `citation_notes.md`, and `research_gaps.md`.
Advisory processing files such as `claim_clusters.csv` and `claim_clusters.md`
remain in `Ingest/`.

## Model Review and Chunks

Review bounded units sequentially. Vision pages and large chunks are separate
queue items, followed by one final document metadata review. The default
threshold is 150,000 Unicode
characters. Preparation splits only at paragraph boundaries; headings and PDF
page transitions naturally qualify. A single unusually long paragraph may
exceed the threshold rather than being split.

For multiple chunks:

1. Review each complete chunk independently and commit it with
   `record-review-unit`; the script writes the matching filename under
   `working/reviewed-chunks/`. Do not add introductions or conclusions.
2. Preserve chunk order and every source passage.
3. Concatenate reviewed chunks without overlap.
4. Review seams, heading hierarchy, metadata evidence, and source mappings
   against `working/extracted.md`.
5. Mark `review.completed` true only after the final complete-document check.

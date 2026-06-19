# File Conversion Contract

Convert files between formats without modifying originals, and disclose every
lossy conversion honestly.

## Run Layout

```text
<run-dir>/
  converted/
    <safe-stem>.<target>     # one output per converted source
    media/<stem>/…           # images extracted from DOCX/HTML sources
    <stem>/<sheet>.csv       # one CSV per sheet for multi-sheet XLSX
  conversion_log.md
  conversion_manifest.csv
  warnings.md
```

Originals are never modified. Every output is written under `converted/`. The
run directory must not pre-exist.

## Conversion Matrix

| Source group | Extensions | Allowed targets | Tool |
|---|---|---|---|
| `docx` | `.docx` | `md`, `txt` | Pandoc |
| `md`   | `.md`, `.markdown` | `docx`, `html`, `txt` | Pandoc |
| `html` | `.html`, `.htm` | `md`, `txt` | Pandoc |
| `pdf`  | `.pdf` | `txt`, `md` | `pdftotext -layout` |
| `csv`  | `.csv`, `.tsv` | `xlsx` | openpyxl |
| `xlsx` | `.xlsx` | `csv` | openpyxl |
| `txt`  | `.txt` | `txt` (cleanup) | in-process |

A source/target pair outside this matrix is `skipped`, not `failed`. Missing
tools produce a `failed` row with actionable remediation rather than a silent
empty output.

## Filenames and Traceability

Output names use a normalized, filesystem-safe stem (NFKC, non-word runs
collapsed to `-`). Colliding stems get numeric suffixes (`name-2.md`). The
`conversion_manifest.csv` links every source to its output and records the
source SHA-256, so outputs remain traceable to their origin.

## Manifest

```text
source_path,source_sha256,source_format,target_format,status,output_path,warning_count,error
```

`output_path` is relative to the run directory. Statuses:

- `success` — converted with no warnings.
- `needs_review` — converted but lossy (one or more warnings in `warnings.md`).
- `skipped` — the source cannot produce the requested target.
- `failed` — a tool or input error; the batch continues past it.

## Lossy-Conversion Disclosure

Never claim complex formatting survived when it did not. Record warnings for:

- DOCX/HTML structural loss (styles, footnotes, complex layout) and
  **extracted media** (location under `converted/media/<stem>/`).
- PDF Markdown being unstructured extracted text (no reconstructed headings or
  tables), and low-text output that suggests a scanned PDF — route those to
  `document-ingest` for OCR.
- CSV→XLSX writing values as text without type inference.
- XLSX→CSV exporting last-computed values and dropping formulas, macros, charts,
  and styling; multi-sheet workbooks splitting into one CSV per sheet.
- Encoding damage: Unicode replacement or control characters in text output.

## Validation

`validate` re-reads the manifest, confirms every `success`/`needs_review` output
still exists, and checks each source hash is unchanged. Missing outputs or
changed sources are errors; counts of `skipped` and `failed` rows are reported as
warnings for review.

# File Conversion Contract

Convert files between formats without modifying originals, and disclose every
lossy conversion honestly.

## Run Layout

```text
<run-dir>/
  converted/
    <safe-stem>.<target>     # one output per converted source
    media/<stem>/…           # media extracted from DOCX/HTML/EPUB sources
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
| `epub` | `.epub` | `md` | Pandoc plus deterministic cleanup |
| `md`   | `.md`, `.markdown` | `docx`, `epub`, `html`, `txt` | Pandoc |
| `html` | `.html`, `.htm` | `md`, `txt` | Pandoc |
| `pdf`  | `.pdf` | `txt`, `md` | `pdftotext -layout` |
| `csv`  | `.csv`, `.tsv` | `xlsx` | openpyxl |
| `xlsx` | `.xlsx` | `csv` | openpyxl |
| `txt`  | `.txt` | `txt` (cleanup) | in-process |

A source/target pair outside this matrix is `skipped`, not `failed`. Missing
tools produce a `failed` row with actionable remediation rather than a silent
empty output.

## Markdown to EPUB

EPUB output is reflowable EPUB 3. Each level-one heading begins a chapter file
and appears in the visible linked table of contents. Lower-level headings remain
inside the chapter. A source without level-one headings produces one readable
content section and a review warning.

Pandoc YAML metadata supplies title, author, language, and date. The converter's
`--title`, `--author`, `--language`, and `--date` options override frontmatter;
missing title and language fall back to the source filename and `en-US`.
Metadata and `--cover` options require exactly one Markdown source. Covers must
be baseline JPEG or PNG. The manifest records the selected cover path and hash.

The embedded stylesheet uses relative sizing, compact list indentation,
responsive images, wrapping code, and semantic tables. It does not embed fonts,
scripts, fixed page sizes, or fixed-layout metadata. Report wide tables,
spanning cells, long unbreakable table values, raw HTML, missing image alt text,
remote resources, and unusually large covers for review.

## EPUB to Markdown

EPUB input produces one consolidated GFM file with YAML metadata. Content images
are extracted under `converted/media/<stem>/` and referenced with relative paths;
the cover is preserved there without insertion into the body unless it was part
of the reading order. EPUB landmark navigation, internal spine markers, section
wrappers, and figure wrappers are removed.

TOC/spine entries define chapter boundaries. Existing level-one headings are
preserved. A chapter without one receives a level-one heading from its EPUB
navigation label and a warning that content was synthesized. Internal chapter
links are rewritten to Markdown heading links where possible.

Every reverse conversion warns that CSS, typography, pagination, and reading-
system behavior were lost. Fixed layout, scripts, audio, and video receive
specific warnings. DRM and unsupported encryption fail conversion.

## Managed EPUBCheck

`install-epubcheck` installs official EPUBCheck 5.3.0 under
`${PI_CODING_AGENT_DIR:-~/.pi-forge/agent}/tools/epubcheck/5.3.0/`. It requires
a working system Java runtime, verifies the pinned official archive SHA-256,
rejects unsafe ZIP paths and symbolic links, verifies the reported version, and
installs atomically. It does not install Java or system packages.

Validator resolution order is `EPUBCHECK_JAR`, the managed pinned JAR, then an
`epubcheck` executable on `PATH`. Built-in structural validation remains the
fallback when none is available. EPUBCheck fatal/errors fail validation;
warnings and usage findings are reported for review.

## Filenames and Traceability

Output names use a normalized, filesystem-safe stem (NFKC, non-word runs
collapsed to `-`). Colliding stems get numeric suffixes (`name-2.md`). The
`conversion_manifest.csv` links every source to its output and records the
source SHA-256, so outputs remain traceable to their origin.

## Manifest

```text
source_path,source_sha256,source_format,target_format,status,output_path,warning_count,error,cover_path,cover_sha256
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
- EPUB portability risks: wide or spanning tables, raw HTML, remote resources,
  missing image alt text, and covers likely to index slowly on small devices.
- EPUB→Markdown presentation loss, synthesized chapter headings, extracted
  media, fixed layout, scripts, and unsupported audio or video.

## Validation

`validate` re-reads the manifest, confirms every `success`/`needs_review` output
still exists, and checks each source and cover hash is unchanged. EPUB validation
checks the archive mimetype, container, package, manifest, spine, navigation
targets, XML documents, and cover declaration. EPUBCheck runs when installed;
otherwise validation reports that only structural checks were performed.
Missing outputs or changed sources are errors; counts of `skipped` and `failed`
rows are reported as warnings for review.

For EPUB→Markdown, validation reparses the Markdown, checks referenced media and
the preserved cover, verifies chapter heading coverage, rejects leaked EPUB
navigation/wrapper markup, and confirms the source EPUB hash is unchanged.

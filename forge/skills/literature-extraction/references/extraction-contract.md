# Literature Extraction Output Contract

Extract structured, source-backed evidence without overclaiming. Keep extracted
content separate from generated synthesis, and never invent details that the
documents do not support.

## Modes

- **Single document**: one source produces evidence and methods rows for that
  document, plus the synthesis Markdown framed around it.
- **Folder / corpus**: every supported source under the folder is processed one
  at a time, and the synthesis deliverables compare across documents.

This skill consumes text sources: `document.md` (from `document-ingest`),
`.md`/`.markdown`, and `.txt`. Convert PDF, DOCX, HTML, and RTF with
`document-ingest` first so page-level provenance and SHA-256 hashes are already
established.

## Run Layout

```text
<run-dir>/
  run_config.json            # schema version, input, item types, document list with hashes
  documents.csv              # per-source manifest with hashes and final disposition
  extraction_results.jsonl   # append-only, one record per document
  evidence_table.csv
  evidence_table.xlsx        # only when openpyxl is available
  methods_matrix.csv
  literature_summary.md      # model-authored
  claims_matrix.md           # model-authored
  research_gaps.md           # model-authored
  citation_notes.md          # model-authored
  working/                   # per-document extraction JSON written before `record`
```

`run_config.json`, `documents.csv`, and `extraction_results.jsonl` are managed
by the script. Do not hand-edit them. The four Markdown deliverables are
scaffolded by `build` only when absent, then authored by the model.

## Extraction Schema

Each document's extraction is a JSON array of item objects. Every item has
exactly these fields:

- `item_type`: one of `claim`, `method`, `data_source`, `finding`,
  `limitation`, `definition`, `citation`, `quoted_evidence`, `variable`,
  `population`, `technology`, `policy`, `research_gap`.
- `text`: the extracted statement in your words or a faithful paraphrase
  (required, nonblank).
- `evidence_quote`: a short verbatim quote from the source supporting the item,
  or null when no single quote applies.
- `locator`: a page number, section, heading, or block reference, or null.
- `interpretation`: `explicit`, `inferred`, or `unclear` (see below).
- `confidence`: `high`, `medium`, or `low`.
- `notes`: optional clarification, or null.

Item-type meanings: `claim` is an assertion the source argues for; `finding` is
a reported result; `method` covers study design, procedures, and analysis;
`data_source` is a dataset, corpus, or instrument used; `definition` is a term
the source defines; `citation` is a referenced work; `quoted_evidence` is a
notable passage worth preserving verbatim; `variable`, `population`,
`technology`, and `policy` capture the studied constructs and context;
`research_gap` is an acknowledged or implied gap.

## Interpretation Discipline

- `explicit`: the source states it directly. Prefer an `evidence_quote`.
- `inferred`: you concluded it from the source but the source does not say it
  outright. Never present inference as fact.
- `unclear`: the source is ambiguous, contradictory, or silent where a value
  was expected. Record it as unclear rather than guessing.

Do not extend a source's claims beyond what it supports. Attribute every item to
its document. When sources disagree, record both and surface the disagreement in
the synthesis rather than reconciling it silently.

## Provenance and Locators

Use the most precise locator the format exposes. For `document-ingest` output,
use the page numbers or headings recorded in its `source_map.json`. For plain
Markdown or text, use headings or section labels. Never claim page-level
precision for a format that does not expose pages. Hashes recorded at `init`
must still match at `build` and `validate`; a changed source aborts the run.

## Evidence Table

`evidence_table.csv` (and the XLSX twin) has one row per extracted item with
columns:

```text
document_id,source_path,source_title,item_type,item_text,evidence_quote,locator,interpretation,confidence,notes
```

## Methods Matrix

`methods_matrix.csv` has one row per successfully extracted document. Each
content column is the `;`-joined `text` values of one item type:

```text
document_id,source_title,methods,data_sources,populations,variables,technologies,policies,limitations,research_gaps
```

## Markdown Deliverables

- `literature_summary.md`: scope, key cross-source findings, agreement and
  disagreement, assumptions/limits, and open questions.
- `claims_matrix.md`: one row per claim with source(s), locator, interpretation,
  and supporting/contradicting sources.
- `research_gaps.md`: explicitly stated gaps and inferred gaps, kept distinct.
- `citation_notes.md`: one annotated entry per document.

Keep verbatim quotes and extracted facts traceable to the evidence table. Keep
generated synthesis, assumptions, and judgment clearly marked as such.

## Statuses

Each document gets exactly one disposition: `success` (with an items array,
possibly empty when the document genuinely yields no items of interest),
`needs_review` (unresolved judgment), `skipped` (intentionally not processed),
or `failed` (a processing error). Every non-success disposition requires a note.
Never hide an unprocessable document behind an empty success.

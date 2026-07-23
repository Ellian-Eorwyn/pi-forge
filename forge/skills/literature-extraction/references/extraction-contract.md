# Literature Extraction Output Contract

Extract structured, source-backed evidence without overclaiming. Keep extracted
content separate from generated synthesis, and never invent details that the
documents do not support.

## Modes

- **Single document**: one source produces evidence and methods rows for that
  document, plus the synthesis Markdown framed around it.
- **Folder / corpus**: every supported source under the folder is processed one
  at a time, and the synthesis deliverables compare across documents.
- **Meta corpus**: one or more completed literature-extraction runs are treated
  as source corpora. The meta run consumes prior structured artifacts first,
  prepares context-bounded packets, and produces cross-corpus synthesis guided
  by a research question.

This skill consumes text sources: `document.md` (from `document-ingest`),
`.md`/`.markdown`, and `.txt`. Convert PDF, DOCX, HTML, and RTF with
`document-ingest` first so page-level provenance and SHA-256 hashes are already
established.

## Run Layout

```text
<run-dir>/
  run_state.json             # durable phase, item, synthesis, and deliverable queues
  run_events.jsonl           # append-only fsynced transition journal
  run_config.json            # schema version, input, item types, document list with hashes
  documents.csv              # per-source manifest with hashes and final disposition
  extraction_results.jsonl   # append-only, one record per document
  evidence_table.csv
  methods_matrix.csv
  claim_clusters.csv         # advisory; only when embeddings ran and >=2 claims/findings
  claim_clusters.md          # advisory worksheet; same condition
  item_index.jsonl           # normalized item index for downstream/meta use
  source_profile.csv         # per-source item counts for downstream/meta use
  synthesis_state.json       # context-bounded hierarchical packet queue
  synthesis_packets/         # deterministic packet inputs
  synthesis_memos/           # atomically recorded packet memos
  literature_summary.md      # model-authored
  claims_matrix.md           # model-authored
  key_terms.md               # model-authored
  research_gaps.md           # model-authored
  citation_notes.md          # model-authored
  working/                   # per-document extraction JSON written before `record`
```

`run_state.json`, `run_events.jsonl`, `run_config.json`, `documents.csv`, and
`extraction_results.jsonl` are managed by the script. Do not hand-edit them.
`documents.csv` is atomically refreshed after every `record`; an incomplete
final journal record is recoverable without accepting malformed interior
records. Markdown deliverables are scaffolded by `build` only when absent,
then committed one at a time with `record-output`.

## Meta Run Layout

```text
<meta-run-dir>/
  run_state.json
  run_events.jsonl
  meta_config.json           # schema version, research question, corpus sources, packets
  meta_sources.csv           # prior runs and inferred/explicit corpus labels
  meta_items.jsonl           # normalized extracted items across prior runs
  meta_artifacts.jsonl       # section-level authored prior synthesis; never evidence
  corpus_digest.md           # deterministic run/document coverage and item-type digest
  context_budget.json        # packet token estimates and warnings
  bridge_candidates.csv      # advisory cross-corpus embedding matches
  topic_clusters.csv         # advisory topic/claim/definition clusters
  packets/
    packet-####.md           # bounded model work packets
  packet_memos.jsonl         # append-only, one model-authored memo per packet
  authoring_context.md       # bounded final memos, digest, warnings, and citation map
  working/
    embedding_cache.json     # per-run embedding cache when embeddings ran
  meta_synthesis.md          # model-authored
  primary_secondary_matrix.md
  concept_register.md
  negative_cases.md
  methods_and_limits.md
```

`meta_config.json`, `meta_sources.csv`, `meta_items.jsonl`, `meta_artifacts.jsonl`,
`context_budget.json`, `bridge_candidates.csv`, `topic_clusters.csv`,
`corpus_digest.md`, `packets/`, `packet_memos.jsonl`, and
`authoring_context.md` are managed by the script. The Markdown deliverables are
scaffolded by `meta-build` only when absent, then authored by the model from the
bounded authoring context.

Meta runs use schema version 2. Existing completed first-pass literature runs
remain valid inputs, but schema-version-1 meta directories must be reinitialized
in a new output directory rather than resumed.

## Extraction Schema

Each document's extraction is a JSON array of item objects. Every item has
exactly these fields:

- `item_type`: one of `claim`, `connection`, `method`, `data_source`,
  `finding`, `limitation`, `definition`, `author`, `citation`,
  `quoted_evidence`, `variable`, `population`, `technology`, `policy`,
  `research_gap`.
- `text`: the extracted statement in your words or a faithful paraphrase
  (required, nonblank).
- `direct_quotes`: short verbatim quote support from the source, or null when no
  quote is available. Include multiple short quotes in one field when needed.
- `locator`: a page number, section, heading, or block reference, or null.
- `interpretation`: `explicit`, `inferred`, or `unclear` (see below).
- `confidence`: `high`, `medium`, or `low`.
- `notes`: optional clarification, or null.

Item-type meanings: `claim` is an assertion the source argues for; `finding` is
a reported result; `connection` is a shared idea or explicit/inferred link
between readings; `method` covers study design, procedures, and analysis;
`data_source` is a dataset, corpus, or instrument used; `definition` is a key
term the source defines; `author` is a brief description of an author based only
on the provided content; `citation` is a referenced work; `quoted_evidence` is a
notable passage worth preserving verbatim; `variable`, `population`,
`technology`, and `policy` capture the studied constructs and context;
`research_gap` is an acknowledged or implied gap.

## Interpretation Discipline

- `explicit`: the source states it directly. Prefer `direct_quotes`.
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
remain the frozen snapshot. `status` reports drift; `refresh` explicitly adds,
supersedes, or retires source revisions without deleting prior artifacts.

## Evidence Table

`evidence_table.csv` has one row per extracted item with columns:

```text
document_id,source_path,source_title,item_type,item_text,direct_quotes,locator,interpretation,confidence,notes
```

## Methods Matrix

`methods_matrix.csv` has one row per successfully extracted document. Each
content column is the `;`-joined `text` values of one item type:

```text
document_id,source_title,methods,data_sources,populations,variables,technologies,policies,limitations,research_gaps
```

## Cross-Document Claim Clusters

When the embeddings endpoint (`FORGE_EMBEDDINGS_URL`, default
`http://llms:8005/v1/embeddings`) is reachable and at least two `claim` or
`finding` items exist, `build` embeds those items and groups semantically similar
ones across documents into `claim_clusters.csv` and the `claim_clusters.md`
worksheet. This raises recall when authoring `claims_matrix.md` and
`literature_summary.md` on larger corpora, where reading the whole evidence table
for every cross-source link is unreliable.

This worksheet is advisory and is not a deliverable:

- It groups related claims; it never reconciles, merges, or decides
  contradictions. The model judges each group against the evidence and records
  genuine agreement and disagreement, attributing every claim to its document and
  locator.
- The `negation_hint` column and the "possible contradiction" flag are a crude
  lexical signal (presence of a negation cue) to prompt review, not a polarity
  determination. Do not treat the flag as a conclusion.
- It degrades cleanly: when embeddings are unavailable, `--no-claim-clusters` is
  passed, or fewer than two claims/findings exist, `build` skips the worksheet
  and records the reason in its JSON output. The run is still valid; `validate`
  does not require the worksheet.
- `report-output` can ingest `claim_clusters.md` and `claim_clusters.csv` from the
  run directory like any other input when assembling synthesis.

## Meta Extraction Discipline

The meta workflow is for cross-run analysis, not re-extraction. It must preserve
the difference between first-pass evidence and generated synthesis:

- Treat `meta_items.jsonl` as the source of extracted evidence. Every row keeps
  item id, corpus label, prior run id, document id, source title, source path,
  item type, item text, quote support, locator, interpretation, confidence, and
  source availability.
- Treat `meta_artifacts.jsonl` as prior generated synthesis. Each `a######`
  section preserves its run, corpus, filename, heading, text, relevance score,
  and estimated size. It can guide interpretation and retrieval but cannot
  support a substantive final claim without an `m######` evidence citation.
- Use `bridge_candidates.csv` and `topic_clusters.csv` as retrieval aids. They
  can suggest conceptual links and tensions, but they never prove a connection
  or contradiction.
- Meta packets must remain under the configured payload budget. The default
  total model-call target is `128000` estimated tokens, with `32000` reserved
  for instructions and output and therefore `96000` available to packet
  material. The hard maximum is `256000`, using `ceil(characters / 4)`.
- Level-one `evidence` packets cover every structured item. Level-one
  `prior-synthesis` packets cover every authored section, and relevant sections
  may also be repeated in evidence packets. When leaf memos do not fit the final
  authoring budget, `meta-next` recursively creates `reduction` packets until
  `authoring_context.md` fits.
- Normal synthesis uses the same 128,000-token target and recursively reduces
  packet memos into higher levels when the corpus still exceeds the target.
- Missing original source files are warnings, not fatal errors, when prior
  structured artifacts are intact. Quote verification and snippets degrade to
  artifact-only provenance.
- Evidence-packet memos must cite packet-local item ids; prior-synthesis memos
  must cite packet-local artifact ids; reduction memos must preserve inherited
  citations. Final meta deliverables require valid evidence item ids. Unknown
  citations and artifact-only substantive support are invalid.
- For social sciences and humanities work, keep primary evidence, secondary
  interpretation, source-native terms, analyst/theory terms, negative cases,
  silences, uncertainty, and methodological limits visible.

## Markdown Deliverables

- `literature_summary.md`: scope, key cross-source findings, agreement and
  disagreement, assumptions/limits, and open questions.
- `claims_matrix.md`: one row per claim with source(s), locator, interpretation,
  and supporting/contradicting sources.
- `key_terms.md`: one row per key term or definition with source(s), locator,
  interpretation, and direct quote support.
- `research_gaps.md`: explicitly stated gaps and inferred gaps, kept distinct.
- `citation_notes.md`: one annotated entry per document.
- Meta deliverables:
  - `meta_synthesis.md`: research-question-guided cross-corpus synthesis.
  - `primary_secondary_matrix.md`: primary-source evidence linked to
    secondary-source concepts or other explicit corpus roles.
  - `concept_register.md`: emic/etic/unclear concepts with item-id support.
  - `negative_cases.md`: contradictions, absences, failed fits, and limits to
    the main interpretation.
  - `methods_and_limits.md`: corpus coverage, extraction limits, packeting
    limits, missing source warnings, and inherited OCR/locator limits.

Keep verbatim quotes and extracted facts traceable to the evidence table. Keep
generated synthesis, assumptions, and judgment clearly marked as such.

## Statuses

Each document gets exactly one disposition: `success` (with an items array,
possibly empty when the document genuinely yields no items of interest),
`needs_review` (unresolved judgment), `skipped` (intentionally not processed),
or `failed` (a processing error). Every non-success disposition requires a note.
Never hide an unprocessable document behind an empty success.

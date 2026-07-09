# Deep Research Output Contract

Deep research is an iterative, provenance-first workflow. Deterministic code
owns search, page reading, source archiving, hashing, manifests, and validation.
The local model only returns bounded JSON for query expansion, evidence
extraction, and claim registration.

## Run Layout

```text
<run-dir>/
  research_run.json          # run configuration, seed queries, counts
  query_log.jsonl            # one record per searched query
  source_index.json          # source registry without full text
  evidence_items.jsonl       # extracted source-backed evidence
  claim_register.jsonl       # final claim records with evidence/source ids
  gap_log.jsonl              # unresolved gaps and limits
  model_calls.jsonl          # every model prompt, response, status, and error
  web_manifest.csv           # web-collection-style provenance rows
  web_manifest.json          # provenance manifest and source metadata
  downloads/<source-id>.txt  # archived extracted source text
  sources.md                 # deterministic source table
  deep_research_report.md    # deterministic synthesis from claim_register
  validation_report.json     # validator output
```

The output directory must not already exist. Source web pages are never modified.

## Provenance Model

The internal model is inspired by W3C PROV but remains JSON/CSV:

- A source page is an entity with a `sourceId`, requested URL, final URL, access
  date, extraction method, SHA-256 hash, and search origins.
- A search, read, extraction, or claim-registration step is an activity recorded
  in `query_log.jsonl` or `model_calls.jsonl`.
- Evidence and claims are derived entities that cite their upstream source and
  evidence ids directly.

Do not add RDF, PROV-N, or WARC export for the v1 workflow. `web_manifest.*`
and the JSONL files are the source of truth.

## Evidence Items

Each evidence item has:

```json
{
  "evidenceId": "ev-0001",
  "sourceId": "src-...",
  "text": "faithful extracted statement",
  "directQuote": "short exact quote or null",
  "locator": "heading, section, or URL",
  "interpretation": "explicit",
  "confidence": "high",
  "notes": null,
  "extractedAt": "ISO-8601 timestamp"
}
```

`interpretation` is one of `explicit`, `inferred`, or `unclear`. `confidence` is
one of `high`, `medium`, or `low`. If `directQuote` is non-null, validation must
find it in the archived extracted source text after whitespace normalization.

## Claims and Gaps

Every claim must cite at least one source id and one evidence id:

```json
{
  "claimId": "cl-0001",
  "text": "source-backed claim",
  "evidenceIds": ["ev-0001"],
  "sourceIds": ["src-..."],
  "confidence": "medium",
  "notes": "limits, disagreement, or null",
  "createdAt": "ISO-8601 timestamp"
}
```

Use confidence as a judgment about support:

- `high`: direct support from a primary source or multiple relevant sources.
- `medium`: one solid source or partial triangulation.
- `low`: thin, inferred, stale, ambiguous, or conflicting support.

Gaps record missing, contradictory, or under-supported areas. Do not smooth over
disagreement; record conflicting evidence and surface the limit in the report.

## Validation Rules

`web-research.mjs validate <run-dir>` fails when:

- a required artifact is missing;
- a source archive path escapes the run directory;
- an archived source hash no longer matches `source_index.json`;
- an evidence item references a missing source;
- a direct quote cannot be found in the archived extracted source text;
- a claim lacks source ids or evidence ids;
- a claim references missing evidence or a missing source;
- a claim omits the source id attached to cited evidence;
- `deep_research_report.md` does not cite every claim, source, and evidence id
  used by the claim register;
- `web_manifest.csv` does not match the required provenance columns.

The report is generated from `claim_register.jsonl` and `evidence_items.jsonl`.
Do not hand-author unsupported findings directly in the report.

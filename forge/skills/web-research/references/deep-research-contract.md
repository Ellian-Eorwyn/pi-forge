# Deep Research Output Contract

Deep research is an iterative, provenance-first workflow. Deterministic code
owns search, page reading, source archiving, hashing, manifests, and validation.
The local model only returns bounded JSON for query expansion, evidence
extraction, and claim registration.

Deep research is optimized for a local single-model runtime. Search and model
calls are serialized, direct page acquisition is bounded separately, Playwright
is capped independently, and embedding batches rank source chunks before LLM
evidence extraction.

## Run Layout

```text
<run-dir>/
  run_state.json              # provider, URL, or iteration queue
  run_events.jsonl            # fsynced transition journal
  deep_checkpoint.json        # resumable deep-research domain checkpoint
  research_run.json          # run configuration, seed queries, counts
  query_log.jsonl            # one record per searched query
  source_index.json          # source registry without full text
  evidence_items.jsonl       # extracted source-backed evidence
  claim_register.jsonl       # final claim records with evidence/source ids
  gap_log.jsonl              # unresolved gaps and limits
  model_calls.jsonl          # every model prompt, response, status, and error
  scheduler_log.jsonl        # FIFO queue wait/start/end records
  search_cache_log.jsonl     # SearXNG cache hit/miss records
  chunks.jsonl               # extracted source chunks and locators
  embedding_log.jsonl        # embedding batch/cache status
  source_rankings.jsonl      # lexical and embedding relevance scores
  normalized_urls.jsonl      # requested/final/canonical URL records
  strategy_decisions.jsonl   # fetch strategy decisions and fallback plan
  acquisition_log.jsonl      # one record per acquisition attempt
  extraction_log.jsonl       # extraction method, text size, locator metadata
  cache_log.jsonl            # stage-cache hit/miss records
  metrics.json               # timing, strategy, bytes, and context counters
  discovery_reports/*.json   # only when discover/network inspection runs
  archive/raw/*              # raw HTTP response bodies or rendered HTML
  archive/rendered/*         # browser-rendered HTML when used
  archive/extracted/*        # extracted document JSON
  archive/chunks/*           # selected chunk text artifacts
  web_manifest.csv           # web-collection-style provenance rows
  web_manifest.json          # provenance manifest and source metadata
  downloads/<source-id>.txt  # archived extracted source text
  sources.md                 # deterministic source table
  deep_research_report.md    # deterministic synthesis from claim_register
  validation_report.json     # validator output
```

A compatible output directory resumes. Quick reads commit each URL, academic
runs commit each provider, and deep runs checkpoint queries, source URLs, and
iteration boundaries. Unrelated or legacy directories are refused. Source web
pages are never modified.

## Provenance Model

The internal model is inspired by W3C PROV but remains JSON/CSV:

- A source page is an entity with a `sourceId`, requested URL, final URL, access
  date, canonical URL, acquisition strategy, extraction method, SHA-256 hash,
  raw artifact path, extracted artifact path, acquisition validation result,
  and search origins.
- A search, read, extraction, or claim-registration step is an activity recorded
  in `query_log.jsonl`, `strategy_decisions.jsonl`, `acquisition_log.jsonl`,
  `extraction_log.jsonl`, or `model_calls.jsonl`.
- Evidence and claims are derived entities that cite their upstream source and
  evidence ids directly.

Evidence extraction may be batched across ranked source packs. A batch can
include multiple sources, but every returned evidence item must still identify
the source id it came from and direct quotes are verified against that source's
archived text.

Do not add RDF, PROV-N, or WARC export for the v1 workflow. `web_manifest.*`
and the JSONL files are the source of truth.

## Acquisition Model

The acquisition ladder must prefer deterministic work before browser work:

1. Known domain strategy or official provider adapter.
2. Direct HTTP.
3. Embedded structured data such as JSON-LD, citation metadata, OpenGraph,
   `__NEXT_DATA__`, or application JSON script tags.
4. Static main-content extraction with Readability and HTML text fallback.
5. Internal JSON/API endpoint discovery and replay where available.
6. Playwright network discovery.
7. Playwright DOM extraction.

Validation decides fallback. A page should not use Playwright merely because it
contains JavaScript if direct HTTP produced good text and metadata.

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

## Acquisition Metrics

`metrics.json` should make performance visible. It records counts for discovered
search results, unique canonical URLs, direct HTTP successes, structured-data
successes, Playwright DOM fallbacks, failed sources, cache hits/misses, raw bytes
downloaded, extracted characters, queue wait/duration totals, embedding counts,
selected chunks, and evidence characters sent to the model.

## Academic Research Output Contract

Academic research is a metadata-first workflow for scholarly discovery. It
queries structured academic providers, normalizes records into canonical works,
deduplicates them, and emits citation-manager-ready RIS.

```text
<run-dir>/
  academic_run.json          # run configuration, provider capabilities, counts
  works.jsonl                # one canonical Work per deduped article/work
  source_records.jsonl       # provider records and normalized payloads
  field_provenance.jsonl     # field-level provider/source-path provenance
  dedupe_decisions.jsonl     # merge/link/keep decisions and evidence
  provider_requests.jsonl    # request URLs, status, raw paths, hashes
  provider_errors.jsonl      # non-fatal provider failures
  raw/<provider>/*           # archived provider responses
  provider_results/*.json    # atomic provider checkpoints
  works.ris                  # aggregate RIS, one record per unique Work
  ris/<work-id>.ris          # individual RIS record for each unique Work
  ris_manifest.json          # Work-to-RIS mapping and dedupe keys
  academic_report.md         # deterministic work/provider summary
  validation_report.json     # validator output
```

RIS is generated only after dedupe. `works.ris` must contain exactly one RIS
record for each canonical Work, and every Work must have a matching
`ris/<work-id>.ris` file. Validation fails on duplicate DOI/PMID/arXiv/title-year
RIS keys, missing per-work RIS files, missing manifest records, or RIS records
without `ER  -` terminators.

---
name: web-research
description: Quick web search and page reading for information lookup. Use to search the web via SearXNG, fetch and extract readable text from URLs, and produce structured research findings with source attribution. Use for fact-checking, finding documentation, current events, quick research before deeper analysis, or any task that needs current web information. Prefer web-collection when archiving full sites with provenance for downstream processing.
---

# Web Research

Search the web, extract readable content from the most relevant pages, and
produce structured findings with source attribution. A deterministic script
handles search, fetching, and text extraction; you supply the judgment — query
formulation, result triage, and final synthesis.

Use **`deep`** when the user asks for a full research pass, multiple seed
queries, iterative follow-up searching, source-backed synthesis, or strict
provenance. Read [references/deep-research-contract.md](references/deep-research-contract.md)
before relying on deep research artifacts.

The search backend is **SearXNG** from `connectedServices.searxng` in
`~/.pi-forge/agent/settings.json` (default: `http://llms/searxng`). SearXNG is
used for discovery only. Page acquisition uses the cheapest reliable strategy
first: domain registry knowledge, direct HTTP, embedded structured data,
Readability/static extraction, then Playwright network discovery or DOM
extraction only when validation indicates a browser is needed.

The configured **Playwright WebSocket endpoint** from
`connectedServices.playwright` (default: `ws://llms/playwright`) is a fallback
and discovery aid, not the default fetcher. Use `--no-browser` or `--mode fast`
when speed matters more than JavaScript-heavy coverage.

Deep research assumes a local single-model runtime by default: model calls are
serialized through one FIFO queue, SearXNG searches are serialized and cached,
direct HTTP acquisition is bounded, Playwright is capped separately, and the
local embeddings endpoint ranks chunks before evidence extraction.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path. Check
   capabilities before relying on search or extraction:

   ```bash
   node <skill-directory>/scripts/web-research.mjs doctor [--json]
   ```

   `doctor` reports SearXNG connectivity, Playwright availability, the
   configured browser endpoint, and remediation steps.

2. Choose a command. `<run-directory>` goes under
   `forge-output/web-research/`, or under the vault workflow root
   `99 Meta/99.06 Workflows/Web Research/` inside an Obsidian vault — the
   `web_research` tools resolve that themselves when `output` is omitted:

   - **`research`** — Full workflow: search → read top results → report.
     The default choice for most information lookups:

     ```bash
     node <skill-directory>/scripts/web-research.mjs research <query...> \
       --output <run-directory> [--limit N] [--read-count N] [--mode fast|standard|deep]
     ```

     This searches SearXNG, normalizes and deduplicates URLs, fetches the top N
     results (default 5) through the acquisition ladder, extracts clean text
     from each, and writes `research_report.md`, `research_report.json`, stage
     logs, and `metrics.json`.

   - **`search`** — Search only, no page fetching:

     ```bash
     node <skill-directory>/scripts/web-research.mjs search <query...> \
       --output <run-directory> [--limit N]
     ```

     Returns ranked results with title, URL, snippet, engine, and score.

   - **`read`** — Fetch and extract text from specific URLs:

     ```bash
     node <skill-directory>/scripts/web-research.mjs read <url...> \
       --output <run-directory> [--input-file <list>] [--mode fast|standard|deep]
     ```

     Extracts clean readable text from each URL. Use when you already know
     which pages to read.

   - **`deep`** — Iterative multi-query research with provenance, evidence,
     claims, gaps, and validation:

     ```bash
     node <skill-directory>/scripts/web-research.mjs deep <query...> \
       --output <run-directory> [--query <query>] [--query-file <list>] \
       [--max-iterations N] [--limit N] [--read-count N] [--mode fast|standard|deep]
     ```

     This writes `research_run.json`, `query_log.jsonl`, `source_index.json`,
     `evidence_items.jsonl`, `claim_register.jsonl`, `gap_log.jsonl`,
     `model_calls.jsonl`, `chunks.jsonl`, `embedding_log.jsonl`,
     `source_rankings.jsonl`, `scheduler_log.jsonl`, `search_cache_log.jsonl`,
     `web_manifest.*`, `sources.md`, `deep_research_report.md`, and
     `validation_report.json`.

   - **`discover`** — Inspect one URL for embedded structured data, framework
     state, and reusable JSON/API endpoints:

     ```bash
     node <skill-directory>/scripts/web-research.mjs discover <url> \
       --output <run-directory> [--render] [--no-browser]
     ```

     This writes `discovery_reports/*.json`, `strategy_decisions.jsonl`,
     `acquisition_log.jsonl`, `cache_log.jsonl`, and `metrics.json`. Use it
     before repeatedly scraping a JavaScript-heavy domain.

   - **`academic`** — Scholarly metadata search with canonical works,
     provider provenance, deduplication, and RIS export:

     ```bash
     node <skill-directory>/scripts/web-research.mjs academic <query...> \
       --output <run-directory> [--limit N] [--providers crossref,openalex,semantic-scholar,pubmed,arxiv] \
       [--contact-email <email>]
     ```

     Crossref, OpenAlex, Semantic Scholar, Europe PMC, PubMed, arXiv, CORE,
     DBLP, DataCite, OpenAIRE and DOAJ answer a query; OpenCitations and
     Unpaywall take the DOIs the others found. All work without a key —
     OpenAlex and CORE go further with a free one, and Unpaywall needs a
     contact email. This writes `works.jsonl`,
     `source_records.jsonl`, `field_provenance.jsonl`,
     `dedupe_decisions.jsonl`, `provider_requests.jsonl`,
     `provider_errors.jsonl`, `academic_report.md`, aggregate `works.ris`,
     one `ris/<work-id>.ris` per unique work, and `ris_manifest.json`.

   - **`validate`** — Validate a deep research run:

     ```bash
     node <skill-directory>/scripts/web-research.mjs validate <run-directory>
     ```

     Validation detects whether the run is deep or academic. Deep validation
     fails for missing provenance, uncited claims, missing evidence, quotes not
     present in archived source text, or source hash drift. Academic validation
     fails when canonical works lack RIS records, aggregate RIS duplicates
     deduped article keys, or per-work RIS files are missing.

3. Read the output. `research_report.md` is human-readable with extracted
   content excerpts. `research_report.json` has the full structured data
   including all extracted text (not truncated). Acquisition runs also write
   `normalized_urls.jsonl`, `strategy_decisions.jsonl`,
   `acquisition_log.jsonl`, `extraction_log.jsonl`, `cache_log.jsonl`,
   `metrics.json`, `archive/raw/`, `archive/rendered/`, `archive/extracted/`,
   and `archive/chunks/`.

   Repeating a command with the same output directory resumes compatible work.
   URL reads, academic providers, and deep-research iterations are committed
   separately. Inspect with `status <run-directory> --json`, reconcile a
   changed `read --input-file` with `refresh <run-directory>`, and explicitly
   retry permanent failures with `retry <run-directory> --item <id>` or
   `--all-failed`. Use a numbered directory only for an independent run.

4. Synthesize findings. For quick `research` runs, use the extracted text to
   answer the user's question, write a summary, or feed into downstream skills.
   For `deep` runs, synthesize only from `claim_register.jsonl` and
   `evidence_items.jsonl`; cite claim ids, evidence ids, and source ids. Always
   attribute claims to source URLs and mark uncertainty explicitly.

### Search Providers

`search` routes a query to the sources that can answer it and merges the
results; SearXNG is tried last, as the fallback for open-ended questions. The
routing is automatic — pass `--providers` only to override it.

- **Reference**: `wikipedia`, `wiktionary`, `wikidata`, `sep` (Stanford
  Encyclopedia of Philosophy), `inpho`, `iep`
- **Buddhist canon**: `suttacentral` (Pali), `cbeta` (Chinese/Taishō), `bdrc`
  (Tibetan, lookup by BDRC id only)
- **Books**: `openlibrary`, `gutendex`, `internetarchive`, `loc`, `hathitrust`
  (lookup by ISBN/OCLC/LCCN only)
- **News**: `gdelt` · **Technical**: `stackexchange`, `hackernews`
- **General**: `searxng`
- **Needs a free key**: `guardian`, `nyt` (news) · `wolfram` (computation) ·
  `marginalia`, `exa`, `tavily` (general)

An identifier in the query is decisive and is looked up rather than searched:
a sutta reference (`MN 118`, `SN 56.11`), a Taishō number (`T. 262`), or an
ISBN. `search_results.json` records the routing decisions, so a run can say why
it asked what it asked and which providers were skipped.

Everything above the keyed tier works with no credentials at all. A key is read
from `connectedServices.apiKeys` or `FORGE_API_KEY_<PROVIDER>`; without one the
provider is skipped with a logged reason and the rest still run. Two services
meter by daily spend rather than by rate — OpenAlex and NYT — and a ledger at
`~/.pi-forge/cache/web-research/budget.json` skips an exhausted provider instead
of retrying into a refusal. Run `doctor` to see what is reachable and which keys
are present.

   - **`reference-resolve`** — Ask one source which of its entries could be
     about a subject:

     ```bash
     node <skill-directory>/scripts/web-research.mjs reference-resolve <subject...> \
       --provider <id> [--limit N]
     ```

     Answers on stdout with unranked `candidates` and any `failures`; there is
     no run directory, because a lookup is not a run. Deciding which candidate
     is *about* the subject is left to the caller: a source's own relevance
     order is not that judgement, and vault-wiki's matcher is calibrated for it.

### SearXNG Parameters

Commands that query SearXNG accept these optional parameters. If omitted,
the script auto-selects based on query content:

- **`--categories`** (comma-separated): `general` (default), `news`,
  `science`, `scientific publications`, `it`, `images`, `videos`, `files`,
  `books`, `q&a`, `dictionaries`, `social media`, `packages`, `repos`,
  `weather`, `map`, `translate`, `music`, `lyrics`, `shopping`, `define`,
  `wikimedia`, `other`, `currency`, `icons`, `cargo`, `movies`, `radio`,
  `apps`, `software wikis`, `web`.

- **`--engines`** (comma-separated): `google`, `duckduckgo`, `wikipedia`,
  `google scholar`, `semantic scholar`, `arxiv`, `pubmed`, `github`,
  `stackoverflow`, `startpage`, `bing`, `brave`, `qwant`, `karmasearch`,
  and many more. See SearXNG `/config` endpoint for the full list.

- **`--language`**: Language code (e.g., `en`, `de`, `fr`, `zh`). Omit for
  auto-detection.

- **`--safesearch`**: `0` (off), `1` (moderate), `2` (strict). Use `0` for
  academic or technical research.

- **`--time-range`**: `day`, `week`, `month`, `year`. Use for time-sensitive
  queries.

- **`--pageno`**: Page number for pagination (1-indexed).

### Advanced Tool Options

The `forge_web_read`, `forge_deep_web_research`, and `forge_web_discover` tools
expose only their common parameters directly. Everything below is reachable
through their `advanced` object, which maps each key to the CLI flag shown.
Unknown keys are rejected rather than ignored, so a typo fails the run instead of
silently changing its budget.

```json
{"advanced": {"evidenceBatchChars": 12000, "playwrightConcurrency": 2}}
```

| Key | CLI flag | Purpose |
|---|---|---|
| `cacheDir` | `--cache-dir` | Override the reusable acquisition cache directory. |
| `forceRefresh` | `--force-refresh` | Ignore existing cache entries (boolean). |
| `forceStrategy` | `--force-strategy` | Force `direct_http`, `playwright_dom`, or another strategy. |
| `noBrowser` | `--no-browser` | Disable browser fallback (boolean). |
| `playwrightWsEndpoint` | `--playwright-ws` | One-run Playwright WebSocket endpoint. |
| `playwrightConcurrency` | `--playwright-concurrency` | Concurrent Playwright page/context tasks. Defaults to 1. |
| `maxConcurrency` | `--max-concurrency` | Global acquisition concurrency budget. |
| `perDomainConcurrency` | `--per-domain-concurrency` | Per-domain acquisition concurrency budget. |
| `delayMs` | `--delay-ms` | Delay between URL reads, in milliseconds. |
| `timeoutMs` | `--timeout-ms` | Request/navigation timeout, in milliseconds. |
| `maxQueries` | `--max-queries` | Whole-run cap on searched queries. |
| `maxFollowupQueries` | `--max-followup-queries` | Follow-up queries accepted per expansion step. |
| `maxModelCalls` | `--max-model-calls` | Whole-run cap on local model calls. |
| `maxRuntimeMs` | `--max-runtime-ms` | Approximate whole-run runtime budget. |
| `maxEvidenceChars` | `--max-evidence-chars` | Source-text characters sent to evidence extraction. |
| `maxClaimEvidenceItems` | `--max-claim-evidence-items` | Evidence items sent to claim registration. |
| `evidenceBatchSources` | `--evidence-batch-sources` | Sources per evidence extraction call. Defaults to 3. |
| `evidenceBatchChars` | `--evidence-batch-chars` | Source characters per evidence batch. Defaults to 24000. |
| `embeddingUrl` | `--embedding-url` | One-run embeddings endpoint override. |
| `embeddingModel` | `--embedding-model` | One-run embeddings model override. |
| `embeddingBatchSize` | `--embedding-batch-size` | Embedding batch size. Defaults to 16. |
| `noEmbeddings` | `--no-embeddings` | Disable embedding-based source ranking (boolean). |
| `searxng` | `--searxng` | Override the SearXNG base URL. |
| `categories`, `engines`, `language`, `safesearch`, `timeRange` | as above | SearXNG parameters, documented in the previous section. |

`forge_web_search` takes the SearXNG parameters directly, not through `advanced`.

### Auto-Selection Heuristics

When parameters are omitted, the script detects query type:

| Query pattern | Auto-selected params |
|---|---|
| Contains "paper", "research", "scholar", "doi" | `science` category, academic engines |
| Contains "news", "recent", "latest" | `news` category, `week` time range |
| Contains "code", "github", "npm", "api" | `it` category, dev engines |
| Contains "define", "what is", "meaning" | `general,dictionaries`, Wikipedia |

### When to Use Which Command

- **`research`** — Default. Search + read in one step. Use for most lookups.
- **`deep`** — Use for multi-query research, provenance-first synthesis,
  source triangulation, and gap/contradiction tracking.
- **`academic`** — Use for scholarly article discovery, DOI/PubMed/arXiv
  metadata, deduped canonical works, and citation-manager-ready RIS exports.
- **`discover`** — Use for unknown JavaScript-heavy sites and adapter planning.
- **`search`** — When you only need result metadata (titles, URLs, snippets)
  to decide what to read next.
- **`read`** — When you already have specific URLs to extract text from.
- **`--mode fast`** — Direct acquisition, strict timeouts, no browser fallback
  unless explicitly forced.
- **`--mode standard`** — Direct acquisition with validation-triggered browser
  fallback.
- **`--mode deep`** — Standard acquisition plus deeper provenance and source
  archiving defaults.
- **`--no-browser` / `--no-render`** — Skip Playwright for faster extraction of
  simple HTML pages.
- **`--force-strategy`** — Force one acquisition strategy for diagnosis.
- **`--cache-dir` / `--force-refresh`** — Override or bypass the reusable cache
  at `~/.pi-forge/cache/web-research`.
- **`--embedding-url` / `--embedding-model`** — Override the local embeddings
  endpoint used for source/chunk triage.
- **`--no-embeddings`** — Fall back to lexical ranking only.
- **`--embedding-batch-size`** — Batch size for embedding requests (default
  16).
- **`--evidence-batch-sources` / `--evidence-batch-chars`** — Cap the number of
  sources and selected characters sent to each evidence extraction model call.
- **`--playwright-concurrency`** — Cap browser fallback/discovery page work
  separately from direct HTTP acquisition (default 1).

### When to Use web-collection Instead

Use `web-collection` when you need:
- Full file downloads (PDFs, images, archives) with SHA-256 checksums
- Provenance manifests for downstream document-ingest processing
- Deduplication across large batches
- Rendered captures (MHTML, screenshots, PDFs)
- `robots.txt` compliance and link harvesting

Use `web-research` when you need:
- Quick answers from web search
- Readable text extraction from pages
- Structured findings with source attribution
- Inline content (not raw file downloads)

## Verification

Evidence extraction and claim registration run on the non-thinking `chat`
service, one call per batch. A `deep` run then reviews all of it on `think`
after every bulk call is finished, in batched packets:

- Each evidence item is reviewed against an excerpt of the **archived source
  text** around its quote, not against a paraphrase. A reviewer with nothing to
  check against approves everything.
- Each claim is reviewed against the full text and quotes of the evidence it
  cites.
- Nothing is deleted. A flagged item keeps its record, carries the reviewer's
  objection in `evidence_items.jsonl` or `claim_register.jsonl`, and is marked
  where it appears in `deep_research_report.md`.
- Counts land in `research_run.json` under `verification`, and every reviewed id
  is journaled to `verify_evidence.jsonl` / `verify_claims.jsonl`. `validate`
  fails when a run claims verification it cannot show.

`--no-verify` skips the review, and both the report and `validate` then say
plainly that nothing was reviewed. An unreachable thinking service does the same
thing: it is reported, never treated as approval. Tell the user when a run was
not verified — an unreviewed report reads exactly like a reviewed one.

## Safety and Failure Handling

- Only `http` and `https` URLs are fetched. Loopback and cloud-metadata hosts
  are refused.
- Extraction failures are recorded with warnings; the run continues.
- Direct HTTP extraction is attempted before browser extraction. Browser
  fallback is triggered by validation failure or an explicit strategy.
- Playwright uses a run-scoped browser connection and avoids `networkidle` as
  the default wait condition.
- Extracted text is truncated to 3000 characters in the Markdown report; the
  full text is in `research_report.json`.
- Do not install browsers or system packages. Report missing capabilities
  through `doctor`.

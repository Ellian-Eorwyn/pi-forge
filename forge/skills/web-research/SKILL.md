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

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path. Check
   capabilities before relying on search or extraction:

   ```bash
   node <skill-directory>/scripts/web-research.mjs doctor [--json]
   ```

   `doctor` reports SearXNG connectivity, Playwright availability, the
   configured browser endpoint, and remediation steps.

2. Choose a command:

   - **`research`** — Full workflow: search → read top results → report.
     The default choice for most information lookups:

     ```bash
     node <skill-directory>/scripts/web-research.mjs research <query...> \
       --output <new-directory> [--limit N] [--read-count N] [--mode fast|standard|deep]
     ```

     This searches SearXNG, normalizes and deduplicates URLs, fetches the top N
     results (default 5) through the acquisition ladder, extracts clean text
     from each, and writes `research_report.md`, `research_report.json`, stage
     logs, and `metrics.json`.

   - **`search`** — Search only, no page fetching:

     ```bash
     node <skill-directory>/scripts/web-research.mjs search <query...> \
       --output <new-directory> [--limit N]
     ```

     Returns ranked results with title, URL, snippet, engine, and score.

   - **`read`** — Fetch and extract text from specific URLs:

     ```bash
     node <skill-directory>/scripts/web-research.mjs read <url...> \
       --output <new-directory> [--input-file <list>] [--mode fast|standard|deep]
     ```

     Extracts clean readable text from each URL. Use when you already know
     which pages to read.

   - **`deep`** — Iterative multi-query research with provenance, evidence,
     claims, gaps, and validation:

     ```bash
     node <skill-directory>/scripts/web-research.mjs deep <query...> \
       --output <new-directory> [--query <query>] [--query-file <list>] \
       [--max-iterations N] [--limit N] [--read-count N] [--mode fast|standard|deep]
     ```

     This writes `research_run.json`, `query_log.jsonl`, `source_index.json`,
     `evidence_items.jsonl`, `claim_register.jsonl`, `gap_log.jsonl`,
     `model_calls.jsonl`, `web_manifest.*`, `sources.md`,
     `deep_research_report.md`, and `validation_report.json`.

   - **`discover`** — Inspect one URL for embedded structured data, framework
     state, and reusable JSON/API endpoints:

     ```bash
     node <skill-directory>/scripts/web-research.mjs discover <url> \
       --output <new-directory> [--render] [--no-browser]
     ```

     This writes `discovery_reports/*.json`, `strategy_decisions.jsonl`,
     `acquisition_log.jsonl`, `cache_log.jsonl`, and `metrics.json`. Use it
     before repeatedly scraping a JavaScript-heavy domain.

   - **`academic`** — Scholarly metadata search with canonical works,
     provider provenance, deduplication, and RIS export:

     ```bash
     node <skill-directory>/scripts/web-research.mjs academic <query...> \
       --output <new-directory> [--limit N] [--providers crossref,semantic-scholar,pubmed,arxiv] \
       [--contact-email <email>]
     ```

     This queries no-key academic providers, writes `works.jsonl`,
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

4. Synthesize findings. For quick `research` runs, use the extracted text to
   answer the user's question, write a summary, or feed into downstream skills.
   For `deep` runs, synthesize only from `claim_register.jsonl` and
   `evidence_items.jsonl`; cite claim ids, evidence ids, and source ids. Always
   attribute claims to source URLs and mark uncertainty explicitly.

### SearXNG Parameters

All commands that query SearXNG accept these optional parameters. If omitted,
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

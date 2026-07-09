---
name: web-collection
description: Collect, archive, and organize web source material from one or more URLs into preserved files with a provenance manifest. Use to download pages, PDFs, or linked resources, harvest links matching a pattern such as every PDF on a page, run searches through a local SearXNG instance, and capture full rendered pages with Playwright as HTML, MHTML, full-page PNG, and PDF for later document-ingest processing.
---

# Web Collection

Archive web sources into preserved files with provenance, deduplication, and an
explicit record of what was downloaded, skipped, blocked, or failed. Never alter
the original sources; produce new files only.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path. Check
   capabilities, especially before rendered capture or SearXNG search:

   ```bash
   node <skill-directory>/scripts/web-collection.mjs doctor --json
   ```

   Rendered capture needs Playwright and the Chromium browser. Search uses
   the default SearXNG instance (`http://llms/searxng`) unless overridden
   by `FORGE_SEARXNG_URL` or `--searxng <url>`.
2. Choose a new output directory under
   `forge-output/web-collection/<source-stem>/`. If it exists, use the next
   numbered suffix. The script refuses to write into an existing directory.
3. Pick a command:
   - **collect** named URLs:

     ```bash
     node <skill-directory>/scripts/web-collection.mjs collect <url...> \
       --output <new-directory> [--render] [--clean] [--input-file <list>]
     ```

   - **harvest** links from a page (for example every PDF):

     ```bash
     node <skill-directory>/scripts/web-collection.mjs harvest <page-url> \
       --output <new-directory> --ext pdf [--match <regex>] [--same-host] [--limit N] [--clean]
     ```

   - **spider** a page with LLM-guided domain extraction:

     ```bash
     node <skill-directory>/scripts/web-collection.mjs spider <page-url> \
       --output <new-directory> [--limit N] [--render] [--clean] [--ignore-robots]
     ```

     This command interactively prompts you to choose which links to follow (e.g. all links, pricing/services, about/contact, or custom instruction). It extracts same-host links and uses the local LLM to intelligently filter down to the most relevant URLs before collecting them.

   - **search** through a local SearXNG instance (default: `http://llms/searxng`):

     ```bash
     node <skill-directory>/scripts/web-collection.mjs search <query...> \
       --output <new-directory> [--searxng <url>] [--limit N] [--collect]
       [--categories <cats>] [--engines <engines>] [--language <lang>]
       [--safesearch <0|1|2>] [--time-range <day|week|month|year>] [--pageno N]
     ```

   Add `--render` to also save full Playwright captures. Plain collection saves
   raw HTTP responses only.
   Add `--clean` to automatically convert downloaded HTML files into clean Markdown by stripping out boilerplate content, navigation menus, and footers using the local LLM.

   ### SearXNG Search Parameters

   Choose parameters based on the query topic and desired result quality:

   - **`--categories`** (comma-separated): Filter by result category. Use
     `general` (default) for broad searches, `news` for current events,
     `science` or `scientific publications` for academic content, `it` for
     developer topics, `images`/`videos` for media, `files` for downloads,
     `books` for literature, `q&a` for Stack Overflow-style answers.
     Combine with commas: `--categories science,it`.

   - **`--engines`** (comma-separated): Restrict to specific search engines.
     Use `google` for broad coverage, `duckduckgo` for privacy-focused results,
     `wikipedia` for encyclopedic context, `google scholar` or `semantic scholar`
     for academic papers, `pubmed` or `arxiv` for scientific preprints,
     `github` for code repositories, `stackoverflow` for programming Q&A,
     `startpage` for Google results via privacy proxy.
     Example: `--engines google scholar,semantic scholar`.

   - **`--language`**: Set result language (e.g., `en`, `de`, `fr`, `zh`).
     Use when the query is in a specific language or you want results in a
     particular language. Omit for auto-detection.

   - **`--safesearch`**: Content filtering level: `0` (off), `1` (moderate),
     `2` (strict). Use `0` for academic or technical research where filtering
     may block relevant results. Use `2` for general-purpose searches.

   - **`--time-range`**: Restrict to recent results. Use `day` for breaking
     news, `week` for weekly developments, `month` for recent trends,
     `year` for annual overviews. Omit for all-time results.

   - **`--pageno`**: Paginate through results (1-indexed). Use with `--limit`
     to collect more than the first page of results.

   ### When to Use Which Settings

   - **Academic/research queries**: `--categories science,scientific publications`
     `--engines google scholar,semantic scholar,arxiv,pubmed` `--safesearch 0`
   - **News/current events**: `--categories news` `--time-range week`
   - **Developer/code queries**: `--categories it` `--engines github,stackoverflow`
   - **General web research**: default settings (uses `general` category,
     all enabled engines)
   - **Multilingual research**: set `--language` to match the target language
   - **Broadest coverage**: `--safesearch 0` to avoid filtering edge results
4. Read [references/collection-contract.md](references/collection-contract.md).
   Review the run against `web_manifest.csv`, `web_manifest.json`, and
   `collection_report.md`. Confirm every intended source downloaded and inspect
   each `needs_review` row and capture warning.
5. Validate the run and resolve every error before completion:

   ```bash
   node <skill-directory>/scripts/web-collection.mjs validate <run-directory>
   ```

6. To extract text and metadata from the saved files, hand the `downloads/`
   directory to the document-ingest skill. Do not summarize or analyze inside
   this skill.

## Mechanical Tools

For lower-level execution, the manifest also exposes `fetch_url`,
`archive_page`, `html_to_markdown`, and `extract_metadata`. These tools accept
structured JSON input and return structured JSON results; use them when the
task needs one repeatable operation rather than the full collection workflow.

## Configuration

- **Default SearXNG URL**: `http://llms/searxng`. Override with
  `FORGE_SEARXNG_URL` environment variable or `--searxng <url>` flag.
- Run `doctor` to verify connectivity and see available capabilities.
- The SearXNG instance exposes its full configuration at `<base>/config`;
  use it to inspect enabled engines, categories, and locales.

## Safety and Failure Handling

- Only `http` and `https` URLs are collected. Loopback and cloud-metadata hosts
  are refused.
- Downloads are sequential with a polite delay and a clear User-Agent. `harvest`
  honors `robots.txt` for User-Agent `*` unless `--ignore-robots` is given.
- Never overwrite an existing output directory or download. Resources exceeding
  `--max-bytes` are reported as failures rather than partially saved.
- Deduplicate by normalized URL, SHA-256, Content-Disposition filename, and page
  title. Duplicates are recorded as `skipped` with the original `resource_id`.
- Report paywalls and blocks (HTTP 401/402/403), redirects and redirect loops,
  timeouts, oversize resources, and non-matching links explicitly. Continue a
  batch after individual failures.
- Record source URL, final URL, access date, HTTP status, content type, title,
  filename, byte size, and SHA-256 for every resource. Keep this provenance
  separate from any later summaries or analysis.
- Do not install browsers or system packages from this skill. Report missing
  capabilities through `doctor` and the run warnings.

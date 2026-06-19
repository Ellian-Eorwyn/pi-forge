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

   Rendered capture needs Playwright and the Chromium browser. Search needs
   `FORGE_SEARXNG_URL` or `--searxng <url>`.
2. Choose a new output directory under
   `forge-output/web-collection/<source-stem>/`. If it exists, use the next
   numbered suffix. The script refuses to write into an existing directory.
3. Pick a command:
   - **collect** named URLs:

     ```bash
     node <skill-directory>/scripts/web-collection.mjs collect <url...> \
       --output <new-directory> [--render] [--input-file <list>]
     ```

   - **harvest** links from a page (for example every PDF):

     ```bash
     node <skill-directory>/scripts/web-collection.mjs harvest <page-url> \
       --output <new-directory> --ext pdf [--match <regex>] [--same-host] [--limit N]
     ```

   - **search** through a local SearXNG instance:

     ```bash
     node <skill-directory>/scripts/web-collection.mjs search <query...> \
       --output <new-directory> [--searxng <url>] [--limit N] [--collect]
     ```

   Add `--render` to also save full Playwright captures. Plain collection saves
   raw HTTP responses only.
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

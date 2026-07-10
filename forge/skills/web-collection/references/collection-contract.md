# Web Collection Output Contract

## Run Layout

Use one new directory per run. The script writes:

```text
downloads/                          # raw HTTP response bodies
captures/<stem>-<sha12>/            # only when --render is used
  rendered.html                    # post-JavaScript DOM
  snapshot.mhtml                   # single-file Chromium snapshot
  screenshot.png                   # full-page screenshot
  page.pdf                         # Chromium print-to-PDF
  capture.json                     # capture metadata
search_results.json                # only for the search command
web_manifest.csv
web_manifest.json
collection_report.md
failed_downloads.csv
```

Saved files are written with exclusive create. Nothing is overwritten. The
original web sources are never modified; only new local files are produced.

## Provenance and Manifest

`web_manifest.csv` uses exactly these columns:

```text
resource_id,source_url,final_url,access_date,status,http_status,content_type,title,filename,output_path,sha256,byte_size,capture_method,rendered,duplicate_of,error
```

- `resource_id` is `sha256:<full body hash>`.
- `source_url` is the requested URL; `final_url` is the URL after redirects.
- `access_date` is an ISO-8601 UTC timestamp.
- `status` is one of `success`, `needs_review`, `failed`, `skipped`.
- `output_path` is relative to the run root for saved resources.
- `duplicate_of` holds the original `resource_id` when a resource was skipped as
  a duplicate.
- `error` carries the failure reason or capture warnings.

`web_manifest.json` keeps `schemaVersion` equal to `1` and records, per
resource, the redirect chain, content type, Content-Disposition filename, saved
path, SHA-256, byte size, rendered-capture path and artifacts, warnings, and the
originating command. It also records the dedup keys used.

`failed_downloads.csv` lists failed and blocked rows with
`source_url,status,http_status,reason,access_date`.

## Deduplication

A resource is a duplicate when any of these match an already-saved resource in
the same run, in priority order: normalized URL (lowercased host, default port
and fragment removed), body SHA-256, Content-Disposition filename, or page
title. Duplicates are recorded as `skipped` with `duplicate_of` set and are not
saved again.

## Rendered Capture

`--render` launches headless Chromium through Playwright, navigates to the final
URL, and saves all four artifacts plus `capture.json`. `capture.json` records
`requestedUrl`, `finalUrl`, `title`, `httpStatus`, `userAgent`, `capturedAt`,
the produced `artifacts`, the count of console errors, and any `warnings`. A
resource whose capture produced warnings is marked `needs_review`. MHTML and PDF
are best-effort; if either fails the other artifacts are still saved and the
failure is recorded as a warning.

## Search

The search command requires a SearXNG instance from `connectedServices.searxng`
in `~/.pi-forge/agent/settings.json`, `--searxng <url>`, or the
`FORGE_SEARXNG_URL` environment variable. It requests
`<base>/search?q=<query>&format=json` and writes the ranked results to
`search_results.json` and the report's `## Search` section. Search does not
download result pages unless `--collect` is given, which feeds the result URLs
through the collection path.

## Politeness and Safety

- Only `http` and `https` schemes are collected; loopback and cloud-metadata
  hosts are refused.
- Downloads run sequentially with a configurable delay (`--delay-ms`, default
  500) and a clear default User-Agent.
- `harvest` fetches and applies `robots.txt` Disallow rules for User-Agent `*`
  unless `--ignore-robots` is set.
- Resources are capped by `--max-bytes` (default 100 MiB) and `--timeout-ms`
  (default 30000). Oversize or timed-out resources fail without partial saves.
- Redirects are followed up to ten hops; loops are detected and reported.

## Failure Taxonomy

Record these distinctly in the `error` column and `failed_downloads.csv`:

- **blocked or paywalled** — HTTP 401, 402, or 403.
- **HTTP error** — other non-2xx responses.
- **redirect loop** / **too many redirects**.
- **timed out** — exceeded `--timeout-ms`.
- **oversize** — exceeded `--max-bytes`.
- **non-matching** — links excluded by `--ext`, `--match`, `--same-host`, or
  robots rules during harvest (these are simply not collected).
- **duplicate** — skipped with `duplicate_of`.

## Hand-off to document-ingest

This skill preserves files and provenance only. To turn the saved files into
normalized Markdown and metadata, run the document-ingest skill on the
`downloads/` directory. Keep extracted content and any later analysis separate
from this collection manifest.

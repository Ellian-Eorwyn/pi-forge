# Page Extraction Guide

Heuristics and edge cases for web page text extraction.

## Extraction Methods

### Playwright (preferred)

Launches headless Chromium, waits for `networkidle`, then extracts text from
the rendered DOM. Handles JavaScript-rendered pages, SPAs, and lazy-loaded
content.

**Selector priority**: `article` → `main` → `[role="main"]` → `.content` →
`.post` → `.entry` → `#content` → `#main` → full body with noise stripping.

**Noise stripping**: Removes `<script>`, `<style>`, `<nav>`, `<footer>`,
`<header>`, `<noscript>`, `<svg>`, and any element with class names matching
`nav`, `header`, `footer`, `sidebar`, `menu`, `widget`, `banner`, `cookie`,
`popup`, `modal`.

### HTTP fallback

Fetches raw HTML, strips `<script>` and `<style>` tags, removes all HTML tags,
decodes common entities, and collapses whitespace. No JavaScript execution.

Use `--no-render` to force this method for speed on simple pages.

## Edge Cases

### Paywalls

- Full paywalls (NYTimes, WSJ): extraction returns minimal text or paywall
  message. Mark as `needs_review` in findings.
- Soft paywalls (word/character limits): extraction returns the visible portion.
  Note truncation in warnings.

### JavaScript-heavy pages

- SPAs (React, Vue, Angular): Require Playwright. HTTP extraction returns
  empty or minimal content.
- Infinite scroll: Only extracts initially loaded content. Use `--render` for
  best results.

### Dynamic content

- Lazy-loaded images/text: Playwright waits for `networkidle` which covers
  most cases. Some aggressive lazy-loading may still miss content.
- Content behind login: Not accessible without credentials. Mark as failed.

### Special page types

- **Documentation sites**: Usually extract well. Look for `<main>` or `#content`.
- **Blog posts**: Usually have `<article>` with clean content.
- **News articles**: Often have paywalls. Use `--safesearch 0` for broader
  results.
- **GitHub pages**: Extract well. README, issues, and PRs are accessible.
- **Stack Overflow**: Extracts question + answers. May include sidebar noise.
- **Wikipedia**: Extracts article content cleanly.
- **PDFs served as HTML**: HTTP extraction may work. Playwright may render
  differently.

## Output Structure

### research_report.json

```json
{
  "query": "search query",
  "searchBase": "http://llms/searxng",
  "params": { ... },
  "retrievedAt": "2024-01-01T00:00:00.000Z",
  "results": [
    {
      "rank": 1,
      "title": "Page Title",
      "url": "https://example.com",
      "content": "Snippet from search engine...",
      "engine": "google",
      "score": 4.5
    }
  ],
  "readings": [
    {
      "url": "https://example.com",
      "title": "Page Title",
      "text": "Full extracted text...",
      "charCount": 5000,
      "extractionMethod": "playwright",
      "warnings": [],
      "extractedAt": "2024-01-01T00:00:00.000Z"
    }
  ]
}
```

### research_report.md

Human-readable report with:
- Query and search parameters
- Ranked results with metadata
- Extracted content excerpts (truncated to 3000 chars)
- Sources table with extraction methods

Full text is always in the JSON file.

## Best Practices

1. **Use `--render`** for most pages — Playwright handles JS rendering.
2. **Check `charCount`** — Low character counts may indicate extraction
   issues (paywall, empty page, JS error).
3. **Review warnings** — Each reading includes extraction warnings.
4. **Compare methods** — If Playwright fails, HTTP fallback may still work.
5. **Verify sources** — Cross-reference extracted text with search snippets
   for consistency.

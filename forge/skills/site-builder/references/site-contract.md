# Site Builder Output Contract

Turn a folder of source material into a self-contained static website without
blending generated commentary into quoted sources, without fabricating facts or
links, and without introducing external runtime dependencies.

## Run Layout

```text
<run-dir>/
  site.json                  # site config: title, theme, tokens, pages, nav (model-edited)
  source_manifest.json       # registered inputs with SHA-256 (script-managed)
  links.json                 # links harvested from sources (script-managed)
  content/                   # one Markdown file per page (model-authored)
    index.md
    <slug>.md
    resources.md
  assets/                    # images/media staged from sources (script-managed)
  theme/                     # optional: run-local CSS overrides (via `eject`)
  templates/                 # optional: run-local HTML overrides (via `eject`)
  site/                      # BUILT, uploadable static site (via `build`)
    index.html
    <slug>.html
    tags.html
    tag-<tag>.html
    404.html
    styles.css
    app.js
    search.js
    search-index.json
    robots.txt
    assets/
  build_log.md
  warnings.md
```

`source_manifest.json` and `links.json` are managed by the script — do not
hand-edit them. `site.json` and everything under `content/` are yours to author.
Each scaffolded page carries the placeholder marker
`<!-- TODO: author this section -->`, which must be removed once written; `build`
refuses any page that still contains it.

## site.json

```jsonc
{
  "schemaVersion": 1,
  "title": "Site Title",
  "description": "One-sentence site description for the home page <meta>.",
  "lang": "en",
  "theme": "editorial",              // one of the 8 base themes, or "custom" (see Customization)
  "tokens": {                         // optional overrides appended to :root, tuned to the subject
    "--accent": "#3b5bdb",
    "--font-head": "Georgia, serif"
  },
  "hero": {                           // optional home-page hero
    "style": "gradient",             // gradient | centered | split | image
    "image": "assets/cover.jpg",     // required for split/image (else they fall back)
    "imageAlt": "Descriptive alt",   // for the split-hero figure
    "cta": { "label": "Get started", "href": "overview.html" }
  },
  "footer": "Built from source materials. © 2026.",
  "nav": [                            // ordered top navigation
    { "label": "Home", "page": "index" },
    { "label": "Overview", "page": "overview" },
    { "label": "Resources", "page": "resources" }
  ],
  "pages": [
    {
      "slug": "index",               // file name without extension; "index" is the home page
      "title": "Home",
      "description": "Per-page <meta> description.",
      "tags": ["intro"],            // optional; drive tag pages
      "file": "content/index.md"
    }
  ]
}
```

Every `page.slug` must be unique and map to an existing `content/<slug>.md`.
Every `nav[].page` and any in-content link to `another-slug.html` must resolve to
a page. `index` is required and is the site home.

## Theme System

Themes are CSS-variable templates shipped under `assets/themes/<name>/tokens.css`
and combined at build time into `site/styles.css` as
`tokens.css` + `base.css` + `print.css`. Pick the base theme whose feel matches
the content, then tune it to the subject through `site.json` `tokens` overrides:

- `editorial` — serif headings, generous measure; long-form reading.
- `technical` — system fonts, dense; documentation and reference.
- `archival` — muted, restrained; collections, catalogs, libraries.
- `gallery` — image-forward, large media; visual or photographic material.
- `magazine` — bold display headings, strong accent; features, storytelling.
- `academic` — refined scholarly serif, restrained navy; papers, reports.
- `brand` — vibrant gradient accents, rounded cards; product/landing energy.
- `terminal` — monospace, sharp, high-contrast; zine/technical/hacker feel.

Token variables (define light + dark in every theme): `--bg`, `--surface`,
`--surface-2`, `--fg`, `--muted`, `--accent`, `--accent-2`, `--accent-contrast`,
`--accent-gradient`, `--border`, `--link`, `--ring`, `--shadow-sm`/`-md`/`-lg`,
`--font-body`, `--font-head`, `--font-display`, `--heading-weight`,
`--heading-tracking`, `--measure`, `--radius`, `--hero-overlay`. Each theme
defines light values and a dark variant; the manual toggle sets `data-theme` on
`<html>` and overrides `prefers-color-scheme`. Fonts use system/serif/mono
stacks only — no external/CDN fonts.

## Customization and `eject`

`build` is override-aware: for each shipped asset it prefers a run-local copy if
present, so a site can be customized without touching the skill:

- `<run-dir>/theme/tokens.css` → else shipped `themes/<theme>/tokens.css`
- `<run-dir>/theme/base.css` / `print.css` → else the shipped versions
- `<run-dir>/templates/<page|index|404>.html` → else the shipped templates

Three levels of customization, lightest first:
1. Tune `site.json` `tokens` (appended to `:root`).
2. `eject <run-dir> [--theme <name>] [--templates] [--all]` copies the chosen
   theme's `tokens.css` (and, with `--templates`/`--all`, the templates,
   `base.css`, `print.css`) into the run for free editing. It refuses to
   overwrite files already ejected.
3. Set `theme: "custom"` and author `theme/tokens.css` (and optionally
   `theme/base.css` and `templates/*.html`) from scratch for a unique look.
   `build` errors if `theme` is `custom` but `theme/tokens.css` is absent.

Templates use `{{...}}` placeholders: `lang`, `title`, `siteTitle`,
`description`, `nav`, `breadcrumbs`, `toc`, `content`, `footer`, and (index only)
`hero`. Keep the semantic landmarks, skip link, and asset references intact when
editing them.

## Authored Markdown and Components

The renderer supports a practical Markdown subset: ATX headings, paragraphs,
bold/italic, inline code, fenced code blocks, links and autolinks, images,
ordered/unordered (and nested) lists, blockquotes, horizontal rules, and pipe
tables. The on-page table of contents is built automatically from `##`/`###`
headings; do not hand-author it. Reference staged images as `assets/<file>` and
always provide alt text. Link to other pages as `<slug>.html`.

Rich components (all degrade to plain, valid HTML):

- **Callouts**: a blockquote whose first line is `[!NOTE]`, `[!TIP]`,
  `[!WARNING]`, `[!IMPORTANT]`, or `[!CAUTION]` (optional label text after it)
  renders as a styled, labelled admonition.
- **Card grids**: a `:::cards` … `:::` block where each list item
  `- [Title](href): summary` becomes a linked card; `:::grid` wraps arbitrary
  Markdown in a responsive grid; `:::name` wraps content in `<div class="name">`.
- **Figures**: an image alone on its own line renders as
  `<figure>` with a `<figcaption>` from its alt text and click-to-zoom.
- **Hero**: configured in `site.json` (see above), not in Markdown.

## Separation and Attribution Rule

Keep generated synthesis, interpretation, and recommendations clearly marked and
separate from quoted or extracted source content. Attribute claims back to the
source files they came from (by file name, and where available the upstream
locator carried in a `document-ingest` source map). Do not present inference as
something the sources stated, and never fabricate facts, quotations, links, or
citations.

## Link Harvesting and Resources

`init` scans text-bearing sources (Markdown, HTML, plain text) for `http(s)`
links and records each in `links.json` with the source file it came from,
deduplicated by normalized URL. Curate these into a resources/references page so
the site links out to the references present in the sources. Links are recorded,
not fetched; do not invent destinations.

## Search Index and Graceful Degradation

`build` writes `search-index.json` as an array of
`{ "slug", "title", "url", "text" }` entries (page title plus extracted plain
text). `search.js` performs a small client-side token match with no dependencies.
With JavaScript disabled the search box is inert but every page, the navigation,
breadcrumbs, the table of contents, and the tag pages remain fully usable.

## Accessibility and Responsive Requirements

- Semantic landmarks (`header`, `nav`, `main`, `aside`, `footer`), a skip link,
  one `<h1>` per page, and no skipped heading levels.
- Descriptive `alt` on every content image; visible keyboard focus; navigable by
  keyboard alone, including the collapsing mobile nav.
- Mobile-first, fluid layout with adequate touch targets; respects
  `prefers-reduced-motion`; color choices should meet WCAG AA contrast.
- `<html lang>` set from `site.json` `lang`; a per-page `<title>` and
  `<meta name="description">`.

## Deployment Notes

The `site/` directory is the deliverable: plain files, relative paths only, no
external or CDN requests, no trackers, no build tooling. It can be uploaded as-is
to any host or served from any subdirectory. A `404.html` and a permissive
`robots.txt` are generated. Open Graph/Twitter cards and a `sitemap.xml` are out
of scope by default; add them only if the user asks.

## Validation and Statuses

`validate` reports the run valid only when: the `site/` build exists; every page
in `site.json` was built with no unresolved placeholder markers; every internal
page link and referenced asset resolves; all source hashes from
`source_manifest.json` still match; and the accessibility lint passes
(`<html lang>`, a non-empty `<title>`, a single `<h1>`, no skipped heading
levels, and `alt` present on content images). Broken external links and the
absence of optional extras are warnings for human review, not errors.
</content>

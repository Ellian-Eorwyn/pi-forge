---
name: site-builder
description: Build a beautiful, self-contained static informational website from a folder of source material. Use to turn raw documents, Markdown, and images — or upstream forge run directories — into plain HTML, CSS, and minimal vanilla JavaScript that uploads to any server and works immediately, with responsive accessible layouts, a content-tuned theme, client-side search, navigation, breadcrumbs, an on-page table of contents, tag pages, dark mode, and a curated resources section that links out to references found in the sources. Generated synthesis stays separate from quoted source material and traceable to its origin.
---

# Site Builder

Turn a loaded folder of content into a publishable static website. A
deterministic script handles discovery, source registration, link harvesting,
asset staging, and HTML/CSS/JS assembly; you supply the judgment — the site's
purpose, its structure, the page content drawn from the sources, and the theme
tuned to the subject. Preserve every source, keep generated synthesis separate
from quoted material, and never invent facts the sources do not support.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path. Check local
   capabilities when unsure:

   ```bash
   node <skill-directory>/scripts/site-builder.mjs doctor
   ```

2. Create a new output directory under `forge-output/site-builder/<title-stem>/`.
   If it exists, use a numbered suffix. Register the inputs and scaffold the
   site:

   ```bash
   node <skill-directory>/scripts/site-builder.mjs init <inputs...> \
     --output <new-directory> --title "<site title>" [--theme editorial]
   ```

   `<inputs>` are files and/or folders, including upstream forge run directories
   (for example a `document-ingest` run — its normalized Markdown and provenance
   are preferred over re-reading raw originals). Folders are discovered
   recursively, skipping hidden paths, symlinks, and run-internal machinery.
   Themes: `editorial` (default), `technical`, `archival`, `gallery`,
   `magazine`, `academic`, `brand`, `terminal`. `init` writes
   `source_manifest.json`, harvests links into `links.json`, stages detected
   images into `assets/`, and scaffolds `site.json` plus one placeholder Markdown
   stub per page under `content/`.

3. Read [references/site-contract.md](references/site-contract.md). Read the
   registered sources (and `links.json`), then **converse with the user to fix
   the site's goal, audience, and scope** before authoring.

4. Design and author:
   - Edit `site.json` — set the page list and the `nav` tree, pick a base
     `theme`, choose a home-page `hero` (`gradient`/`centered`/`split`/`image`,
     with an optional `cta`), and tune `tokens` (colors, fonts, shadows, radius)
     to the content's subject. Give each page a `slug`, `title`, `description`,
     and any `tags`.
   - Author each `content/<slug>.md` from the sources, removing its
     `<!-- TODO: author this section -->` marker. Use the rich components where
     they help: callouts (`> [!NOTE|TIP|WARNING|IMPORTANT|CAUTION]`), card grids
     (`:::cards` … `:::`), and captioned figures (an image alone on its own
     line). Keep generated synthesis clearly separate from quoted or extracted
     source material, and attribute claims back to the source files. Reference
     staged images as `assets/<file>`.
   - Curate `links.json` into a resources/references page so the site links out
     to the references found in the sources.
   - **To make something unique**, you are not limited to the shipped themes.
     Tune `site.json` `tokens`; or run `eject` to copy a theme's CSS (and
     optionally the templates) into the run directory and edit them freely; or
     set `theme: "custom"` and author `theme/tokens.css` (and optionally
     `theme/base.css` / `templates/*.html`) from scratch. `build` always prefers
     these run-local files over the shipped assets, so your edits affect only
     this site:

     ```bash
     node <skill-directory>/scripts/site-builder.mjs eject <run-directory> \
       [--theme <name>] [--templates] [--all]
     ```

5. Build the uploadable site, then open it to review:

   ```bash
   node <skill-directory>/scripts/site-builder.mjs build <run-directory>
   ```

   This writes plain files under `<run-directory>/site/` with relative paths
   throughout. Open `site/index.html` in a browser and check desktop and mobile
   layouts, search, dark mode, breadcrumbs, the table of contents, and that
   resource links resolve.

6. Validate and resolve every error before completion:

   ```bash
   node <skill-directory>/scripts/site-builder.mjs validate <run-directory>
   ```

   `validate` fails on unbuilt pages, unresolved placeholders, broken internal
   links or missing assets, changed source hashes, and basic accessibility lint
   failures. Broken external links are warnings for human review.

## Safety and Output Rules

- Preserve sources. Inputs are referenced by path and SHA-256, never modified;
  hashes recorded at `init` must still match at `validate`.
- Keep generated synthesis, interpretation, and recommendations separate from
  quoted or extracted source content, and attribute claims to their source
  files. Do not present inference as something the sources stated. Never invent
  facts, links, or citations.
- The generated site is fully self-contained: relative paths only, no external
  or CDN requests, no analytics or trackers, no build step. It must work
  uploaded to any subdirectory and degrade gracefully with JavaScript disabled.
- Meet the accessibility and responsive requirements in the contract (semantic
  landmarks, alt text, keyboard navigation, sufficient contrast, mobile-first
  layout, `prefers-color-scheme` plus a manual toggle).
- Never overwrite an existing output directory; `init` refuses a populated one.
  Report produced artifacts and any warnings on completion.
</content>

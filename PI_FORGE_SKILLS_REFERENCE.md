# Pi-Forge Skills Reference

A comprehensive guide to the 13 available skills in the pi-forge agent harness, including implementation details and usage patterns for designing external interfaces.

---

## Overview

**Total Skills:** 13  
**Launch Context:** 2,618 tokens (managed instructions + skills menu)  
**Maximum Full Load:** 18,155 tokens (if all skill bodies loaded at once)

Each skill is implemented as:
- A skill definition file (`SKILL.md`) with YAML frontmatter (name, description)
- Executable scripts (Node.js `.mjs` or Python `.py`) that run the skill workflows
- Reference documentation in `references/` subdirectories
- Output structured in `forge-output/<skill-name>/` directories

---

## Skills

### 1. Coding
**Purpose:** Assist with code repositories, scripts, automation, debugging, and small software tools.

**Implementation:**
- Script: `scripts/coding.mjs` (Node.js)
- Commands: `doctor`, `inspect`, `validate`
- Output directory: `forge-output/coding/<repo-stem>/`

**Workflow:**
1. `doctor` - Check toolchain availability
2. `inspect <repo> --output <dir>` - Profile repository (read-only), generates `repo_profile.md` and `repo_profile.json`
3. Make targeted edits, run tests/linters, record in `run_log.md`
4. Write `change_summary.md` describing changes and verification
5. `validate <output-dir>` - Ensure all checks pass

**Key Features:**
- Read-only inspection before editing
- Preserves existing work and git state
- Records every command and test result
- Never runs destructive git operations without explicit request

---

### 2. Document Ingest
**Purpose:** Ingest and normalize PDF, DOCX, TXT, Markdown, HTML, and RTF documents into complete Markdown with evidence-backed metadata.

**Implementation:**
- Script: `scripts/document-ingest.mjs` (Node.js)
- Commands: `doctor`, `prepare`, `validate`
- Output directory: `forge-output/document-ingest/<input-stem>/`

**Workflow:**
1. `doctor` - Check local tools (pdftotext, ocrmypdf, GLM-OCR availability)
2. `prepare <input> --output <dir>` - Convert to Markdown with OCR
   - Optional OCR backends: `auto` (default), `glmocr`, `local`, `never`
   - Recursive folder processing
   - Endpoint: `http://llms:5002/glmocr/parse` (via `FORGE_GLMOCR_URL`)
3. Review prepared documents sequentially
4. Handle vision transcription if required
5. Review and improve structure
6. Enrich `metadata.json` with evidence-backed values
7. `validate <run-directory>` - Resolve all errors

**Key Features:**
- Preserves originals, never modifies sources
- Automatic OCR with fallback for multi-column pages
- Outputs: `document.md`, `metadata.json`, `source_map.json`, `extraction_report.md`

---

### 3. File Conversion
**Purpose:** Convert files between formats deterministically while preserving originals.

**Implementation:**
- Script: `scripts/file-conversion.py` (Python)
- Commands: `doctor`, `convert`, `install-epubcheck`, `validate`
- Output directory: `forge-output/file-conversion/<input-stem>/`

**Workflow:**
1. `doctor` - Check tool availability
2. `convert <input...> --to <target> --output <dir>` - Convert files
   - Supported targets: `md`, `docx`, `html`, `txt`, `epub`, `csv`, `xlsx`
3. Review `conversion_manifest.csv`, `warnings.md`, `conversion_log.md`
4. `validate <run-directory>` - Check all outputs

**Key Features:**
- Preserves all originals
- EPUB output is reflowable EPUB 3
- Continues batches past individual failures
- Discloses lossy conversions

---

### 4. Literature Extraction
**Purpose:** Extract structured, source-backed evidence from academic articles, reports, and research documents.

**Implementation:**
- Script: `scripts/literature-extraction.py` (Python)
- Commands: `doctor`, `init`, `next`, `record`, `build`, `validate`
- Output directory: `forge-output/literature-extraction/<input-stem>/`

**Workflow:**
1. `doctor` - Check capabilities
2. `init <input> --output <dir>` - Initialize run
3. `next <run-dir>` - Request next pending document
4. Extract items with evidence quotes and locators
5. `record <run-dir> --doc-id <id> --extraction-file <file>` - Record extraction
6. `build <run-dir>` - Assemble tables and generate deliverables
   - Embeddings endpoint: `http://llms:8005/v1/embeddings` (via `FORGE_EMBEDDINGS_URL`)
7. `validate <run-dir>` - Resolve all errors

**Key Features:**
- One document at a time
- Distinguishes explicit vs. inferred vs. unclear claims
- Surfaces source disagreements
- Outputs: `evidence_table.csv`, `claim_clusters.csv/.md`, `key_terms.md`

---

### 5. Organize Folder
**Purpose:** Sort a messy folder via a reviewable manifest without moving until user agrees.

**Implementation:**
- Script: `scripts/organize-folder.py` (Python)
- Commands: `doctor`, `scan`, `plan`, `apply`, `undo`
- Output directory: `forge-output/organize-folder/<folder-name>/`

**Workflow:**
1. `doctor` - Confirm safeguards
2. `scan <target-folder> --output <dir>` - Recursive scan
   - Outputs: `manifest.csv`, `profile.md`, `near_duplicates.md`
   - Routes exact duplicates to `_duplicates/`
3. Design destination layout from profile signals
4. Edit `manifest.csv` with user
5. `plan <run-dir>` - Validate manifest, produce plan report
6. User agrees to plan
7. `apply <run-dir>` - Move files with hash verification
8. `undo <run-dir>` - Reverse all moves if needed

**Key Features:**
- Reviewable manifest-based approach
- Exact deduplication by SHA-256
- Re-verifies hashes before moving
- Never deletes files
- Full reversal support

---

### 6. Personal Admin
**Purpose:** Process personal, household, medical, financial documents into summaries and action plans.

**Implementation:**
- Script: `scripts/personal-admin.py` (Python)
- Commands: `doctor`, `init`, `next`, `record`, `build`, `validate`
- Output directory: `forge-output/personal-admin/<title-or-stem>/`

**Workflow:**
1. `doctor` - Check capabilities
2. `init <inputs...> --output <dir> --title "<title>"` - Initialize run
   - Optional: `--deliverables admin_summary,next_steps,deadline_checklist,contact_list`
3. `next <run-dir>` - Request next document
4. Extract facts (deadlines, actions, contacts, dates, fees)
5. `record <run-dir> --doc-id <id> --facts-file <file>` - Record facts
6. `build <run-dir>` - Assemble tables and author Markdown
7. `validate <run-dir>` - Resolve all errors

**Key Features:**
- One document at a time
- Keeps extracted facts separate from suggested steps
- Outputs: `extracted_facts.csv`, deadline/contact tables, Markdown deliverables

---

### 7. Report Output
**Purpose:** Assemble processed forge outputs into polished deliverables.

**Implementation:**
- Script: `scripts/report-output.py` (Python)
- Commands: `doctor`, `init`, `tables`, `render`, `validate`
- Output directory: `forge-output/report-output/<title-or-stem>/`

**Workflow:**
1. `doctor` - Check XLSX/DOCX/HTML capabilities
2. `init <inputs...> --output <dir> --detail <level> --title "<title>"`
   - Detail levels: `brief`, `memo`, `full`, `outline`
3. Read `sources.md` (input manifest)
4. Author deliverables, cite sources by id
5. Record caveats in `assumptions_and_limits.md`
6. `tables <run-dir>` - Build `tables.xlsx`
7. `render <run-dir> --format html|docx` - Convert to Pandoc formats
8. `validate <run-dir>` - Resolve all errors

**Key Features:**
- Chains upstream forge outputs
- Keeps synthesis separate from quotes
- Preserves source hashes
- Multiple detail levels
- Pandoc-dependent for format conversion

---

### 8. Site Builder
**Purpose:** Build a beautiful, self-contained static informational website from source material.

**Implementation:**
- Script: `scripts/site-builder.mjs` (Node.js)
- Commands: `doctor`, `init`, `eject`, `build`, `validate`
- Output directory: `forge-output/site-builder/<title-stem>/`

**Workflow:**
1. `doctor` - Check local capabilities
2. `init <inputs...> --output <dir> --title "<title>" [--theme <name>]`
   - Themes: `editorial`, `technical`, `archival`, `gallery`, `magazine`, `academic`, `brand`, `terminal`
   - Generates: `source_manifest.json`, `links.json`, `assets/`, `site.json`, content stubs
3. Converse with user on goal, audience, scope
4. Edit `site.json` (pages, nav, theme, tokens)
5. Author `content/<slug>.md` from sources
6. Optional: `eject <run-dir>` - Copy theme for custom editing
7. `build <run-dir>` - Generate plain HTML/CSS/JS
8. Review in browser
9. `validate <run-dir>` - Check links, accessibility

**Key Features:**
- Fully self-contained with relative paths
- No external/CDN requests
- Accessible and responsive
- Deterministic site generation
- Custom theming via ejection

---

### 9. Spreadsheet Analysis
**Purpose:** Inspect, profile, clean, transform, and enrich CSV, TSV, and XLSX tabular data.

**Implementation:**
- Script: `scripts/spreadsheet-analysis.py` (Python)
- Commands: `doctor`, `inspect`, `cluster`, `row-init`, `row-next`, `row-record`, `row-finalize`, `validate`
- Output directory: `forge-output/spreadsheet-analysis/<source-stem>/`

**Workflow:**

**Inspection:**
1. `doctor` - Check XLSX support
2. `inspect <input> --output <dir>` - Profile data

**Fuzzy Clustering:**
1. `cluster <input> --output <dir> --columns "Col1" --threshold 0.85`
   - Embeddings: `http://llms:8005/v1/embeddings` (via `FORGE_EMBEDDINGS_URL`)
   - Outputs: `clusters.csv`, `cluster_groups.md`

**Row Enrichment:**
1. `row-init <input> --output <dir> --column "Output Col"`
2. `row-next <run-dir>` - Get next pending row
3. Generate value
4. `row-record <run-dir> --row-id <id> --value-file <file>` - Record result
5. `row-finalize <run-dir>` and `validate <run-dir>`
   - Outputs: `enriched.csv/.tsv/.xlsx`, `review_report.md`

**Key Features:**
- Preserves source row numbers
- Fuzzy matching for entity resolution
- One-row-at-a-time enrichment
- Preserves XLSX sheets and formulas
- Keeps extracted values separate from interpretation

---

### 10. Transcript Cleanup
**Purpose:** Clean, structure, and transform raw transcripts from meetings, interviews, calls, lectures.

**Implementation:**
- Script: `scripts/extract-transcript.mjs` (Node.js) for DOCX extraction
- Output directory: `forge-output/transcript-cleanup/<source-stem>/`

**Workflow:**
1. Identify input and requested output track
2. For DOCX: `node <skill-dir>/scripts/extract-transcript.mjs <input.docx> <output.md>`
3. For audio/video: Use `transcription` skill first
4. Create output directory
5. Choose output track:
   - **Faithful Cleanup**: Full transcripts, preserves all content
     - Output: `cleaned_transcript.md`
   - **Structured Memo**: Meetings, calls with decisions
     - Output: `review_memo.md` with: Summary, Actions, Decisions, Questions, Key Quotes, Topics

**Key Features:**
- Removes filler only when meaning preserved
- Preserves timestamps and speaker intent
- Marks uncertain speakers and unintelligible text
- Distinguishes interpretations from facts

---

### 11. Transcription
**Purpose:** Transcribe audio or video files with local speech-to-text (NVIDIA Parakeet TDT v3), then correct with user-controlled dictionary.

**Implementation:**
- Script: `scripts/transcription.py` (Python)
- Commands: `doctor`, `setup`, `transcribe`, `dict add|list|apply`, `validate`
- Output directory: `forge-output/transcription/<source-stem>/`
- Managed venv: `~/.pi-forge/transcription`
- Model: ~2.5 GB, auto-installed

**Workflow:**
1. `doctor` - Check engine (parakeet-mlx on Apple Silicon, NVIDIA NeMo elsewhere)
2. `setup` - Build venv and download model (one-time)
3. `transcribe <media> --output <dir> --type <type>`
   - Types: `lecture`, `interview`, `meeting`, `call`, `voice-note`, `other`
   - Outputs: `raw_transcript.txt`, `corrected_transcript.md/.txt`, `corrections_log.csv`
4. **Correction Dictionary** (global: `~/.pi-forge/transcription/dictionary.json`):
   - `dict add --correct "Term" --variant "misheard" --category term`
   - `dict apply <transcript> --output <out>`
5. Chain into `transcript-cleanup` skill
6. Review corrections log

**Key Features:**
- Runs locally; no audio leaves machine
- Autoselected backend per platform
- Deterministic dictionary-based correction
- All corrections logged
- Persistent dictionary

---

### 12. Vault Handoff
**Purpose:** Send completed Markdown or text artifact to pi-vault for explicit review.

**Implementation:**
- Tool call: `pi_vault_submit_artifact`
- No script; integrates with external pi-vault system

**Workflow:**
1. Identify completed `.md` or `.txt` artifact
2. Report all outstanding pi-forge warnings
3. Call `pi_vault_submit_artifact` with:
   - Absolute artifact path
   - Suggested note name (optional)
   - Pi-forge task ID
   - Source operation
4. Treat structured status as authoritative
5. For `pending_review`: report proposal path

**Key Features:**
- Validates artifact before handoff
- Tracks task ID and source operation
- Structured status reporting
- Requires explicit vault review

---

### 13. Web Collection
**Purpose:** Collect, archive, and organize web source material from URLs into preserved files with provenance manifest.

**Implementation:**
- Script: `scripts/web-collection.mjs` (Node.js)
- Commands: `doctor`, `collect`, `harvest`, `search`, `validate`
- Output directory: `forge-output/web-collection/<source-stem>/`

**Workflow:**
1. `doctor --json` - Check capabilities (Playwright, SearXNG)
2. Choose command:
   - `collect <url...> --output <dir> [--render]`
   - `harvest <page-url> --output <dir> --ext pdf [--match <regex>]`
   - `search <query...> --output <dir> [--limit N]`
   - Add `--render` for full Playwright captures
3. Review: `web_manifest.csv`, `collection_report.md`
4. Inspect `needs_review` rows
5. `validate <run-dir>` - Resolve all errors
6. For text extraction: hand `downloads/` to `document-ingest`

**Key Features:**
- Only http/https URLs
- Polite delays and clear User-Agent
- `harvest` honors `robots.txt`
- Deduplicates by URL, SHA-256, filename, title
- Records full provenance
- Refuses to overwrite existing directories

---

## External Interface Design Patterns

### Input/Output Structure
All skills follow a consistent pattern:
- **Input**: Files, folders, or URLs (absolute or relative paths)
- **Output**: Structured directory under `forge-output/<skill-name>/<source-stem>/` (numbered suffix if exists)
- **Manifest**: `manifest.csv` or `<skill>_manifest.json` tracks provenance and status
- **Artifacts**: Markdown, CSV, JSON organized in subdirectories

### Script Invocation
- **Node.js**: `node <skill-directory>/scripts/<skill>.mjs <command> [args]`
- **Python**: `python3 <skill-directory>/scripts/<skill>.py <command> [args]`
- **Common**: `doctor`, `--output <dir>`, `--json` for machine-readable output

### Error Handling
- Scripts exit with status codes
- Errors reported in logs and `*_report.md`
- Individual failures don't halt batch processing
- Validation step required before completion
- All errors must be resolved

### Chaining Skills
- Output directory of one skill can be input to another
- Example: `document-ingest` → `literature-extraction` → `report-output`
- Upstream provenance (hashes, source maps) preserved
- Example: `transcription` → `transcript-cleanup` → `report-output`

### Persistent Configuration
- Global settings: `~/.pi-forge/`
- Per-project overrides: `.forge/` directory
- Environment variables: `FORGE_EMBEDDINGS_URL`, `FORGE_GLMOCR_URL`, `FORGE_SEARXNG_URL`

### Human Judgment Points
- One-document-at-a-time: `literature-extraction`, `personal-admin`, `spreadsheet-analysis` (row enrichment)
- Reviewable manifests: `organize-folder`, `web-collection`
- Optional customization: `site-builder` theme ejection
- Advisory signals: near-duplicates, content clusters (embeddings-based)

### Integrity Guarantees
- Source files preserved by path and SHA-256
- Outputs never overwrite inputs or existing runs
- Hashes verified at init/validate boundaries
- All changes logged in detail
- Reversible operations (undo, manifest editing)

---

## Shared Dependencies & Endpoints

| Endpoint | Default | Env Var | Used By |
|----------|---------|---------|---------|
| GLM-OCR | `http://llms:5002/glmocr/parse` | `FORGE_GLMOCR_URL` | document-ingest |
| Embeddings | `http://llms:8005/v1/embeddings` | `FORGE_EMBEDDINGS_URL` | organize-folder, spreadsheet-analysis, literature-extraction |
| SearXNG | (required for search) | `FORGE_SEARXNG_URL` | web-collection |

---

## Recommended Integration Pipelines

**Pipeline: Document Processing**
- `web-collection` → `document-ingest` → `literature-extraction` → `report-output`

**Pipeline: Media Processing**
- `transcription` → `transcript-cleanup` → `personal-admin` or `report-output`

**Pipeline: Site Generation**
- `web-collection` → `document-ingest` → `site-builder`

**Pipeline: Folder Organization + Analysis**
- `organize-folder` → `spreadsheet-analysis` (if CSV/XLSX) or `report-output`

**Single-Skill Use**
- `coding` for repository inspection/modification
- `file-conversion` for format transformation
- `vault-handoff` for deliverable submission

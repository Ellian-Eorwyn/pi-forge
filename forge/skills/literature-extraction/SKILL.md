---
name: literature-extraction
description: Extract structured, source-backed evidence from academic articles, reports, policy documents, white papers, and research corpora. Use for single documents or folders that need claims, methods, data sources, findings, limitations, definitions, citations, quoted evidence, variables, populations, technologies, policies, and research gaps captured with provenance, an explicit-versus-inferred distinction, evidence and methods tables, and synthesis for literature reviews, grant writing, coding schemes, annotated bibliographies, and comparison across sources.
---

# Literature Extraction

Extract reviewable, provenance-backed evidence from research documents without
overclaiming or blending source content with synthesis.

## Natural Language Routing

Use this skill directly when the user asks for literature review, source-backed
evidence extraction, claims/terms/research gaps, annotated bibliography,
cross-source synthesis, or similar analysis over already-clean Markdown/text
sources.

When this skill is reached from `document-ingest`, run it after document ingest
has finalized the source folder. Use the finalized source folder as input and
write the literature run to:

```bash
<input-folder>/Generated/Literature-Extraction
```

The default folder discovery skips `Ingest/`, `Originals/`, and `Generated/`, so
this processes only the finalized clean Markdown files at the source folder
surface.

## Command Card

- `doctor --json`: capability check and embeddings availability.
- `init <input> --output <run-directory>`: discover finalized Markdown/text sources; skips `Ingest/`, `Originals/`, and `Generated/` folders by default.
- `init <input> --output <run-directory> --include-reserved`: opt in to processing reserved workspace folders.
- `next <run-directory>`: one pending source with item types and progress.
- `record <run-directory> --doc-id <id> --extraction-file <items.json>`: append one model-approved extraction.
- `build <run-directory>`: build tables, claim clusters, and Markdown scaffolds.
- `validate <run-directory> --fix-hints --json`: machine-readable quality gate with repair hints.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path. Check local
   capabilities and optional embeddings availability:

   ```bash
   python3 <skill-directory>/scripts/literature-extraction.py doctor
   ```

   This skill consumes `document.md`, `.md`, and `.txt`. Convert PDF, DOCX,
   HTML, and RTF sources with `document-ingest` first so provenance and hashes
   already exist.
2. Create a new output directory under
   `forge-output/literature-extraction/<input-stem>/`. If it exists, use a
   numbered suffix. When invoked from `document-ingest`, instead use
   `<input-folder>/Generated/Literature-Extraction`. Then initialize the run:

   ```bash
   python3 <skill-directory>/scripts/literature-extraction.py init <input> --output <new-directory>
   ```

   `<input>` is a single file or a folder; folders are discovered recursively,
   skipping hidden paths, symlinks, and finalized ingest workspace folders
   (`Ingest/`, `Originals/`, and `Generated/`). Use `--include-reserved` only
   when you explicitly need internal artifacts.
3. Read [references/extraction-contract.md](references/extraction-contract.md).
   Process **one document at a time**. Request the next pending document:

   ```bash
   python3 <skill-directory>/scripts/literature-extraction.py next <run-directory>
   ```

4. Read that document's text, extract items with evidence quotes, locators, and
   the `explicit`/`inferred`/`unclear` distinction, and write the JSON array to
   a file under `<run-directory>/working/`. Record it with the returned id:

   ```bash
   python3 <skill-directory>/scripts/literature-extraction.py record <run-directory> \
     --doc-id <document-id> --extraction-file <working-file>
   ```

   When a document cannot be processed, record an explicit disposition instead:
   `--status needs_review --note "<reason>"`, `--status skipped --note "<reason>"`,
   or `--status failed --note "<error>"`. Do not hide an unprocessable document
   behind an empty success. Resume safely by calling `next` again; progress is
   derived from `extraction_results.jsonl`.
5. After every document has a disposition, assemble the tables and scaffold the
   deliverables:

   ```bash
   python3 <skill-directory>/scripts/literature-extraction.py build <run-directory>
   ```

   When the embeddings endpoint (`FORGE_EMBEDDINGS_URL`, default
   `http://llms:8005/v1/embeddings`) is reachable and at least two claims or
   findings exist, `build` also writes `claim_clusters.csv` and the advisory
   `claim_clusters.md` worksheet grouping similar claims across documents and
   flagging possible contradictions for review. It degrades cleanly when the
   endpoint is unavailable; pass `--no-claim-clusters` to skip it or
   `--claim-cluster-threshold` to tune grouping.

   Then author `literature_summary.md`, `claims_matrix.md`, `key_terms.md`,
   `research_gaps.md`, and `citation_notes.md` from the evidence table, using
   `claim_clusters.md` when present to find cross-source agreement and
   disagreement with better recall. Prioritize key terms and definitions,
   connections between readings, claims and arguments, source-grounded author
   descriptions, and methodology when applicable. Keep verbatim quotes and
   extracted facts separate from generated synthesis, and judge every flagged
   contradiction against the evidence rather than trusting the lexical hint.
   Re-running `build` refreshes the tables and worksheet without overwriting
   authored Markdown.
6. Validate the completed run and report outcomes:

   ```bash
   python3 <skill-directory>/scripts/literature-extraction.py validate <run-directory> --fix-hints --json
   ```

   Resolve every error before completion. Report skipped, failed, and
   review-needed documents rather than concealing incomplete coverage.

## Safety and Output Rules

- Preserve source files. Hashes recorded at `init` must still match at `build`
  and `validate`; a changed source aborts the run.
- Attribute every extracted item to its document and locator. Never claim
  page-level precision for a format that does not expose pages.
- Distinguish what a source states explicitly, what you infer, and what is
  unclear. Do not extend a source's claims beyond what it supports.
- When sources disagree, record both and surface the disagreement; do not
  reconcile it silently.
- Keep `evidence_table.csv` and `methods_matrix.csv` machine-readable and
  traceable. Use the `direct_quotes` column for quote support. Keep the
  Markdown deliverables interpretive and clearly marked as generated synthesis.
- Do not assume Obsidian schemas or frontmatter unless the user requests them.

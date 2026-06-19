---
name: literature-extraction
description: Extract structured, source-backed evidence from academic articles, reports, policy documents, white papers, and research corpora. Use for single documents or folders that need claims, methods, data sources, findings, limitations, definitions, citations, quoted evidence, variables, populations, technologies, policies, and research gaps captured with provenance, an explicit-versus-inferred distinction, evidence and methods tables, and synthesis for literature reviews, grant writing, coding schemes, annotated bibliographies, and comparison across sources.
---

# Literature Extraction

Extract reviewable, provenance-backed evidence from research documents without
overclaiming or blending source content with synthesis.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path. Check local
   capabilities when XLSX export is uncertain:

   ```bash
   python3 <skill-directory>/scripts/literature-extraction.py doctor
   ```

   This skill consumes `document.md`, `.md`, and `.txt`. Convert PDF, DOCX,
   HTML, and RTF sources with `document-ingest` first so provenance and hashes
   already exist.
2. Create a new output directory under
   `forge-output/literature-extraction/<input-stem>/`. If it exists, use a
   numbered suffix. Then initialize the run:

   ```bash
   python3 <skill-directory>/scripts/literature-extraction.py init <input> --output <new-directory>
   ```

   `<input>` is a single file or a folder; folders are discovered recursively,
   skipping hidden paths and symlinks.
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

   Then author `literature_summary.md`, `claims_matrix.md`, `research_gaps.md`,
   and `citation_notes.md` from the evidence table, keeping verbatim quotes and
   extracted facts separate from generated synthesis. Re-running `build`
   refreshes the tables without overwriting authored Markdown.
6. Validate the completed run and report outcomes:

   ```bash
   python3 <skill-directory>/scripts/literature-extraction.py validate <run-directory>
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
- Keep `evidence_table.csv`, `evidence_table.xlsx`, and `methods_matrix.csv`
  machine-readable and traceable. Keep the Markdown deliverables interpretive
  and clearly marked as generated synthesis.
- Do not assume Obsidian schemas or frontmatter unless the user requests them.

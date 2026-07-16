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
- `meta-init <folder-or-run...> --output <run-directory> --research-question <text|file>`: discover completed prior literature runs and create context-bounded meta packets.
- `meta-init --group primary=<path> --group secondary=<path> ...`: explicitly label corpora instead of inferring labels from folder names.
- `meta-next <run-directory>`: one pending meta packet with budget and progress.
- `meta-record <run-directory> --packet-id <id> --memo-file <memo.md>`: append one model-authored packet memo.
- `meta-build <run-directory>`: scaffold cross-corpus meta deliverables.
- `meta-validate <run-directory> --fix-hints --json`: validate packet memos, citations, and provenance warnings.

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

   The `init` command will interactively prompt you to select an extraction schema (Academic, Products & Services, or Custom). The chosen `itemTypes` and any `customInstructions` will be saved to `run_config.json`.
   
   `<input>` is a single file or a folder; folders are discovered recursively,
   skipping hidden paths, symlinks, and finalized ingest workspace folders
   (`Ingest/`, `Originals/`, and `Generated/`). Use `--include-reserved` only
   when you explicitly need internal artifacts.
3. Read [references/extraction-contract.md](references/extraction-contract.md).
   Process **one document at a time**. Request the next pending document:

   ```bash
   python3 <skill-directory>/scripts/literature-extraction.py next <run-directory>
   ```

   The `next` command will return the `itemTypes` and any `customInstructions` configured during initialization for the current document.

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
   authored Markdown. `build` also writes `item_index.jsonl` and
   `source_profile.csv`, which are lightweight machine-readable inputs for
   future meta runs.
6. Validate the completed run and report outcomes:

   ```bash
   python3 <skill-directory>/scripts/literature-extraction.py validate <run-directory> --fix-hints --json
   ```

   Resolve every error before completion. Report skipped, failed, and
   review-needed documents rather than concealing incomplete coverage.

## Meta Literature Extraction

Use the meta workflow when the user points pi-forge at one or more folders that
already contain completed literature-extraction runs and asks for cross-corpus
analysis, for example primary sources analyzed through secondary-source
concepts.

1. Initialize a meta run with an explicit research question. Use `--group` when
   source roles matter:

   ```bash
   python3 <skill-directory>/scripts/literature-extraction.py meta-init \
     --group primary=<primary-literature-run-or-parent> \
     --group secondary=<secondary-literature-run-or-parent> \
     --research-question "<question>" \
     --output <new-meta-run-directory>
   ```

   The default packet target is `128000` estimated tokens with a hard
   `256000` maximum using Pi's conservative `ceil(characters / 4)` heuristic.
   The script uses prior structured artifacts first and only reopens source
   text for targeted quote/snippet checks when the source files still exist.
2. Process one packet at a time:

   ```bash
   python3 <skill-directory>/scripts/literature-extraction.py meta-next <meta-run>
   ```

   Read the returned packet, `meta_items.jsonl`, `bridge_candidates.csv`, and
   `topic_clusters.csv` as needed. Write a memo that cites item ids such as
   `m000001` for every substantive analytic claim, then record it:

   ```bash
   python3 <skill-directory>/scripts/literature-extraction.py meta-record <meta-run> \
     --packet-id <packet-id> --memo-file <memo.md>
   ```
3. After every packet has a memo, scaffold the final cross-corpus deliverables:

   ```bash
   python3 <skill-directory>/scripts/literature-extraction.py meta-build <meta-run>
   ```

   Author `meta_synthesis.md`, `primary_secondary_matrix.md`,
   `concept_register.md`, `negative_cases.md`, and `methods_and_limits.md`.
   Preserve source roles, separate primary evidence from secondary
   interpretation, distinguish emic and etic concepts, and surface disagreement,
   silence, uncertainty, and source limits rather than smoothing them into
   consensus.
4. Validate before completion:

   ```bash
   python3 <skill-directory>/scripts/literature-extraction.py meta-validate <meta-run> --fix-hints --json
   ```

   Missing original source files are reported as warnings; the meta run remains
   valid when the prior structured artifacts are intact. Unresolved placeholders
   or uncited meta deliverables are errors.

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
- In meta runs, treat `bridge_candidates.csv` and `topic_clusters.csv` as
  advisory retrieval aids, not conclusions. Judge every proposed connection
  against item ids, source titles, corpus labels, locators, and quotes/snippets.
- Do not assume Obsidian schemas or frontmatter unless the user requests them.

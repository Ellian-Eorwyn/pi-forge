---
name: document-ingest
description: One-stop-shop folder ingestion pipeline. Ingest, normalize, and process folders of documents, presentations, images, audio, and video files. Automatically categorizes folders and routes files to the appropriate specialized skills (transcription, personal-admin, literature-extraction, project-extraction). Uses a hybrid deterministic and LLM-driven orchestration approach.
---

# Document Ingest

Process entire folders of documents and media deterministically, then orchestrate follow-up actions and file organization using the LLM.

## Natural Language Routing

Use this skill when the user asks to "ingest", "process", "clean up",
"convert these files", "handle this folder", or similar natural-language
requests over a folder of documents or media. The user does not need to name
`document-ingest`.

If prepared files are categorized as `literature`, or the folder clearly
contains readings, articles, reports, lecture transcripts, research material,
or a corpus that would benefit from source-backed claims/terms/synthesis, run
the literature workflow too. After document ingest validates and finalizes, run
`literature-extraction` on the finalized source folder with output under:

```bash
<input-folder>/Generated/Literature-Extraction
```

Do this because `literature-extraction` skips `Ingest/`, `Originals/`, and
`Generated/` by default, so running it on the finalized source folder processes
only the clean top-level Markdown outputs. Do not ask the user to name the
second skill when the document type makes the handoff clear.

If prepared files are categorized as `project`, or the folder contains grants,
awards, proposals, scopes of work, contracts, work plans, reports,
presentations, meetings, or interviews that need deliverables and dates tracked,
run `project-extraction` after finalization. Put its refreshable workspace at:

```bash
<input-folder>/Generated/Project-Extraction
```

Project recordings follow `transcription`, `transcript-cleanup`, then
`project-extraction`. Do not treat proposal language as an awarded obligation.

## Command Card

- `doctor --json`: capability check.
- `prepare <input> --output <input>/Ingest`: deterministic extraction and manifest creation.
- `status <run-directory>`: durable progress, validation state, input drift, pending review list, and repair hints.
- `refresh <run-directory>`: explicitly reconcile added, changed, and removed sources while preserving revision history.
- `retry <run-directory> --item <id>|--all-failed`: explicitly requeue permanent failures.
- `next-review <run-directory>`: one structured review packet with allowed enum values, paths, metadata, and exact recording commands.
- `record-review-unit <run-directory> --doc-id <id> --kind chunk|vision-page --index <n> --reviewed-file <markdown>`: atomically commit one granular review unit.
- `record-review <run-directory> --review-file <review.json>`: atomically update `metadata.json`, `manifest.csv`, and `extraction_report.md` after model review.
- `record-transcript <run-directory> --doc-id <id> --transcript <cleaned.md>`: atomically install a cleaned transcript as the final document text and repair transcript chunk validation state.
- `validate <run-directory> --fix-hints --json`: machine-readable quality gate with repair hints.
- `run <input> --output <input>/Ingest [--literature] [--project]`: deterministic prepare/resume wrapper that reports the next review action and downstream handoff.
- For finalized literature-like folders: `python3 <literature-skill>/scripts/literature-extraction.py init <input-folder> --output <input-folder>/Generated/Literature-Extraction`.

## Mechanical Tools

For lower-level execution, the manifest also exposes `pdf_to_markdown`,
`docx_to_markdown`, `pptx_to_markdown`, and `extract_metadata`. These tools accept structured JSON
input and return structured JSON results with prepared Markdown, metadata,
source-map, and extraction-report artifact paths. Use the full workflow for
folder ingestion, review, finalization, and literature handoff.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path. Run the capability check:

   ```bash
   node <skill-directory>/scripts/document-ingest.mjs doctor
   ```

2. Choose the source folder's `Ingest/` directory as the run directory for
   folder ingestion, or an explicit run directory for a single-file run. The
   same command resumes a compatible run, and returns its completion summary
   when already complete. Use a numbered directory only for an independent run.
   Prepare the input without moving or overwriting sources:

   ```bash
   node <skill-directory>/scripts/document-ingest.mjs prepare <input-folder> --output <input-folder>/Ingest
   ```

   This writes `run_state.json`, `run_events.jsonl`, and a pending
   `manifest.csv` before extraction. It commits each completed document
   immediately, automatically extracts audio from videos (via `ffmpeg`),
   applies OCR to images/PDFs (via `glmocr`), extracts PPTX slide text, tables,
   notes, and alt text with slide source maps, and determines a
   `suggested_pipeline` based on the folder contents and file formats.

3. Use the structured review queue instead of reading the full manifest and
   guessing valid values:

   ```bash
   node <skill-directory>/scripts/document-ingest.mjs status <new-directory>
   node <skill-directory>/scripts/document-ingest.mjs next-review <new-directory>
   ```

   Each packet is one vision page, large chunk, or final document metadata
   review. Commit page/chunk packets with `record-review-unit`; if context ends
   before recording, `next-review` returns the same unit. For each final
   document packet with `complete == false`, follow its `suggestedPipeline`:

   **For `basic-markdown`, `personal-admin`, `literature`, or `project-extraction`**:
   - Review and clean up the `document.md` structure in the file's output directory. 
   - Improve headings, paragraphs, lists, and tables supported by the source. Leave uncertain text visible and note it in `extraction_report.md`.
   - Complete a review JSON file matching the packet shape, set
     `finalOutput.filename` to a meaningful
     final Markdown filename, and mark review as complete.
   - Choose final filenames from the cleaned file contents, not just the
     original filename. Use concise names that make the file easy to browse and
     sort. Lecture transcripts should say they are lecture transcripts when
     supported by the content. Administrative files with dates must begin with
     `YYYY-MM-DD`. Insurance claim filenames should include the date,
     diagnosis, procedure, and facility when those details are present.
   - `finalOutput.filename` must be a filename only, not a path, and must end
     in `.md`.
   - Record the review with `record-review`; do not hand-edit `metadata.json`,
     `manifest.csv`, or `extraction_report.md`.
   - If the pipeline names a specialized skill, load and execute that skill's instructions on the finalized Markdown outputs.

   **For pipelines containing `transcription,transcript-cleanup`**:
   - Locate the extracted `derived/audio.mp3` in the file's output directory.
   - Load and execute the `transcription` skill instructions on the audio file to produce a transcript.
   - Load and execute the `transcript-cleanup` skill instructions to format the raw transcript into a clean, readable Markdown document.
   - Save the cleaned transcript with `record-transcript`, including a
     meaningful `--filename` when known. Do not manually copy transcript text
     into `working/` files.
   - If the pipeline ends with `project-extraction`, include the cleaned
     transcript in the finalized folder before starting project extraction.

4. As you complete each file, update your internal task checklist or the `manifest.csv` to track progress. Ensure every successfully processed file is reviewed.

5. Validate the completed run (this checks the formatting of the document ingest outputs):

   ```bash
   node <skill-directory>/scripts/document-ingest.mjs validate <new-directory> --fix-hints --json
   ```

6. **Final File Organization**:
   - Once all files in the manifest have been processed and validated, finalize
     the source folder:

     ```bash
     node <skill-directory>/scripts/document-ingest.mjs finalize <input-folder>/Ingest --destination <input-folder>
     ```

   - `finalize` refuses to run unless validation passes and no destination
     conflicts exist. It records a hash-bound `finalize_plan.json`, commits each
     move/copy independently, and safely resumes after interruption.
   - For a flat source folder, final cleaned Markdown files go at the source
     folder root. For a clearly structured folder with multiple source
     subfolders, final cleaned Markdown preserves the relative subfolder
     structure.
   - Original source files move into `Originals/`, preserving relative paths.
     Audio and video originals are moved there too.
   - User-facing generated synthesis such as `evidence_table.csv`,
     `claims_matrix.md`, `key_terms.md`, `literature_summary.md`,
     `citation_notes.md`, and `research_gaps.md` goes under `Generated/`.
   - `Ingest/` keeps the manifest, `artifact_manifest.csv`, extraction reports,
     source maps, OCR/media derivatives, claim-clustering worksheets, and other
     background processing files.
   - Do not place raw originals or raw transcripts at the source-folder top
     level. The top level should contain only final cleaned Markdown outputs.

7. **Automatic Literature Handoff**:
   - If any successful item has `suggested_pipeline` containing `literature`, or
     the final folder is clearly a set of readings, articles, reports, lecture
     transcripts, or research sources, run `literature-extraction` after
     finalization.
   - Use the finalized source folder as the literature input and
     `<input-folder>/Generated/Literature-Extraction` as the literature output.
   - Complete the literature extraction workflow through `build`, model-authored
     deliverables, and `validate --fix-hints --json`.
   - Report both ingestion outputs and generated literature deliverables in the
   final response.

8. **Automatic Project Handoff**:
   - If any successful item has `suggested_pipeline` containing
     `project-extraction`, initialize that workflow after finalization.
   - Use the finalized source folder as input and
     `<input-folder>/Generated/Project-Extraction` as output.
   - Complete packet extraction, reconciliation, build, authored Markdown, and
     validation. Preserve `project_status.csv` for later refreshes.

## Safety and Failure Handling

- Never overwrite an input or unrelated output directory. Resume only a
  compatible marked run. Use `status` to inspect drift and `refresh` before
  adopting source revisions.
- Do not upload files to external internet APIs unless using a locally configured backend.
- Keep automatically generated OCR PDFs under `derived/ocr.pdf` and media under `derived/audio.mp3`.
- Do not install system packages. Report missing capabilities and the commands that require them.
- Legacy `.ppt`, Keynote, and ODP are unsupported in v1. PPTX charts,
  visual-only slides, and embedded objects require explicit review warnings.

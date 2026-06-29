---
name: document-ingest
description: One-stop-shop folder ingestion pipeline. Ingest, normalize, and process folders of documents, images, audio, and video files. Automatically categorizes folders and routes files to the appropriate specialized skills (transcription, personal-admin, literature-extraction). Uses a hybrid deterministic and LLM-driven orchestration approach.
---

# Document Ingest

Process entire folders of documents and media deterministically, then orchestrate follow-up actions and file organization using the LLM.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path. Run the capability check:

   ```bash
   node <skill-directory>/scripts/document-ingest.mjs doctor
   ```

2. Choose a new output directory and prepare the input folder:

   ```bash
   node <skill-directory>/scripts/document-ingest.mjs prepare <input> --output <new-directory>
   ```

   This deterministic step creates a `manifest.csv` containing all files, automatically extracts audio from videos (via `ffmpeg`), applies OCR to images/PDFs (via `glmocr`), and determines a `suggested_pipeline` (e.g. `personal-admin`, `literature`, `transcription,transcript-cleanup`, `basic-markdown`) based on the folder contents and file formats.

3. Read the `manifest.csv` located in `<new-directory>`. For each file in the manifest with `status` == "needs_review", follow its `suggested_pipeline`:

   **For `basic-markdown`, `personal-admin`, or `literature`**:
   - Review and clean up the `document.md` structure in the file's output directory. 
   - Improve headings, paragraphs, lists, and tables supported by the source. Leave uncertain text visible and note it in `extraction_report.md`.
   - Complete `metadata.json` and mark review as complete.
   - If the pipeline is `personal-admin` or `literature`, load and execute that specific skill's instructions on the finalized `document.md` to produce the required summary/spreadsheet outputs.

   **For `transcription,transcript-cleanup`**:
   - Locate the extracted `derived/audio.mp3` in the file's output directory.
   - Load and execute the `transcription` skill instructions on the audio file to produce a transcript.
   - Load and execute the `transcript-cleanup` skill instructions to format the raw transcript into a clean, readable Markdown document.
   - Save the final transcript as `document.md` and mark the item as complete.

4. As you complete each file, update your internal task checklist or the `manifest.csv` to track progress. Ensure every successfully processed file is reviewed.

5. Validate the completed run (this checks the formatting of the document ingest outputs):

   ```bash
   node <skill-directory>/scripts/document-ingest.mjs validate <new-directory>
   ```

6. **Final File Organization**:
   - Once all files in the manifest have been processed and validated, reorganize the folder.
   - Create an `Originals/` folder inside `<new-directory>`.
   - Move all original source files into `Originals/`.
   - Extract the processed, final Markdown files (and any generated spreadsheets or reports) from their respective `output_directory` subfolders and place them directly in the top level of `<new-directory>`. Give them clean, descriptive names based on their content or original filename.

## Safety and Failure Handling

- Never overwrite an input or existing output directory.
- Do not upload files to external internet APIs unless using a locally configured backend.
- Keep automatically generated OCR PDFs under `derived/ocr.pdf` and media under `derived/audio.mp3`.
- Do not install system packages. Report missing capabilities and the commands that require them.

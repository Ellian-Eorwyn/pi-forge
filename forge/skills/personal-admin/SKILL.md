---
name: personal-admin
description: Process personal, household, medical, financial, insurance, travel, repair, purchase, and bureaucratic documents into clear summaries and action plans. Use for bills, forms, letters, policies, appointment instructions, receipts, order records, and emails pasted as text to extract deadlines, required actions, contacts, account/order/reference numbers, dates, fees, requirements, and missing information, and to draft checklists, next-step plans, call scripts, message drafts, and comparison tables. Works on a single document or a folder, keeps document facts separate from suggested steps, and organizes and summarizes rather than giving legal, medical, or financial advice.
---

# Personal Admin

Turn personal-admin documents into reviewable summaries and action plans. Keep
extracted facts separate from suggested steps, and organize rather than advise.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path:

   ```bash
   python3 <skill-directory>/scripts/personal-admin.py doctor
   ```

   This skill consumes `document.md`, `.md`, and `.txt` (paste emails or notes
   into a `.txt`). Convert PDF, DOCX, HTML, and RTF with `document-ingest` first.
2. Use an output directory under
   `forge-output/personal-admin/<title-or-stem>/`. Repeating `init` with the
   same paths and options resumes a compatible marked run; use a numbered
   suffix only for an independent run. Initialize the run:

   ```bash
   python3 <skill-directory>/scripts/personal-admin.py init <inputs...> \
     --output <new-directory> --title "<title>"
   ```

   Use `--deliverables admin_summary,next_steps,deadline_checklist,contact_list,message_draft,comparison_table,call_script`
   to choose outputs; the default set is the first four.
   Use `status <run-directory> --json` to inspect durable progress and source
   drift. The run keeps `run_state.json`, an fsynced `run_events.jsonl`, and its
   existing domain manifests.
3. Read [references/admin-contract.md](references/admin-contract.md). Extract
   facts **one document at a time**:

   ```bash
   python3 <skill-directory>/scripts/personal-admin.py next <run-directory>
   ```

   Read the document, write its fact array (deadlines, actions, contacts,
   reference numbers, dates, fees, requirements, missing info) to a file under
   `working/`, then record it:

   ```bash
   python3 <skill-directory>/scripts/personal-admin.py record <run-directory> \
     --doc-id <document-id> --facts-file <working-file>
   ```

   Use `--status needs_review|skipped|failed --note "<reason>"` for documents you
   cannot process, rather than empty facts. Resume by calling `next` again.
4. Assemble the tables, then author the selected Markdown:

   ```bash
   python3 <skill-directory>/scripts/personal-admin.py build <run-directory>
   ```

   `build` writes `extracted_facts.csv` and the selected `deadline_checklist.csv`
   / `contact_list.csv`. Author `admin_summary.md`, `next_steps.md`, and any
   other selected deliverables, keeping document facts separate from suggested
   steps and resolving each placeholder.
5. Validate and report outcomes:

   ```bash
   python3 <skill-directory>/scripts/personal-admin.py validate <run-directory>
   ```

   Resolve every error before completion; report skipped, failed, and
   review-needed documents.

## Safety and Output Rules

- Preserve originals. Inputs are referenced by path and SHA-256, never copied;
  hashes recorded at `init` must still match at `validate`.
- Keep document facts (the CSVs) separate from suggested next steps (the authored
  Markdown). Never record a suggested action as a fact.
- Organize and summarize; do not give legal, medical, or financial advice. Point
  to where a professional is warranted instead.
- Mark missing or unclear information rather than guessing; do not invent
  account numbers, dates, or details.
- Keep sensitive material local and recommend redaction before any output is
  shared externally.
- Do not assume Obsidian schemas or frontmatter unless the user requests them.

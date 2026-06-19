# Personal Admin Contract

Turn personal-admin documents into clear summaries and action plans. Keep
document facts strictly separate from suggested next steps, organize rather than
advise, and keep sensitive material local.

## Run Layout

```text
<run-dir>/
  run_config.json
  source_manifest.json
  sources.md                 # script-generated
  facts_results.jsonl        # append-only staged facts
  extracted_facts.csv        # always built
  deadline_checklist.csv     # built when selected
  contact_list.csv           # built when selected
  admin_summary.md           # } authored, per --deliverables
  next_steps.md              # }
  message_draft.md           # }
  comparison_table.md        # }
  call_script.md             # }
  working/                   # per-document facts JSON written before record
```

`run_config.json`, `source_manifest.json`, `sources.md`, `facts_results.jsonl`,
and the CSVs are script-managed — do not hand-edit them. Authored Markdown is
scaffolded with a placeholder marker (`<!-- TODO: author this section -->`) that
must be removed once written.

This skill consumes `document.md` (from `document-ingest`), `.md`, and `.txt`
(including emails or notes pasted into a `.txt`). Convert PDF, DOCX, HTML, and
RTF with `document-ingest` first.

## Fact Schema

Each document's extraction is a JSON array of fact items. Every item has exactly:

- `fact_type`: one of `deadline`, `action`, `contact`, `reference_number`,
  `date`, `fee`, `requirement`, `missing_info`.
- `text`: the fact in plain language (required, nonblank).
- `value`: the normalized detail — the number, amount, phone, email, or address
  — or null.
- `due_date`: `YYYY-MM-DD` or null. Used to sort the deadline checklist.
- `locator`: where in the document it appears, or null.
- `confidence`: `high`, `medium`, or `low`.
- `notes`: optional clarification, or null.

Fact-type meanings: `deadline` is a dated obligation; `action` is an action the
document *requires* of the reader; `contact` is a person, office, phone, email,
or address; `reference_number` is an account/order/policy/claim/case number;
`date` is any other significant date (appointment, statement, effective date);
`fee` is an amount owed or charged; `requirement` is a condition or document the
reader must provide or meet; `missing_info` is information the document expects
but does not provide, or that is unclear.

## Facts vs Next Steps

`extracted_facts.csv` and the derived CSVs contain only what the documents
**state**. `next_steps.md` is **synthesized guidance** — clearly labeled as
generated, kept out of the facts. Never record a suggested action as a fact, and
never present a fact as advice. When the documents disagree or are ambiguous,
record the ambiguity (often as `missing_info`) rather than resolving it silently.

## No Professional Advice

Summarize and organize information. Do not provide legal, medical, or financial
advice. Where a decision plausibly warrants a professional (a lawyer, clinician,
tax adviser, insurer), say so plainly in `next_steps.md` rather than advising.
General, clearly-framed informational context is acceptable when the user asks.

## Privacy

Inputs are referenced by path and SHA-256 and are never copied into the run.
Keep sensitive documents local, avoid unnecessary duplication, and recommend
redacting account numbers, identifiers, and personal details before any output
is shared externally. Hashes recorded at `init` must still match at `build` and
`validate`; a changed source aborts the run.

## CSV Columns

```text
extracted_facts.csv: document_id,source_title,fact_type,text,value,due_date,locator,confidence,notes
deadline_checklist.csv: document_id,source_title,due_date,item,fact_type,locator,confidence,notes
contact_list.csv: document_id,source_title,contact,value,locator,confidence,notes
```

`deadline_checklist.csv` holds `deadline` facts plus `action` facts that carry a
`due_date`, sorted by date with undated rows last. `contact_list.csv` holds
`contact` facts.

## Authored Deliverables

- `admin_summary.md`: what each document is, key facts, missing/unclear info.
- `next_steps.md`: prioritized actions, upcoming deadlines, and where
  professional advice may be warranted.
- `message_draft.md`: a draft email/letter/message, leaving user-specific blanks
  rather than inventing details.
- `comparison_table.md`: options compared on cost, terms, deadlines, notes.
- `call_script.md`: preparation, what to say, information to have ready,
  questions to ask.

## Statuses

Each document gets one disposition: `success` (with a facts array, possibly empty
when the document yields no facts of interest), `needs_review` (unresolved
judgment), `skipped` (intentionally not processed), or `failed` (a processing
error). Every non-success disposition requires a note. Never hide an
unprocessable document behind an empty success.

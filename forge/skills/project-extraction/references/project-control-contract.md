# Project Control Extraction Contract

Load this reference before extracting evidence or reconciling canonical
controls. It is the authoritative behavioral and data contract for the bundled
`project-extraction` workflow.

## Four Layers

1. **Evidence items** are immutable facts from one source revision. Each item
   carries an evidence ID, source and revision IDs, packet ID, quote or source
   wording, locator, interpretation, and confidence.
2. **Control records** reconcile evidence into canonical project records. Each
   control cites evidence IDs and may relate to other control IDs.
3. **`project_status.csv`** is a human-maintained overlay for current owner,
   working status, forecast date, forecast start/end dates, last update, and
   notes. Extraction and refresh may add rows or mark them for review, but must
	   not infer or overwrite human status fields.
4. **The search index** is a disposable derivative of active controls,
   evidence, status, and frozen source passages. Hash-keyed embeddings affect
   retrieval order only; they never change source authority or reconciliation.

All version-2 records may include `teams`, `workstreams`, and
`scope_relation` (`direct`, `dependency`, `shared`, or `full`). These fields
support focused views but do not weaken provenance requirements.

## Evidence Types

Use exactly the item types returned by `next`: objectives, outcomes,
deliverables, milestones, tasks, deadlines, requirements, reporting
requirements, proposal requirements, acceptance criteria, commitments, action
items, decisions, risks, issues, assumptions, dependencies, stakeholders,
metrics, budget facts, and open questions.

Do not collapse neighboring concepts:

- A **deliverable** is a product, service, or result that must be handed over.
- A **task** is work performed to create or manage something.
- A **milestone** is a significant control point, not necessarily an output.
- A **requirement** is a condition that must be satisfied.
- An **acceptance criterion** is how completion or conformity is judged.
- A **reporting requirement** records content, recipient or system, cadence,
  deadline rule, and required evidence when the source provides them.

## Source Authority

Classify each packet as award, funding notice, proposal, scope of work,
contract, amendment, work plan, report, presentation, meeting, interview,
correspondence, budget, or other. Classify each item as required, committed,
proposed, discussed, informational, or unclear.

Document role does not establish legal precedence. When award, amendment,
contract, proposal, meeting, or other sources disagree, preserve each item and
reconcile it as conflicting or superseded only when the source explicitly says
so. Surface unresolved authority as a conflict or open question.

## Evidence Shape

Every item requires `item_type` and `title`. Populate these fields when the
source supports them:

```json
{
  "item_type": "deliverable",
  "title": "Final evaluation report",
  "description": "Source-grounded description",
  "party": "Named responsible party",
  "counterparty": "Recipient or approver",
  "date_text": "30 days after the period of performance ends",
  "date_kind": "relative",
  "date": null,
  "trigger": "period of performance ends",
  "offset_days": 30,
  "recurrence": null,
  "acceptance_criteria": "Explicit criteria or null",
  "evidence_required": "Explicit submission evidence or null",
  "source_status": "Current, draft, amended, or unclear",
  "commitment_level": "required",
  "direct_quotes": ["Short exact source wording"],
  "locator": {"type": "page", "value": 12},
  "interpretation": "explicit",
  "confidence": "high",
  "teams": ["Delivery"],
  "workstreams": ["Evaluation"],
  "scope_relation": "full",
  "start_date": "2026-07-01",
  "end_date": "2026-08-01",
  "duration_days": 32,
  "schedule_basis": "Source states the performance period",
  "notes": null
}
```

Use `interpretation: inferred` only for a conservative classification grounded
in quoted text. Do not infer missing legal obligations, owners, dates, amounts,
or precedence.

## Dates

Set `date_kind` to one of:

- `exact`: normalize an unambiguous date to `YYYY-MM-DD` and preserve the
  original `date_text`.
- `relative`: retain the trigger and integer offset where explicit.
- `recurring`: retain cadence or recurrence wording.
- `conditional`: retain the condition or triggering event.
- `none`: no usable timing statement.

Never calculate a relative or conditional calendar date unless the trigger date
is explicit and the user requests that derived calculation. A missing trigger
date is an open question, not permission to invent a deadline.

Gantt outputs may plot only source-backed exact start/end dates or
human-maintained forecast start/end dates. Relative, recurring, conditional,
and missing dates belong in the unscheduled section. Never model-estimate a
date or duration.

## Reconciliation

Every review packet includes one control type, evidence items, suitable
existing controls, and the required ID prefix. A review file contains:

```json
{
  "reviewPacketId": "review-...",
  "controls": [
    {
      "control_id": "DEL-001",
      "control_type": "deliverable",
      "title": "Final evaluation report",
      "description": "Canonical source-backed description",
      "date_kind": "relative",
      "date": null,
      "trigger": "period of performance ends",
      "offset_days": 30,
      "commitment_level": "required",
      "source_evidence_ids": ["ev-..."],
      "relationships": {
        "parent": [],
        "depends_on": [],
        "satisfies": [],
        "supersedes": [],
        "conflicts_with": []
      }
    }
  ],
  "dispositions": []
}
```

Reference every packet evidence ID exactly once: through one control or one
explicit disposition. `tracked` is represented by control membership;
non-control dispositions are `contextual`, `duplicate`, `superseded`, or
`conflicting`. Duplicate, superseded, and conflicting dispositions require the
related control ID. Nothing may disappear silently.

Preserve an existing control ID when it still represents the same canonical
record. Add new evidence or update fields when a changed source supports it.
Create a new ID when it is genuinely a different record. Relationships must
reference valid current control IDs.

Exact source hashes can establish duplicate packets. Embedding similarity is
advisory and requires model confirmation. Evidence with materially different
owners, dates, acceptance criteria, or obligations must remain separate unless
the reconciliation records an explicit merge justification.

## Scope and Packet Dispositions

Full-project extraction is the default. Focused extraction screens the complete
frozen inventory, then extracts direct matches and relevant dependencies,
shared milestones, decisions, risks, and reporting obligations. Every packet
must end as `extracted`, `screened_no_controls`, `duplicate_source`,
`excluded_by_scope`, `needs_review`, `preempted`, or `failed`.
`excluded_by_scope` is invalid in a full-project run, and generic unprocessed
skips are invalid in every run. Every direct quote must match frozen packet
text exactly.

## Live Inbox and Search

A marked top-level `Inbox/` is a landing area, not project evidence. Intake must
stage a hash-bound batch, finish document-ingest review, verify publication,
archive originals under `Originals/Inbox/`, and publish cleaned Markdown under
`Sources/Inbox/` before project `refresh` discovers it. Never overwrite a
destination collision. Interrupted batches resume from their durable ingest
and Inbox manifests; failed files remain reviewable.

Search results must identify their hit kind, source paths and revisions,
locators, packet IDs when applicable, and related evidence/control IDs. Use
hybrid lexical and embedding ranking when embeddings are reachable and lexical
ranking otherwise. Load a full source only after a ranked passage is
insufficient or ambiguous. A stale index may answer against the last completed
extraction only when the response explicitly reports pending intake or refresh.

## Outputs and Completion

Machine-readable outputs are `evidence_items.csv`, `controls.jsonl`, the eight
project registers, `conflicts_and_gaps.csv`, `source_changes.csv`, and
`project_status.csv`. Version-2 runs also produce `scope_manifest.csv`,
`gantt.csv`, `inference_schedule.jsonl`, `run_metrics.json`,
`search_index.jsonl`, and `search_index_meta.json`; live runs also maintain
`inbox_manifest.json` and `inbox_events.jsonl`. Human outputs are the six
project briefs, `gantt.md`, and `gantt.html`; add
`proposal_checklist.md` only when a funding notice or proposal is present.

A run is complete only when all extraction packets and review packets are
explicitly dispositioned, every control relationship resolves, every Markdown
placeholder is authored, source hashes match, and `validate` succeeds.

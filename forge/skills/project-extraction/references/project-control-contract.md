# Project Control Extraction Contract

Load this reference before extracting evidence or reconciling canonical
controls. It is the authoritative behavioral and data contract for the bundled
`project-extraction` workflow.

## Three Layers

1. **Evidence items** are immutable facts from one source revision. Each item
   carries an evidence ID, source and revision IDs, packet ID, quote or source
   wording, locator, interpretation, and confidence.
2. **Control records** reconcile evidence into canonical project records. Each
   control cites evidence IDs and may relate to other control IDs.
3. **`project_status.csv`** is a human-maintained overlay for current owner,
   working status, forecast date, last update, and notes. Extraction and refresh
   may add rows or mark them for review, but must not infer or overwrite human
   status fields.

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

## Outputs and Completion

Machine-readable outputs are `evidence_items.csv`, `controls.jsonl`, the eight
project registers, `conflicts_and_gaps.csv`, `source_changes.csv`, and
`project_status.csv`. Human outputs are the six project briefs; add
`proposal_checklist.md` only when a funding notice or proposal is present.

A run is complete only when all extraction packets and review packets are
explicitly dispositioned, every control relationship resolves, every Markdown
placeholder is authored, source hashes match, and `validate` succeeds.

#!/usr/bin/env python3
"""The blind comparison bundle, and the merge that unblinds it.

Some of what the suite measures has no gate. Whether a cleaned transcript still
sounds like the person who spoke it, whether a summary quietly asserts something
the source did not — those need a reader. This builds what that reader sees.

Outputs are labelled A/B/C with the model names held back in a separate key
file, because a grader told which model produced which text grades the name.
The shuffle is seeded, so re-running `judge` does not silently relabel a bundle
someone is halfway through grading.
"""

import json

import harness

JUDGE_DIR = harness.RESULTS / "judge"
KEY_PATH = JUDGE_DIR / "key.json"

LABELS = "ABCDEFGH"

RUBRIC = """\
Score every output on four axes, 1-5, and say what decided it. Judge only the
text in front of you: the outputs are labelled A/B/C and which model wrote which
is deliberately withheld.

- **voice** (1-5) - does this still read as the source's own wording and register?
  5 = the speaker's phrasing survives, only filler and repair are gone.
  3 = recognisably the same content in someone else's register.
  1 = rewritten into generic prose, or a summary where prose was asked for.
- **faithfulness** (1-5) - does it assert anything the source did not?
  5 = every claim traceable to the source. 3 = one soft overstatement.
  1 = invented specifics: a name, a number, a date, a cause.
- **coverage** (1-5) - is anything material gone?
  5 = nothing of substance dropped. 3 = a minor point lost.
  1 = a whole thread of the source missing.
- **usability** (1-5) - would this go into the vault as it stands?
  5 = yes. 3 = yes after a small edit. 1 = start over.

Where a **reference** is shown, it is the output the existing pipeline already
produced and Ellie kept. It is a strong signal, not a ceiling: an output that
beats it should score above it.

Write verdicts.json as:

```json
{"verdicts": [
  {"case": "<case>", "item": "<item>", "label": "A",
   "scores": {"voice": 4, "faithfulness": 5, "coverage": 4, "usability": 4},
   "note": "one sentence on what decided it"}
]}
```
"""

AXES = ("voice", "faithfulness", "coverage", "usability")


def _judged_cases(model_ids, only):
    """Cases that produced judgeable output for every model asked for."""
    cases = []
    for case_id in harness.case_ids():
        if only and case_id not in only:
            continue
        module = harness.load_case(case_id)
        if not getattr(module, "JUDGE", False):
            continue
        if all(case_id in harness.read_results(model_id) for model_id in model_ids):
            cases.append(case_id)
    return cases


def _context(case_id, item_id):
    """A case's optional source and reference text for one item."""
    module = harness.load_case(case_id)
    provider = getattr(module, "judge_context", None)
    if provider is None:
        return {}
    try:
        return provider(item_id) or {}
    except Exception as error:
        return {"error": f"judge_context raised {type(error).__name__}: {error}"}


def _as_text(item):
    """What the grader reads.

    A failed item still had something to say, and it usually matters: the 9B's
    summaries were good prose that simply was not wrapped in JSON, and a bundle
    that showed "(no output)" for those would have thrown away the finding.
    """
    output = item.get("output")
    if output is None:
        raw = item.get("raw")
        if raw:
            return f"(the scorer could not read this as a valid result — raw reply follows)\n\n{raw}"
        return "(no output — the item failed before producing one)"
    if isinstance(output, str):
        return output
    return json.dumps(output, indent=2, ensure_ascii=False)


def build(model_ids, only=None, seed=20260803):
    JUDGE_DIR.mkdir(parents=True, exist_ok=True)
    cases = _judged_cases(model_ids, only)
    if not cases:
        raise harness.EvalError(
            "no judged case has results for every model named. Run the suite for each model first."
        )

    key = {}
    comparisons = 0
    written = []
    for case_id in cases:
        documents = {model_id: harness.read_results(model_id)[case_id] for model_id in model_ids}
        # Attempt 1 only: repeats measure variance, and grading the same model
        # three times under three labels would just dilute the comparison.
        by_item = {}
        for model_id, document in documents.items():
            for item in document["items"]:
                if item["attempt"] != 1:
                    continue
                by_item.setdefault(item["id"], {})[model_id] = item

        lines = [f"# Judge bundle — {case_id}", "", RUBRIC, "", "---", ""]
        for item_id, per_model in by_item.items():
            present = [model_id for model_id in model_ids if model_id in per_model]
            if len(present) < 2:
                continue
            comparisons += 1
            shuffled = harness.stable_shuffle(present, f"{seed}:{case_id}:{item_id}")
            assignment = {LABELS[index]: model_id for index, model_id in enumerate(shuffled)}
            key[f"{case_id}/{item_id}"] = assignment

            context = _context(case_id, item_id)
            lines.append(f"## {item_id}")
            lines.append("")
            if context.get("instruction"):
                lines.extend([f"**Asked for:** {context['instruction']}", ""])
            if context.get("source"):
                lines.extend(["<details><summary>Source</summary>", "", "```text", context["source"], "```", "", "</details>", ""])
            if context.get("reference"):
                lines.extend(["**Reference** (what the pipeline produced and Ellie kept):", "", "```text", context["reference"], "```", ""])
            for label in LABELS[: len(shuffled)]:
                item = per_model[assignment[label]]
                lines.extend([f"### {item_id} — {label}", ""])
                if item["notes"]:
                    # Gate findings travel with the output. A grader who cannot
                    # see that a name was invented will score the prose highly.
                    lines.extend(["> Deterministic checks said:", ""])
                    lines.extend(f"> - {note}" for note in item["notes"][:8])
                    lines.append("")
                lines.extend(["```text", _as_text(item), "```", ""])
            lines.extend(["---", ""])

        path = JUDGE_DIR / f"{case_id}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        written.append(path)

    KEY_PATH.write_text(json.dumps({"seed": seed, "assignments": key}, indent=2) + "\n", encoding="utf-8")
    return {"files": len(written), "comparisons": comparisons, "cases": cases}


def score(verdicts_path):
    """Unblind graded verdicts and roll them up per model."""
    if not KEY_PATH.exists():
        raise harness.EvalError(f"no key at {KEY_PATH}; run `judge` before `score`")
    key = harness.load_json(KEY_PATH)["assignments"]
    payload = harness.load_json(verdicts_path)
    verdicts = payload.get("verdicts") if isinstance(payload, dict) else payload
    if not isinstance(verdicts, list):
        raise harness.EvalError("verdicts file must be a list, or an object with a 'verdicts' list")

    unblinded = []
    unmatched = []
    for verdict in verdicts:
        lookup = f"{verdict.get('case')}/{verdict.get('item')}"
        assignment = key.get(lookup, {})
        model_id = assignment.get(verdict.get("label"))
        if not model_id:
            unmatched.append(f"{lookup} label {verdict.get('label')!r}")
            continue
        unblinded.append({**verdict, "model": model_id})

    rollup = {}
    for verdict in unblinded:
        per_model = rollup.setdefault(verdict["model"], {axis: [] for axis in AXES})
        for axis in AXES:
            value = verdict.get("scores", {}).get(axis)
            if isinstance(value, (int, float)):
                per_model[axis].append(value)

    summary = {}
    for model_id, axes in sorted(rollup.items()):
        summary[model_id] = {
            axis: round(sum(values) / len(values), 2) if values else None for axis, values in axes.items()
        }
        graded = [len(values) for values in axes.values()]
        summary[model_id]["graded"] = max(graded) if graded else 0

    document = {"summary": summary, "verdicts": unblinded, "unmatched": unmatched}
    path = JUDGE_DIR / "scored.json"
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if unmatched:
        document["summary"]["_unmatched"] = unmatched
    return {"summary": document["summary"], "path": path}


CALIBRATION_AXIS_TOLERANCE = 0.5


def _prior_gradings():
    """Archived grading passes, newest first.

    A grading pass grades the models that run asked about, and writing
    `scored.json` replaces the file rather than adding to it. So the overnight
    MoE/9B pass graded `chat-27b` and the three new profiles, and the previous
    pass's `think-27b` and `task-4b` grades moved to an archive folder — leaving
    two models that no longer had a judge mean.

    That is not a cosmetic loss. `_recommendation` holds any judged case where a
    model has no grade ("gates hold up, but the quality was never graded"), so
    `think-27b` stopped being a routing candidate everywhere, and
    `routing_table` quietly crowned the runner-up. On `transcript-cleanup-memo`
    that meant a model scoring 0.875 was recorded as what the report supports
    over one scoring 1.000 with no silent failures — because of missing grades,
    not because of anything measured.
    """
    return sorted(JUDGE_DIR.glob("_prior-grading-*/scored.json"), reverse=True)


def comparable(current_summary, prior_summary):
    """Whether two grading passes can be read together, on their shared models.

    The criterion is `merge-verdicts.calibrate`'s and the reasoning is its: a
    model graded by both passes differs only by grader, so the gap between its
    two sets of axis means is the calibration between the passes. Within
    tolerance on every shared model and axis, the passes are directly
    comparable; past it they are two different rulers and must not be mixed.
    """
    shared = [
        model_id
        for model_id in prior_summary
        if not model_id.startswith("_") and isinstance(current_summary.get(model_id), dict)
    ]
    if not shared:
        return False
    for model_id in shared:
        for axis in AXES:
            now, then = current_summary[model_id].get(axis), prior_summary[model_id].get(axis)
            if not (isinstance(now, (int, float)) and isinstance(then, (int, float))):
                continue
            if abs(now - then) > CALIBRATION_AXIS_TOLERANCE:
                return False
    return True


def scored():
    """The current grading, with comparable archived passes folded in.

    Models the current pass graded always win — a fresh grade supersedes an old
    one for the same model. Archived grades are only consulted for models the
    current pass did not cover at all, and only from passes that calibrate
    against it. Anything folded in is recorded under `_mergedFrom` so a reader
    can see that a mean did not come from the latest run.
    """
    path = JUDGE_DIR / "scored.json"
    if not path.exists():
        return None
    document = harness.load_json(path)
    summary = document.get("summary")
    if not isinstance(summary, dict):
        return document

    merged_from = {}
    for prior_path in _prior_gradings():
        prior = harness.load_json(prior_path) or {}
        prior_summary = prior.get("summary")
        if not isinstance(prior_summary, dict) or not comparable(summary, prior_summary):
            continue
        for model_id, axes in prior_summary.items():
            if model_id.startswith("_") or model_id in summary:
                continue
            summary[model_id] = axes
            merged_from[model_id] = prior_path.parent.name
    if merged_from:
        summary["_mergedFrom"] = merged_from
    return document

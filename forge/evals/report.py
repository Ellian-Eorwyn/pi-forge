#!/usr/bin/env python3
"""The comparison, and the recommendation that follows from it.

The report answers the question the suite was built for: which stages can move
to another model. A stage clears only when every deterministic gate passed and,
where a grader looked, the quality did not fall away from the baseline. Anything
else is reported as it is — "did not clear" with the reason, never a number
massaged into a pass.
"""

import statistics

import judge as judging

import harness

# How far below the baseline a judged mean may sit and still count as holding up.
# One point on a five-point scale: "as good", or "worse in a way a reader noticed
# but would still accept". Below that a human is being asked to fix output.
JUDGE_TOLERANCE = 1.0

# And how far its gate pass rate may sit below the baseline's. The comparison is
# against the baseline rather than against perfection on purpose: several
# fixtures are notes where Ellie's own filing is one defensible answer among
# two, and demanding a clean sweep would let those items veto every case they
# appear in. What matters is whether the candidate is worse than what runs today.
GATE_TOLERANCE = 0.15

# And an absolute floor, because "better than the baseline" is not the same as
# "good enough to use". An early version of this report recommended routing a
# case the candidate passed 1 item of 4 on, purely because the baseline passed
# 0 of 4. Below this, the honest answer is that neither model does the job.
GATE_FLOOR = 0.6


# A graded faithfulness at or below this means the output asserted something its
# source did not. Three is "one soft overstatement" on the rubric; below that is
# invention. An item that scores here while passing every deterministic check is
# the failure the pipeline cannot see.
SILENT_FAITHFULNESS = 3

# Below this many fixtures a case cannot carry a routing verdict: one flip moves
# it across any threshold. Nine of the twelve cases started under it.
MIN_ITEMS_FOR_VERDICT = 8


def severity_for(documents, graded, model_id):
    """Classify every item of one model as gated, silent, unknown, or clean.

    Joins what the run recorded (which items a deterministic check caught) with
    what a grader found (which items passed every check and were wrong anyway).
    Neither half can produce this alone.
    """
    verdicts = {}
    for verdict in (graded or {}).get("verdicts", []):
        if verdict.get("model") != model_id:
            continue
        scores = verdict.get("scores") or {}
        if isinstance(scores.get("faithfulness"), (int, float)):
            key = (verdict.get("case"), verdict.get("item"))
            verdicts[key] = min(verdicts.get(key, 5), scores["faithfulness"])

    counts = {"gated": 0, "silent": 0, "unknown": 0, "clean": 0}
    detail = {"silent": [], "unknown": []}
    by_case = {}
    for case_id, document in documents.items():
        per_case = by_case.setdefault(case_id, {"gated": 0, "silent": 0, "unknown": 0, "clean": 0})
        for entry in harness.stability_by_item(document["items"]).values():
            if entry["severity"] == "gated":
                counts["gated"] += 1
                per_case["gated"] += 1
                continue
            faithfulness = verdicts.get((case_id, entry["id"]))
            if faithfulness is not None:
                bucket = "silent" if faithfulness <= SILENT_FAITHFULNESS else "clean"
                if bucket == "silent":
                    detail["silent"].append(f"{case_id}/{entry['id']} (faithfulness {faithfulness})")
            elif document.get("judged"):
                # Prose that no one read. Not a failure, but not a clean bill
                # either, and saying "clean" here would be the false confidence
                # this whole split exists to prevent.
                bucket = "unknown"
                detail["unknown"].append(f"{case_id}/{entry['id']}")
            else:
                bucket = "clean"
            counts[bucket] += 1
            per_case[bucket] += 1
    return counts, detail, by_case


def _fmt(value, digits=2):
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _load(model_ids):
    return {model_id: harness.read_results(model_id) for model_id in model_ids}


def render(model_ids, baseline=None):
    loaded = _load(model_ids)
    present = [model_id for model_id in model_ids if loaded.get(model_id)]
    missing = [model_id for model_id in model_ids if not loaded.get(model_id)]
    if not present:
        return "No results on disk. Run `run.py run --model <id>` first.\n"
    if baseline not in present:
        baseline = present[0]

    cases = sorted({case_id for documents in loaded.values() for case_id in documents})
    graded = judging.scored()
    lines = ["# Model comparison", ""]
    # The label lives on any of the baseline's own documents; it may not have run
    # the same cases as everyone else.
    label = next((document["modelLabel"] for document in loaded[baseline].values()), baseline)
    lines.append(f"Baseline: **{label}** (`{baseline}`)")
    if missing:
        lines.append("")
        lines.append(f"No results for: {', '.join(f'`{model_id}`' for model_id in missing)}")
    lines.append("")

    # --- gates -------------------------------------------------------------
    lines.extend(["## Deterministic gates", "", "Pass rate is items where every gate the case runs came back clean.", ""])
    header = "| Case | Dimension | " + " | ".join(f"`{model_id}`" for model_id in present) + " |"
    lines.append(header)
    lines.append("| --- | --- | " + " | ".join("---" for _ in present) + " |")
    for case_id in cases:
        dimension = next(
            (loaded[m][case_id]["dimension"] for m in present if case_id in loaded[m]),
            "—",
        )
        cells = []
        for model_id in present:
            document = loaded[model_id].get(case_id)
            if not document:
                cells.append("—")
                continue
            summary = document["summary"]
            cells.append(f"{summary['passed']}/{summary['items']}")
        lines.append(f"| `{case_id}` | {dimension} | " + " | ".join(cells) + " |")
    lines.append("")

    # --- per-case metrics --------------------------------------------------
    lines.extend(["## Metrics", ""])
    for case_id in cases:
        names = sorted(
            {
                name
                for model_id in present
                if case_id in loaded[model_id]
                for name in loaded[model_id][case_id]["summary"].get("metrics", {})
            }
        )
        if not names:
            continue
        lines.append(f"### `{case_id}`")
        lines.append("")
        lines.append("| Metric | " + " | ".join(f"`{model_id}`" for model_id in present) + " |")
        lines.append("| --- | " + " | ".join("---" for _ in present) + " |")
        for name in names:
            cells = []
            for model_id in present:
                document = loaded[model_id].get(case_id)
                entry = document["summary"]["metrics"].get(name) if document else None
                cells.append(_fmt(entry["mean"]) if entry else "—")
            lines.append(f"| {name} | " + " | ".join(cells) + " |")
        lines.append("")

    # --- what was actually running -----------------------------------------
    lines.extend(
        [
            "## What was measured",
            "",
            "Read from the endpoint at run time, not from configuration. Thinking is",
            "reported as observed rather than as requested, because the two have disagreed.",
            "",
            "| Model | Weights | Params | Quant | Thinking requested | Thinking observed |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for model_id in present:
        document = next(iter(loaded[model_id].values()))
        served = document.get("served") or {}
        requested = document.get("requested") or {}
        kwargs = requested.get("chatTemplateKwargs") or {}
        asked = "—"
        if "enable_thinking" in kwargs:
            asked = "on" if kwargs["enable_thinking"] else "off"
        # Aggregated across every item this model ran, not rolled up from
        # per-case medians — a median of medians is not a median, and on
        # two-item cases it reports the maximum.
        hidden = [
            candidate["telemetry"].get("hiddenTokens") or 0
            for document in loaded[model_id].values()
            for candidate in document["items"]
        ]
        reasoned = sum(
            1
            for document in loaded[model_id].values()
            for candidate in document["items"]
            if candidate["telemetry"].get("reasoned")
        )
        seen = (
            f"{reasoned}/{len(hidden)} items, median {round(statistics.median(hidden))} hidden tok"
            if hidden
            else "not recorded"
        )
        lines.append(
            f"| `{model_id}` | {served.get('modelFile', '—')} | {_fmt(served.get('paramsB'), 2)} | "
            f"{served.get('quant', '—')} | {asked} | {seen} |"
        )
    lines.append("")

    # --- failure severity --------------------------------------------------
    lines.extend(
        [
            "## How the failures fail",
            "",
            "The number that should decide a handoff is **silent**: output that passed every",
            "deterministic check and was still graded unfaithful. A gated failure is the",
            "pipeline working. A silent one is the pipeline not seeing anything wrong.",
            "`unknown` is prose in a judged case that nobody has graded — not a clean bill.",
            "",
            "| Model | Gated | Silent | Unknown | Clean |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    silent_detail = {}
    for model_id in present:
        counts, detail, _by_case = severity_for(loaded[model_id], graded, model_id)
        silent_detail[model_id] = detail["silent"]
        lines.append(
            f"| `{model_id}` | {counts['gated']} | **{counts['silent']}** | {counts['unknown']} | {counts['clean']} |"
        )
    lines.append("")
    for model_id, entries in silent_detail.items():
        if entries:
            lines.append(f"Silent failures on `{model_id}`:")
            lines.extend(f"- {entry}" for entry in entries)
            lines.append("")

    # --- cost --------------------------------------------------------------
    lines.extend(
        [
            "## Cost",
            "",
            "Wall time is measured on an otherwise idle GPU, so it is a latency figure, not",
            "throughput. Tokens per item is the one that scales: a model generating half again",
            "as much finishes a single item faster and a 500-note batch slower. A `task` batch",
            "also pays roughly 6s of router swap whenever it alternates with `embed`.",
            "",
        ]
    )
    lines.append("| Model | Items | Generated tokens | Tokens/item | Items/min | Hidden reasoning | Wall time |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for model_id in present:
        documents = loaded[model_id]
        items = sum(document["summary"]["items"] for document in documents.values())
        generated = sum(document["summary"]["generatedTokens"] for document in documents.values())
        hidden = sum(document["summary"]["hiddenTokens"] for document in documents.values())
        elapsed = sum(document["elapsedMs"] for document in documents.values())
        attempts = sum(document["summary"].get("attempts", document["summary"]["items"]) for document in documents.values())
        per_item = round(generated / attempts) if attempts else 0
        per_minute = round(attempts / (elapsed / 60000), 1) if elapsed else 0
        lines.append(
            f"| `{model_id}` | {items} | {generated:,} | {per_item:,} | {per_minute} | {hidden:,} | {elapsed / 1000:.0f}s |"
        )
    lines.append("")

    # --- judged ------------------------------------------------------------
    if graded and graded.get("summary"):
        lines.extend(["## Graded quality", "", "Blind means, 1-5.", ""])
        lines.append("| Model | Voice | Faithfulness | Coverage | Usability | Graded |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for model_id, axes in graded["summary"].items():
            if model_id.startswith("_"):
                continue
            lines.append(
                f"| `{model_id}` | {_fmt(axes.get('voice'))} | {_fmt(axes.get('faithfulness'))} | "
                f"{_fmt(axes.get('coverage'))} | {_fmt(axes.get('usability'))} | {axes.get('graded', 0)} |"
            )
        lines.append("")
    else:
        lines.extend(
            [
                "## Graded quality",
                "",
                "Not graded. The judged cases measure what no gate can settle — voice, faithfulness,",
                "whether a summary is any good — so a routing decision made without them rests only on",
                "the parts a checker could see. Run `judge`, grade the bundle, then `score`.",
                "",
            ]
        )

    lines.extend(_recommendation(loaded, present, baseline, graded))
    return "\n".join(lines) + "\n"


def _recommendation(loaded, present, baseline, graded):
    lines = ["## Routing recommendation", ""]
    others = [model_id for model_id in present if model_id != baseline]
    if not others:
        return lines + [f"Only `{baseline}` has results, so there is nothing to compare it against.", ""]

    judge_means = {}
    if graded and graded.get("summary"):
        for model_id, axes in graded["summary"].items():
            values = [axes.get(axis) for axis in judging.AXES if isinstance(axes.get(axis), (int, float))]
            if values:
                judge_means[model_id] = sum(values) / len(values)

    for model_id in others:
        lines.append(f"### `{model_id}`")
        lines.append("")
        _counts, _detail, severity_by_case = severity_for(loaded[model_id], graded, model_id)
        cleared, held, blocked = [], [], []
        for case_id, document in sorted(loaded[model_id].items()):
            summary = document["summary"]
            base = loaded[baseline].get(case_id)
            if not base or not summary["items"]:
                held.append(f"`{case_id}` — no baseline to compare against")
                continue
            rate = summary["passRate"]
            baseline_rate = base["summary"]["passRate"]
            delta = rate - baseline_rate
            shape = f"{summary['passed']}/{summary['items']} vs {base['summary']['passed']}/{base['summary']['items']}"

            # Three refusals that come before any comparison of numbers, because
            # each one means the numbers cannot bear the weight.
            unstable = summary.get("stability", {}).get("unstableIds") or []
            if unstable:
                held.append(
                    f"`{case_id}` — {len(unstable)} item(s) flipped between attempts "
                    f"({', '.join(unstable[:3])}); the result is not repeatable"
                )
                continue
            if summary["items"] < MIN_ITEMS_FOR_VERDICT:
                held.append(
                    f"`{case_id}` — {shape}, but only {summary['items']} fixtures; "
                    f"one flip moves it across any threshold. Indicative only."
                )
                continue
            silent = (severity_by_case or {}).get(case_id, {}).get("silent", 0)
            if silent:
                blocked.append(
                    f"`{case_id}` — {shape} on the gates, but {silent} item(s) passed every check "
                    f"and were still graded unfaithful. Nothing downstream would catch that."
                )
                continue
            # Falling behind the baseline is checked first, and deliberately.
            # Reading the floor first swallowed a real gap: on one case the
            # candidate returned nothing at all where the baseline covered 13 of
            # 15 item types, and "neither model does this well" is the wrong
            # summary of that.
            if delta < -GATE_TOLERANCE:
                blocked.append(f"`{case_id}` — {shape} items clean, {abs(delta) * 100:.0f}% below the baseline")
                continue
            if rate < GATE_FLOOR and baseline_rate < GATE_FLOOR:
                held.append(f"`{case_id}` — {shape}; neither model does this well enough to route anywhere")
                continue
            if rate < GATE_FLOOR:
                blocked.append(
                    f"`{case_id}` — {shape}; better than the baseline but only {rate:.0%} of items came back clean"
                )
                continue
            # Checked per model, not globally. A stale `scored.json` from an
            # earlier set of runs made this read as "quality held" for a model
            # that had never been graded at all — the precise false confidence
            # the judged/ungraded split exists to prevent.
            if document["judged"] and (model_id not in judge_means or baseline not in judge_means):
                held.append(f"`{case_id}` — gates hold up ({shape}), but the quality was never graded")
                continue
            if document["judged"]:
                gap = judge_means[baseline] - judge_means[model_id]
                if gap > JUDGE_TOLERANCE:
                    blocked.append(f"`{case_id}` — gates hold up ({shape}), but graded {gap:.1f} below the baseline")
                    continue
            cleared.append(f"`{case_id}` — {shape}")

        if cleared:
            lines.append("**Safe to route here** (every gate clean, quality held):")
            lines.extend(f"- {entry}" for entry in cleared)
            lines.append("")
        if held:
            lines.append("**Not decided:**")
            lines.extend(f"- {entry}" for entry in held)
            lines.append("")
        if blocked:
            lines.append("**Keep on the baseline:**")
            lines.extend(f"- {entry}" for entry in blocked)
            lines.append("")
        if not (cleared or held or blocked):
            lines.append("No results.")
            lines.append("")
    return lines

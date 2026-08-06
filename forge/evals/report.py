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


def render(model_ids, baseline=None, prefer="capability"):
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
            # "n/a" and "—" say different things and both differ from "0/8".
            # A dash is "not measured yet", n/a is "this model cannot be asked
            # this question", and a zero is a model that was asked and failed.
            if document.get("notApplicable"):
                cells.append("n/a")
                continue
            summary = document["summary"]
            cells.append(f"{summary['passed']}/{summary['items']}")
        lines.append(f"| `{case_id}` | {dimension} | " + " | ".join(cells) + " |")
    lines.append("")
    skipped = [
        (model_id, case_id, loaded[model_id][case_id]["notApplicable"])
        for model_id in present
        for case_id in cases
        if loaded[model_id].get(case_id, {}).get("notApplicable")
    ]
    if skipped:
        lines.append("`n/a` — not run, and not a failure:")
        lines.append("")
        for model_id, case_id, why in skipped:
            lines.append(f"- `{model_id}` / `{case_id}` — {why}")
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
                entry = (document["summary"].get("metrics") or {}).get(name) if document else None
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
            "**Read the per-attempt column, not wall time.** `--stabilize` repeats the cases a",
            "single item could have decided, and it repeats *different* cases for each model, so",
            "the wall times below cover different amounts of work and are not comparable to each",
            "other. Attempts is what actually ran; fixtures is how many distinct questions were",
            "asked. Dividing wall time by fixtures across models is how a model that repeated",
            "twice as often reads as half as fast.",
            "",
            "Wall time is measured on an otherwise idle GPU, so it is a latency figure, not",
            "throughput. Tokens per attempt is the one that scales: a model generating half again",
            "as much finishes a single item faster and a 500-note batch slower. A `task` batch",
            "also pays roughly 6s of router swap whenever it alternates with `embed`.",
            "",
        ]
    )
    lines.append(
        "| Model | Fixtures | Attempts | Generated tokens | Tokens/attempt | **s/attempt** | "
        "Items/min | Prefill tok/s | Decode tok/s | Hidden reasoning | Wall time |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for model_id in present:
        # A skipped case contributes no items and no time. Including its empty
        # summary in these sums would be harmless; including it in the rates
        # would not, which is why they are computed from what actually ran.
        documents = [d for d in loaded[model_id].values() if not d.get("notApplicable")]
        items = sum(document["summary"]["items"] for document in documents)
        generated = sum(document["summary"]["generatedTokens"] for document in documents)
        hidden = sum(document["summary"]["hiddenTokens"] for document in documents)
        elapsed = sum(document["elapsedMs"] for document in documents)
        attempts = sum(document["summary"].get("attempts", document["summary"]["items"]) for document in documents)
        per_item = round(generated / attempts) if attempts else 0
        per_minute = round(attempts / (elapsed / 60000), 1) if elapsed else 0
        prefill = [
            d["summary"]["speed"]["prefillTokensPerSecond"]
            for d in documents
            if (d["summary"].get("speed") or {}).get("prefillTokensPerSecond")
        ]
        decode = [
            d["summary"]["speed"]["decodeTokensPerSecond"]
            for d in documents
            if (d["summary"].get("speed") or {}).get("decodeTokensPerSecond")
        ]
        seconds_per_attempt = (elapsed / 1000) / attempts if attempts else 0
        lines.append(
            f"| `{model_id}` | {items} | {attempts} | {generated:,} | {per_item:,} | "
            f"**{seconds_per_attempt:.1f}** | {per_minute} | "
            f"{_fmt(statistics.median(prefill), 0) if prefill else '—'} | "
            f"{_fmt(statistics.median(decode), 1) if decode else '—'} | "
            f"{hidden:,} | {elapsed / 1000:.0f}s |"
        )
    lines.append("")

    # --- judged ------------------------------------------------------------
    # Only when a case in *this* comparison is actually judged. `scored.json`
    # outlives the runs it describes, and printing an old grade beside new
    # numbers reads as if the new ones had been graded.
    judged_here = any(document.get("judged") for documents in loaded.values() for document in documents.values())
    if graded and graded.get("summary") and judged_here:
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
                *(
                    [
                        "No judged case ran in this comparison, so there is nothing for a grader to have",
                        "read. That is expected for a gates-only selection and is not a gap.",
                    ]
                    if not judged_here
                    else [
                        "Not graded. The judged cases measure what no gate can settle — voice, faithfulness,",
                        "whether a summary is any good — so a routing decision made without them rests only on",
                        "the parts a checker could see. Run `judge`, grade the bundle, then `score`.",
                    ]
                ),
                "",
            ]
        )

    cleared_by_model = {}
    recommendation = _recommendation(loaded, present, baseline, graded, cleared_by_model)
    # The baseline is a candidate for its own stages. Filled after the loop above
    # because that loop only ever walks the *other* models.
    cleared_by_model[baseline] = baseline_cleared(loaded, baseline, graded)
    lines.extend(_stage_routing(loaded, present, baseline, cleared_by_model, graded, prefer=prefer))
    lines.extend(recommendation)
    return "\n".join(lines) + "\n"


def routing_table(model_ids, baseline=None, prefer="capability"):
    """The stage table as data, in the shape ``forge_routing`` consumes.

    The report is prose and the deployment is a dict, and nothing kept them
    honest with each other. This is the join: `tests/test_evals.py` asserts the
    committed routing table only sends a stage somewhere the latest report
    supports, so a routing decision cannot outlive the evidence for it.
    """
    loaded = _load(model_ids)
    present = [model_id for model_id in model_ids if loaded.get(model_id)]
    if not present:
        return {}
    if baseline not in present:
        baseline = present[0]
    graded = judging.scored()
    cleared_by_model = {}
    _recommendation(loaded, present, baseline, graded, cleared_by_model)
    cleared_by_model[baseline] = baseline_cleared(loaded, baseline, graded)

    registry = harness.models()
    silent_by_case = {model_id: severity_for(loaded[model_id], graded, model_id)[2] for model_id in present}
    table = {}
    for case_id in sorted({case for documents in loaded.values() for case in documents}):
        candidates = [
            model_id
            for model_id in present
            if case_id in cleared_by_model.get(model_id, set()) and case_id in loaded[model_id]
        ]
        if not candidates:
            continue

        def rank(model_id, case_id=case_id):
            silent = (silent_by_case[model_id].get(case_id) or {}).get("silent", 0)
            speed = _median_ms(loaded, model_id, case_id) or float("inf")
            if prefer == "size":
                return (registry.get(model_id, {}).get("sizeGiB") or float("inf"), speed)
            return (-loaded[model_id][case_id]["summary"]["passRate"], silent, speed)

        winner = min(candidates, key=rank)
        document = loaded[winner][case_id]
        entry = {
            "model": winner,
            "tier": registry.get(winner, {}).get("tier"),
            "skill": document.get("skill"),
            "dimension": document["dimension"],
            "passRate": round(document["summary"]["passRate"], 4),
            "isBaseline": winner == baseline,
        }
        # A model can be kept out of `candidates` for reasons that are not about
        # the work: the loudest is having no grade, which `_recommendation`
        # holds on rather than blocks. When such a model scored strictly better
        # on the gates, crowning the runner-up records a preference the evidence
        # never expressed. This happened: a partial grading pass left
        # `think-27b` ungraded and `transcript-cleanup-memo` was recorded as a
        # `small`-tier case at 0.875 over an ungraded 1.000. `judge.scored` now
        # folds comparable archived passes back in, which fixes that instance;
        # this names the shape so the next one is visible rather than silent.
        # `.get` throughout: a case a model could not run carries a summary with
        # no passRate, and that is not a better result.
        won_at = document["summary"]["passRate"]
        outscored = [
            model_id
            for model_id in present
            if model_id not in candidates
            and isinstance((loaded[model_id].get(case_id, {}).get("summary") or {}).get("passRate"), (int, float))
            and loaded[model_id][case_id]["summary"]["passRate"] > won_at
        ]
        if outscored:
            entry["betterButNotCandidates"] = sorted(outscored)
        table[case_id] = entry
    return {"baseline": baseline, "prefer": prefer, "models": present, "cases": table}


def baseline_cleared(loaded, baseline, graded):
    """Cases the baseline does well enough to keep, judged on its own terms.

    ``_recommendation`` only ever evaluates candidates *against* the baseline, so
    nothing recorded whether the baseline cleared its own cases, and it was never
    a candidate in the stage table. On this deployment that was not cosmetic:
    `chat` and `think` are one set of weights behind a proxy and therefore the
    same size, so every tie went to `think` on sort order alone. The table
    recommended `abstention-grounded` (12/12 against the baseline's 12/12) at
    6.9x slower, and `lcr-48k` (10/10 against 10/10) at 10.5x slower — pure cost
    for no measured gain.

    The criteria are the absolute half of the ones applied to a candidate: the
    result has to be repeatable, carry enough fixtures to mean anything, be free
    of silent failures, and clear the floor. There is no relative half, because
    there is nothing above the baseline to compare it against.
    """
    _counts, _detail, severity_by_case = severity_for(loaded[baseline], graded, baseline)
    cleared = set()
    for case_id, document in loaded[baseline].items():
        if document.get("notApplicable"):
            continue
        summary = document["summary"]
        if summary["items"] < MIN_ITEMS_FOR_VERDICT:
            continue
        if summary.get("stability", {}).get("unstableIds"):
            continue
        if (severity_by_case.get(case_id) or {}).get("silent"):
            continue
        if summary["passRate"] < GATE_FLOOR:
            continue
        cleared.add(case_id)
    return cleared


def _median_ms(loaded, model_id, case_id):
    return ((loaded.get(model_id, {}).get(case_id) or {}).get("summary", {}).get("speed") or {}).get("msPerItemMedian")


def _stage_routing(loaded, present, baseline, cleared_by_model, graded, prefer="capability"):
    """One row per pi-forge stage: the model that should run it, and what that costs.

    The old premise was "the smallest model that cleared", which is the wrong
    question on a deployment where the two 27B profiles are one set of weights
    behind a proxy — "smaller" does not describe the choice between them at all.

    The ordering is the routing rule read in both directions: where a model is
    better take it even when it is slower, and where capability ties take the
    faster one. So candidates rank by gate pass rate, then by carrying no silent
    failures, and only then by speed. ``--prefer size`` restores the old ordering
    for the genuinely smaller tier, where weights and VRAM do differ.
    """
    lines = [
        "## Stage routing",
        "",
        f"The model that should run each stage, ranked by {'size' if prefer == 'size' else 'capability'},",
        "and what running it there costs.",
        "",
        "Ratios carry the absolute per-item difference beside them, because a ratio on a five-second",
        'base makes a cheap upgrade look expensive: "10.5x slower" and "+15s per item" are the same',
        "fact and lead to opposite decisions.",
        "",
        "| Stage | Case | Runs on | vs baseline |",
        "| --- | --- | --- | --- |",
    ]
    registry = harness.models()
    silent_by_case = {model_id: severity_for(loaded[model_id], graded, model_id)[2] for model_id in present}

    def rank(model_id, case_id):
        document = loaded[model_id][case_id]
        silent = (silent_by_case[model_id].get(case_id) or {}).get("silent", 0)
        speed = _median_ms(loaded, model_id, case_id) or float("inf")
        if prefer == "size":
            return (registry.get(model_id, {}).get("sizeGiB") or float("inf"), speed)
        return (-document["summary"]["passRate"], silent, speed)

    for case_id in sorted({case for documents in loaded.values() for case in documents}):
        document = next((loaded[m][case_id] for m in present if case_id in loaded[m]), None)
        if not document:
            continue
        stage = f"`{document.get('skill') or '—'}` / {document['dimension']}"
        candidates = [
            model_id
            for model_id in present
            if case_id in cleared_by_model.get(model_id, set()) and case_id in loaded[model_id]
        ]
        if not candidates:
            # Not "stays on the baseline": the baseline did not clear it either.
            lines.append(f"| {stage} | `{case_id}` | nothing cleared it | — |")
            continue
        winner = min(candidates, key=lambda model_id: rank(model_id, case_id))
        if winner == baseline:
            detail = "baseline"
        else:
            base_ms, here_ms = _median_ms(loaded, baseline, case_id), _median_ms(loaded, winner, case_id)
            detail = "—"
            if base_ms and here_ms:
                seconds = abs(here_ms - base_ms) / 1000
                detail = (
                    f"{base_ms / here_ms:.1f}x faster (−{seconds:.1f}s/item)"
                    if here_ms < base_ms
                    else f"{here_ms / base_ms:.1f}x slower (+{seconds:.1f}s/item)"
                )
        lines.append(f"| {stage} | `{case_id}` | `{winner}` | {detail} |")
    lines.append("")
    return lines



def _recommendation(loaded, present, baseline, graded, cleared_by_model=None):
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
        cleared_ids = (cleared_by_model if cleared_by_model is not None else {}).setdefault(model_id, set())
        for case_id, document in sorted(loaded[model_id].items()):
            if document.get("notApplicable"):
                held.append(f"`{case_id}` — not run: {document['notApplicable']}")
                continue
            summary = document["summary"]
            base = loaded[baseline].get(case_id)
            if base and base.get("notApplicable"):
                held.append(f"`{case_id}` — the baseline could not run it: {base['notApplicable']}")
                continue
            if not base or not summary.get("items"):
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
            cleared_ids.add(case_id)

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

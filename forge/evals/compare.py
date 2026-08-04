#!/usr/bin/env python3
"""Did a change help? Paired, per item, with a significance test.

Both arms run the same frozen fixtures, so the comparison is paired and only the
items that *disagree* carry information. That matters here more than usual: 8 of
12 cases moved between two runs of the same model with nothing changed, so a bare
"18 passed versus 15" is not evidence and must not be printed as though it were.

The test is an exact two-sided McNemar — a sign test over the discordant pairs.
With b improvements and c regressions it asks how likely that split is if the
change did nothing, which for the item counts here is the only honest question.
"""

import math

import harness

# Fewer discordant pairs than this and no split is significant at any threshold
# worth acting on: 5 improvements and 0 regressions is p = 0.0625.
MIN_DISCORDANT = 6


def exact_mcnemar(improved, regressed):
    """Two-sided exact binomial p for a b/c split under p=0.5."""
    total = improved + regressed
    if total == 0:
        return 1.0
    smaller = min(improved, regressed)
    tail = sum(math.comb(total, k) for k in range(smaller + 1)) / (2**total)
    return min(1.0, 2 * tail)


def _outcomes(documents):
    """``{(case, item): passed}`` — an item counts as passed only if every attempt did."""
    outcomes = {}
    for case_id, document in documents.items():
        for entry in harness.stability_by_item(document["items"]).values():
            outcomes[(case_id, entry["id"])] = {
                "passed": entry["passed"] == entry["attempts"],
                "stable": entry["stable"],
            }
    return outcomes


def _metrics(documents):
    """``{(case, item): {metric: mean across attempts}}``.

    Pass/fail throws away almost everything a case measured. Calibrating against
    a change with a known effect showed how much: stripping the enumeration
    clause moved item-type coverage from 9 to 4 — the exact degradation
    `service-split-handoff.md` 2.1 recorded — while the binary comparison saw one
    discordant pair and reported p = 1.0. The metric is the instrument; the gate
    is a floor.
    """
    collected = {}
    for case_id, document in documents.items():
        for item in document["items"]:
            key = (case_id, item["id"])
            for name, value in (item.get("metrics") or {}).items():
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                collected.setdefault(key, {}).setdefault(name, []).append(value)
    return {key: {name: sum(v) / len(v) for name, v in metrics.items()} for key, metrics in collected.items()}


def compare_metrics(before, after, shared):
    """Paired per-item metric deltas, for the metrics both arms produced."""
    left, right = _metrics(before), _metrics(after)
    names = sorted({name for key in shared for name in set(left.get(key, {})) & set(right.get(key, {}))})
    summary = {}
    for name in names:
        pairs = [
            (left[key][name], right[key][name])
            for key in shared
            if name in left.get(key, {}) and name in right.get(key, {})
        ]
        if not pairs:
            continue
        deltas = [after_value - before_value for before_value, after_value in pairs]
        summary[name] = {
            "items": len(pairs),
            "before": round(sum(b for b, _ in pairs) / len(pairs), 3),
            "after": round(sum(a for _, a in pairs) / len(pairs), 3),
            "meanDelta": round(sum(deltas) / len(deltas), 3),
            "improved": sum(1 for d in deltas if d > 0),
            "regressed": sum(1 for d in deltas if d < 0),
        }
    return summary


def compare(model_id, from_variant, to_variant, only=None):
    before = harness.read_results(model_id, only=only, variant=from_variant)
    after = harness.read_results(model_id, only=only, variant=to_variant)
    if not before or not after:
        missing = from_variant if not before else to_variant
        raise harness.EvalError(
            f"no results for {model_id!r} under variant {missing!r}. "
            f"Run: run.py run --model {model_id}" + ("" if missing == harness.BASE_VARIANT else f" --variant {missing}")
        )

    left, right = _outcomes(before), _outcomes(after)
    shared = sorted(set(left) & set(right))
    if not shared:
        raise harness.EvalError("the two variants share no items; they did not run the same cases")

    improved, regressed, unchanged, unstable = [], [], [], []
    for key in shared:
        # An item that flips on its own cannot testify about a change.
        if not (left[key]["stable"] and right[key]["stable"]):
            unstable.append(key)
            continue
        if left[key]["passed"] == right[key]["passed"]:
            unchanged.append(key)
        elif right[key]["passed"]:
            improved.append(key)
        else:
            regressed.append(key)

    discordant = len(improved) + len(regressed)
    p = exact_mcnemar(len(improved), len(regressed))
    return {
        "metrics": compare_metrics(before, after, shared),
        "model": model_id,
        "from": from_variant,
        "to": to_variant,
        "sharedItems": len(shared),
        "improved": [f"{c}/{i}" for c, i in improved],
        "regressed": [f"{c}/{i}" for c, i in regressed],
        "unchanged": len(unchanged),
        "excludedUnstable": [f"{c}/{i}" for c, i in unstable],
        "discordant": discordant,
        "p": round(p, 4),
        "underpowered": discordant < MIN_DISCORDANT,
    }


def render(result):
    lines = [
        f"# {result['from']} -> {result['to']} on `{result['model']}`",
        "",
        f"{result['sharedItems']} shared items; {result['unchanged']} unchanged, "
        f"{len(result['improved'])} improved, {len(result['regressed'])} regressed.",
        "",
    ]
    if result["excludedUnstable"]:
        lines.extend(
            [
                f"Excluded {len(result['excludedUnstable'])} item(s) that flipped between attempts "
                "in one arm or the other — an item that changes on its own cannot testify about a change:",
                *(f"- {entry}" for entry in result["excludedUnstable"][:8]),
                "",
            ]
        )
    for label, key in (("Improved", "improved"), ("Regressed", "regressed")):
        if result[key]:
            lines.append(f"**{label}:**")
            lines.extend(f"- {entry}" for entry in result[key])
            lines.append("")

    if result.get("metrics"):
        lines.extend(
            [
                "## Metrics",
                "",
                "Paired per item. These carry far more signal than pass/fail — a gate is a floor,",
                "and a change can move a case a long way without crossing it.",
                "",
                "| Metric | items | before | after | delta | up | down |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for name, entry in result["metrics"].items():
            arrow = "" if entry["meanDelta"] == 0 else ("  ▲" if entry["meanDelta"] > 0 else "  ▼")
            lines.append(
                f"| {name} | {entry['items']} | {entry['before']} | {entry['after']} | "
                f"{entry['meanDelta']:+}{arrow} | {entry['improved']} | {entry['regressed']} |"
            )
        lines.append("")

    lines.append("## Pass/fail")
    lines.append("")
    lines.append(f"Exact two-sided McNemar on the {result['discordant']} discordant pair(s): **p = {result['p']}**")
    lines.append("")
    if result["discordant"] == 0:
        lines.append("No item changed. Either the variant does nothing, or nothing here can see it.")
    elif result["underpowered"]:
        lines.append(
            f"**Underpowered.** {result['discordant']} discordant pairs cannot reach significance — even a "
            f"clean sweep of {MIN_DISCORDANT - 1} would give p = {exact_mcnemar(MIN_DISCORDANT - 1, 0):.3f}. "
            "Treat the direction as a hint and add fixtures before concluding anything."
        )
    elif result["p"] < 0.05:
        direction = "improvement" if len(result["improved"]) > len(result["regressed"]) else "regression"
        lines.append(f"A real {direction} at p < 0.05.")
    else:
        lines.append("Not distinguishable from the run-to-run noise this suite has already measured.")
    return "\n".join(lines) + "\n"

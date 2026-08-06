#!/usr/bin/env python3
"""Incumbent against challenger, per tier.

`report --baseline chat-27b` reads everything against the bulk model, which is
the right frame for "can this stage move" and the wrong one for the three
decisions this run exists to settle. Each is a swap within a tier:

    bulk    chat-27b    -> moe-35a3b
    verify  think-27b   -> moe-35a3b-think
    small   task-4b     -> task-9b

A swap is only worth making if the challenger holds the gates *and* carries no
more silent failures, so both are shown per case. Speed is the median item time
in absolute terms as well as a ratio, because a ratio on a small base is how a
cheap upgrade comes to look expensive.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, "forge/evals")
import harness  # noqa: E402
import judge as judging  # noqa: E402
import report as reporting  # noqa: E402

PAIRS = [
    # The three swaps this run exists to settle.
    ("bulk", "chat-27b", "moe-35a3b"),
    ("verify", "think-27b", "moe-35a3b-think"),
    ("small", "task-4b", "task-9b"),
    # And the one that decides the routing table *if the MoE stays*. The table
    # sends single-speaker cleanup and braindump splitting to `think` because
    # thinking beat non-thinking on the 27B. Those are service names, not
    # weights: on a MoE deployment the same rows become this comparison, and if
    # thinking does not pay here they should go back to `chat`.
    ("within the MoE", "moe-35a3b", "moe-35a3b-think"),
    # The same question on the dense weights, for reference — this is the pair
    # the shipped routing table was actually derived from.
    ("within the 27B (shipped table)", "chat-27b", "think-27b"),
]


def main():
    graded = judging.scored()
    loaded = {}
    for _tier, *models in PAIRS:
        for model_id in models:
            if model_id not in loaded:
                loaded[model_id] = harness.read_results(model_id)

    # Which models a grader has actually looked at. Without this the silent
    # column reads "2 -> 0" for a model nobody graded, which looks like the
    # challenger fixed two silent failures when it means nobody has checked. An
    # ungraded model cannot be exonerated, only un-accused.
    has_grades = set()
    for verdict in (graded or {}).get("verdicts", []):
        if verdict.get("model"):
            has_grades.add(verdict["model"])

    for tier, incumbent, challenger in PAIRS:
        old, new = loaded.get(incumbent) or {}, loaded.get(challenger) or {}
        if not new:
            print(f"\n## {tier}: {incumbent} -> {challenger}\n\n  no results for {challenger}\n")
            continue
        silent_old = reporting.severity_for(old, graded, incumbent)[2] if old else {}
        silent_new = reporting.severity_for(new, graded, challenger)[2] if new else {}
        ungraded = [m for m in (incumbent, challenger) if m not in has_grades]

        print(f"\n## {tier}: `{incumbent}` -> `{challenger}`\n")
        print(f"| Case | {incumbent} | {challenger} | silent | s/item | verdict |")
        print("| --- | --- | --- | --- | --- | --- |")
        wins = losses = 0
        for case_id in sorted(set(old) | set(new)):
            a, b = old.get(case_id), new.get(case_id)
            if not a or not b or a.get("notApplicable") or b.get("notApplicable"):
                note = "n/a" if (a or b) else "—"
                print(f"| `{case_id}` | {note} | {note} | | | not comparable |")
                continue
            sa, sb = a["summary"], b["summary"]
            so = (silent_old.get(case_id) or {}).get("silent", 0)
            sn = (silent_new.get(case_id) or {}).get("silent", 0)
            show_o = so if incumbent in has_grades else "?"
            show_n = sn if challenger in has_grades else "?"
            ma = (sa.get("speed") or {}).get("msPerItemMedian")
            mb = (sb.get("speed") or {}).get("msPerItemMedian")
            speed = ""
            if ma and mb:
                delta = (mb - ma) / 1000
                speed = f"{ma/1000:.1f}->{mb/1000:.1f} ({delta:+.1f})"
            gate = sb["passRate"] - sa["passRate"]
            if challenger not in has_grades and sn == 0:
                sn = 0  # unknown, not clean — see the note under the table
            if sn > so and challenger in has_grades:
                verdict, symbol = "**silent failures**", "x"
            elif gate < -0.15:
                verdict, symbol = "worse", "x"
            elif gate > 0.15:
                verdict, symbol = "better", "o"
            else:
                verdict, symbol = "holds", "="
            wins += symbol == "o"
            losses += symbol == "x"
            print(
                f"| `{case_id}` | {sa['passed']}/{sa['items']} | {sb['passed']}/{sb['items']} | "
                f"{show_o}->{show_n} | {speed} | {verdict} |"
            )
        print(f"\n  {challenger}: better on {wins}, worse on {losses}.")
        if ungraded:
            print(f"  `?` — not graded yet: {', '.join(f'`{m}`' for m in ungraded)}. An ungraded")
            print("  model has no silent failures the way an unopened envelope has no bad news.")

    if not graded:
        print("\n! Nothing is graded yet, so every `silent` column reads 0 and means nothing.")
        print("  A stage can only be blocked, never cleared, until the bundle is graded.")


if __name__ == "__main__":
    main()

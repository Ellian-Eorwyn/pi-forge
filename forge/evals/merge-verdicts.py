#!/usr/bin/env python3
"""Collect per-case grader verdicts into one file, and check them before scoring.

Each grader writes `verdicts-<case>[.partN].json`. This merges them, refuses the
mistakes that would otherwise show up as a quiet gap in the report, and reports
how much of the bundle actually got graded — because an ungraded item is not a
clean bill, and the routing recommendation reads it as one if nobody notices.

Also compares `chat-27b` against the earlier grading pass Ellie did herself.
That model is in both, so the difference between the two means is grader
variance and nothing else: it says whether the two passes can be read together.
"""

import json
from pathlib import Path

JUDGE = Path("forge/evals/results/judge")
AXES = ("voice", "faithfulness", "coverage", "usability")
PRIOR = JUDGE / "_prior-grading-2026-08-05" / "scored.json"


def main():
    key = json.loads((JUDGE / "key.json").read_text())["assignments"]
    expected = {(lookup, label) for lookup, labels in key.items() for label in labels}

    verdicts, problems, seen = [], [], set()
    files = sorted(JUDGE.glob("verdicts-*.json"))
    for path in files:
        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError as error:
            problems.append(f"{path.name}: not valid JSON ({error})")
            continue
        rows = payload.get("verdicts") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            problems.append(f"{path.name}: no verdicts list")
            continue
        for row in rows:
            lookup = f"{row.get('case')}/{row.get('item')}"
            pair = (lookup, row.get("label"))
            if pair not in expected:
                problems.append(f"{path.name}: {lookup} label {row.get('label')!r} is not in the bundle")
                continue
            if pair in seen:
                problems.append(f"{path.name}: {lookup} label {row.get('label')} graded twice")
                continue
            scores = row.get("scores") or {}
            missing = [axis for axis in AXES if not isinstance(scores.get(axis), (int, float))]
            if missing:
                problems.append(f"{path.name}: {lookup} {row.get('label')} missing {', '.join(missing)}")
                continue
            out_of_range = [a for a in AXES if not 1 <= scores[a] <= 5]
            if out_of_range:
                problems.append(f"{path.name}: {lookup} {row.get('label')} out of range on {', '.join(out_of_range)}")
                continue
            seen.add(pair)
            verdicts.append(row)

    out = JUDGE / "verdicts.json"
    out.write_text(json.dumps({"verdicts": verdicts}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"{len(files)} grader file(s) -> {len(verdicts)} verdicts, {len(expected)} expected")
    ungraded = expected - seen
    if ungraded:
        by_case = {}
        for lookup, label in ungraded:
            by_case.setdefault(lookup.split("/")[0], []).append(label)
        print(f"\nUNGRADED — {len(ungraded)} output(s). These read as 'unknown', not as clean:")
        for case, labels in sorted(by_case.items()):
            print(f"  {case}: {len(labels)}")
    for problem in problems[:20]:
        print(f"  ! {problem}")
    if len(problems) > 20:
        print(f"  ! ... and {len(problems) - 20} more")
    print(f"\nwrote {out}")
    return out


def calibrate():
    """chat-27b under both grading passes. It is in both, so the gap is grader."""
    current = JUDGE / "scored.json"
    if not (PRIOR.exists() and current.exists()):
        return
    then = json.loads(PRIOR.read_text())["summary"].get("chat-27b")
    now = json.loads(current.read_text())["summary"].get("chat-27b")
    if not (then and now):
        return
    print("\nGrader calibration — chat-27b, the model both passes graded:")
    print(f"  {'axis':<14} {'Ellie':>7} {'agents':>7} {'delta':>7}")
    for axis in AXES:
        a, b = then.get(axis), now.get(axis)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            print(f"  {axis:<14} {a:>7.2f} {b:>7.2f} {b - a:>+7.2f}")
    print("  A delta much past ±0.5 means the two passes are not directly comparable,")
    print("  and the earlier task-4b / think-27b numbers should not be read beside these.")


if __name__ == "__main__":
    main()
    calibrate()

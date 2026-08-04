#!/usr/bin/env python3
"""Evaluate a model against the stages pi-forge skills actually delegate.

    run.py freeze [--vault PATH] [--check]   materialize fixtures from the vault
    run.py doctor --model ID                 probe one endpoint before spending on it
    run.py run --model ID [--cases a,b]      run the suite, write results/<model>/
    run.py judge --models a,b,c              build the blind comparison bundle
    run.py score --verdicts PATH             merge graded verdicts, unblind them
    run.py compare --model M --to VARIANT     did a prompt or parameter change help?
    run.py report [--models a,b]             the comparison table and routing call

Deterministic scoring comes from the skills' own gates; `judge` covers what no
gate can settle — voice, faithfulness, whether a summary is any good.
"""

import argparse
import json
import sys
from pathlib import Path

EVALS_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(EVALS_ROOT))

import harness  # noqa: E402
import judge as judging  # noqa: E402
import compare as comparing  # noqa: E402
import report as reporting  # noqa: E402


def command_freeze(args):
    rows = harness.freeze(vault=args.vault, check=args.check, repin=args.repin)
    problems = 0
    for fixture_id, status, detail in rows:
        if status in {"refused", "missing", "drifted", "stale"}:
            problems += 1
        print(f"  {status:>8}  {fixture_id:<32} {detail}")
    verb = "checked" if args.check else "froze"
    print(f"\n{verb} {len(rows)} fixtures, {problems} needing attention")
    if problems and args.check:
        print("\nRe-run without --check to refresh, or `freeze --repin` to accept the vault's current content.")
    if args.repin:
        print("Re-pinned. Results taken before this point are no longer comparable with results after it.")
    return 1 if problems and args.check else 0


def command_doctor(args):
    service = harness.resolve_model(args.model)
    fingerprint = harness.served_fingerprint(service)
    report = harness.forge_llm.service_doctor(service, expect_non_thinking=True, timeout=args.timeout)
    report["served"] = fingerprint
    print(json.dumps(report, indent=2, ensure_ascii=False))
    for problem in harness.check_served(service, fingerprint):
        print(f"\nwarning: {problem}", file=sys.stderr)
    if not report.get("reachable"):
        print(f"\n{args.model} is not usable: {report.get('detail')}", file=sys.stderr)
        return 1
    if report.get("emptyContent"):
        print(f"\n{args.model} answers with no visible content; fix models.json before running.", file=sys.stderr)
        return 1
    if report.get("thinking"):
        print(
            f"\nNote: {args.model} is reasoning (~{report['hiddenTokens']} hidden tokens on a one-word reply). "
            "That is expected for a thinking tier and a real cost for a bulk one."
        )
    return 0


def command_run(args):
    service = harness.resolve_model(args.model)
    variant = harness.load_variant(getattr(args, "variant", None))
    drift = [row for row in harness.freeze(vault=args.vault, check=True) if row[1] != "ok"]
    if drift and not args.allow_drift:
        print("Fixtures are not frozen or have drifted:", file=sys.stderr)
        for fixture_id, status, detail in drift:
            print(f"  {status:>8}  {fixture_id:<32} {detail}", file=sys.stderr)
        print("\nRun `run.py freeze` first, or pass --allow-drift to measure anyway.", file=sys.stderr)
        return 1

    selected = args.cases.split(",") if args.cases else harness.case_ids()
    unknown = [case for case in selected if case not in harness.case_ids()]
    if unknown:
        print(f"unknown cases: {', '.join(unknown)}", file=sys.stderr)
        return 1

    fingerprint = harness.served_fingerprint(service)
    mismatch = harness.check_served(service, fingerprint)
    if mismatch and not args.allow_mismatch:
        for problem in mismatch:
            print(f"error: {problem}", file=sys.stderr)
        print("\nLoad the right weights, pick the entry that matches, or pass --allow-mismatch.", file=sys.stderr)
        return 1

    served = ""
    if fingerprint.get("n_params"):
        served = f"  [serving {fingerprint['n_params'] / 1e9:.1f}B"
        served += f", {fingerprint['ftype']}]" if fingerprint.get("ftype") else "]"
    mode = f"repeat {args.repeat}" if args.repeat > 1 else f"stabilize {args.stabilize}" if args.stabilize > 1 else "single pass"
    print(f"{service['label']}  ->  {service['url']}{served}  ({len(selected)} cases, {mode})\n")

    documents = {}
    failures = 0
    for case_id in selected:
        document = _run_and_report(case_id, service, args.repeat, args, "", variant)
        documents[case_id] = document
        if document["summary"]["passed"] != document["summary"]["items"]:
            failures += 1

    # Second pass: repeat only what a single item could have decided. Which cases
    # those are is recomputed from this run's own numbers, so the set is never
    # stale and never hand-maintained.
    if args.stabilize > 1 and args.repeat == 1:
        baseline = harness.read_results(args.baseline) if args.baseline != args.model else None
        undecided = harness.undecided_cases(documents, baseline)
        if undecided:
            print(f"\n{len(undecided)} case(s) a single item could decide — repeating at {args.stabilize}x:\n")
            for case_id in undecided:
                document = _run_and_report(case_id, service, args.stabilize, args, "  ", variant)
                documents[case_id] = document
        else:
            print("\nNo case sits close enough to a threshold to need repeating.")

    print(f"\nwrote {harness.results_dir(args.model, (variant or {}).get('name', harness.BASE_VARIANT))}")
    unstable = [case for case, d in documents.items() if d["summary"].get("stability", {}).get("unstableIds")]
    if unstable:
        print(f"{len(unstable)} case(s) have items that flipped between attempts; the report will not clear those.")
    if failures:
        print(f"{failures} case(s) had items fail a deterministic gate. That is a result, not an error.")
    return 0


def _run_and_report(case_id, service, repeat, args, indent, variant=None):
    document = harness.run_case(
        case_id, service, repeat=repeat, progress=_progress if args.verbose else None, variant=variant
    )
    harness.write_result(document)
    summary = document["summary"]
    flag = "" if summary["passed"] == summary["items"] else "  <-"
    unstable = summary.get("stability", {}).get("unstableIds") or []
    if unstable:
        flag = f"  <- {len(unstable)} item(s) flipped"
    print(
        f"{indent}  {case_id:<28} {summary['passed']:>3}/{summary['items']:<3} items clean"
        f"   {document['elapsedMs'] / 1000:>6.1f}s   {summary['generatedTokens']:>6} tok{flag}"
    )
    return document


def _progress(message):
    print(message, flush=True)


def command_judge(args):
    model_ids = args.models.split(",")
    bundle = judging.build(model_ids, only=args.cases.split(",") if args.cases else None, seed=args.seed)
    print(f"wrote {bundle['files']} bundle file(s) to {judging.JUDGE_DIR}")
    print(f"key at {judging.KEY_PATH} (gitignored; do not read it before grading)")
    print(f"\n{bundle['comparisons']} comparison(s) across {len(model_ids)} models.")
    print("Grade each with the rubric in the bundle header, write verdicts.json, then:")
    print(f"  run.py score --verdicts {judging.JUDGE_DIR / 'verdicts.json'}")
    return 0


def command_score(args):
    merged = judging.score(Path(args.verdicts))
    print(json.dumps(merged["summary"], indent=2, ensure_ascii=False))
    print(f"\nwrote {merged['path']}")
    return 0


def command_compare(args):
    result = comparing.compare(args.model, getattr(args, "from"), args.to, only=args.cases.split(",") if args.cases else None)
    print(comparing.render(result))
    return 0


def command_report(args):
    model_ids = args.models.split(",") if args.models else sorted(harness.models())
    text = reporting.render(model_ids, baseline=args.baseline)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="run.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    freeze = sub.add_parser("freeze", help="materialize fixtures from the vault")
    freeze.add_argument("--vault", help="vault root (default ~/Documents/Obsidian/Loom or FORGE_EVAL_VAULT)")
    freeze.add_argument("--check", action="store_true", help="report drift without writing")
    freeze.add_argument(
        "--repin", action="store_true", help="accept the vault's current content as the new baseline in fixtures.json"
    )
    freeze.set_defaults(handler=command_freeze)

    doctor = sub.add_parser("doctor", help="probe one model endpoint")
    doctor.add_argument("--model", required=True)
    doctor.add_argument("--timeout", type=float, default=120.0)
    doctor.set_defaults(handler=command_doctor)

    run = sub.add_parser("run", help="run the suite against one model")
    run.add_argument("--model", required=True)
    run.add_argument("--cases", help="comma-separated case ids (default: all)")
    run.add_argument("--repeat", type=int, default=1, help="fixed samples per item for every case; usually --stabilize is what you want")
    run.add_argument(
        "--stabilize",
        type=int,
        default=1,
        help="two-pass: run once, then repeat this many times only the cases a single item could decide",
    )
    run.add_argument("--baseline", default="chat-27b", help="model whose results decide which cases are close (for --stabilize)")
    run.add_argument("--vault", help="vault root, for the fixture freshness check")
    run.add_argument("--allow-drift", action="store_true", help="run even if fixtures drifted from their pinned hashes")
    run.add_argument(
        "--allow-mismatch", action="store_true", help="run even if the endpoint is serving weights the entry does not name"
    )
    run.add_argument("--variant", help="a patch from variants/ to test without editing any skill")
    run.add_argument("--verbose", action="store_true")
    run.set_defaults(handler=command_run)

    judge = sub.add_parser("judge", help="build the blind comparison bundle")
    judge.add_argument("--models", required=True, help="comma-separated model ids")
    judge.add_argument("--cases", help="comma-separated case ids (default: every judged case)")
    judge.add_argument("--seed", type=int, default=20260803, help="label shuffle seed; changing it relabels the bundle")
    judge.set_defaults(handler=command_judge)

    score = sub.add_parser("score", help="merge graded verdicts and unblind them")
    score.add_argument("--verdicts", required=True)
    score.set_defaults(handler=command_score)

    compare = sub.add_parser("compare", help="did a variant change anything? paired, with a significance test")
    compare.add_argument("--model", required=True)
    compare.add_argument("--from", dest="from", default=harness.BASE_VARIANT, help="variant to compare against (default: base)")
    compare.add_argument("--to", required=True, help="variant under test")
    compare.add_argument("--cases", help="comma-separated case ids (default: every case both arms ran)")
    compare.set_defaults(handler=command_compare)

    report = sub.add_parser("report", help="comparison table and routing recommendation")
    report.add_argument("--models", help="comma-separated model ids (default: all in models.json)")
    report.add_argument("--baseline", default="chat-27b", help="model the others are read against")
    report.add_argument("--out", help="write to a file instead of stdout")
    report.set_defaults(handler=command_report)

    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except harness.EvalError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

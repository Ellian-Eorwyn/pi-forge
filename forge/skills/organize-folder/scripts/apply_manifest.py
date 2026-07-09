#!/usr/bin/env python3

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
from tool_contract import ok_result, required_string, run_tool

from organize_tool_common import run_artifacts, run_workflow


def main(payload):
    run_directory = payload.get("runDirectory") or payload.get("run_directory")
    if not isinstance(run_directory, str) or not run_directory.strip():
        run_directory = required_string(payload, "runDirectory")
    summary = run_workflow(["apply", run_directory])
    warnings = []
    if summary.get("failed"):
        warnings.append(f"{summary['failed']} file(s) failed to move")
    return ok_result(artifacts=run_artifacts(run_directory), warnings=warnings, data={"summary": summary})


if __name__ == "__main__":
    run_tool(main)

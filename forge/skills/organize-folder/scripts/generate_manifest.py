#!/usr/bin/env python3

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
from tool_contract import ToolInputError, ok_result, required_string, run_tool

from organize_tool_common import add_scan_options, run_artifacts, run_workflow


def main(payload):
    if payload.get("target"):
        target = required_string(payload, "target")
        output = required_string(payload, "output")
        args = add_scan_options(["scan", target, "--output", output], payload)
        summary = run_workflow(args)
        return ok_result(artifacts=run_artifacts(output), data={"summary": summary})

    run_directory = payload.get("runDirectory") or payload.get("run_directory")
    if not isinstance(run_directory, str) or not run_directory.strip():
        raise ToolInputError("missing_required_field", "target/output or runDirectory is required")
    profile = run_workflow(["profile", run_directory])
    plan = run_workflow(["plan", run_directory])
    return ok_result(
        artifacts=run_artifacts(run_directory),
        warnings=plan.get("warnings", []),
        data={"profile": profile, "plan": plan},
    )


if __name__ == "__main__":
    run_tool(main)

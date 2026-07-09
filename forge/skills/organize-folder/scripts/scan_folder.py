#!/usr/bin/env python3

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
from tool_contract import ok_result, required_string, run_tool

from organize_tool_common import add_scan_options, run_artifacts, run_workflow


def main(payload):
    target = required_string(payload, "target")
    output = required_string(payload, "output")
    args = add_scan_options(["scan", target, "--output", output], payload)
    summary = run_workflow(args)
    return ok_result(artifacts=run_artifacts(output), data={"summary": summary})


if __name__ == "__main__":
    run_tool(main)

#!/usr/bin/env python3

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
from tool_contract import ToolInputError


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
WORKFLOW_SCRIPT = SCRIPT_DIRECTORY / "organize-folder.py"


def run_workflow(args):
    result = subprocess.run(
        [sys.executable, str(WORKFLOW_SCRIPT), *args],
        check=False,
        capture_output=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or f"exit status {result.returncode}"
        raise ToolInputError("organize_command_failed", message)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"stdout": result.stdout}


def add_scan_options(args, payload):
    if payload.get("fullHash"):
        args.append("--full-hash")
    if payload.get("noEmbeddings"):
        args.append("--no-embeddings")
    option_map = {
        "confidenceThreshold": "--confidence-threshold",
        "embeddingsUrl": "--embeddings-url",
        "nearDuplicateThreshold": "--near-duplicate-threshold",
        "clusterThreshold": "--cluster-threshold",
    }
    for key, flag in option_map.items():
        value = payload.get(key)
        if value is not None:
            args.extend([flag, str(value)])
    return args


def run_artifacts(run_directory):
    run = Path(run_directory)
    roles = {
        "manifest.csv": "manifest",
        "scan.json": "scan",
        "profile.md": "profile_markdown",
        "profile.json": "profile_json",
        "review_queue.md": "review_queue",
        "near_duplicates.md": "near_duplicates",
        "skipped.md": "skipped",
        "plan_report.md": "plan_report",
        "move_log.jsonl": "move_log",
        "final_manifest.csv": "final_manifest",
    }
    return [{"role": role, "path": str(run / name)} for name, role in roles.items() if (run / name).exists()]

#!/usr/bin/env python3

import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
from tool_contract import ToolInputError, ok_result, run_tool


FINGERPRINT_EDGE_BYTES = 65536


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(path, size):
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    with path.open("rb") as handle:
        if size <= 2 * FINGERPRINT_EDGE_BYTES:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        else:
            digest.update(handle.read(FINGERPRINT_EDGE_BYTES))
            handle.seek(-FINGERPRINT_EDGE_BYTES, os.SEEK_END)
            digest.update(handle.read(FINGERPRINT_EDGE_BYTES))
    return "fp:" + digest.hexdigest()


def selected_files(payload):
    paths = payload.get("paths")
    if isinstance(paths, list) and paths:
        return [Path(path).expanduser().resolve() for path in paths]
    target = payload.get("target")
    if not isinstance(target, str) or not target.strip():
        raise ToolInputError("missing_required_field", "paths or target is required")
    root = Path(target).expanduser().resolve()
    if root.is_file():
        return [root]
    if not root.is_dir():
        raise ToolInputError("input_not_found", f"Target does not exist: {root}")
    recursive = bool(payload.get("recursive", True))
    iterator = root.rglob("*") if recursive else root.iterdir()
    return sorted(path for path in iterator if path.is_file())


def main(payload):
    entries = []
    for path in selected_files(payload):
        if not path.is_file():
            raise ToolInputError("input_not_file", f"Input is not a file: {path}")
        size = path.stat().st_size
        entries.append(
            {
                "path": str(path),
                "sizeBytes": size,
                "modifiedAt": path.stat().st_mtime,
                "sha256": sha256(path),
                "fingerprint": fingerprint(path, size),
            }
        )
    return ok_result(data={"files": entries, "count": len(entries)})


if __name__ == "__main__":
    run_tool(main)

#!/usr/bin/env python3

import contextlib
import hashlib
import json
import os
import socket
import tempfile
from datetime import datetime, timezone
from pathlib import Path


RUN_STATE_SCHEMA_VERSION = 1
DEFAULT_MAX_ATTEMPTS = 3


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def configuration_fingerprint(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def atomic_write_text(path, value):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        try:
            directory_descriptor = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError:
            pass
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def atomic_write_json(path, value):
    atomic_write_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def append_jsonl_fsync(path, value):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("a", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_jsonl_recover_tail(path, repair=False):
    source = Path(path)
    if not source.is_file():
        return [], []
    text = source.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    rows = []
    valid = []
    warnings = []
    for index, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
            valid.append(line)
        except json.JSONDecodeError as error:
            is_tail = index == len(lines) and not raw.endswith("\n")
            if not is_tail:
                raise ValueError(f"invalid JSONL at {source}:{index}: {error}") from error
            warnings.append(f"Ignored an incomplete final JSONL record at {source}:{index}.")
            if repair:
                atomic_write_text(source, "" if not valid else "\n".join(valid) + "\n")
    return rows, warnings


def create_run_state(workflow, command, input_config, options, items=None, phase="initialized", next_action=None, children=None):
    created_at = utc_now()
    configuration = {"workflow": workflow, "command": command, "input": input_config, "options": options}
    return {
        "schemaVersion": RUN_STATE_SCHEMA_VERSION,
        "workflow": workflow,
        "createdAt": created_at,
        "updatedAt": created_at,
        "command": command,
        "input": input_config,
        "options": options,
        "optionsFingerprint": configuration_fingerprint(configuration),
        "status": "running",
        "phase": phase,
        "nextAction": next_action,
        "items": items or [],
        "children": children or {},
        "warnings": [],
    }


def initialize_run_state(run_directory, state):
    root = Path(run_directory)
    state_path = root / "run_state.json"
    if state_path.exists():
        raise ValueError(f"run state already exists: {root}")
    atomic_write_json(state_path, state)
    append_run_event(root, {"type": "run_initialized", "workflow": state["workflow"], "phase": state["phase"]})
    return state


def load_run_state(run_directory, workflow=None):
    root = Path(run_directory)
    state_path = root / "run_state.json"
    if not state_path.is_file():
        raise ValueError(f"legacy or unrelated output directory has no run_state.json: {root}")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("schemaVersion") != RUN_STATE_SCHEMA_VERSION:
        raise ValueError(f"unsupported run state schema version: {state.get('schemaVersion')}")
    if workflow and state.get("workflow") != workflow:
        raise ValueError(f"run belongs to {state.get('workflow')}, not {workflow}")
    return state


def assert_compatible_run(state, configuration):
    if configuration_fingerprint(configuration) != state.get("optionsFingerprint"):
        raise ValueError("existing run options or input do not match this invocation; use status/refresh or choose a new output directory")


def update_run_state(run_directory, mutate, event=None):
    root = Path(run_directory)
    state = load_run_state(root)
    draft = json.loads(json.dumps(state))
    updated = mutate(draft) or draft
    updated["updatedAt"] = utc_now()
    atomic_write_json(root / "run_state.json", updated)
    if event:
        append_run_event(root, event)
    return updated


def append_run_event(run_directory, event):
    root = Path(run_directory)
    path = root / "run_events.jsonl"
    prior, _ = read_jsonl_recover_tail(path, repair=True)
    append_jsonl_fsync(path, {"sequence": len(prior) + 1, "at": utc_now(), **event})


def input_drift(snapshot, current):
    original = {item["path"]: item for item in snapshot}
    observed = {item["path"]: item for item in current}
    return {
        "added": [item for item in current if item["path"] not in original],
        "removed": [item for item in snapshot if item["path"] not in observed],
        "changed": [
            {"before": original[item["path"]], "after": item}
            for item in current
            if item["path"] in original and original[item["path"]].get("sha256") != item.get("sha256")
        ],
    }


def is_transient_failure(error):
    code = str(getattr(error, "code", "")).lower()
    message = str(error).lower()
    values = ("econnreset", "econnrefused", "etimedout", "timeout", "interrupted", "aborted")
    return any(value in code or value in message for value in values) or any(f"http {status}" in message for status in range(500, 600))


def retryable_item(item, maximum_attempts=DEFAULT_MAX_ATTEMPTS):
    return item.get("status") in {"pending", "in_progress"} or (
        item.get("status") == "failed" and item.get("transient") is True and item.get("attempts", 0) < maximum_attempts
    )


def _pid_alive(pid):
    if not isinstance(pid, int) or pid < 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False


@contextlib.contextmanager
def run_lock(run_directory):
    root = Path(run_directory)
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".run.lock"
    payload = {"pid": os.getpid(), "host": socket.gethostname(), "createdAt": utc_now()}
    try:
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            existing = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
        if existing.get("host") and existing["host"] != socket.gethostname():
            raise RuntimeError(f"run is locked by PID {existing.get('pid', 'unknown')} on {existing['host']}") from None
        if _pid_alive(existing.get("pid")):
            raise RuntimeError(f"run is locked by active PID {existing['pid']}") from None
        lock_path.unlink(missing_ok=True)
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        yield
    finally:
        lock_path.unlink(missing_ok=True)

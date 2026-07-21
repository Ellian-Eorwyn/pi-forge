#!/usr/bin/env python3

import argparse
import contextlib
import csv
import hashlib
import html
import http.client
import io
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import unicodedata
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
import run_state
import forge_embeddings


RUN_SCHEMA_VERSION = 2
DEFAULT_PACKET_CHARACTERS = 48_000
MIN_ADAPTIVE_PACKET_CHARACTERS = 24_000
MAX_ADAPTIVE_PACKET_CHARACTERS = 72_000
TARGET_SOURCE_TOKENS = 14_000
DEFAULT_CHARS_PER_TOKEN = 3.42
DEFAULT_PROMPT_TOKEN_CEILING = 36_864
PROMPT_TOKEN_RESERVE = 3_500
LEASE_STALE_MS = 15_000
EXTRACTION_SCHEMA_VERSION = 1
SOURCE_EXTENSIONS = {".md", ".markdown", ".txt", ".csv"}
RESERVED_DIRECTORIES = {"Inbox", "Ingest", "Originals", "Generated"}
PLACEHOLDER = "<!-- TODO: author this section -->"
SEARCH_CHUNK_CHARACTERS = 4_000
SEARCH_CHUNK_OVERLAP = 400
SEARCH_EMBED_CHARACTERS = 8_000
SEARCH_RRF_K = 60
INBOX_MARKER = ".project-extraction-inbox.json"
INBOX_MANIFEST_SCHEMA_VERSION = 1

ITEM_TYPES = (
    "objective",
    "outcome",
    "deliverable",
    "milestone",
    "task",
    "deadline",
    "requirement",
    "reporting_requirement",
    "proposal_requirement",
    "acceptance_criterion",
    "commitment",
    "action_item",
    "decision",
    "risk",
    "issue",
    "assumption",
    "dependency",
    "stakeholder",
    "metric",
    "budget_fact",
    "open_question",
)
ITEM_TYPE_SET = set(ITEM_TYPES)
DOCUMENT_ROLES = {
    "award",
    "funding_notice",
    "proposal",
    "scope_of_work",
    "contract",
    "amendment",
    "work_plan",
    "report",
    "presentation",
    "meeting",
    "interview",
    "correspondence",
    "budget",
    "other",
}
COMMITMENT_LEVELS = {"required", "committed", "proposed", "discussed", "informational", "unclear"}
DATE_KINDS = {"exact", "relative", "recurring", "conditional", "none"}
INTERPRETATIONS = {"explicit", "inferred", "unclear"}
CONFIDENCES = {"high", "medium", "low"}
PACKET_STATUSES = {
    "extracted",
    "screened_no_controls",
    "duplicate_source",
    "excluded_by_scope",
    "needs_review",
    "preempted",
    "failed",
}
COMPLETION_PACKET_STATUSES = {"extracted", "screened_no_controls", "duplicate_source", "excluded_by_scope"}
BLOCKING_PACKET_STATUSES = {"pending", "needs_review", "preempted", "failed"}
DEFERRED_SCREENING_PATTERN = re.compile(
    r"\b(await(?:ing)?|defer(?:red)?|later|not processed|not extracted|full extraction|scheduling|runtime unavailable)\b",
    flags=re.IGNORECASE,
)
REVIEW_DISPOSITIONS = {"contextual", "duplicate", "superseded", "conflicting"}
WORKING_STATUSES = {"", "unknown", "not_started", "in_progress", "blocked", "submitted", "accepted", "completed", "cancelled"}

CONTROL_PREFIXES = {
    "objective": "OBJ",
    "outcome": "OUT",
    "deliverable": "DEL",
    "milestone": "MIL",
    "task": "TSK",
    "deadline": "DAT",
    "requirement": "REQ",
    "reporting_requirement": "RPT",
    "proposal_requirement": "PRP",
    "acceptance_criterion": "ACC",
    "commitment": "COM",
    "action_item": "ACT",
    "decision": "DEC",
    "risk": "RSK",
    "issue": "ISS",
    "assumption": "ASM",
    "dependency": "DEP",
    "stakeholder": "STK",
    "metric": "MET",
    "budget_fact": "BUD",
    "open_question": "QST",
}

OPTIONAL_ITEM_FIELDS = (
    "description",
    "party",
    "counterparty",
    "date_text",
    "date_kind",
    "date",
    "trigger",
    "offset_days",
    "recurrence",
    "acceptance_criteria",
    "evidence_required",
    "metric",
    "amount",
    "currency",
    "source_status",
    "commitment_level",
    "direct_quotes",
    "locator",
    "interpretation",
    "confidence",
    "notes",
	"teams",
	"workstreams",
	"scope_relation",
	"start_date",
	"end_date",
	"duration_days",
	"schedule_basis",
)

EVIDENCE_COLUMNS = (
    "evidence_id",
    "source_id",
    "source_revision",
    "packet_id",
    "source_path",
    "source_title",
    "document_role",
    "item_type",
    "title",
    *OPTIONAL_ITEM_FIELDS,
)

CONTROL_COLUMNS = (
    "control_id",
    "control_type",
    "title",
    "description",
    "owner",
    "recipient",
    "date_text",
    "date_kind",
    "date",
    "trigger",
    "offset_days",
    "recurrence",
    "acceptance_criteria",
    "evidence_required",
    "source_status",
    "commitment_level",
    "source_evidence_ids",
    "parent_control_ids",
    "depends_on_control_ids",
    "satisfies_control_ids",
    "supersedes_control_ids",
    "conflicts_with_control_ids",
    "notes",
	"teams",
	"workstreams",
	"scope_relation",
	"start_date",
	"end_date",
	"duration_days",
	"schedule_basis",
)

STATUS_COLUMNS = (
    "control_id",
    "current_owner",
    "working_status",
    "forecast_date",
    "forecast_start_date",
    "forecast_end_date",
    "last_updated",
    "notes",
)
SOURCE_CHANGE_COLUMNS = ("changed_at", "change_type", "source_id", "relative_path", "previous_revision", "current_revision")

MARKDOWN_TEMPLATES = {
    "project_brief.md": """# Project Brief

## Purpose and Scope

<!-- TODO: author this section -->

## Objectives and Outcomes

<!-- TODO: author this section -->

## Source Authority and Limits

<!-- TODO: author this section -->
""",
    "deliverables_and_dates.md": """# Deliverables and Dates

## Deliverables

<!-- TODO: author this section -->

## Milestones and Deadlines

<!-- TODO: author this section -->

## Conditional and Recurring Dates

<!-- TODO: author this section -->
""",
    "compliance_and_reporting.md": """# Compliance and Reporting

## Requirements

<!-- TODO: author this section -->

## Reporting Calendar

<!-- TODO: author this section -->

## Acceptance and Evidence

<!-- TODO: author this section -->
""",
    "status_brief.md": """# Status Brief

## Current Position

<!-- TODO: author this section -->

## Upcoming and Overdue

<!-- TODO: author this section -->

## Decisions or Support Needed

<!-- TODO: author this section -->
""",
    "decisions_and_open_questions.md": """# Decisions and Open Questions

## Decisions

<!-- TODO: author this section -->

## Open Questions

<!-- TODO: author this section -->

## Source Conflicts

<!-- TODO: author this section -->
""",
    "risks_issues_dependencies.md": """# Risks, Issues, and Dependencies

## Risks

<!-- TODO: author this section -->

## Issues

<!-- TODO: author this section -->

## Assumptions and Dependencies

<!-- TODO: author this section -->
""",
}

PROPOSAL_TEMPLATE = """# Proposal Checklist

## Eligibility and Submission Requirements

<!-- TODO: author this section -->

## Required Components

<!-- TODO: author this section -->

## Review Criteria and Open Questions

<!-- TODO: author this section -->
"""


def fail(message, exit_code=1):
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(exit_code)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(prefix, value, length=12):
    return f"{prefix}-{sha256_bytes(value.encode('utf-8'))[:length]}"


def normalized_quote_map(value):
    normalized = []
    spans = []
    whitespace_open = False
    replacements = {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
    }
    for index, character in enumerate(value):
        expanded = unicodedata.normalize("NFKC", replacements.get(character, character))
        for normalized_character in expanded:
            if normalized_character.isspace():
                if whitespace_open or not normalized:
                    continue
                normalized.append(" ")
                spans.append((index, index + 1))
                whitespace_open = True
            else:
                normalized.append(normalized_character)
                spans.append((index, index + 1))
                whitespace_open = False
    if normalized and normalized[-1] == " ":
        normalized.pop()
        spans.pop()
    return "".join(normalized), spans


def exact_source_quote(packet_text, proposed):
    if proposed in packet_text:
        return proposed
    normalized_packet, spans = normalized_quote_map(packet_text)
    normalized_proposed, _ = normalized_quote_map(proposed)
    if not normalized_proposed:
        return None
    starts = [match.start() for match in re.finditer(re.escape(normalized_proposed), normalized_packet)]
    if len(starts) != 1:
        return None
    start = starts[0]
    end = start + len(normalized_proposed) - 1
    return packet_text[spans[start][0] : spans[end][1]]


def write_json(path, value):
    run_state.atomic_write_json(path, value)


def append_jsonl(path, value):
    run_state.append_jsonl_fsync(path, value)


def read_jsonl(path):
    try:
        rows, warnings = run_state.read_jsonl_recover_tail(path, repair=True)
    except ValueError as error:
        fail(str(error))
    for warning in warnings:
        print(f"Warning: {warning}", file=sys.stderr)
    return rows


def write_csv(path, columns, rows):
    handle = io.StringIO(newline="")
    writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: csv_value(row.get(column)) for column in columns})
    run_state.atomic_write_text(path, handle.getvalue())


def csv_value(value):
    if value is None:
        return ""
    if isinstance(value, list):
        return ";".join(str(item) for item in value)
    return str(value)


def read_csv(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def forge_agent_directory():
    root = os.environ.get("PI_FORGE_HOME")
    if root:
        return Path(root).expanduser() / "agent"
    explicit = os.environ.get("PI_CODING_AGENT_DIR") or os.environ.get("PI_FORGE_AGENT_DIR")
    return Path(explicit).expanduser() if explicit else Path.home() / ".pi-forge" / "agent"


def chat_configuration():
    settings_path = forge_agent_directory() / "settings.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        settings = {}
    chat = ((settings.get("connectedServices") or {}).get("chat") or {})
    scheduling = chat.get("scheduling") or {}
    return {
        "enabled": bool(chat.get("enabled", True)),
        "url": os.environ.get("FORGE_BASE_CHAT_URL") or os.environ.get("FORGE_CHAT_URL") or chat.get("baseUrl") or "http://llms:8008/v1/chat/completions",
        "model": os.environ.get("FORGE_BASE_MODEL") or chat.get("model") or "code",
        "scheduling": {
            "enabled": bool(scheduling.get("enabled", False)),
            "interactiveSlot": int(scheduling.get("interactiveSlot", 0)),
            "backgroundSlot": int(scheduling.get("backgroundSlot", 1)),
            "idleGraceMs": int(scheduling.get("idleGraceMs", 2000)),
            "yieldMs": int(scheduling.get("yieldMs", 1000)),
            "backgroundOutputTokens": int(scheduling.get("backgroundOutputTokens", 4096)),
        },
    }


def active_interactive_leases():
    directory = forge_agent_directory() / "inference-leases"
    if not directory.is_dir():
        return []
    now_ms = time.time() * 1000
    active = []
    for path in directory.glob("*.json"):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if row.get("kind", "interactive") == "interactive" and now_ms - float(row.get("updatedAtMs", 0)) <= LEASE_STALE_MS:
            active.append(row)
    return active


def wait_for_interactive_idle(run_directory, scheduling):
    started = time.monotonic()
    grace = max(0, scheduling["idleGraceMs"]) / 1000
    while True:
        while active_interactive_leases():
            time.sleep(0.2)
        if not grace:
            break
        time.sleep(grace)
        if not active_interactive_leases():
            break
    waited_ms = int((time.monotonic() - started) * 1000)
    if waited_ms:
        append_jsonl(run_directory / "inference_schedule.jsonl", {"at": utc_now(), "event": "idle_wait", "waitedMs": waited_ms})


def extract_json_text(value):
    text = str(value or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = min((position for position in (text.find("{"), text.find("[")) if position >= 0), default=-1)
        end = max(text.rfind("}"), text.rfind("]"))
        if start < 0 or end < start:
            raise
        return json.loads(text[start : end + 1])


def post_chat_json(
    run_directory,
    task,
    system_prompt,
    user_prompt,
    chat,
    allow_preemption=True,
    parse_content=True,
    background=False,
):
    parsed = urllib.parse.urlsplit(chat["url"])
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        fail(f"unsupported chat URL: {chat['url']}")
    scheduling = chat["scheduling"]
    background_lease = None
    if background:
        while True:
            wait_for_interactive_idle(run_directory, scheduling)
            lease_directory = forge_agent_directory() / "inference-leases"
            lease_directory.mkdir(parents=True, exist_ok=True)
            background_lease = lease_directory / f"background-{os.getpid()}-{threading.get_ident()}.json"
            temporary = background_lease.with_suffix(".tmp")
            run_state.atomic_write_text(
                temporary,
                json.dumps({"pid": os.getpid(), "kind": "background", "slot": scheduling["backgroundSlot"], "updatedAtMs": int(time.time() * 1000)}) + "\n",
            )
            temporary.replace(background_lease)
            if not active_interactive_leases():
                break
            background_lease.unlink(missing_ok=True)

    def clear_background_lease():
        if background_lease is not None:
            background_lease.unlink(missing_ok=True)

    def refresh_background_lease():
        if background_lease is not None:
            run_state.atomic_write_text(
                background_lease,
                json.dumps({"pid": os.getpid(), "kind": "background", "slot": scheduling["backgroundSlot"], "updatedAtMs": int(time.time() * 1000)}) + "\n",
            )
    worker_session = "project-extraction-slot-probe"
    config_path = run_directory / "run_config.json"
    if config_path.is_file():
        worker_session = json.loads(config_path.read_text(encoding="utf-8")).get("worker", {}).get("sessionId") or worker_session
    request = {
        "model": chat["model"],
        "user": worker_session,
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        "temperature": 0.1,
        "max_tokens": scheduling["backgroundOutputTokens"],
        "stream": False,
        "cache_prompt": True,
    }
    if background:
        request["id_slot"] = scheduling["backgroundSlot"]
    body = json.dumps(request).encode("utf-8")
    connection_class = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_class(parsed.hostname, parsed.port, timeout=600)
    result = {}
    failure = {}

    def execute():
        try:
            request_path = parsed.path or "/"
            if parsed.query:
                request_path += f"?{parsed.query}"
            connection.request("POST", request_path, body=body, headers={"Content-Type": "application/json", "Authorization": "Bearer local"})
            response = connection.getresponse()
            payload = response.read()
            if response.status >= 400:
                raise RuntimeError(f"chat endpoint returned HTTP {response.status}: {payload.decode('utf-8', errors='replace')[:500]}")
            result["payload"] = json.loads(payload)
        except BaseException as error:
            failure["error"] = error

    started = time.monotonic()
    thread = threading.Thread(target=execute, daemon=True)
    thread.start()
    preempted = False
    last_lease_refresh = time.monotonic()
    while thread.is_alive():
        thread.join(0.1)
        if background and time.monotonic() - last_lease_refresh >= 1:
            refresh_background_lease()
            last_lease_refresh = time.monotonic()
        if background and allow_preemption and active_interactive_leases():
            preempted = True
            connection.close()
            break
    if preempted:
        thread.join(2)
        clear_background_lease()
        append_jsonl(run_directory / "inference_schedule.jsonl", {"at": utc_now(), "event": "preempted", "task": task, "slot": scheduling["backgroundSlot"]})
        raise InterruptedError("background inference preempted by interactive activity")
    thread.join()
    connection.close()
    if failure:
        clear_background_lease()
        raise failure["error"]
    payload = result["payload"]
    usage = payload.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    timings = payload.get("timings") or {}
    elapsed_ms = int((time.monotonic() - started) * 1000)
    record = {
        "at": utc_now(),
        "event": "model_call",
        "task": task,
        "sessionId": worker_session,
        "mode": "background" if background else "foreground",
        "slot": scheduling["backgroundSlot"] if background else None,
        "promptCharacters": len(system_prompt) + len(user_prompt),
        "promptTokens": usage.get("prompt_tokens"),
        "cachedTokens": details.get("cached_tokens", timings.get("cache_n")),
        "prefillMs": timings.get("prompt_ms"),
        "generationMs": timings.get("predicted_ms"),
        "elapsedMs": elapsed_ms,
    }
    choices = payload.get("choices") or []
    record["finishReason"] = choices[0].get("finish_reason") if choices else None
    append_jsonl(run_directory / "inference_schedule.jsonl", record)
    clear_background_lease()
    content = ((choices[0].get("message") or {}).get("content") if choices else None)
    if not parse_content:
        return content, record
    try:
        return extract_json_text(content), record
    except json.JSONDecodeError:
        if record["finishReason"] == "length":
            return None, record
        raise


def probe_background_slot(chat):
    if not chat["scheduling"]["enabled"]:
        return {"configured": False, "available": False, "detail": "cache-aware scheduling is disabled"}
    temporary = Path(os.environ.get("TMPDIR") or "/tmp") / f"project-extraction-slot-probe-{os.getpid()}"
    temporary.mkdir(parents=True, exist_ok=True)
    probe_chat = json.loads(json.dumps(chat))
    probe_chat["scheduling"]["backgroundOutputTokens"] = 16
    try:
        post_chat_json(
            temporary,
            "slot-probe",
            "Reply briefly.",
            "Reply ok.",
            probe_chat,
            allow_preemption=False,
            parse_content=False,
            background=True,
        )
        return {"configured": True, "available": True, "slot": chat["scheduling"]["backgroundSlot"]}
    except BaseException as error:
        return {"configured": True, "available": False, "slot": chat["scheduling"]["backgroundSlot"], "detail": str(error)}
    finally:
        for path in temporary.glob("*"):
            path.unlink(missing_ok=True)
        temporary.rmdir()


def require_run_directory(raw_path):
    path = Path(raw_path).expanduser().resolve()
    if not path.is_dir() or not (path / "run_config.json").is_file():
        fail(f"not a project-extraction run: {path}")
    try:
        schema_version = json.loads((path / "run_config.json").read_text(encoding="utf-8")).get("schemaVersion")
    except (OSError, json.JSONDecodeError):
        fail(f"invalid project-extraction run configuration: {path}")
    if schema_version != RUN_SCHEMA_VERSION:
        fail(f"project-extraction run schema {schema_version} is unsupported; create a new version-{RUN_SCHEMA_VERSION} extraction")
    return path


def load_run(run_directory):
    config = json.loads((run_directory / "run_config.json").read_text(encoding="utf-8"))
    manifest = json.loads((run_directory / "source_manifest.json").read_text(encoding="utf-8"))
    return config, manifest


def inferred_project_root(config):
    roots = [Path(value).expanduser().resolve() for value in config.get("inputs", [])]
    return roots[0] if len(roots) == 1 and roots[0].is_dir() else None


def live_repository_config(config):
    configured = config.get("liveRepository") or {}
    project_root = Path(configured["projectRoot"]).expanduser().resolve() if configured.get("projectRoot") else inferred_project_root(config)
    if project_root is None:
        return None
    inbox = Path(configured.get("inbox") or project_root / "Inbox").expanduser().resolve()
    return {
        "projectRoot": project_root,
        "inbox": inbox,
        "publishDirectory": Path(configured.get("publishDirectory") or project_root / "Sources" / "Inbox").expanduser().resolve(),
        "originalsDirectory": Path(configured.get("originalsDirectory") or project_root / "Originals" / "Inbox").expanduser().resolve(),
    }


def initialize_live_repository(run_directory, config, persist=True):
    live = live_repository_config(config)
    if live is None:
        return None
    live["inbox"].mkdir(parents=True, exist_ok=True)
    marker_path = live["inbox"] / INBOX_MARKER
    marker = {
        "schemaVersion": 1,
        "workflow": "project-extraction",
        "runDirectory": str(run_directory),
        "projectRoot": str(live["projectRoot"]),
    }
    if marker_path.is_file():
        existing = json.loads(marker_path.read_text(encoding="utf-8"))
        if Path(existing.get("runDirectory", "")).expanduser().resolve() != run_directory:
            fail(f"Inbox is already linked to another project-extraction run: {marker_path}")
    else:
        write_json(marker_path, marker)
    if persist:
        config["liveRepository"] = {key: str(value) for key, value in live.items()}
        config["updatedAt"] = utc_now()
        write_json(run_directory / "run_config.json", config)
    return live


def inbox_manifest(run_directory):
    path = run_directory / "inbox_manifest.json"
    if not path.is_file():
        return {"schemaVersion": INBOX_MANIFEST_SCHEMA_VERSION, "updatedAt": None, "activeBatch": None, "items": []}
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schemaVersion") != INBOX_MANIFEST_SCHEMA_VERSION:
        fail(f"unsupported inbox manifest schema in {path}")
    return value


def write_inbox_manifest(run_directory, value):
    value["updatedAt"] = utc_now()
    write_json(run_directory / "inbox_manifest.json", value)


def scan_inbox(live):
    inbox = live["inbox"]
    if not inbox.is_dir():
        return []
    rows = []
    for path in sorted(inbox.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(inbox)
        if any(part.startswith(".") for part in relative.parts) or path.name == INBOX_MARKER:
            continue
        digest = sha256_file(path)
        rows.append(
            {
                "itemId": stable_id("inbox", f"{relative.as_posix()}|{digest}"),
                "relativePath": relative.as_posix(),
                "path": str(path),
                "sha256": digest,
                "sizeBytes": path.stat().st_size,
            }
        )
    return rows


def inbox_status_value(run_directory):
    config, _ = load_run(run_directory)
    live = live_repository_config(config)
    if live is None:
        return {
            "configured": False,
            "reason": "multiple or non-directory inputs require an explicit liveRepository configuration",
            "pending": [],
            "activeBatch": None,
        }
    manifest = inbox_manifest(run_directory)
    current = scan_inbox(live)
    completed_hashes = {row.get("sha256") for row in manifest["items"] if row.get("status") == "ingested"}
    pending = [{**row, "duplicate": row["sha256"] in completed_hashes} for row in current]
    return {
        "configured": True,
        "projectRoot": str(live["projectRoot"]),
        "inbox": str(live["inbox"]),
        "pending": pending,
        "pendingCount": len(pending),
        "activeBatch": manifest.get("activeBatch"),
        "failed": [row for row in manifest["items"] if row.get("status") == "failed"],
    }


def remove_empty_directories(root):
    if not root.is_dir():
        return
    for path in sorted((value for value in root.rglob("*") if value.is_dir()), reverse=True):
        try:
            path.rmdir()
        except OSError:
            pass


def archive_duplicate_inbox_item(live, row):
    source = Path(row["path"])
    destination = live["originalsDirectory"] / "Duplicates" / row["relativePath"]
    if destination.exists():
        if not destination.is_file() or sha256_file(destination) != row["sha256"]:
            destination = destination.with_name(f"{destination.stem}.duplicate-{row['sha256'][:8]}{destination.suffix}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if sha256_file(destination) != row["sha256"]:
            fail(f"duplicate archive destination hash mismatch: {destination}")
        source.unlink()
    else:
        source.rename(destination)
    return str(destination)


def stage_inbox_batch(run_directory, live, manifest, pending):
    batch_key = "|".join(f"{row['relativePath']}:{row['sha256']}" for row in pending)
    batch_id = stable_id("batch", batch_key)
    staging = live["inbox"] / ".processing" / batch_id
    items = []
    for row in pending:
        source = Path(row["path"])
        destination = staging / row["relativePath"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if sha256_file(destination) != row["sha256"]:
                fail(f"Inbox staging destination hash mismatch: {destination}")
        else:
            source.rename(destination)
        item = {**row, "path": str(destination), "stagedAt": utc_now(), "status": "processing", "batchId": batch_id}
        items.append(item)
        append_jsonl(run_directory / "inbox_events.jsonl", {"at": utc_now(), "type": "inbox_item_staged", "itemId": row["itemId"], "batchId": batch_id, "sha256": row["sha256"]})
    batch = {
        "batchId": batch_id,
        "stagingDirectory": str(staging),
        "ingestRun": str(live["projectRoot"] / "Ingest" / "Inbox" / batch_id),
        "itemIds": [row["itemId"] for row in items],
        "status": "processing",
        "createdAt": utc_now(),
    }
    manifest["items"].extend(items)
    manifest["activeBatch"] = batch
    write_inbox_manifest(run_directory, manifest)
    return batch


def normalized_relative(path):
    return path.as_posix().lstrip("./") or path.name


def ingest_provenance(root):
    manifest_path = root / "Ingest" / "artifact_manifest.csv"
    if not manifest_path.is_file():
        return {}
    result = {}
    for row in read_csv(manifest_path):
        if row.get("role") != "final_markdown":
            continue
        destination = (root / row.get("destination_path", "")).resolve()
        source_path = Path(row.get("source_path", ""))
        source_map = source_path.parent / "source_map.json"
        result[str(destination)] = {
            "documentId": row.get("document_id") or None,
            "sourceMapPath": str(source_map) if source_map.is_file() else None,
        }
    return result


def discover_sources(raw_inputs):
    records = []
    seen_paths = set()
    for input_index, raw_input in enumerate(raw_inputs, 1):
        root = Path(raw_input).expanduser().resolve()
        if not root.exists():
            fail(f"input does not exist: {root}")
        provenance = ingest_provenance(root) if root.is_dir() else {}
        if root.is_file():
            candidates = [(root, root.name)]
        else:
            candidates = []
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                relative = path.relative_to(root)
                if any(part.startswith(".") for part in relative.parts[:-1]) or (relative.parts and relative.parts[0] in RESERVED_DIRECTORIES):
                    continue
                if path.suffix.lower() in SOURCE_EXTENSIONS:
                    candidates.append((path, normalized_relative(relative)))
        for path, relative_path in candidates:
            if path.suffix.lower() not in SOURCE_EXTENSIONS:
                continue
            resolved = str(path.resolve())
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            source_key = f"{input_index}:{relative_path}"
            source_hash = sha256_file(path)
            source_id = stable_id("src", source_key)
            source = {
                "sourceId": source_id,
                "sourceKey": source_key,
                "inputIndex": input_index,
                "path": resolved,
                "relativePath": relative_path,
                "format": path.suffix.lower().lstrip("."),
                "sha256": source_hash,
                "revisionId": f"rev-{source_hash[:12]}",
                "sizeBytes": path.stat().st_size,
                "modifiedAt": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                "active": True,
                "ingestProvenance": provenance.get(resolved),
            }
            records.append(source)
    if not records:
        fail("no supported Markdown, text, or CSV sources were found")
    return sorted(records, key=lambda row: row["sourceKey"])


def source_locator(source, start_line, end_line):
    provenance = source.get("ingestProvenance") or {}
    raw_source_map = provenance.get("sourceMapPath")
    if raw_source_map:
        source_map_path = Path(raw_source_map)
        if source_map_path.is_file():
            try:
                source_map = json.loads(source_map_path.read_text(encoding="utf-8"))
                for entry in source_map.get("entries", []):
                    entry_start = entry.get("markdownStartLine")
                    entry_end = entry.get("markdownEndLine")
                    if entry_start and entry_end and entry_start <= end_line and entry_end >= start_line:
                        return entry.get("sourceLocator")
            except (OSError, json.JSONDecodeError):
                pass
    return {"type": "line-range", "start": start_line, "end": end_line}


def split_text_packets(text, maximum_characters):
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines(keepends=True)
    blocks = []
    current_lines = []
    block_start = 1
    for line_number, line in enumerate(lines, 1):
        is_heading = line.lstrip().startswith("#")
        if current_lines and is_heading and any(value.strip() for value in current_lines):
            blocks.append(("".join(current_lines), block_start, line_number - 1))
            current_lines = []
            block_start = line_number
        current_lines.append(line)
        if not line.strip() and any(value.strip() for value in current_lines):
            blocks.append(("".join(current_lines), block_start, line_number))
            current_lines = []
            block_start = line_number + 1
    if current_lines or not blocks:
        blocks.append(("".join(current_lines), block_start, max(block_start, len(lines))))

    bounded_blocks = []
    for block, start_line, end_line in blocks:
        if len(block) <= maximum_characters:
            bounded_blocks.append((block, start_line, end_line))
            continue
        offset = 0
        while offset < len(block):
            chunk = block[offset : offset + maximum_characters]
            chunk_start = start_line + block[:offset].count("\n")
            chunk_end = chunk_start + chunk.count("\n")
            bounded_blocks.append((chunk, chunk_start, min(end_line, max(chunk_start, chunk_end))))
            offset += maximum_characters

    packets = []
    current = []
    current_size = 0
    packet_start = 1
    packet_end = 1
    for block, start_line, end_line in bounded_blocks:
        if current and current_size + len(block) > maximum_characters:
            packets.append(("".join(current), packet_start, packet_end))
            current = []
            current_size = 0
        if not current:
            packet_start = start_line
        current.append(block)
        current_size += len(block)
        packet_end = end_line
    if current or not packets:
        packets.append(("".join(current), packet_start, packet_end))
    return packets


def split_csv_packets(path, maximum_characters):
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    if not rows:
        return [("", 1, 1)]
    header = rows[0]
    packets = []
    current = []
    current_size = 0
    start_row = 2
    for row_number, row in enumerate(rows[1:], 2):
        rendered = ",".join(row) + "\n"
        if current and current_size + len(rendered) > maximum_characters:
            packets.append((header, current, start_row, row_number - 1))
            current = []
            current_size = 0
            start_row = row_number
        current.append(row)
        current_size += len(rendered)
    if current or len(rows) == 1:
        packets.append((header, current, start_row, max(start_row, len(rows))))
    result = []
    for packet_header, packet_rows, row_start, row_end in packets:
        rendered_rows = [packet_header, *packet_rows]
        text = "\n".join(",".join(row) for row in rendered_rows) + "\n"
        result.append((text, row_start, row_end))
    return result


def write_source_packets(run_directory, source, packet_characters):
    path = Path(source["path"])
    if source["format"] == "csv":
        raw_packets = split_csv_packets(path, packet_characters)
        locator_type = "csv-row-range"
    else:
        raw_packets = split_text_packets(path.read_text(encoding="utf-8"), packet_characters)
        locator_type = "line-range"
    packets_directory = run_directory / "packets"
    packets_directory.mkdir(exist_ok=True)
    packets = []
    for index, (text, start, end) in enumerate(raw_packets, 1):
        packet_key = f"{source['sourceId']}|{source['revisionId']}|{index}"
        packet_id = stable_id("pkt", packet_key)
        extension = ".csv" if source["format"] == "csv" else ".md"
        packet_path = packets_directory / f"{packet_id}{extension}"
        if not packet_path.exists():
            packet_path.write_text(text, encoding="utf-8")
        locator = {"type": locator_type, "start": start, "end": end}
        if source["format"] != "csv":
            locator = source_locator(source, start, end)
        packets.append(
            {
                "packetId": packet_id,
                "sourceId": source["sourceId"],
                "sourceRevision": source["revisionId"],
                "sequence": index,
                "path": str(packet_path),
                "sha256": sha256_file(packet_path),
                "characters": len(text),
                "locator": locator,
            }
        )
    return packets


def initialize_manifest(run_directory, sources, packet_characters, previous=None):
    previous_by_id = {row["sourceId"]: row for row in (previous or {}).get("sources", [])}
    current_by_id = {row["sourceId"]: row for row in sources}
    combined = []
    for source in sources:
        source["packets"] = write_source_packets(run_directory, source, packet_characters)
        combined.append(source)
    for source_id, old in previous_by_id.items():
        if source_id not in current_by_id:
            combined.append({**old, "active": False})
    return {"schemaVersion": RUN_SCHEMA_VERSION, "sources": sorted(combined, key=lambda row: row["sourceKey"])}


def append_source_changes(run_directory, previous, current):
    history_path = run_directory / "source_history.jsonl"
    previous_by_id = {row["sourceId"]: row for row in (previous or {}).get("sources", []) if row.get("active", True)}
    current_by_id = {row["sourceId"]: row for row in current.get("sources", []) if row.get("active", True)}
    timestamp = utc_now()
    for source_id in sorted(set(previous_by_id) | set(current_by_id)):
        old = previous_by_id.get(source_id)
        new = current_by_id.get(source_id)
        if old is None:
            change_type = "added"
        elif new is None:
            change_type = "removed"
        elif old["revisionId"] != new["revisionId"]:
            change_type = "changed"
        else:
            continue
        append_jsonl(
            history_path,
            {
                "changedAt": timestamp,
                "changeType": change_type,
                "sourceId": source_id,
                "relativePath": (new or old)["relativePath"],
                "previousRevision": old.get("revisionId") if old else None,
                "currentRevision": new.get("revisionId") if new else None,
            },
        )


def active_sources(manifest):
    return [source for source in manifest["sources"] if source.get("active", True)]


def active_packets(manifest):
    return [packet for source in active_sources(manifest) for packet in source.get("packets", [])]


def current_results(run_directory, manifest):
    active_ids = {packet["packetId"] for packet in active_packets(manifest)}
    latest = {}
    for result in read_jsonl(run_directory / "extraction_results.jsonl"):
        if result.get("packetId") in active_ids:
            latest[result["packetId"]] = result
    return latest


def coverage_summary(run_directory, manifest):
    results = current_results(run_directory, manifest)
    packets = active_packets(manifest)
    counts = Counter(results.get(packet["packetId"], {}).get("status", "pending") for packet in packets)
    complete_packets = sum(counts[status] for status in COMPLETION_PACKET_STATUSES)
    source_rows = []
    for source in active_sources(manifest):
        statuses = [results.get(packet["packetId"], {}).get("status", "pending") for packet in source.get("packets", [])]
        source_rows.append(
            {
                "sourceId": source["sourceId"],
                "relativePath": source["relativePath"],
                "complete": bool(statuses) and all(status in COMPLETION_PACKET_STATUSES for status in statuses),
                "statuses": dict(Counter(statuses)),
            }
        )
    unresolved_review_items = []
    review_manifest_path = run_directory / "review_manifest.json"
    if review_manifest_path.is_file():
        review_manifest = json.loads(review_manifest_path.read_text(encoding="utf-8"))
        review_results = current_review_results(run_directory, review_manifest)
        unresolved_review_items = [packet["reviewPacketId"] for packet in review_manifest.get("packets", []) if packet["reviewPacketId"] not in review_results]
    invalid_screenings = []
    for packet_id, result in results.items():
        if result.get("status") != "screened_no_controls":
            continue
        screening = result.get("screening")
        finding = screening.get("finding") if isinstance(screening, dict) else None
        if (
            not isinstance(screening, dict)
            or screening.get("method") not in {"model", "human"}
            or screening.get("source") not in {"worker", "manual"}
            or not finding
            or DEFERRED_SCREENING_PATTERN.search(finding)
        ):
            invalid_screenings.append(packet_id)
    blocking_counts = {status: counts[status] for status in sorted(BLOCKING_PACKET_STATUSES) if counts[status]}
    if invalid_screenings:
        blocking_counts["invalid_screening"] = len(invalid_screenings)
    deferred_count = sum(counts[status] for status in BLOCKING_PACKET_STATUSES)
    completion_eligible = not blocking_counts and not unresolved_review_items and complete_packets == len(packets)
    return {
        "packets": len(packets),
        "packetCounts": dict(sorted(counts.items())),
        "coveredPackets": complete_packets,
        "extractedCount": counts["extracted"],
        "screenedCount": counts["screened_no_controls"],
        "deferredCount": deferred_count,
        "coveragePercent": round(100 * complete_packets / len(packets), 2) if packets else 100.0,
        "sources": len(source_rows),
        "coveredSources": sum(row["complete"] for row in source_rows),
        "sourceCoveragePercent": round(100 * sum(row["complete"] for row in source_rows) / len(source_rows), 2) if source_rows else 100.0,
        "blockingCounts": blocking_counts,
        "invalidScreenings": invalid_screenings,
        "pendingReviewPackets": len(unresolved_review_items),
        "unresolvedReviewItems": unresolved_review_items,
        "completionEligible": completion_eligible,
        "sourceCoverage": source_rows,
    }


def write_coverage(run_directory, manifest, draft):
    value = coverage_summary(run_directory, manifest)
    value.update({"generatedAt": utc_now(), "draft": draft})
    write_json(run_directory / "coverage.json", value)
    return value


def source_for_packet(manifest, packet_id):
    for source in active_sources(manifest):
        for packet in source.get("packets", []):
            if packet["packetId"] == packet_id:
                return source, packet
    fail(f"unknown active packet id: {packet_id}")


def validate_iso_date(value, label):
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        fail(f"{label} must be YYYY-MM-DD or null")


def is_iso_date(value):
    if value in (None, ""):
        return True
    try:
        date.fromisoformat(value)
        return True
    except (TypeError, ValueError):
        return False


def normalize_item(raw, index):
    if not isinstance(raw, dict):
        fail(f"item {index} must be an object")
    item_type = raw.get("item_type")
    if item_type not in ITEM_TYPE_SET:
        fail(f"item {index} item_type must be one of: {', '.join(ITEM_TYPES)}")
    title = str(raw.get("title") or "").strip()
    if not title:
        fail(f"item {index} title is required")
    item = {"item_type": item_type, "title": title}
    for field in OPTIONAL_ITEM_FIELDS:
        value = raw.get(field)
        item[field] = value.strip() if isinstance(value, str) else value
    item["date_kind"] = item["date_kind"] or "none"
    item["interpretation"] = item["interpretation"] or "explicit"
    item["confidence"] = item["confidence"] or "medium"
    item["commitment_level"] = item["commitment_level"] or "unclear"
    if item["date_kind"] not in DATE_KINDS:
        fail(f"item {index} date_kind must be one of: {', '.join(sorted(DATE_KINDS))}")
    if item["interpretation"] not in INTERPRETATIONS:
        fail(f"item {index} interpretation must be explicit, inferred, or unclear")
    if item["confidence"] not in CONFIDENCES:
        fail(f"item {index} confidence must be high, medium, or low")
    if item["commitment_level"] not in COMMITMENT_LEVELS:
        fail(f"item {index} commitment_level is invalid")
    if item["date_kind"] == "exact":
        item["date"] = validate_iso_date(item["date"], f"item {index} date")
        if item["date"] is None:
            fail(f"item {index} exact date requires date")
    elif item["date"] not in (None, ""):
        fail(f"item {index} must not normalize a non-exact date")
    else:
        item["date"] = None
    if item["date_kind"] in {"relative", "conditional"} and not item["trigger"]:
        fail(f"item {index} {item['date_kind']} date requires trigger text")
    if item["date_kind"] == "recurring" and not item["recurrence"]:
        fail(f"item {index} recurring date requires recurrence text")
    if item["offset_days"] not in (None, "") and not isinstance(item["offset_days"], int):
        fail(f"item {index} offset_days must be an integer or null")
    quotes = item["direct_quotes"]
    if isinstance(quotes, str) and quotes.strip():
        quotes = [quotes.strip()]
    if not isinstance(quotes, list) or not quotes or any(not isinstance(quote, str) or not quote.strip() for quote in quotes):
        fail(f"item {index} direct_quotes must be a nonempty array of source excerpts")
    item["direct_quotes"] = [quote.strip() for quote in quotes]
    for field in ("teams", "workstreams"):
        value = item.get(field)
        if value is None:
            item[field] = []
        elif isinstance(value, str):
            item[field] = [value.strip()] if value.strip() else []
        elif isinstance(value, list) and all(isinstance(entry, str) and entry.strip() for entry in value):
            item[field] = [entry.strip() for entry in value]
        else:
            fail(f"item {index} {field} must be an array of nonblank strings")
    item["scope_relation"] = item.get("scope_relation") or "direct"
    if item["scope_relation"] not in {"direct", "dependency", "shared", "full"}:
        fail(f"item {index} scope_relation is invalid")
    for field in ("start_date", "end_date"):
        item[field] = validate_iso_date(item.get(field), f"item {index} {field}")
    duration = item.get("duration_days")
    if duration not in (None, "") and (not isinstance(duration, int) or duration < 0):
        fail(f"item {index} duration_days must be a nonnegative integer or null")
    item["duration_days"] = None if duration in (None, "") else duration
    item["schedule_basis"] = item.get("schedule_basis") or ("source" if item.get("start_date") or item.get("end_date") or item.get("date") else None)
    return item


def command_doctor(args):
    chat = chat_configuration()
    slot_probe = probe_background_slot(chat) if args.probe_slot else {
        "configured": chat["scheduling"]["enabled"],
        "available": None,
        "slot": chat["scheduling"]["backgroundSlot"],
        "detail": "pass --probe-slot to verify the configured background slot",
    }
    background_configured = bool(chat["enabled"] and chat["scheduling"]["enabled"] and chat["scheduling"]["interactiveSlot"] != chat["scheduling"]["backgroundSlot"])
    remediation = []
    if not chat["enabled"]:
        remediation.append("Enable connectedServices.chat for serial foreground extraction.")
    if chat["enabled"] and not chat["scheduling"]["enabled"]:
        remediation.append("Foreground extraction is available. Enable scheduling only if optional background processing is required.")
    if chat["scheduling"]["enabled"] and chat["scheduling"]["interactiveSlot"] == chat["scheduling"]["backgroundSlot"]:
        remediation.append("Configure different interactiveSlot and backgroundSlot values before using --background.")
    if args.probe_slot and slot_probe.get("available") is False:
        remediation.append("Omit --background, or repair the configured background slot before retrying it.")
    result = {
        "status": "ok",
        "python": sys.version.split()[0],
        "schemaVersion": RUN_SCHEMA_VERSION,
        "sourceExtensions": sorted(SOURCE_EXTENSIONS),
        "defaultPacketCharacters": DEFAULT_PACKET_CHARACTERS,
        "targetSourceTokens": TARGET_SOURCE_TOKENS,
        "foregroundAvailable": chat["enabled"],
        "backgroundAvailable": bool(background_configured and slot_probe.get("available") is not False),
        "chat": {"url": chat["url"], "model": chat["model"], "scheduling": chat["scheduling"], "backgroundSlotProbe": slot_probe},
        "remediation": remediation,
    }
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Project extraction OK (Python {result['python']})")
        print(f"Source formats: {', '.join(result['sourceExtensions'])}")


def scope_options(args):
    return {
        "focus": args.focus,
        "teams": args.team or [],
        "people": args.person or [],
        "workstreams": args.workstream or [],
        "includeSources": args.include_source or [],
        "excludeSources": args.exclude_source or [],
        "controlTypes": args.control_type or [],
        "dateFrom": args.date_from,
        "dateTo": args.date_to,
    }


def project_configuration(inputs, title, packet_characters, scope, inbox=None):
    options = {"title": title, "packetCharacters": packet_characters, "scope": scope}
    if inbox:
        options["inbox"] = str(Path(inbox).expanduser().resolve())
    return {
        "workflow": "project-extraction",
        "command": "init",
        "input": {"roots": [str(Path(value).expanduser().resolve()) for value in inputs]},
        "options": options,
    }


def project_items(manifest, results=None):
    results = results or {}
    return [
        {
            "id": packet["packetId"],
            "path": packet["path"],
            "sha256": packet["sha256"],
            "status": results.get(packet["packetId"], {}).get("status", "pending"),
            "attempts": 1 if packet["packetId"] in results else 0,
            "transient": False,
        }
        for packet in active_packets(manifest)
    ]


def project_input_drift(config, manifest):
    current = discover_sources(config["inputs"])
    before = [{"path": source["path"], "sha256": source["sha256"]} for source in active_sources(manifest)]
    after = [{"path": source["path"], "sha256": source["sha256"]} for source in current]
    return run_state.input_drift(before, after)


def command_init(args):
    if args.packet_chars < 1_000:
        fail("--packet-chars must be at least 1000")
    title = args.title or Path(args.inputs[0]).expanduser().stem or "Project"
    scope = scope_options(args)
    configuration = project_configuration(args.inputs, title, args.packet_chars, scope, args.inbox)
    run_directory = Path(args.output).expanduser().resolve()
    if run_directory.exists():
        try:
            existing_config = json.loads((run_directory / "run_config.json").read_text(encoding="utf-8"))
            if existing_config.get("schemaVersion") != RUN_SCHEMA_VERSION:
                raise ValueError(f"existing project-extraction run uses schema {existing_config.get('schemaVersion')}; create a new version-{RUN_SCHEMA_VERSION} extraction")
            state = run_state.load_run_state(run_directory, "project-extraction")
            run_state.assert_compatible_run(state, configuration)
        except (OSError, ValueError) as error:
            fail(str(error))
        chat = chat_configuration()
        print(json.dumps({"runDirectory": str(run_directory), "resumed": True, "status": state["status"], "phase": state["phase"], "nextAction": state.get("nextAction"), "foregroundAvailable": chat["enabled"], "nextCommand": f"project-extraction.py process {run_directory}" if chat["enabled"] else "Configure connectedServices.chat, then rerun process."}, indent=2))
        return
    run_directory.mkdir(parents=True)
    (run_directory / "working").mkdir()
    sources = discover_sources(args.inputs)
    manifest = initialize_manifest(run_directory, sources, args.packet_chars)
    config = {
        "schemaVersion": RUN_SCHEMA_VERSION,
        "title": title,
        "inputs": [str(Path(value).expanduser().resolve()) for value in args.inputs],
        "packetCharacters": args.packet_chars,
        "createdAt": utc_now(),
        "updatedAt": utc_now(),
        "asOfDate": None,
        "scope": scope,
        "scopeMode": "focused" if any(value for value in scope.values()) else "full",
        "worker": {
            "targetSourceTokens": TARGET_SOURCE_TOKENS,
            "charactersPerToken": DEFAULT_CHARS_PER_TOKEN,
            "promptTokenCeiling": DEFAULT_PROMPT_TOKEN_CEILING,
            "packetCharacters": args.packet_chars,
            "consecutiveNoCacheReuse": 0,
            "sessionId": stable_id("worker", str(run_directory)),
        },
    }
    if args.inbox:
        inbox = Path(args.inbox).expanduser().resolve()
        config["liveRepository"] = {
            "projectRoot": str(inbox.parent),
            "inbox": str(inbox),
            "publishDirectory": str(inbox.parent / "Sources" / "Inbox"),
            "originalsDirectory": str(inbox.parent / "Originals" / "Inbox"),
        }
    write_json(run_directory / "run_config.json", config)
    write_json(run_directory / "source_manifest.json", manifest)
    state = run_state.create_run_state(
        "project-extraction",
        "init",
        configuration["input"],
        configuration["options"],
        items=project_items(manifest),
        phase="extracting",
        next_action="next",
    )
    run_state.initialize_run_state(run_directory, state)
    append_source_changes(run_directory, None, manifest)
    initialize_live_repository(run_directory, config)
    print(
        json.dumps(
            {
                "runDirectory": str(run_directory),
                "sources": len(sources),
                "packets": len(active_packets(manifest)),
                "foregroundAvailable": chat_configuration()["enabled"],
                "nextAction": "inbox_status_then_process",
                "nextCommands": [
                    f"project-extraction.py inbox-status {run_directory}",
                    f"project-extraction.py process {run_directory}",
                ],
            },
            indent=2,
        )
    )


def command_next(args):
    run_directory = require_run_directory(args.run_directory)
    _, manifest = load_run(run_directory)
    results = current_results(run_directory, manifest)
    for packet in active_packets(manifest):
        if packet["packetId"] in results:
            continue
        source, packet_record = source_for_packet(manifest, packet["packetId"])
        print(
            json.dumps(
                {
                    "complete": False,
                    "packetId": packet_record["packetId"],
                    "packetPath": packet_record["path"],
                    "sourceId": source["sourceId"],
                    "sourcePath": source["path"],
                    "sourceRevision": source["revisionId"],
                    "locator": packet_record["locator"],
                    "allowedValues": {
                        "documentRoles": sorted(DOCUMENT_ROLES),
                        "itemTypes": list(ITEM_TYPES),
                        "commitmentLevels": sorted(COMMITMENT_LEVELS),
                        "dateKinds": sorted(DATE_KINDS),
                        "interpretations": sorted(INTERPRETATIONS),
                        "confidences": sorted(CONFIDENCES),
                    },
                    "extractionSchemaVersion": EXTRACTION_SCHEMA_VERSION,
                    "requiredResponseShape": {
                        "documentRole": "other",
                        "items": [
                            {
                                "item_type": "deliverable",
                                "title": "Source-backed title",
                                "date_kind": "none",
                                "date": None,
                                "commitment_level": "unclear",
                                "direct_quotes": ["Exact source excerpt"],
                                "interpretation": "explicit",
                                "confidence": "medium",
                            }
                        ],
                    },
                    "validateCommand": f"project-extraction.py validate-extraction {run_directory} --packet-id {packet_record['packetId']} --items-file <items.json>",
                },
                indent=2,
            )
        )
        return
    print(json.dumps({"complete": True, "packets": len(results)}, indent=2))


def command_record(args):
    run_directory = require_run_directory(args.run_directory)
    _, manifest = load_run(run_directory)
    source, packet = source_for_packet(manifest, args.packet_id)
    results = current_results(run_directory, manifest)
    if args.packet_id in results:
        fail(f"packet already has a disposition: {args.packet_id}")
    if args.status != "extracted":
        if not args.note:
            fail("non-success dispositions require --note")
        if re.search(r"\bunprocessed\b", args.note, flags=re.IGNORECASE):
            fail("generic unprocessed dispositions are not allowed; screen the packet or mark it needs_review")
        config, _ = load_run(run_directory)
        if args.status == "excluded_by_scope" and config.get("scopeMode") != "focused":
            fail("full-project runs cannot exclude packets by scope")
        screening = None
        if args.status == "screened_no_controls":
            screening_method = string_or_none(getattr(args, "screening_method", None))
            screening_source = string_or_none(getattr(args, "disposition_source", None))
            screening_finding = string_or_none(getattr(args, "screening_finding", None)) or args.note
            if screening_method not in {"model", "human"}:
                fail("screened_no_controls requires --screening-method model|human")
            if screening_source not in {"worker", "manual"}:
                fail("screened_no_controls requires --disposition-source worker|manual")
            if not screening_finding or DEFERRED_SCREENING_PATTERN.search(screening_finding):
                fail("screened_no_controls requires a substantive finding that the reviewed packet contains no project controls")
            screening = {
                "method": screening_method,
                "source": screening_source,
                "finding": screening_finding,
                "packetSha256": packet["sha256"],
                "documentRole": getattr(args, "document_role", None) or "other",
            }
        result = {
            "recordedAt": utc_now(),
            "packetId": args.packet_id,
            "sourceId": source["sourceId"],
            "sourceRevision": source["revisionId"],
            "status": args.status,
            "documentRole": "other",
            "items": [],
            "note": args.note,
            "dispositionSource": getattr(args, "disposition_source", None) or "manual",
            "screening": screening,
        }
    else:
        if not args.items_file:
            fail("success requires --items-file")
        raw = json.loads(Path(args.items_file).expanduser().read_text(encoding="utf-8"))
        if isinstance(raw, list):
            document_role = "other"
            raw_items = raw
        elif isinstance(raw, dict):
            document_role = raw.get("documentRole", "other")
            raw_items = raw.get("items")
        else:
            fail("items file must be an array or an object with documentRole and items")
        if document_role not in DOCUMENT_ROLES:
            fail(f"documentRole must be one of: {', '.join(sorted(DOCUMENT_ROLES))}")
        if not isinstance(raw_items, list):
            fail("items must be an array")
        items = [normalize_item(item, index) for index, item in enumerate(raw_items, 1)]
        normalized = []
        for index, item in enumerate(items, 1):
            if not item.get("locator"):
                item["locator"] = packet["locator"]
            item_key = json.dumps(item, sort_keys=True, ensure_ascii=False)
            evidence_id = stable_id("ev", f"{args.packet_id}|{index}|{item_key}")
            normalized.append({"evidenceId": evidence_id, **item})
        result = {
            "recordedAt": utc_now(),
            "packetId": args.packet_id,
            "packetSha256": packet["sha256"],
            "sourceId": source["sourceId"],
            "sourceRevision": source["revisionId"],
            "status": "extracted",
            "documentRole": document_role,
            "items": normalized,
            "note": args.note,
            "dispositionSource": getattr(args, "disposition_source", None) or "manual",
            "screening": None,
        }
        packet_text = Path(packet["path"]).read_text(encoding="utf-8")
        for item in normalized:
            exact_quotes = []
            for quote in item["direct_quotes"]:
                exact = exact_source_quote(packet_text, quote)
                if exact is None:
                    fail(f"direct quote is not uniquely present in frozen packet {args.packet_id}: {quote[:120]}")
                exact_quotes.append(exact)
            item["direct_quotes"] = exact_quotes
    append_jsonl(run_directory / "extraction_results.jsonl", result)
    def recorded(state):
        for item in state["items"]:
            if item["id"] == args.packet_id:
                item.update({"status": result["status"], "attempts": item.get("attempts", 0) + 1, "error": result.get("note") if result["status"] == "failed" else None})
        if all(item["status"] != "pending" for item in state["items"]):
            state["phase"] = "reconciling"
            state["nextAction"] = "reconcile"
        return state
    run_state.update_run_state(run_directory, recorded, {"type": "item_recorded", "itemId": args.packet_id, "status": result["status"]})
    print(json.dumps({"packetId": args.packet_id, "status": result["status"], "items": len(result["items"])}, indent=2))


def extraction_validation_errors(raw, packet_text):
    errors = []
    if isinstance(raw, list):
        document_role = "other"
        items = raw
    elif isinstance(raw, dict):
        document_role = raw.get("documentRole", "other")
        items = raw.get("items")
    else:
        return ["items file must be an array or an object with documentRole and items"]
    if document_role not in DOCUMENT_ROLES:
        errors.append(f"documentRole must be one of: {', '.join(sorted(DOCUMENT_ROLES))}")
    if not isinstance(items, list):
        return [*errors, "items must be an array"]
    for index, item in enumerate(items, 1):
        label = f"item {index}"
        if not isinstance(item, dict):
            errors.append(f"{label} must be an object")
            continue
        if item.get("item_type") not in ITEM_TYPE_SET:
            errors.append(f"{label} item_type must be one of: {', '.join(ITEM_TYPES)}")
        if not str(item.get("title") or "").strip():
            errors.append(f"{label} title is required")
        date_kind = item.get("date_kind") or "none"
        if date_kind not in DATE_KINDS:
            errors.append(f"{label} date_kind is invalid")
        if date_kind == "exact" and not is_iso_date(item.get("date")):
            errors.append(f"{label} exact date must be YYYY-MM-DD")
        if date_kind == "exact" and item.get("date") in (None, ""):
            errors.append(f"{label} exact date requires date")
        if date_kind != "exact" and item.get("date") not in (None, ""):
            errors.append(f"{label} must not normalize a non-exact date")
        if date_kind in {"relative", "conditional"} and not item.get("trigger"):
            errors.append(f"{label} {date_kind} date requires trigger text")
        if date_kind == "recurring" and not item.get("recurrence"):
            errors.append(f"{label} recurring date requires recurrence text")
        if (item.get("commitment_level") or "unclear") not in COMMITMENT_LEVELS:
            errors.append(f"{label} commitment_level is invalid")
        quotes = item.get("direct_quotes")
        if isinstance(quotes, str):
            quotes = [quotes]
        if not isinstance(quotes, list) or not quotes:
            errors.append(f"{label} direct_quotes must be a nonempty array")
        else:
            for quote_index, quote in enumerate(quotes, 1):
                if not isinstance(quote, str) or not quote.strip():
                    errors.append(f"{label} direct quote {quote_index} must be nonblank text")
                elif exact_source_quote(packet_text, quote.strip()) is None:
                    errors.append(f"{label} direct quote {quote_index} is not uniquely present in the frozen packet")
    return errors


def command_validate_extraction(args):
    run_directory = require_run_directory(args.run_directory)
    _, manifest = load_run(run_directory)
    _, packet = source_for_packet(manifest, args.packet_id)
    try:
        raw = json.loads(Path(args.items_file).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        result = {"valid": False, "errors": [f"items file is not valid JSON: {error}"]}
    else:
        errors = extraction_validation_errors(raw, Path(packet["path"]).read_text(encoding="utf-8"))
        result = {"valid": not errors, "errors": errors, "schemaVersion": EXTRACTION_SCHEMA_VERSION}
    print(json.dumps(result, indent=2))
    if not result["valid"]:
        raise SystemExit(1)


def evidence_rows(run_directory, manifest):
    results = current_results(run_directory, manifest)
    source_by_id = {source["sourceId"]: source for source in active_sources(manifest)}
    rows = []
    for packet in active_packets(manifest):
        result = results.get(packet["packetId"])
        if not result or result["status"] != "extracted":
            continue
        source = source_by_id[result["sourceId"]]
        for item in result["items"]:
            rows.append(
                {
                    "evidence_id": item["evidenceId"],
                    "source_id": source["sourceId"],
                    "source_revision": source["revisionId"],
                    "packet_id": packet["packetId"],
                    "source_path": source["path"],
                    "source_title": Path(source["path"]).stem,
                    "document_role": result["documentRole"],
                    **{key: value for key, value in item.items() if key != "evidenceId"},
                }
            )
    return sorted(rows, key=lambda row: (row["item_type"], row["source_id"], row["evidence_id"]))


def command_reconcile(args):
    run_directory = require_run_directory(args.run_directory)
    _, manifest = load_run(run_directory)
    results = current_results(run_directory, manifest)
    missing = [packet["packetId"] for packet in active_packets(manifest) if packet["packetId"] not in results]
    if missing and not getattr(args, "draft", False):
        fail(f"all extraction packets need dispositions before reconciliation; pending: {', '.join(missing[:5])}")
    rows = evidence_rows(run_directory, manifest)
    write_csv(run_directory / "evidence_items.csv", EVIDENCE_COLUMNS, rows)
    with (run_directory / "evidence_items.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    existing_controls = read_jsonl(run_directory / "controls.jsonl")
    existing_by_type = {}
    for control in existing_controls:
        existing_by_type.setdefault(control["control_type"], []).append(control)
    prior_manifest = {}
    review_manifest_path = run_directory / "review_manifest.json"
    if review_manifest_path.exists():
        prior_manifest = json.loads(review_manifest_path.read_text(encoding="utf-8"))
    prior_status = {row["reviewPacketId"]: row.get("status", "pending") for row in prior_manifest.get("packets", [])}
    review_directory = run_directory / "review_packets"
    review_directory.mkdir(exist_ok=True)
    review_packets = []
    for item_type in ITEM_TYPES:
        type_rows = [row for row in rows if row["item_type"] == item_type]
        for index in range(0, len(type_rows), 40):
            batch = type_rows[index : index + 40]
            evidence_ids = [row["evidence_id"] for row in batch]
            review_packet_id = stable_id("review", "|".join(evidence_ids))
            path = review_directory / f"{review_packet_id}.json"
            payload = {
                "reviewPacketId": review_packet_id,
                "controlType": item_type,
                "evidenceItems": batch,
                "existingControls": existing_by_type.get(item_type, []),
                "requiredDisposition": "Reference each evidence_id exactly once from a control or an explicit disposition.",
                "allowedDispositions": sorted(REVIEW_DISPOSITIONS),
                "controlIdPrefix": CONTROL_PREFIXES[item_type],
            }
            write_json(path, payload)
            status = "complete" if prior_status.get(review_packet_id) == "complete" and review_result(run_directory, review_packet_id) else "pending"
            review_packets.append(
                {
                    "reviewPacketId": review_packet_id,
                    "controlType": item_type,
                    "path": str(path),
                    "evidenceIds": evidence_ids,
                    "status": status,
                }
            )
    review_manifest = {"schemaVersion": 1, "createdAt": utc_now(), "packets": review_packets}
    write_json(review_manifest_path, review_manifest)
    print(json.dumps({"evidenceItems": len(rows), "reviewPackets": len(review_packets), "pending": sum(row["status"] == "pending" for row in review_packets), "draft": bool(getattr(args, "draft", False)), "undispositionedPackets": len(missing)}, indent=2))


def review_result(run_directory, review_packet_id):
    result = None
    for row in read_jsonl(run_directory / "control_review_results.jsonl"):
        if row.get("reviewPacketId") == review_packet_id:
            result = row
    return result


def load_review_manifest(run_directory):
    path = run_directory / "review_manifest.json"
    if not path.is_file():
        fail("reconcile must run before control review")
    return json.loads(path.read_text(encoding="utf-8"))


def command_next_review(args):
    run_directory = require_run_directory(args.run_directory)
    manifest = load_review_manifest(run_directory)
    for packet in manifest["packets"]:
        if packet["status"] == "complete" and review_result(run_directory, packet["reviewPacketId"]):
            continue
        print(json.dumps({"complete": False, **packet}, indent=2))
        return
    print(json.dumps({"complete": True, "reviewPackets": len(manifest["packets"])}, indent=2))


def string_or_none(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def string_list(value, label):
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        fail(f"{label} must be an array of nonblank strings")
    return [item.strip() for item in value]


def normalize_control(raw, expected_type, evidence_ids, index):
    if not isinstance(raw, dict):
        fail(f"control {index} must be an object")
    control_type = raw.get("control_type")
    if control_type != expected_type:
        fail(f"control {index} control_type must be {expected_type}")
    control_id = string_or_none(raw.get("control_id"))
    expected_prefix = CONTROL_PREFIXES[control_type]
    if not control_id or not re.fullmatch(rf"{expected_prefix}-\d{{3,}}", control_id):
        fail(f"control {index} control_id must match {expected_prefix}-NNN")
    title = string_or_none(raw.get("title"))
    if not title:
        fail(f"control {index} title is required")
    source_evidence_ids = string_list(raw.get("source_evidence_ids"), f"control {index} source_evidence_ids")
    if not source_evidence_ids:
        fail(f"control {index} must cite at least one evidence id")
    unknown_evidence = set(source_evidence_ids) - evidence_ids
    if unknown_evidence:
        fail(f"control {index} cites evidence outside this review packet: {', '.join(sorted(unknown_evidence))}")
    date_kind = raw.get("date_kind") or "none"
    if date_kind not in DATE_KINDS:
        fail(f"control {index} date_kind is invalid")
    normalized_date = validate_iso_date(raw.get("date"), f"control {index} date")
    if date_kind == "exact" and normalized_date is None:
        fail(f"control {index} exact date requires date")
    if date_kind != "exact" and normalized_date is not None:
        fail(f"control {index} must not normalize a non-exact date")
    trigger = string_or_none(raw.get("trigger"))
    recurrence = string_or_none(raw.get("recurrence"))
    offset_days = raw.get("offset_days")
    if date_kind in {"relative", "conditional"} and not trigger:
        fail(f"control {index} {date_kind} date requires trigger text")
    if date_kind == "recurring" and not recurrence:
        fail(f"control {index} recurring date requires recurrence text")
    if offset_days not in (None, "") and not isinstance(offset_days, int):
        fail(f"control {index} offset_days must be an integer or null")
    commitment_level = raw.get("commitment_level") or "unclear"
    if commitment_level not in COMMITMENT_LEVELS:
        fail(f"control {index} commitment_level is invalid")
    relationships = raw.get("relationships") or {}
    if not isinstance(relationships, dict):
        fail(f"control {index} relationships must be an object")
    duration_days = raw.get("duration_days")
    if duration_days not in (None, "") and (not isinstance(duration_days, int) or duration_days < 0):
        fail(f"control {index} duration_days must be a nonnegative integer or null")
    scope_relation = string_or_none(raw.get("scope_relation")) or "full"
    if scope_relation not in {"direct", "dependency", "shared", "full"}:
        fail(f"control {index} scope_relation is invalid")
    return {
        "control_id": control_id,
        "control_type": control_type,
        "title": title,
        "description": string_or_none(raw.get("description")),
        "owner": string_or_none(raw.get("owner")),
        "recipient": string_or_none(raw.get("recipient")),
        "date_text": string_or_none(raw.get("date_text")),
        "date_kind": date_kind,
        "date": normalized_date,
        "trigger": trigger,
        "offset_days": offset_days,
        "recurrence": recurrence,
        "acceptance_criteria": string_or_none(raw.get("acceptance_criteria")),
        "evidence_required": string_or_none(raw.get("evidence_required")),
        "source_status": string_or_none(raw.get("source_status")),
        "commitment_level": commitment_level,
        "source_evidence_ids": source_evidence_ids,
        "parent_control_ids": string_list(relationships.get("parent"), f"control {index} relationships.parent"),
        "depends_on_control_ids": string_list(relationships.get("depends_on"), f"control {index} relationships.depends_on"),
        "satisfies_control_ids": string_list(relationships.get("satisfies"), f"control {index} relationships.satisfies"),
        "supersedes_control_ids": string_list(relationships.get("supersedes"), f"control {index} relationships.supersedes"),
        "conflicts_with_control_ids": string_list(relationships.get("conflicts_with"), f"control {index} relationships.conflicts_with"),
        "notes": string_or_none(raw.get("notes")),
        "teams": string_list(raw.get("teams"), f"control {index} teams"),
        "workstreams": string_list(raw.get("workstreams"), f"control {index} workstreams"),
        "scope_relation": scope_relation,
        "start_date": validate_iso_date(raw.get("start_date"), f"control {index} start_date"),
        "end_date": validate_iso_date(raw.get("end_date"), f"control {index} end_date"),
        "duration_days": None if duration_days in (None, "") else duration_days,
        "schedule_basis": string_or_none(raw.get("schedule_basis")),
    }


def command_record_review(args):
    run_directory = require_run_directory(args.run_directory)
    manifest = load_review_manifest(run_directory)
    raw = json.loads(Path(args.review_file).expanduser().read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        fail("review file must be an object")
    review_packet_id = raw.get("reviewPacketId")
    packet = next((row for row in manifest["packets"] if row["reviewPacketId"] == review_packet_id), None)
    if packet is None:
        fail(f"unknown reviewPacketId: {review_packet_id}")
    if packet["status"] == "complete" and review_result(run_directory, review_packet_id):
        fail(f"review packet is already complete: {review_packet_id}")
    evidence_ids = set(packet["evidenceIds"])
    controls_raw = raw.get("controls") or []
    dispositions_raw = raw.get("dispositions") or []
    if not isinstance(controls_raw, list) or not isinstance(dispositions_raw, list):
        fail("controls and dispositions must be arrays")
    controls = [normalize_control(value, packet["controlType"], evidence_ids, index) for index, value in enumerate(controls_raw, 1)]
    observed_control_ids = set()
    covered = []
    for control in controls:
        if control["control_id"] in observed_control_ids:
            fail(f"duplicate control_id in review file: {control['control_id']}")
        observed_control_ids.add(control["control_id"])
        covered.extend(control["source_evidence_ids"])
    dispositions = []
    for index, value in enumerate(dispositions_raw, 1):
        if not isinstance(value, dict):
            fail(f"disposition {index} must be an object")
        evidence_id = value.get("evidence_id")
        disposition = value.get("disposition")
        if evidence_id not in evidence_ids:
            fail(f"disposition {index} evidence_id is outside this review packet")
        if disposition not in REVIEW_DISPOSITIONS:
            fail(f"disposition {index} disposition is invalid")
        control_id = string_or_none(value.get("control_id"))
        if disposition != "contextual" and not control_id:
            fail(f"disposition {index} {disposition} requires control_id")
        dispositions.append(
            {
                "evidence_id": evidence_id,
                "disposition": disposition,
                "control_id": control_id,
                "note": string_or_none(value.get("note")),
            }
        )
        covered.append(evidence_id)
    duplicates = sorted({value for value in covered if covered.count(value) > 1})
    missing = sorted(evidence_ids - set(covered))
    if duplicates:
        fail(f"evidence ids were dispositioned more than once: {', '.join(duplicates)}")
    if missing:
        fail(f"evidence ids are missing a control or disposition: {', '.join(missing)}")
    result = {
        "recordedAt": utc_now(),
        "reviewPacketId": review_packet_id,
        "controlType": packet["controlType"],
        "controls": controls,
        "dispositions": dispositions,
    }
    append_jsonl(run_directory / "control_review_results.jsonl", result)
    packet["status"] = "complete"
    write_json(run_directory / "review_manifest.json", manifest)
    print(json.dumps({"reviewPacketId": review_packet_id, "controls": len(controls), "dispositions": len(dispositions)}, indent=2))


def current_review_results(run_directory, review_manifest):
    packet_ids = {row["reviewPacketId"] for row in review_manifest["packets"]}
    results = {}
    for result in read_jsonl(run_directory / "control_review_results.jsonl"):
        if result.get("reviewPacketId") in packet_ids:
            results[result["reviewPacketId"]] = result
    return results


def merge_controls(run_directory, review_manifest, evidence_ids):
    controls = {
        row["control_id"]: row
        for row in read_jsonl(run_directory / "controls.jsonl")
        if set(row.get("source_evidence_ids", [])) <= evidence_ids
    }
    for result in current_review_results(run_directory, review_manifest).values():
        for control in result["controls"]:
            controls[control["control_id"]] = control
    return sorted(controls.values(), key=lambda row: row["control_id"])


def validate_relationships(controls):
    control_ids = {row["control_id"] for row in controls}
    for control in controls:
        for field in (
            "parent_control_ids",
            "depends_on_control_ids",
            "satisfies_control_ids",
            "supersedes_control_ids",
            "conflicts_with_control_ids",
        ):
            unknown = set(control.get(field, [])) - control_ids
            if unknown:
                fail(f"{control['control_id']} {field} references unknown controls: {', '.join(sorted(unknown))}")


def control_csv_rows(controls):
    return [{column: row.get(column) for column in CONTROL_COLUMNS} for row in controls]


def append_status_review_note(row):
    marker = "Status review required after source refresh."
    notes = (row.get("notes") or "").strip()
    if marker not in notes:
        row["notes"] = f"{notes} {marker}".strip()


def write_status_overlay(run_directory, controls, previous_controls):
    path = run_directory / "project_status.csv"
    existing = read_csv(path)
    existing_ids = {row.get("control_id") for row in existing}
    current_by_id = {row["control_id"]: row for row in controls}
    previous_by_id = {row["control_id"]: row for row in previous_controls}
    rows = list(existing)
    for row in rows:
        control_id = row.get("control_id")
        current = current_by_id.get(control_id)
        previous = previous_by_id.get(control_id)
        if previous and (current is None or current != previous):
            append_status_review_note(row)
    for control in controls:
        if control["control_id"] not in existing_ids:
            rows.append(
                {
                    "control_id": control["control_id"],
                    "current_owner": "",
                    "working_status": "unknown",
                    "forecast_date": "",
                    "forecast_start_date": "",
                    "forecast_end_date": "",
                    "last_updated": "",
                    "notes": "",
                }
            )
    write_csv(path, STATUS_COLUMNS, rows)


def conflicts_and_gaps(controls):
    rows = []
    for control in controls:
        control_id = control["control_id"]
        for conflicting in control.get("conflicts_with_control_ids", []):
            rows.append({"control_id": control_id, "issue_type": "source_conflict", "detail": f"Conflicts with {conflicting}"})
        if control["control_type"] == "deadline" and control.get("date_kind") == "none":
            rows.append({"control_id": control_id, "issue_type": "missing_date", "detail": "Deadline has no resolved or conditional date"})
        if control["control_type"] == "deliverable":
            if not control.get("owner"):
                rows.append({"control_id": control_id, "issue_type": "missing_owner", "detail": "Deliverable has no source-backed owner"})
            if control.get("date_kind") == "none":
                rows.append({"control_id": control_id, "issue_type": "missing_date", "detail": "Deliverable has no source-backed date"})
            if not control.get("acceptance_criteria"):
                rows.append({"control_id": control_id, "issue_type": "missing_acceptance", "detail": "Deliverable has no source-backed acceptance criteria"})
        if control.get("commitment_level") == "unclear":
            rows.append({"control_id": control_id, "issue_type": "unclear_commitment", "detail": "Requirement or commitment authority is unclear"})
    return rows


def scaffold_markdown(run_directory, proposal_present):
    for filename, body in MARKDOWN_TEMPLATES.items():
        path = run_directory / filename
        if not path.exists():
            path.write_text(body, encoding="utf-8")
    proposal_path = run_directory / "proposal_checklist.md"
    if proposal_present and not proposal_path.exists():
        proposal_path.write_text(PROPOSAL_TEMPLATE, encoding="utf-8")


def markdown_control_list(controls):
    if not controls:
        return "No source-backed controls were identified."
    lines = []
    for control in controls:
        timing = control.get("date") or control.get("date_text") or control.get("end_date") or "unscheduled"
        lines.append(f"- **{control['control_id']} — {control['title']}** ({timing}; {control.get('commitment_level') or 'unclear'})")
    return "\n".join(lines)


def draft_banner(coverage):
    if not coverage or not coverage.get("draft"):
        return ""
    return (
        "> [!WARNING] Incomplete draft\n"
        f"> Coverage: {coverage['coveredPackets']}/{coverage['packets']} packets "
        f"({coverage['coveragePercent']}%). Blocking states: "
        f"{json.dumps(coverage['blockingCounts'], sort_keys=True)}. Do not treat this as a complete extraction.\n\n"
    )


def author_markdown_briefs(run_directory, config, controls, proposal_present, coverage=None):
    by_type = {item_type: [row for row in controls if row["control_type"] == item_type] for item_type in ITEM_TYPES}
    source_note = "Document roles do not establish legal precedence. Review conflicts and source evidence before relying on these controls."
    files = {
        "project_brief.md": f"# Project Brief\n\n## Purpose and Scope\n\n{config['title']} project-control extraction as of {config.get('asOfDate') or 'unspecified'}.\n\n## Objectives and Outcomes\n\n{markdown_control_list(by_type['objective'] + by_type['outcome'])}\n\n## Source Authority and Limits\n\n{source_note}\n",
        "deliverables_and_dates.md": f"# Deliverables and Dates\n\n## Deliverables\n\n{markdown_control_list(by_type['deliverable'])}\n\n## Milestones and Deadlines\n\n{markdown_control_list(by_type['milestone'] + by_type['deadline'])}\n\n## Conditional and Recurring Dates\n\n{markdown_control_list([row for row in controls if row.get('date_kind') in {'relative', 'conditional', 'recurring'}])}\n",
        "compliance_and_reporting.md": f"# Compliance and Reporting\n\n## Requirements\n\n{markdown_control_list(by_type['requirement'])}\n\n## Reporting Calendar\n\n{markdown_control_list(by_type['reporting_requirement'])}\n\n## Acceptance and Evidence\n\n{markdown_control_list(by_type['acceptance_criterion'])}\n",
        "status_brief.md": f"# Status Brief\n\n## Current Position\n\nStatus is maintained in `project_status.csv`; extraction does not infer completion.\n\n## Upcoming and Overdue\n\n{markdown_control_list([row for row in controls if row.get('date') or row.get('end_date')])}\n\n## Decisions or Support Needed\n\n{markdown_control_list(by_type['decision'] + by_type['open_question'])}\n",
        "decisions_and_open_questions.md": f"# Decisions and Open Questions\n\n## Decisions\n\n{markdown_control_list(by_type['decision'])}\n\n## Open Questions\n\n{markdown_control_list(by_type['open_question'])}\n\n## Source Conflicts\n\n{markdown_control_list([row for row in controls if row.get('conflicts_with_control_ids')])}\n",
        "risks_issues_dependencies.md": f"# Risks, Issues, and Dependencies\n\n## Risks\n\n{markdown_control_list(by_type['risk'])}\n\n## Issues\n\n{markdown_control_list(by_type['issue'])}\n\n## Assumptions and Dependencies\n\n{markdown_control_list(by_type['assumption'] + by_type['dependency'])}\n",
    }
    if proposal_present:
        files["proposal_checklist.md"] = f"# Proposal Checklist\n\n## Eligibility and Submission Requirements\n\n{markdown_control_list(by_type['proposal_requirement'])}\n\n## Required Components\n\n{markdown_control_list(by_type['deliverable'] + by_type['requirement'])}\n\n## Review Criteria and Open Questions\n\n{markdown_control_list(by_type['acceptance_criterion'] + by_type['open_question'])}\n"
    banner = draft_banner(coverage)
    for filename, body in files.items():
        lines = body.splitlines(keepends=True)
        rendered = lines[0] + "\n" + banner + "".join(lines[1:]) if banner and lines else body
        run_state.atomic_write_text(run_directory / filename, rendered)


GANTT_COLUMNS = (
    "control_id", "title", "section", "start_date", "end_date", "duration_days", "date_basis", "scheduled",
    "working_status", "owner", "teams", "workstreams", "depends_on", "source_evidence_ids",
)


def gantt_rows(run_directory, controls):
    status = {row.get("control_id"): row for row in read_csv(run_directory / "project_status.csv")}
    rows = []
    for control in controls:
        overlay = status.get(control["control_id"], {})
        source_start = control.get("start_date")
        source_end = control.get("end_date") or control.get("date")
        forecast_start = overlay.get("forecast_start_date") or None
        forecast_end = overlay.get("forecast_end_date") or overlay.get("forecast_date") or None
        start = source_start or forecast_start
        end = source_end or forecast_end
        basis = "source" if source_start or source_end else "human_forecast" if forecast_start or forecast_end else "unscheduled"
        if start is None and end is not None:
            start = end
        section_values = control.get("workstreams") or control.get("teams") or ["Unassigned"]
        rows.append(
            {
                "control_id": control["control_id"],
                "title": control["title"],
                "section": section_values[0],
                "start_date": start,
                "end_date": end,
                "duration_days": control.get("duration_days"),
                "date_basis": basis,
                "scheduled": "yes" if start or end else "no",
                "working_status": overlay.get("working_status") or "unknown",
                "owner": overlay.get("current_owner") or control.get("owner"),
                "teams": control.get("teams", []),
                "workstreams": control.get("workstreams", []),
                "depends_on": control.get("depends_on_control_ids", []),
                "source_evidence_ids": control.get("source_evidence_ids", []),
            }
        )
    return rows


def mermaid_text(rows, coverage=None):
    lines = ["# Project Gantt", ""]
    if coverage and coverage.get("draft"):
        lines.extend(draft_banner(coverage).rstrip().splitlines())
        lines.append("")
    lines.extend(["```mermaid", "gantt", "    title Source-backed project schedule", "    dateFormat YYYY-MM-DD"])
    scheduled = [row for row in rows if row["scheduled"] == "yes"]
    for section in sorted({row["section"] for row in scheduled}):
        lines.append(f"    section {section.replace(':', ' - ')}")
        for row in [value for value in scheduled if value["section"] == section]:
            title = row["title"].replace(":", " - ").replace("\n", " ")
            gantt_id = re.sub(r"[^A-Za-z0-9_]", "_", row["control_id"])
            if row["start_date"] == row["end_date"] or not row["end_date"]:
                lines.append(f"    {title} :milestone, {gantt_id}, {row['start_date']}, 0d")
            else:
                lines.append(f"    {title} :{gantt_id}, {row['start_date']}, {row['end_date']}")
    lines.extend(["```", "", "## Unscheduled", "", markdown_control_list([{"control_id": row["control_id"], "title": row["title"], "commitment_level": "", "date_text": None} for row in rows if row["scheduled"] == "no"]), ""])
    return "\n".join(lines)


def gantt_html(rows, coverage=None):
    safe_rows = []
    for row in rows:
        safe_rows.append({key: [html.escape(str(item), quote=True) for item in value] if isinstance(value, list) else html.escape(str(value), quote=True) if value is not None else None for key, value in row.items()})
    payload = json.dumps(safe_rows, ensure_ascii=False).replace("</", "<\\/")
    draft_html = ""
    if coverage and coverage.get("draft"):
        draft_html = (
            f'<div class="draft">Incomplete draft: {coverage["coveredPackets"]}/{coverage["packets"]} packets covered '
            f'({coverage["coveragePercent"]}%). Do not treat this schedule as complete.</div>'
        )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Project Gantt</title><style>
body{{font:14px system-ui;margin:2rem;color:#17202a}} .controls{{display:flex;gap:1rem;flex-wrap:wrap;margin-bottom:1rem}}
.draft{{border:2px solid #a65f00;background:#fff4d6;padding:1rem;margin-bottom:1rem;font-weight:600}}
table{{border-collapse:collapse;width:100%}} th,td{{border:1px solid #ccd1d1;padding:.45rem;text-align:left}} th{{background:#eef2f3}}
.timeline{{min-width:24rem}} .bar{{height:1rem;background:#315b7d;border-radius:3px;transform-origin:left}} .forecast{{background:#b36b00}} .unscheduled{{color:#666}}
</style></head><body><h1>Project Gantt</h1>
{draft_html}
<div class="controls"><label>Search <input id="search"></label><label>Section <select id="section"><option value="">All</option></select></label><label>Status <select id="status"><option value="">All</option></select></label><label>Zoom <input id="zoom" type="range" min="50" max="200" value="100"></label></div>
<table><thead><tr><th>ID</th><th>Task</th><th>Section</th><th>Dates</th><th class="timeline">Timeline</th><th>Status</th><th>Dependencies</th><th>Evidence</th></tr></thead><tbody id="rows"></tbody></table>
<script>const data={payload};const q=id=>document.getElementById(id);const unique=k=>[...new Set(data.map(x=>x[k]).filter(Boolean))].sort();
for(const k of ['section','status']){{for(const v of unique(k==='status'?'working_status':k)){{const o=document.createElement('option');o.value=o.textContent=v;q(k).append(o)}}}}
function render(){{const text=q('search').value.toLowerCase(),section=q('section').value,status=q('status').value,zoom=+q('zoom').value;q('rows').replaceChildren();for(const x of data){{if(text&&!JSON.stringify(x).toLowerCase().includes(text)||section&&x.section!==section||status&&x.working_status!==status)continue;const tr=document.createElement('tr');const dates=x.scheduled==='yes'?`${{x.start_date||''}} – ${{x.end_date||x.start_date||''}}`:'Unscheduled';const width=x.scheduled==='yes'?Math.max(8,Math.min(100,(x.duration_days||1)*3))*zoom/100:0;tr.innerHTML=`<td>${{x.control_id}}</td><td>${{x.title}}</td><td>${{x.section}}</td><td>${{dates}} (${{x.date_basis}})</td><td>${{width?`<div class="bar ${{x.date_basis==='human_forecast'?'forecast':''}}" style="width:${{width}}%" title="${{dates}}"></div>`:'<span class="unscheduled">Not plotted</span>'}}</td><td>${{x.working_status}}</td><td>${{(x.depends_on||[]).join(', ')}}</td><td>${{(x.source_evidence_ids||[]).join(', ')}}</td>`;q('rows').append(tr)}}}}
for(const id of ['search','section','status','zoom'])q(id).addEventListener('input',render);render();</script></body></html>"""


def write_gantt_outputs(run_directory, controls, coverage=None):
    rows = gantt_rows(run_directory, controls)
    write_csv(run_directory / "gantt.csv", GANTT_COLUMNS, rows)
    run_state.atomic_write_text(run_directory / "gantt.md", mermaid_text(rows, coverage))
    run_state.atomic_write_text(run_directory / "gantt.html", gantt_html(rows, coverage))


def write_source_changes(run_directory):
    rows = []
    for row in read_jsonl(run_directory / "source_history.jsonl"):
        rows.append(
            {
                "changed_at": row.get("changedAt"),
                "change_type": row.get("changeType"),
                "source_id": row.get("sourceId"),
                "relative_path": row.get("relativePath"),
                "previous_revision": row.get("previousRevision"),
                "current_revision": row.get("currentRevision"),
            }
        )
    write_csv(run_directory / "source_changes.csv", SOURCE_CHANGE_COLUMNS, rows)


def search_tokens(value):
    return re.findall(r"[\w-]+", str(value or "").lower(), flags=re.UNICODE)


def search_source_chunks(source, packet):
    text = Path(packet["path"]).read_text(encoding="utf-8")
    if not text:
        return [(0, 0, "")]
    chunks = []
    start = 0
    while start < len(text):
        end = min(len(text), start + SEARCH_CHUNK_CHARACTERS)
        if end < len(text):
            boundary = text.rfind("\n\n", start, end)
            if boundary > start + SEARCH_CHUNK_CHARACTERS // 2:
                end = boundary + 2
        chunks.append((start, end, text[start:end]))
        if end >= len(text):
            break
        start = max(start + 1, end - SEARCH_CHUNK_OVERLAP)
    return chunks


def search_index_documents(run_directory, manifest):
    evidence = read_csv(run_directory / "evidence_items.csv")
    evidence_by_id = {row.get("evidence_id"): row for row in evidence}
    status = {row.get("control_id"): row for row in read_csv(run_directory / "project_status.csv")}
    documents = []
    for row in read_jsonl(run_directory / "controls.jsonl"):
        evidence_rows_for_control = [evidence_by_id[value] for value in row.get("source_evidence_ids", []) if value in evidence_by_id]
        source_paths = sorted({value.get("source_path") for value in evidence_rows_for_control if value.get("source_path")})
        locators = [value.get("locator") for value in evidence_rows_for_control if value.get("locator")]
        overlay = status.get(row["control_id"], {})
        text = "\n".join(
            value
            for value in (
                row.get("control_id"),
                row.get("control_type"),
                row.get("title"),
                row.get("description"),
                row.get("date_text"),
                row.get("acceptance_criteria"),
                row.get("evidence_required"),
                row.get("notes"),
                " ".join(row.get("teams") or []),
                " ".join(row.get("workstreams") or []),
                overlay.get("current_owner"),
                overlay.get("working_status"),
                overlay.get("notes"),
            )
            if value
        )
        documents.append(
            {
                "hitId": f"control:{row['control_id']}",
                "kind": "control",
                "title": f"{row['control_id']} — {row['title']}",
                "text": text,
                "sourcePaths": source_paths,
                "locators": locators,
                "controlIds": [row["control_id"]],
                "evidenceIds": row.get("source_evidence_ids", []),
                "packetId": None,
                "sourceRevision": None,
            }
        )
    for row in evidence:
        quotes = row.get("direct_quotes") or ""
        text = "\n".join(value for value in (row.get("title"), row.get("description"), quotes, row.get("date_text"), row.get("notes")) if value)
        documents.append(
            {
                "hitId": f"evidence:{row['evidence_id']}",
                "kind": "evidence",
                "title": row.get("title") or row["evidence_id"],
                "text": text,
                "sourcePaths": [row["source_path"]] if row.get("source_path") else [],
                "locators": [row.get("locator")] if row.get("locator") else [],
                "controlIds": [],
                "evidenceIds": [row["evidence_id"]],
                "packetId": row.get("packet_id"),
                "sourceRevision": row.get("source_revision"),
                "directQuotes": quotes.split(";") if quotes else [],
            }
        )
    for source in active_sources(manifest):
        for packet in source.get("packets", []):
            for index, (start, end, text) in enumerate(search_source_chunks(source, packet), 1):
                documents.append(
                    {
                        "hitId": f"source:{packet['packetId']}:{index}",
                        "kind": "source",
                        "title": f"{Path(source['path']).stem} — excerpt {index}",
                        "text": text,
                        "sourcePaths": [source["path"]],
                        "locators": [{"packet": packet["locator"], "characterStart": start, "characterEnd": end}],
                        "controlIds": [],
                        "evidenceIds": [],
                        "packetId": packet["packetId"],
                        "sourceRevision": source["revisionId"],
                    }
                )
    for document in documents:
        document["contentSha256"] = sha256_bytes(f"{document['title']}\n{document['text']}".encode("utf-8"))
    return sorted(documents, key=lambda row: row["hitId"])


def search_index_fingerprint(run_directory, manifest):
    values = {
        "sources": [
            {
                "sourceId": source["sourceId"],
                "revisionId": source["revisionId"],
                "packets": [(packet["packetId"], packet["sha256"]) for packet in source.get("packets", [])],
            }
            for source in active_sources(manifest)
        ],
        "controls": sha256_file(run_directory / "controls.jsonl") if (run_directory / "controls.jsonl").is_file() else None,
        "evidence": sha256_file(run_directory / "evidence_items.csv") if (run_directory / "evidence_items.csv").is_file() else None,
        "status": sha256_file(run_directory / "project_status.csv") if (run_directory / "project_status.csv").is_file() else None,
    }
    return sha256_bytes(json.dumps(values, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def search_index_ready(run_directory, manifest):
    results = current_results(run_directory, manifest)
    return all(packet["packetId"] in results for packet in active_packets(manifest)) and all(
        (run_directory / filename).is_file() for filename in ("controls.jsonl", "evidence_items.csv", "project_status.csv")
    )


def build_search_index(run_directory, force=False, allow_incomplete=False):
    _, manifest = load_run(run_directory)
    built_artifacts = all((run_directory / filename).is_file() for filename in ("controls.jsonl", "evidence_items.csv", "project_status.csv"))
    if not search_index_ready(run_directory, manifest) and not (allow_incomplete and built_artifacts):
        fail("search indexing requires all active packets and built control artifacts")
    documents = search_index_documents(run_directory, manifest)
    model = forge_embeddings.model_name()
    cache_path = run_directory / "working" / "search_embeddings.json"
    existing = {}
    if cache_path.is_file() and not force:
        raw_cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if raw_cache.get("model") == model:
            existing = raw_cache.get("entries") or {}
    entries = {}
    missing = []
    reused = 0
    for document in documents:
        cached = existing.get(document["hitId"])
        if cached and cached.get("contentSha256") == document["contentSha256"] and isinstance(cached.get("vector"), list):
            entries[document["hitId"]] = cached
            reused += 1
        else:
            missing.append(document)
    embedding_info = {"enabled": bool(entries), "model": model, "embedded": 0, "reused": reused, "reason": None}
    if missing:
        embedded = forge_embeddings.embed_texts(
            [f"{row['title']}\n{row['text'][:SEARCH_EMBED_CHARACTERS]}" for row in missing],
            timeout=5.0,
        )
        if embedded.get("ok"):
            for document, vector in zip(missing, embedded["vectors"]):
                entries[document["hitId"]] = {"contentSha256": document["contentSha256"], "vector": vector}
            embedding_info.update({"enabled": True, "model": embedded["model"], "dimensions": embedded["dimensions"], "embedded": len(missing)})
        else:
            embedding_info["reason"] = embedded.get("reason")
    write_json(cache_path, {"schemaVersion": 1, "model": model, "entries": entries})
    run_state.atomic_write_text(
        run_directory / "search_index.jsonl",
        "" if not documents else "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in documents) + "\n",
    )
    meta = {
        "schemaVersion": 1,
        "generatedAt": utc_now(),
        "fingerprint": search_index_fingerprint(run_directory, manifest),
        "documents": len(documents),
        "kinds": dict(Counter(row["kind"] for row in documents)),
        "embeddings": embedding_info,
        "draft": allow_incomplete and not search_index_ready(run_directory, manifest),
    }
    write_json(run_directory / "search_index_meta.json", meta)
    return meta


def ensure_search_index(run_directory):
    _, manifest = load_run(run_directory)
    meta_path = run_directory / "search_index_meta.json"
    current_fingerprint = search_index_fingerprint(run_directory, manifest)
    if meta_path.is_file() and (run_directory / "search_index.jsonl").is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("fingerprint") == current_fingerprint:
            return meta, False
        if not search_index_ready(run_directory, manifest):
            return meta, True
    return build_search_index(run_directory), False


def lexical_search_scores(query, documents):
    query_terms = search_tokens(query)
    if not query_terms:
        return {}
    token_counts = [Counter(search_tokens(f"{row['title']} {row['text']}")) for row in documents]
    document_frequency = Counter()
    for counts in token_counts:
        document_frequency.update(set(counts))
    total = max(1, len(documents))
    scores = {}
    for document, counts in zip(documents, token_counts):
        length = max(1, sum(counts.values()))
        score = 0.0
        for term in query_terms:
            frequency = counts.get(term, 0)
            if frequency:
                inverse = 1.0 + math.log((total + 1) / (document_frequency[term] + 1))
                score += inverse * frequency / (frequency + 1.2 * (0.25 + 0.75 * length / 200))
        if score:
            scores[document["hitId"]] = score
    return scores


def semantic_search_scores(run_directory, query):
    cache_path = run_directory / "working" / "search_embeddings.json"
    if not cache_path.is_file():
        return {}, "embedding cache is missing"
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    result = forge_embeddings.embed_texts([query], model=cache.get("model"), timeout=5.0)
    if not result.get("ok"):
        return {}, result.get("reason")
    query_vector = forge_embeddings.normalize(result["vectors"][0])
    return {
        hit_id: forge_embeddings.cosine(query_vector, forge_embeddings.normalize(row["vector"]))
        for hit_id, row in (cache.get("entries") or {}).items()
        if isinstance(row.get("vector"), list)
    }, None


def ranked_search_hits(run_directory, query, limit):
    documents = read_jsonl(run_directory / "search_index.jsonl")
    by_id = {row["hitId"]: row for row in documents}
    lexical = lexical_search_scores(query, documents)
    semantic, semantic_warning = semantic_search_scores(run_directory, query)
    lexical_rank = {hit_id: index for index, (hit_id, _) in enumerate(sorted(lexical.items(), key=lambda row: (-row[1], row[0])), 1)}
    semantic_rank = {hit_id: index for index, (hit_id, _) in enumerate(sorted(semantic.items(), key=lambda row: (-row[1], row[0])), 1)}
    query_lower = query.lower().strip()
    ranked = []
    for hit_id in set(lexical_rank) | set(semantic_rank):
        document = by_id.get(hit_id)
        if document is None:
            continue
        score = 0.0
        if hit_id in lexical_rank:
            score += 1 / (SEARCH_RRF_K + lexical_rank[hit_id])
        if hit_id in semantic_rank:
            score += 1 / (SEARCH_RRF_K + semantic_rank[hit_id])
        if query_lower == hit_id.lower() or query_lower in {value.lower() for value in document.get("controlIds", []) + document.get("evidenceIds", [])}:
            score += 1
        elif query_lower and query_lower in document["title"].lower():
            score += 0.25
        snippet = re.sub(r"\s+", " ", document.get("text", "")).strip()[:500]
        ranked.append(
            {
                **{key: value for key, value in document.items() if key != "text"},
                "score": round(score, 8),
                "lexicalScore": round(lexical.get(hit_id, 0), 8),
                "semanticScore": round(semantic.get(hit_id), 8) if hit_id in semantic else None,
                "snippet": snippet,
            }
        )
    ranked.sort(key=lambda row: (-row["score"], row["hitId"]))
    return ranked[:limit], semantic_warning


def command_index(args):
    run_directory = require_run_directory(args.run_directory)
    meta = build_search_index(run_directory, force=args.rebuild)
    print(json.dumps({"runDirectory": str(run_directory), **meta, "inbox": inbox_status_value(run_directory)}, indent=2))


def command_search(args):
    run_directory = require_run_directory(args.run_directory)
    if args.limit < 1:
        fail("--limit must be at least 1")
    meta, stale = ensure_search_index(run_directory)
    hits, semantic_warning = ranked_search_hits(run_directory, args.query, args.limit)
    inbox = inbox_status_value(run_directory)
    warnings = []
    if stale:
        warnings.append("search index is stale because refreshed project controls are not built yet")
    if inbox.get("pendingCount") or inbox.get("activeBatch"):
        warnings.append("Inbox intake is pending; results cover the last completed project index")
    if semantic_warning:
        warnings.append(f"semantic ranking unavailable; lexical results remain available: {semantic_warning}")
    print(
        json.dumps(
            {
                "query": args.query,
                "runDirectory": str(run_directory),
                "indexGeneratedAt": meta.get("generatedAt"),
                "indexStale": stale,
                "ranking": "hybrid" if not semantic_warning else "lexical",
                "hits": hits,
                "warnings": warnings,
                "inbox": inbox,
                "nextAction": "Use show on relevant hit IDs; load full sources only when the passages are insufficient.",
            },
            indent=2,
        )
    )


def command_show(args):
    run_directory = require_run_directory(args.run_directory)
    ensure_search_index(run_directory)
    hit = next((row for row in read_jsonl(run_directory / "search_index.jsonl") if row.get("hitId") == args.hit_id), None)
    if hit is None:
        fail(f"search hit not found: {args.hit_id}")
    result = {"hit": hit, "fullSources": []}
    if args.full_source:
        for raw_path in hit.get("sourcePaths", []):
            path = Path(raw_path)
            if not path.is_file():
                result["fullSources"].append({"path": raw_path, "error": "source file is missing"})
                continue
            result["fullSources"].append({"path": raw_path, "sha256": sha256_file(path), "text": path.read_text(encoding="utf-8")})
    print(json.dumps(result, indent=2))


def command_build(args):
    run_directory = require_run_directory(args.run_directory)
    config, manifest = load_run(run_directory)
    draft = bool(getattr(args, "draft", False))
    coverage = coverage_summary(run_directory, manifest)
    if not coverage["completionEligible"] and not draft:
        fail(
            "complete build requires substantive coverage for every packet and completed reconciliation; "
            f"blocking: {json.dumps(coverage['blockingCounts'], sort_keys=True)}, pending reviews: {coverage['pendingReviewPackets']}. "
            "Use build --draft for explicitly incomplete artifacts."
        )
    if not (run_directory / "review_manifest.json").is_file():
        with contextlib.redirect_stdout(io.StringIO()):
            command_reconcile(argparse.Namespace(run_directory=str(run_directory), draft=draft))
    review_manifest = load_review_manifest(run_directory)
    results = current_review_results(run_directory, review_manifest)
    pending = [row["reviewPacketId"] for row in review_manifest["packets"] if row["reviewPacketId"] not in results]
    if pending and not draft:
        fail(f"all reconciliation packets must be reviewed before build; pending: {', '.join(pending[:5])}")
    previous_controls = read_jsonl(run_directory / "controls.jsonl")
    current_evidence_ids = {row["evidence_id"] for row in read_csv(run_directory / "evidence_items.csv")}
    controls = merge_controls(run_directory, review_manifest, current_evidence_ids)
    validate_relationships(controls)
    with (run_directory / "controls.jsonl").open("w", encoding="utf-8") as handle:
        for control in controls:
            handle.write(json.dumps(control, ensure_ascii=False, sort_keys=True) + "\n")
    rows = control_csv_rows(controls)
    write_csv(run_directory / "deliverables.csv", CONTROL_COLUMNS, [row for row in rows if row["control_type"] in {"deliverable", "milestone", "acceptance_criterion"}])
    write_csv(run_directory / "requirements.csv", CONTROL_COLUMNS, [row for row in rows if row["control_type"] in {"requirement", "reporting_requirement", "proposal_requirement", "acceptance_criterion"}])
    write_csv(run_directory / "schedule.csv", CONTROL_COLUMNS, [row for row in rows if row["date_kind"] != "none" or row["control_type"] in {"deadline", "milestone"}])
    write_csv(run_directory / "actions.csv", CONTROL_COLUMNS, [row for row in rows if row["control_type"] in {"task", "action_item", "commitment"}])
    write_csv(run_directory / "decisions.csv", CONTROL_COLUMNS, [row for row in rows if row["control_type"] == "decision"])
    write_csv(run_directory / "raid.csv", CONTROL_COLUMNS, [row for row in rows if row["control_type"] in {"risk", "issue", "assumption", "dependency"}])
    write_csv(run_directory / "stakeholders.csv", CONTROL_COLUMNS, [row for row in rows if row["control_type"] == "stakeholder"])
    write_csv(run_directory / "conflicts_and_gaps.csv", ("control_id", "issue_type", "detail"), conflicts_and_gaps(controls))
    write_source_changes(run_directory)
    write_status_overlay(run_directory, controls, previous_controls)
    proposal_present = any(result.get("documentRole") in {"funding_notice", "proposal"} for result in current_results(run_directory, manifest).values())
    scaffold_markdown(run_directory, proposal_present)
    config["asOfDate"] = args.as_of or date.today().isoformat()
    config["updatedAt"] = utc_now()
    write_json(run_directory / "run_config.json", config)
    coverage = write_coverage(run_directory, manifest, draft)
    author_markdown_briefs(run_directory, config, controls, proposal_present, coverage)
    write_gantt_outputs(run_directory, controls, coverage)
    search_meta = build_search_index(run_directory, allow_incomplete=draft)
    write_run_metrics(run_directory)
    def built(state):
        state["status"] = "running"
        state["phase"] = "draft" if draft else "validating"
        state["nextAction"] = "process" if draft else "validate"
        return state
    run_state.update_run_state(run_directory, built, {"type": "build_completed", "controls": len(controls), "draft": draft})
    print(json.dumps({"controls": len(controls), "asOfDate": config["asOfDate"], "proposalChecklist": proposal_present, "draft": draft, "coverage": coverage, "nextAction": "process" if draft else "validate", "searchIndex": search_meta}, indent=2))


def command_refresh(args):
    run_directory = require_run_directory(args.run_directory)
    config, previous = load_run(run_directory)
    sources = discover_sources(config["inputs"])
    manifest = initialize_manifest(run_directory, sources, config["packetCharacters"], previous)
    append_source_changes(run_directory, previous, manifest)
    config["updatedAt"] = utc_now()
    write_json(run_directory / "run_config.json", config)
    write_json(run_directory / "source_manifest.json", manifest)
    previous_active = {row["sourceId"]: row for row in active_sources(previous)}
    current_active = {row["sourceId"]: row for row in active_sources(manifest)}
    counts = {"added": 0, "changed": 0, "removed": 0, "unchanged": 0}
    for source_id in set(previous_active) | set(current_active):
        old = previous_active.get(source_id)
        new = current_active.get(source_id)
        if old is None:
            counts["added"] += 1
        elif new is None:
            counts["removed"] += 1
        elif old["revisionId"] != new["revisionId"]:
            counts["changed"] += 1
        else:
            counts["unchanged"] += 1
    results = current_results(run_directory, manifest)
    def refreshed(state):
        state["items"] = project_items(manifest, results)
        state["status"] = "running"
        state["phase"] = "extracting"
        state["nextAction"] = "next"
        return state
    run_state.update_run_state(run_directory, refreshed, {"type": "input_refreshed", **counts})
    print(json.dumps({"runDirectory": str(run_directory), **counts, "pendingPackets": len(active_packets(manifest)) - len(current_results(run_directory, manifest))}, indent=2))


def command_status(args):
    run_directory = require_run_directory(args.run_directory)
    config, manifest = load_run(run_directory)
    try:
        state = run_state.load_run_state(run_directory, "project-extraction")
    except ValueError as error:
        fail(str(error))
    results = current_results(run_directory, manifest)
    drift = project_input_drift(config, manifest)
    worker = worker_control(run_directory)
    pid = worker.get("pid")
    worker["alive"] = run_state._pid_alive(pid) if pid else False
    coverage = coverage_summary(run_directory, manifest)
    if state.get("status") == "complete":
        last_successful_stage = "validate"
    elif (run_directory / "controls.jsonl").is_file():
        last_successful_stage = "build"
    elif (run_directory / "review_manifest.json").is_file():
        last_successful_stage = "reconcile"
    elif results:
        last_successful_stage = "extract"
    else:
        last_successful_stage = "init"
    if state.get("status") == "complete":
        exact_next_action = None
    elif coverage["blockingCounts"] or coverage["pendingReviewPackets"]:
        exact_next_action = f"project-extraction.py process {run_directory}"
    elif (run_directory / "controls.jsonl").is_file():
        exact_next_action = f"project-extraction.py validate {run_directory} --fix-hints --json"
    else:
        exact_next_action = f"project-extraction.py process {run_directory}"
    print(
        json.dumps(
            {
                "runDirectory": str(run_directory),
                "status": state["status"],
                "phase": state["phase"],
                "nextAction": state.get("nextAction"),
                "processedPackets": len(results),
                "totalPackets": len(active_packets(manifest)),
                "inputDrift": drift,
                "refreshRequired": any(drift.values()),
                "worker": worker,
                "scopeMode": config.get("scopeMode", "full"),
                "inbox": inbox_status_value(run_directory),
                "completionEligible": coverage["completionEligible"],
                "coveragePercent": coverage["coveragePercent"],
                "sourceCoveragePercent": coverage["sourceCoveragePercent"],
                "blockingCounts": coverage["blockingCounts"],
                "pendingReviewPackets": coverage["pendingReviewPackets"],
                "lastSuccessfulStage": last_successful_stage,
                "exactNextAction": exact_next_action,
            },
            indent=2,
        )
    )


def command_inbox_status(args):
    run_directory = require_run_directory(args.run_directory)
    print(json.dumps(inbox_status_value(run_directory), indent=2))


def configure_inbox_for_sync(run_directory, config, explicit_inbox):
    if explicit_inbox:
        inbox = Path(explicit_inbox).expanduser().resolve()
        config["liveRepository"] = {
            "projectRoot": str(inbox.parent),
            "inbox": str(inbox),
            "publishDirectory": str(inbox.parent / "Sources" / "Inbox"),
            "originalsDirectory": str(inbox.parent / "Originals" / "Inbox"),
        }
    live = initialize_live_repository(run_directory, config)
    if live is None:
        fail("multiple or non-directory inputs require inbox-sync --inbox <project-folder>/Inbox")
    return live


def parse_command_json(output, label):
    try:
        return json.loads(output)
    except json.JSONDecodeError as error:
        fail(f"{label} returned invalid JSON: {error}")


def finalize_inbox_batch(run_directory, live, manifest, batch, intake_result):
    artifact_rows = read_csv(Path(intake_result["finalized"]["artifactManifest"]))
    originals = {str(Path(row["source_path"]).resolve()): row for row in artifact_rows if row.get("role") == "original"}
    published_by_document = {
        row.get("document_id"): str(live["projectRoot"] / row["destination_path"])
        for row in artifact_rows
        if row.get("role") == "final_markdown"
    }
    batch_ids = set(batch["itemIds"])
    for item in manifest["items"]:
        if item.get("itemId") not in batch_ids:
            continue
        original = originals.get(str(Path(item["path"]).resolve()))
        if original:
            item.update(
                {
                    "status": "ingested",
                    "completedAt": utc_now(),
                    "archivedPath": str(live["projectRoot"] / original["destination_path"]),
                    "publishedPath": published_by_document.get(original.get("document_id")),
                }
            )
            append_jsonl(run_directory / "inbox_events.jsonl", {"at": utc_now(), "type": "inbox_item_ingested", "itemId": item["itemId"], "batchId": batch["batchId"], "sha256": item["sha256"]})
            continue
        staged = Path(item["path"])
        restored = live["inbox"] / item["relativePath"]
        if staged.is_file() and not restored.exists():
            restored.parent.mkdir(parents=True, exist_ok=True)
            staged.rename(restored)
            item["path"] = str(restored)
        item.update({"status": "failed", "completedAt": utc_now(), "error": "document ingest did not publish this item"})
        append_jsonl(run_directory / "inbox_events.jsonl", {"at": utc_now(), "type": "inbox_item_failed", "itemId": item["itemId"], "batchId": batch["batchId"], "error": item["error"]})
    batch["status"] = "complete"
    batch["completedAt"] = utc_now()
    manifest["activeBatch"] = None
    write_inbox_manifest(run_directory, manifest)
    remove_empty_directories(live["inbox"] / ".processing")


def command_inbox_sync(args):
    run_directory = require_run_directory(args.run_directory)
    config, _ = load_run(run_directory)
    live = configure_inbox_for_sync(run_directory, config, args.inbox)
    manifest = inbox_manifest(run_directory)
    batch = manifest.get("activeBatch")
    archived_duplicates = []
    if batch is None:
        status = inbox_status_value(run_directory)
        pending = []
        for row in status["pending"]:
            if row["duplicate"]:
                archived_path = archive_duplicate_inbox_item(live, row)
                archived_duplicates.append({"itemId": row["itemId"], "archivedPath": archived_path})
                manifest["items"].append({**row, "status": "ingested", "completedAt": utc_now(), "archivedPath": archived_path, "duplicate": True})
                append_jsonl(run_directory / "inbox_events.jsonl", {"at": utc_now(), "type": "inbox_duplicate_archived", "itemId": row["itemId"], "sha256": row["sha256"]})
            else:
                pending.append(row)
        if not pending:
            write_inbox_manifest(run_directory, manifest)
            print(json.dumps({"synced": False, "pending": 0, "archivedDuplicates": archived_duplicates, "nextAction": "search_or_status"}, indent=2))
            return
        batch = stage_inbox_batch(run_directory, live, manifest, pending)
    document_ingest = Path(__file__).resolve().parents[2] / "document-ingest" / "scripts" / "document-ingest.mjs"
    result = subprocess.run(
        [
            "node",
            str(document_ingest),
            "intake",
            batch["stagingDirectory"],
            "--destination",
            str(live["projectRoot"]),
            "--output",
            batch["ingestRun"],
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        batch["status"] = "needs_review"
        batch["error"] = result.stderr.strip() or result.stdout.strip()
        write_inbox_manifest(run_directory, manifest)
        fail(f"document Inbox intake failed: {batch['error']}")
    intake = parse_command_json(result.stdout, "document-ingest intake")
    if not intake.get("finalized"):
        batch["status"] = "needs_review" if intake.get("nextAction") == "review" else "processing"
        batch["nextAction"] = intake.get("nextAction")
        write_inbox_manifest(run_directory, manifest)
        ingest_run = Path(batch["ingestRun"])
        review_result = subprocess.run(
            ["node", str(document_ingest), "next-review", str(ingest_run)],
            capture_output=True,
            text=True,
        )
        review_packet = parse_command_json(review_result.stdout, "document-ingest next-review") if review_result.returncode == 0 else None
        commands = {
            "nextReview": f"node {document_ingest} next-review {ingest_run}",
            "recordReview": review_packet.get("commands", {}).get("recordReview") if review_packet else None,
            "validate": f"node {document_ingest} validate {ingest_run} --fix-hints --json",
            "finalize": f"node {document_ingest} finalize {ingest_run} --destination {live['projectRoot']}",
            "resumeProjectInbox": f"project-extraction.py inbox-sync {run_directory}",
        }
        print(
            json.dumps(
                {
                    "synced": False,
                    "batch": batch,
                    "documentIngest": intake,
                    "reviewPacket": review_packet,
                    "archivedDuplicates": archived_duplicates,
                    "nextAction": "complete_document_ingest_review",
                    "commands": commands,
                },
                indent=2,
            )
        )
        return
    finalize_inbox_batch(run_directory, live, manifest, batch, intake)
    refresh = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "refresh", str(run_directory)],
        capture_output=True,
        text=True,
    )
    if refresh.returncode != 0:
        fail(f"Inbox intake completed but project refresh failed: {refresh.stderr.strip() or refresh.stdout.strip()}")
    refresh_value = parse_command_json(refresh.stdout, "project-extraction refresh")
    print(
        json.dumps(
            {
                "synced": True,
                "batchId": batch["batchId"],
                "documentIngest": intake["finalized"],
                "projectRefresh": refresh_value,
                "archivedDuplicates": archived_duplicates,
                "nextAction": "process" if refresh_value.get("pendingPackets", 0) else "index",
            },
            indent=2,
        )
    )


def command_retry(args):
    run_directory = require_run_directory(args.run_directory)
    _, manifest = load_run(run_directory)
    results = current_results(run_directory, manifest)
    invalid_screenings = set(coverage_summary(run_directory, manifest)["invalidScreenings"])
    targets = {
        packet_id
        for packet_id, result in results.items()
        if (
            (result.get("status") == "failed" and args.all_failed)
            or (packet_id in invalid_screenings and args.invalid_screenings)
            or (packet_id == args.item and (result.get("status") == "failed" or packet_id in invalid_screenings))
        )
    }
    if not targets:
        fail(f"retryable item not found: {args.item}" if args.item else "run has no matching retryable items")
    retained = [result for result in read_jsonl(run_directory / "extraction_results.jsonl") if result.get("packetId") not in targets]
    run_state.atomic_write_text(
        run_directory / "extraction_results.jsonl",
        "" if not retained else "\n".join(json.dumps(result, ensure_ascii=False, sort_keys=True) for result in retained) + "\n",
    )
    def retried(state):
        for item in state["items"]:
            if item["id"] in targets:
                item.update({"status": "pending", "attempts": 0, "error": None, "transient": False})
        state["status"] = "running"
        state["phase"] = "extracting"
        state["nextAction"] = "next"
        return state
    run_state.update_run_state(run_directory, retried, {"type": "items_retried", "itemIds": sorted(targets)})
    print(json.dumps({"runDirectory": str(run_directory), "retried": len(targets), "nextAction": "next"}, indent=2))


def validation_issue(code, message, fix=None):
    return {"code": code, "message": message, "fix": fix}


def validate_run(run_directory):
    config, manifest = load_run(run_directory)
    issues = []
    warnings = []
    if config.get("schemaVersion") != RUN_SCHEMA_VERSION:
        issues.append(validation_issue("schema_version", "run_config.json has an unsupported schemaVersion"))
    for source in active_sources(manifest):
        path = Path(source["path"])
        if not path.is_file():
            issues.append(validation_issue("source_missing", f"source file is missing: {path}"))
        elif sha256_file(path) != source["sha256"]:
            issues.append(validation_issue("source_changed", f"source file hash differs; run refresh: {path}", "Run refresh, then process changed packets."))
    results = current_results(run_directory, manifest)
    coverage = coverage_summary(run_directory, manifest)
    if not coverage["completionEligible"]:
        issues.append(
            validation_issue(
                "incomplete_coverage",
                f"run is not completion-eligible; blocking packet states: {json.dumps(coverage['blockingCounts'], sort_keys=True)}, pending review packets: {coverage['pendingReviewPackets']}",
                "Resume process or repair the listed packet and review states. Use build --draft only for labeled partial artifacts.",
            )
        )
    for packet_id in coverage["invalidScreenings"]:
        issues.append(
            validation_issue(
                "invalid_screening",
                f"{packet_id} uses screened_no_controls without substantive screening provenance",
                f"Requeue {packet_id} and process it, or record a human/model screening with packet-bound evidence.",
            )
        )
    for packet in active_packets(manifest):
        result = results.get(packet["packetId"])
        if result is None:
            issues.append(validation_issue("packet_pending", f"packet has no disposition: {packet['packetId']}", "Run next and record."))
        elif result["status"] not in COMPLETION_PACKET_STATUSES:
            warnings.append(f"{packet['packetId']} is {result['status']}: {result.get('note') or 'no note'}")
    required_after_reconcile = ("evidence_items.csv", "evidence_items.jsonl", "review_manifest.json")
    for filename in required_after_reconcile:
        if not (run_directory / filename).is_file():
            issues.append(validation_issue("artifact_missing", f"missing required artifact: {filename}", "Run reconcile."))
    review_manifest_path = run_directory / "review_manifest.json"
    if review_manifest_path.is_file():
        review_manifest = json.loads(review_manifest_path.read_text(encoding="utf-8"))
        review_results = current_review_results(run_directory, review_manifest)
        for packet in review_manifest.get("packets", []):
            if packet["reviewPacketId"] not in review_results:
                issues.append(validation_issue("review_pending", f"reconciliation packet is pending: {packet['reviewPacketId']}", "Run next-review and record-review."))
    required_built = (
        "controls.jsonl",
        "deliverables.csv",
        "requirements.csv",
        "schedule.csv",
        "actions.csv",
        "decisions.csv",
        "raid.csv",
        "stakeholders.csv",
        "conflicts_and_gaps.csv",
        "source_changes.csv",
        "project_status.csv",
        "gantt.csv",
        "gantt.md",
        "gantt.html",
        "run_metrics.json",
        "search_index.jsonl",
        "search_index_meta.json",
        "coverage.json",
    )
    for filename in required_built:
        if not (run_directory / filename).is_file():
            issues.append(validation_issue("artifact_missing", f"missing required artifact: {filename}", "Run build."))
    search_meta_path = run_directory / "search_index_meta.json"
    if search_meta_path.is_file() and search_index_ready(run_directory, manifest):
        search_meta = json.loads(search_meta_path.read_text(encoding="utf-8"))
        if search_meta.get("fingerprint") != search_index_fingerprint(run_directory, manifest):
            issues.append(validation_issue("search_index_stale", "search index does not match current controls, status, and source revisions", "Run index or build."))
    evidence_ids = {row["evidence_id"] for row in read_csv(run_directory / "evidence_items.csv")}
    controls = read_jsonl(run_directory / "controls.jsonl")
    evidence_by_id = {row.get("evidence_id"): row for row in read_csv(run_directory / "evidence_items.csv")}
    control_ids = {row.get("control_id") for row in controls}
    for control in controls:
        unknown_evidence = set(control.get("source_evidence_ids", [])) - evidence_ids
        if unknown_evidence:
            issues.append(validation_issue("unknown_evidence", f"{control['control_id']} cites unknown evidence: {', '.join(sorted(unknown_evidence))}"))
        cited_titles = {
            re.sub(r"\W+", " ", evidence_by_id[evidence_id].get("title", "").lower()).strip()
            for evidence_id in control.get("source_evidence_ids", [])
            if evidence_id in evidence_by_id
        }
        if len(cited_titles) >= 3 and "merge justification" not in (control.get("notes") or "").lower():
            issues.append(validation_issue("heterogeneous_merge", f"{control['control_id']} collapses heterogeneous evidence without a merge justification"))
        for field in ("parent_control_ids", "depends_on_control_ids", "satisfies_control_ids", "supersedes_control_ids", "conflicts_with_control_ids"):
            unknown_controls = set(control.get(field, [])) - control_ids
            if unknown_controls:
                issues.append(validation_issue("unknown_control", f"{control['control_id']} {field} cites unknown controls: {', '.join(sorted(unknown_controls))}"))
    status_path = run_directory / "project_status.csv"
    if status_path.is_file():
        for row_number, row in enumerate(read_csv(status_path), 2):
            if row.get("working_status", "") not in WORKING_STATUSES:
                issues.append(validation_issue("status_value", f"project_status.csv:{row_number} has invalid working_status"))
            if not all(is_iso_date(row.get(field)) for field in ("forecast_date", "forecast_start_date", "forecast_end_date", "last_updated")):
                issues.append(validation_issue("status_date", f"project_status.csv:{row_number} has an invalid date"))
            if row.get("control_id") not in control_ids:
                warnings.append(f"project_status.csv:{row_number} references a non-current control: {row.get('control_id')}")
    markdown_files = list(MARKDOWN_TEMPLATES)
    if (run_directory / "proposal_checklist.md").exists():
        markdown_files.append("proposal_checklist.md")
    for filename in markdown_files:
        path = run_directory / filename
        if not path.is_file():
            issues.append(validation_issue("deliverable_missing", f"missing authored deliverable: {filename}", "Run build and author the scaffold."))
        elif PLACEHOLDER in path.read_text(encoding="utf-8"):
            issues.append(validation_issue("placeholder", f"{filename} contains an unresolved placeholder", "Author every scaffolded section."))
        elif "Incomplete draft" in path.read_text(encoding="utf-8"):
            issues.append(validation_issue("draft_artifact", f"{filename} is explicitly marked as an incomplete draft", "Complete processing and run a non-draft build."))
    return {"valid": not issues, "issues": issues, "errors": [row["message"] for row in issues], "warnings": warnings}


def command_validate(args):
    run_directory = require_run_directory(args.run_directory)
    result = validate_run(run_directory)
    if args.fix_hints:
        result["fixHints"] = [row["fix"] for row in result["issues"] if row.get("fix")]
    if args.json:
        print(json.dumps(result, indent=2))
    elif result["valid"]:
        print(f"Valid project-extraction run: {run_directory}")
        for warning in result["warnings"]:
            print(f"Warning: {warning}")
    else:
        for error in result["errors"]:
            print(f"Error: {error}", file=sys.stderr)
    if not result["valid"]:
        raise SystemExit(1)
    def completed(state):
        state["status"] = "complete"
        state["phase"] = "complete"
        state["nextAction"] = None
        state["completion"] = {"validatedAt": utc_now()}
        return state
    run_state.update_run_state(run_directory, completed, {"type": "run_completed"})


WORKER_SYSTEM_PROMPT = """You extract source-backed project controls. Return only valid JSON. Keep each packet separate. Never invent dates, owners, obligations, quotes, teams, workstreams, or precedence. A direct quote must occur exactly in that packet. Use screened_no_controls only after reading the entire packet and include a screening object with a substantive finding. Never use screened_no_controls for deferred, unavailable, truncated, or unprocessed work. Preserve distinct deliverables, milestones, tasks, requirements, decisions, risks, dependencies, and stakeholders as distinct items."""


def focused_terms(config):
    scope = config.get("scope") or {}
    values = [scope.get("focus"), *scope.get("teams", []), *scope.get("people", []), *scope.get("workstreams", [])]
    return [value.strip().lower() for value in values if isinstance(value, str) and value.strip()]


def source_scope_decision(config, source, packet_text):
    scope = config.get("scope") or {}
    relative = source["relativePath"].lower()
    if any(value.lower() in relative for value in scope.get("excludeSources", [])):
        return "excluded_by_scope", "excluded source filter", "direct"
    include = scope.get("includeSources", [])
    if include and not any(value.lower() in relative for value in include):
        return "excluded_by_scope", "not selected by include-source filter", "direct"
    if config.get("scopeMode") != "focused":
        return "extract", "full-project run", "full"
    terms = focused_terms(config)
    haystack = f"{relative}\n{packet_text}".lower()
    matched = [term for term in terms if term in haystack]
    if matched:
        return "extract", f"matched focus terms: {', '.join(matched[:8])}", "direct"
    if re.search(r"\b(dependenc|shared milestone|reporting requirement|decision|risk|issue|blocking|approval)\w*\b", haystack):
        return "extract", "included for dependency closure", "dependency"
    return "excluded_by_scope", "no direct or dependency-closure match", "direct"


def extraction_prompt(batch, config):
    scope = config.get("scope") or {}
    packet_blocks = []
    for source, packet, text, relation in batch:
        packet_blocks.append(
            f"\n--- PACKET {packet['packetId']} ---\nsource_path: {source['relativePath']}\nsource_locator: {json.dumps(packet['locator'])}\nscope_relation: {relation}\n{text}"
        )
    return f"""Extract project controls from every supplied packet.

Focus configuration: {json.dumps(scope, ensure_ascii=False, sort_keys=True)}

Return exactly:
{{"schemaVersion":{EXTRACTION_SCHEMA_VERSION},"packets":[{{"packetId":"pkt-...","documentRole":"award|funding_notice|proposal|scope_of_work|contract|amendment|work_plan|report|presentation|meeting|interview|correspondence|budget|other","disposition":"extracted|screened_no_controls|needs_review","reason":"specific reason","screening":null,"items":[{{"item_type":"one allowed project-control type","title":"concise distinct control","description":"source-backed description or null","party":"responsible party or null","counterparty":"recipient or null","date_text":"source wording or null","date_kind":"exact|relative|recurring|conditional|none","date":"YYYY-MM-DD only for exact dates or null","trigger":"source trigger or null","offset_days":null,"recurrence":null,"acceptance_criteria":null,"evidence_required":null,"commitment_level":"required|committed|proposed|discussed|informational|unclear","direct_quotes":["short exact quote"],"interpretation":"explicit|inferred|unclear","confidence":"high|medium|low","teams":[],"workstreams":[],"scope_relation":"direct|dependency|shared|full","start_date":null,"end_date":null,"duration_days":null,"schedule_basis":null}}]}}]}}

For screened_no_controls, items must be empty and screening must be {{"method":"model","finding":"specific description of what was reviewed and why it contains no project controls"}}. For extracted packets screening must be null.

Allowed item types: {', '.join(ITEM_TYPES)}.
Packets:{''.join(packet_blocks)}
"""


def packet_embedding_scores(run_directory, candidates, config):
    cache_path = run_directory / "working" / "embedding_scores.json"
    query = (config.get("scope") or {}).get("focus") or "project deliverables requirements milestones decisions risks dependencies reporting obligations"
    if cache_path.is_file():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        entries = cache.get("packets") or {}
        if cache.get("query") == query and all(entries.get(entry[1]["packetId"], {}).get("sha256") == entry[1]["sha256"] for entry in candidates):
            return {entry[1]["packetId"]: entries[entry[1]["packetId"]]["score"] for entry in candidates}
    texts = [query, *[f"{entry[0]['relativePath']}\n{entry[2][:8000]}" for entry in candidates]]
    embedded = forge_embeddings.embed_texts(texts)
    if not embedded.get("ok"):
        write_json(cache_path, {"query": query, "packets": {}, "reason": embedded.get("reason")})
        return {}
    vectors = [forge_embeddings.normalize(vector) for vector in embedded["vectors"]]
    scores = {entry[1]["packetId"]: round(forge_embeddings.cosine(vectors[0], vectors[index + 1]), 6) for index, entry in enumerate(candidates)}
    candidates_rows = []
    for left in range(len(candidates)):
        for right in range(left + 1, len(candidates)):
            similarity = forge_embeddings.cosine(vectors[left + 1], vectors[right + 1])
            if similarity >= 0.985:
                candidates_rows.append({"left_packet_id": candidates[left][1]["packetId"], "right_packet_id": candidates[right][1]["packetId"], "similarity": round(similarity, 6), "disposition": "review_candidate"})
    write_csv(run_directory / "duplicate_candidates.csv", ("left_packet_id", "right_packet_id", "similarity", "disposition"), candidates_rows)
    write_json(
        cache_path,
        {
            "query": query,
            "packets": {entry[1]["packetId"]: {"sha256": entry[1]["sha256"], "score": scores[entry[1]["packetId"]]} for entry in candidates},
            "model": embedded.get("model"),
            "dimensions": embedded.get("dimensions"),
        },
    )
    return scores


def pending_extraction_batches(run_directory, config, manifest):
    results = current_results(run_directory, manifest)
    worker = config.get("worker", {})
    characters_per_token = float(worker.get("charactersPerToken") or DEFAULT_CHARS_PER_TOKEN)
    ceiling = int(worker.get("promptTokenCeiling") or DEFAULT_PROMPT_TOKEN_CEILING)
    ceiling_characters = max(1_000, int(max(1, ceiling - PROMPT_TOKEN_RESERVE) * characters_per_token))
    target = min(int(worker.get("packetCharacters") or DEFAULT_PACKET_CHARACTERS), ceiling_characters)
    candidates = []
    scope_rows = []
    canonical_by_hash = {}
    for source in active_sources(manifest):
        for packet in source.get("packets", []):
            canonical_packet = canonical_by_hash.setdefault(packet["sha256"], packet["packetId"])
            if packet["packetId"] in results:
                continue
            text = Path(packet["path"]).read_text(encoding="utf-8")
            decision, reason, relation = source_scope_decision(config, source, text)
            scope_rows.append({"source_id": source["sourceId"], "packet_id": packet["packetId"], "source_path": source["relativePath"], "decision": decision, "scope_relation": relation, "embedding_score": "", "matched_filters": "", "reason": reason})
            if canonical_packet != packet["packetId"]:
                reason = f"exact duplicate of frozen packet {canonical_packet}"
                command_record(argparse.Namespace(run_directory=str(run_directory), packet_id=packet["packetId"], items_file=None, status="duplicate_source", note=reason))
                scope_rows[-1].update({"decision": "duplicate_source", "reason": reason})
            elif decision == "excluded_by_scope":
                command_record(argparse.Namespace(run_directory=str(run_directory), packet_id=packet["packetId"], items_file=None, status="excluded_by_scope", note=reason))
            else:
                candidates.append((source, packet, text, relation))
    scores = packet_embedding_scores(run_directory, candidates, config) if candidates else {}
    for row in scope_rows:
        if row["packet_id"] in scores:
            row["embedding_score"] = scores[row["packet_id"]]
    if scope_rows:
        existing = read_csv(run_directory / "scope_manifest.csv")
        write_csv(run_directory / "scope_manifest.csv", ("source_id", "packet_id", "source_path", "decision", "scope_relation", "embedding_score", "matched_filters", "reason"), [*existing, *scope_rows])
    candidates.sort(key=lambda entry: (-scores.get(entry[1]["packetId"], 0), entry[0]["sourceKey"], entry[1]["sequence"]))
    batches = []
    current = []
    current_chars = 0
    current_source = None
    for entry in candidates:
        source_id = entry[0]["sourceId"]
        if current and (current_chars + len(entry[2]) > target or source_id != current_source):
            batches.append(current)
            current = []
            current_chars = 0
        current_source = source_id
        current.append(entry)
        current_chars += len(entry[2])
    if current:
        batches.append(current)
    return batches


def record_worker_packet(run_directory, packet_id, payload):
    disposition = payload.get("disposition")
    if disposition not in {"extracted", "screened_no_controls", "needs_review"}:
        disposition = "needs_review"
    if disposition != "extracted":
        screening = payload.get("screening") if isinstance(payload.get("screening"), dict) else {}
        command_record(
            argparse.Namespace(
                run_directory=str(run_directory),
                packet_id=packet_id,
                items_file=None,
                status=disposition,
                note=payload.get("reason") or "model could not extract this packet",
                screening_method=screening.get("method"),
                screening_finding=screening.get("finding"),
                disposition_source="worker",
                document_role=payload.get("documentRole") or "other",
            )
        )
        return
    path = run_directory / "working" / f"{packet_id}-worker.json"
    write_json(path, {"documentRole": payload.get("documentRole") or "other", "items": payload.get("items") or []})
    command_record(argparse.Namespace(run_directory=str(run_directory), packet_id=packet_id, items_file=str(path), status="extracted", note=payload.get("reason"), disposition_source="worker"))


def validate_worker_packet(packet_text, payload):
    disposition = payload.get("disposition")
    if disposition == "extracted":
        return extraction_validation_errors({"documentRole": payload.get("documentRole") or "other", "items": payload.get("items")}, packet_text)
    if disposition == "screened_no_controls":
        screening = payload.get("screening")
        finding = screening.get("finding") if isinstance(screening, dict) else None
        if not isinstance(screening, dict) or screening.get("method") != "model":
            return ["screened_no_controls requires model screening provenance"]
        if not isinstance(finding, str) or not finding.strip() or DEFERRED_SCREENING_PATTERN.search(finding):
            return ["screened_no_controls requires a substantive non-deferral finding"]
        if payload.get("items"):
            return ["screened_no_controls must not contain extracted items"]
        return []
    if disposition == "needs_review":
        return []
    return ["disposition must be extracted, screened_no_controls, or needs_review"]


def update_worker_calibration(run_directory, config, source_characters, generated_items, record):
    prompt_tokens = record.get("promptTokens")
    if not isinstance(prompt_tokens, int) or prompt_tokens <= 1500:
        return
    observed = max(2.5, min(5.0, source_characters / max(1, prompt_tokens - 1500)))
    prior = float(config.get("worker", {}).get("charactersPerToken") or DEFAULT_CHARS_PER_TOKEN)
    calibrated = prior * 0.7 + observed * 0.3
    density_limit = MAX_ADAPTIVE_PACKET_CHARACTERS
    if generated_items:
        characters_per_item = source_characters / generated_items
        density_limit = int(max(MIN_ADAPTIVE_PACKET_CHARACTERS, min(MAX_ADAPTIVE_PACKET_CHARACTERS, characters_per_item * 20)))
    target = int(max(MIN_ADAPTIVE_PACKET_CHARACTERS, min(density_limit, TARGET_SOURCE_TOKENS * calibrated)))
    config.setdefault("worker", {})["charactersPerToken"] = round(calibrated, 4)
    config["worker"]["packetCharacters"] = target
    config["worker"]["lastGeneratedItems"] = generated_items
    config["worker"]["lastFinishReason"] = record.get("finishReason")
    config["updatedAt"] = utc_now()
    write_json(run_directory / "run_config.json", config)


def update_cache_health(run_directory, config, record):
    cached_tokens = record.get("cachedTokens")
    prompt_tokens = record.get("promptTokens")
    if not isinstance(cached_tokens, int) or not isinstance(prompt_tokens, int):
        return False
    worker = config.setdefault("worker", {})
    consecutive = int(worker.get("consecutiveNoCacheReuse") or 0)
    consecutive = consecutive + 1 if cached_tokens == 0 else 0
    worker["consecutiveNoCacheReuse"] = consecutive
    write_json(run_directory / "run_config.json", config)
    if consecutive < 3:
        return False
    warning = {
        "at": utc_now(),
        "event": "cache_warning",
        "task": record.get("task"),
        "slot": record.get("slot"),
        "detail": "three consecutive worker calls reported zero cached prompt tokens; worker paused",
    }
    append_jsonl(run_directory / "inference_schedule.jsonl", warning)
    set_worker_control(run_directory, "paused")
    return True


def semantic_halves(text):
    if len(text) < 4_000:
        return [text]
    midpoint = len(text) // 2
    candidates = [
        text.rfind("\n#", 0, midpoint),
        text.find("\n#", midpoint),
        text.rfind("\n\n", 0, midpoint),
        text.find("\n\n", midpoint),
    ]
    candidates = [value for value in candidates if len(text) // 4 <= value <= 3 * len(text) // 4]
    split_at = min(candidates, key=lambda value: abs(value - midpoint)) if candidates else midpoint
    return [text[:split_at], text[split_at:]]


def merge_segment_payloads(packet_id, payloads):
    items = [item for payload in payloads for item in (payload.get("items") or [])]
    document_role = next((payload.get("documentRole") for payload in payloads if payload.get("documentRole")), "other")
    if items:
        return {"packetId": packet_id, "documentRole": document_role, "disposition": "extracted", "reason": "combined from semantic sub-packets after output truncation", "screening": None, "items": items}
    if all(payload.get("disposition") == "screened_no_controls" for payload in payloads):
        return {
            "packetId": packet_id,
            "documentRole": document_role,
            "disposition": "screened_no_controls",
            "reason": "all semantic sub-packets were substantively screened",
            "screening": {"method": "model", "finding": "; ".join((payload.get("screening") or {}).get("finding", "") for payload in payloads)},
            "items": [],
        }
    return {"packetId": packet_id, "documentRole": document_role, "disposition": "needs_review", "reason": "semantic sub-packets produced incompatible dispositions", "screening": None, "items": []}


def extract_segment(run_directory, entry, config, chat, background, depth=0):
    source, packet, text, relation = entry
    prompt = extraction_prompt([(source, packet, text, relation)], config)
    value, record = post_chat_json(
        run_directory,
        "extract",
        WORKER_SYSTEM_PROMPT,
        prompt,
        chat,
        allow_preemption=background,
        background=background,
    )
    if record.get("finishReason") == "length":
        halves = semantic_halves(text)
        if len(halves) == 1 or depth >= 3:
            raise ValueError(f"model output remained truncated after {depth + 1} semantic splits")
        append_jsonl(
            run_directory / "inference_schedule.jsonl",
            {"at": utc_now(), "event": "truncation_split", "packetId": packet["packetId"], "depth": depth + 1, "characters": len(text)},
        )
        payloads = [extract_segment(run_directory, (source, packet, half, relation), config, chat, background, depth + 1)[0] for half in halves]
        return merge_segment_payloads(packet["packetId"], payloads), record
    packets = value.get("packets") if isinstance(value, dict) else None
    if not isinstance(packets, list) or len(packets) != 1 or packets[0].get("packetId") != packet["packetId"]:
        raise ValueError("worker segment response must contain exactly the requested packet id")
    return packets[0], record


def process_extraction(run_directory, chat, background=False):
    config, manifest = load_run(run_directory)
    while True:
        if worker_control(run_directory).get("desiredState") != "running":
            return
        batches = pending_extraction_batches(run_directory, config, manifest)
        if not batches:
            return
        batch = batches[0]
        prompt = extraction_prompt(batch, config)
        source_characters = sum(len(entry[2]) for entry in batch)
        last_error = None
        for attempt in range(2):
            try:
                value, record = post_chat_json(
                    run_directory,
                    "extract",
                    WORKER_SYSTEM_PROMPT,
                    prompt if attempt == 0 else f"{prompt}\n\nPrevious output failed validation: {last_error}. Return corrected JSON only.",
                    chat,
                    allow_preemption=background,
                    background=background,
                )
                if record.get("finishReason") == "length":
                    split_payloads = []
                    for entry in batch:
                        source, packet, text, relation = entry
                        halves = semantic_halves(text)
                        append_jsonl(run_directory / "inference_schedule.jsonl", {"at": utc_now(), "event": "truncation_split", "packetId": packet["packetId"], "depth": 1, "characters": len(text)})
                        payloads = [extract_segment(run_directory, (source, packet, half, relation), config, chat, background, 1)[0] for half in halves]
                        split_payloads.append(merge_segment_payloads(packet["packetId"], payloads))
                    value = {"schemaVersion": EXTRACTION_SCHEMA_VERSION, "packets": split_payloads}
                packets = value.get("packets") if isinstance(value, dict) else None
                if not isinstance(packets, list):
                    raise ValueError("worker response requires a packets array")
                by_id = {row.get("packetId"): row for row in packets if isinstance(row, dict)}
                expected = {entry[1]["packetId"] for entry in batch}
                if set(by_id) != expected:
                    raise ValueError("worker response packet ids do not exactly match the requested batch")
                packet_text_by_id = {entry[1]["packetId"]: entry[2] for entry in batch}
                validation_errors = [
                    f"{packet_id}: {error}"
                    for packet_id in [entry[1]["packetId"] for entry in batch]
                    for error in validate_worker_packet(packet_text_by_id[packet_id], by_id[packet_id])
                ]
                if validation_errors:
                    raise ValueError("; ".join(validation_errors))
                for packet_id in [entry[1]["packetId"] for entry in batch]:
                    record_worker_packet(run_directory, packet_id, by_id[packet_id])
                update_worker_calibration(run_directory, config, source_characters, sum(len(row.get("items") or []) for row in packets), record)
                update_cache_health(run_directory, config, record)
                break
            except InterruptedError:
                time.sleep(max(0, chat["scheduling"]["yieldMs"]) / 1000)
                break
            except (ValueError, KeyError, json.JSONDecodeError, SystemExit) as error:
                last_error = str(error)
                append_jsonl(run_directory / "inference_schedule.jsonl", {"at": utc_now(), "event": "validation_retry", "task": "extract", "attempt": attempt + 1, "detail": last_error})
                if attempt == 1:
                    for _, packet, _, _ in batch:
                        if packet["packetId"] not in current_results(run_directory, manifest):
                            command_record(argparse.Namespace(run_directory=str(run_directory), packet_id=packet["packetId"], items_file=None, status="failed", note=f"worker validation failed twice; retry after correcting the response contract: {last_error}", disposition_source="worker"))
        time.sleep(max(0, chat["scheduling"]["yieldMs"]) / 1000)
        if worker_control(run_directory).get("desiredState") != "running":
            return
        config, manifest = load_run(run_directory)


RECONCILIATION_SYSTEM_PROMPT = """Reconcile source-backed evidence into canonical project controls. Return JSON only. Group evidence only when it represents the same real control. Preserve different owners, dates, acceptance criteria, commitment levels, and source authority as separate or conflicting records. Do not invent facts. Use existingControlId only when the existing control still has the same semantic identity. Mark unresolved authority or merge ambiguity with needsReview=true."""


def control_counters(run_directory):
    counters = {item_type: 0 for item_type in ITEM_TYPES}
    for control in read_jsonl(run_directory / "controls.jsonl"):
        match = re.fullmatch(r"[A-Z]+-(\d+)", control.get("control_id", ""))
        if match and control.get("control_type") in counters:
            counters[control["control_type"]] = max(counters[control["control_type"]], int(match.group(1)))
    return counters


def reconciliation_prompt(raw):
    return f"""Reconcile this one control-type review packet.

Return exactly:
{{"reviewPacketId":"{raw['reviewPacketId']}","needsReview":false,"groups":[{{"existingControlId":null,"title":"canonical title","description":null,"evidenceIds":["ev-..."],"owner":null,"recipient":null,"date_text":null,"date_kind":"none","date":null,"trigger":null,"offset_days":null,"recurrence":null,"acceptance_criteria":null,"evidence_required":null,"source_status":null,"commitment_level":"unclear","teams":[],"workstreams":[],"scope_relation":"full","start_date":null,"end_date":null,"duration_days":null,"schedule_basis":null,"mergeJustification":null}}],"dispositions":[{{"evidence_id":"ev-...","disposition":"contextual|duplicate|superseded|conflicting","controlGroup":0,"note":"reason"}}],"reviewReason":null}}

Every evidence id must appear exactly once in one group or disposition. A duplicate, superseded, or conflicting disposition must identify the related group. Evidence:
{json.dumps(raw, ensure_ascii=False, sort_keys=True)}
"""


def automatic_review(run_directory, chat, background=False):
    manifest = load_review_manifest(run_directory)
    counters = control_counters(run_directory)
    for packet in manifest["packets"]:
        if packet["status"] == "complete" and review_result(run_directory, packet["reviewPacketId"]):
            continue
        raw = json.loads(Path(packet["path"]).read_text(encoding="utf-8"))
        value, _ = post_chat_json(
            run_directory,
            "reconcile",
            RECONCILIATION_SYSTEM_PROMPT,
            reconciliation_prompt(raw),
            chat,
            allow_preemption=background,
            background=background,
        )
        if not isinstance(value, dict) or value.get("reviewPacketId") != packet["reviewPacketId"]:
            fail(f"reconciliation response did not match {packet['reviewPacketId']}")
        if value.get("needsReview"):
            set_worker_control(run_directory, "paused")
            append_jsonl(
                run_directory / "inference_schedule.jsonl",
                {"at": utc_now(), "event": "reconciliation_needs_review", "reviewPacketId": packet["reviewPacketId"], "detail": value.get("reviewReason")},
            )
            return False
        groups = value.get("groups")
        dispositions = value.get("dispositions") or []
        if not isinstance(groups, list) or not isinstance(dispositions, list):
            fail(f"reconciliation response for {packet['reviewPacketId']} requires groups and dispositions arrays")
        evidence_by_id = {row["evidence_id"]: row for row in raw["evidenceItems"]}
        controls = []
        group_control_ids = []
        for group_index, group in enumerate(groups):
            if not isinstance(group, dict):
                fail(f"reconciliation group {group_index + 1} must be an object")
            evidence_ids = group.get("evidenceIds") or []
            if not evidence_ids or any(evidence_id not in evidence_by_id for evidence_id in evidence_ids):
                fail(f"reconciliation group {group_index + 1} has invalid evidenceIds")
            representative = evidence_by_id[evidence_ids[0]]
            existing_id = group.get("existingControlId")
            existing_ids = {row.get("control_id") for row in raw.get("existingControls", [])}
            if existing_id and existing_id not in existing_ids:
                fail(f"reconciliation group {group_index + 1} selected an unknown existingControlId")
            if existing_id:
                control_id = existing_id
            else:
                counters[packet["controlType"]] += 1
                control_id = f"{CONTROL_PREFIXES[packet['controlType']]}-{counters[packet['controlType']]:03d}"
            group_control_ids.append(control_id)
            merge_justification = string_or_none(group.get("mergeJustification"))
            notes = merge_justification and f"Merge justification: {merge_justification}"
            controls.append(
                {
                    "control_id": control_id,
                    "control_type": packet["controlType"],
                    "title": group.get("title") or representative["title"],
                    "description": group.get("description", representative.get("description")),
                    "owner": group.get("owner", representative.get("party")),
                    "recipient": group.get("recipient", representative.get("counterparty")),
                    "date_text": group.get("date_text", representative.get("date_text")),
                    "date_kind": group.get("date_kind", representative.get("date_kind") or "none"),
                    "date": group.get("date", representative.get("date")),
                    "trigger": group.get("trigger", representative.get("trigger")),
                    "offset_days": group.get("offset_days", representative.get("offset_days")),
                    "recurrence": group.get("recurrence", representative.get("recurrence")),
                    "acceptance_criteria": group.get("acceptance_criteria", representative.get("acceptance_criteria")),
                    "evidence_required": group.get("evidence_required", representative.get("evidence_required")),
                    "source_status": group.get("source_status", representative.get("source_status")),
                    "commitment_level": group.get("commitment_level", representative.get("commitment_level") or "unclear"),
                    "source_evidence_ids": evidence_ids,
                    "relationships": {"parent": [], "depends_on": [], "satisfies": [], "supersedes": [], "conflicts_with": []},
                    "notes": notes,
                    "teams": group.get("teams", representative.get("teams") or []),
                    "workstreams": group.get("workstreams", representative.get("workstreams") or []),
                    "scope_relation": group.get("scope_relation", representative.get("scope_relation") or "full"),
                    "start_date": group.get("start_date", representative.get("start_date")),
                    "end_date": group.get("end_date", representative.get("end_date")),
                    "duration_days": group.get("duration_days", representative.get("duration_days")),
                    "schedule_basis": group.get("schedule_basis", representative.get("schedule_basis")),
                }
            )
        normalized_dispositions = []
        for disposition in dispositions:
            group_index = disposition.get("controlGroup")
            control_id = group_control_ids[group_index] if isinstance(group_index, int) and 0 <= group_index < len(group_control_ids) else None
            normalized_dispositions.append(
                {
                    "evidence_id": disposition.get("evidence_id"),
                    "disposition": disposition.get("disposition"),
                    "control_id": control_id,
                    "note": disposition.get("note"),
                }
            )
        review_path = run_directory / "working" / f"{packet['reviewPacketId']}-automatic.json"
        write_json(review_path, {"reviewPacketId": packet["reviewPacketId"], "controls": controls, "dispositions": normalized_dispositions})
        command_record_review(argparse.Namespace(run_directory=str(run_directory), review_file=str(review_path)))
    return True


def automatic_relationship_review(run_directory, chat, background=False):
    review_manifest = load_review_manifest(run_directory)
    results = current_review_results(run_directory, review_manifest)
    controls = [control for result in results.values() for control in result.get("controls", [])]
    if len(controls) < 2:
        return
    prompt = f"""Review cross-control relationships. Return exactly {{"relationships":[{{"from":"CONTROL-ID","type":"parent|depends_on|satisfies|supersedes|conflicts_with","to":"CONTROL-ID","reason":"source-backed reason"}}],"needsReview":false,"reviewReason":null}}. Do not invent relationships. Mark needsReview for unresolved authority conflicts. Controls: {json.dumps(controls, ensure_ascii=False, sort_keys=True)}"""
    value, _ = post_chat_json(
        run_directory,
        "relationships",
        RECONCILIATION_SYSTEM_PROMPT,
        prompt,
        chat,
        allow_preemption=background,
        background=background,
    )
    if not isinstance(value, dict) or value.get("needsReview"):
        set_worker_control(run_directory, "paused")
        fail(f"cross-control relationship review needs human review: {value.get('reviewReason') if isinstance(value, dict) else 'invalid response'}")
    by_id = {control["control_id"]: control for control in controls}
    field_by_type = {
        "parent": "parent_control_ids",
        "depends_on": "depends_on_control_ids",
        "satisfies": "satisfies_control_ids",
        "supersedes": "supersedes_control_ids",
        "conflicts_with": "conflicts_with_control_ids",
    }
    for relationship in value.get("relationships") or []:
        source = by_id.get(relationship.get("from"))
        target = relationship.get("to")
        field = field_by_type.get(relationship.get("type"))
        if source is None or target not in by_id or field is None:
            fail("relationship review returned an unknown control or relationship type")
        source[field] = sorted(set([*source.get(field, []), target]))
        reason = string_or_none(relationship.get("reason"))
        if reason:
            source["notes"] = " ".join(value for value in [source.get("notes"), f"Relationship basis: {reason}"] if value)
    for packet_id, result in results.items():
        updated = {**result, "recordedAt": utc_now(), "controls": [by_id[control["control_id"]] for control in result.get("controls", [])]}
        append_jsonl(run_directory / "control_review_results.jsonl", updated)


def write_run_metrics(run_directory):
    config, manifest = load_run(run_directory)
    results = current_results(run_directory, manifest)
    schedule = read_jsonl(run_directory / "inference_schedule.jsonl")
    evidence = read_jsonl(run_directory / "evidence_items.jsonl")
    controls = read_jsonl(run_directory / "controls.jsonl")
    cached = sum(int(row.get("cachedTokens") or 0) for row in schedule)
    prompt = sum(int(row.get("promptTokens") or 0) for row in schedule)
    metrics = {
        "generatedAt": utc_now(),
        "packets": len(active_packets(manifest)),
        "dispositions": {status: sum(row.get("status") == status for row in results.values()) for status in PACKET_STATUSES},
        "coverage": coverage_summary(run_directory, manifest),
        "modelCalls": sum(row.get("event") == "model_call" for row in schedule),
        "foregroundModelCalls": sum(row.get("event") == "model_call" and row.get("mode") == "foreground" for row in schedule),
        "backgroundModelCalls": sum(row.get("event") == "model_call" and row.get("mode") == "background" for row in schedule),
        "preemptions": sum(row.get("event") == "preempted" for row in schedule),
        "promptTokens": prompt,
        "cachedTokens": cached,
        "cacheHitRatio": round(cached / prompt, 4) if prompt else None,
        "prefillMs": sum(int(row.get("prefillMs") or 0) for row in schedule),
        "generationMs": sum(int(row.get("generationMs") or 0) for row in schedule),
        "retries": sum(row.get("event") == "validation_retry" for row in schedule),
        "warnings": [row.get("detail") for row in schedule if row.get("event") == "cache_warning"],
        "evidenceItems": len(evidence),
        "controls": len(controls),
        "evidencePerControl": round(len(evidence) / len(controls), 3) if controls else None,
        "elapsedSeconds": round((datetime.now(timezone.utc) - datetime.fromisoformat(config["createdAt"])).total_seconds(), 3),
    }
    write_json(run_directory / "run_metrics.json", metrics)


def worker_control(run_directory):
    path = run_directory / "worker_control.json"
    if not path.is_file():
        write_json(path, {"desiredState": "running", "pid": None, "updatedAt": utc_now()})
    return json.loads(path.read_text(encoding="utf-8"))


def set_worker_control(run_directory, desired_state, pid=None):
    value = worker_control(run_directory)
    value.update({"desiredState": desired_state, "updatedAt": utc_now()})
    if pid is not None:
        value["pid"] = pid
    write_json(run_directory / "worker_control.json", value)
    return value


def require_worker_runtime(run_directory, background=False):
    chat = chat_configuration()
    if not chat["enabled"]:
        fail("connectedServices.chat is disabled; configure the local llama.cpp chat endpoint before processing")
    if not background:
        return chat
    if not chat["scheduling"]["enabled"]:
        fail("background processing is unavailable because connectedServices.chat.scheduling is disabled; omit --background to process serially in the foreground")
    if chat["scheduling"]["interactiveSlot"] == chat["scheduling"]["backgroundSlot"]:
        fail("interactiveSlot and backgroundSlot must be different; reserve slot 0 for interactive work and slot 1 for background extraction")
    config, _ = load_run(run_directory)
    identity = {"url": chat["url"], "model": chat["model"], "slot": chat["scheduling"]["backgroundSlot"]}
    verified = config.get("worker", {}).get("slotProbe")
    if not isinstance(verified, dict) or any(verified.get(key) != value for key, value in identity.items()):
        probe = probe_background_slot(chat)
        if not probe.get("available"):
            fail(f"background slot {chat['scheduling']['backgroundSlot']} is unavailable: {probe.get('detail')}. Configure llama.cpp with at least two slots; slot 0 is interactive and slot 1 is background.")
        config.setdefault("worker", {})["slotProbe"] = {**identity, "verifiedAt": utc_now()}
        write_json(run_directory / "run_config.json", config)
    return chat


def run_worker(run_directory, background=False):
    chat = require_worker_runtime(run_directory, background)
    with run_state.run_lock(run_directory):
        set_worker_control(run_directory, "running", os.getpid())
        process_extraction(run_directory, chat, background)
        control = worker_control(run_directory)
        if control["desiredState"] != "running":
            return
        command_reconcile(argparse.Namespace(run_directory=str(run_directory), draft=False))
        if not automatic_review(run_directory, chat, background):
            return
        automatic_relationship_review(run_directory, chat, background)
        command_build(argparse.Namespace(run_directory=str(run_directory), as_of=None, draft=False))
        command_validate(argparse.Namespace(run_directory=str(run_directory), fix_hints=True, json=True))
        write_run_metrics(run_directory)
        set_worker_control(run_directory, "complete", os.getpid())


def command_process(args):
    run_directory = require_run_directory(args.run_directory)
    if args.worker:
        run_worker(run_directory, True)
        return
    if not args.background:
        run_worker(run_directory, False)
        return
    require_worker_runtime(run_directory, True)
    logs = run_directory / "working" / "worker.log"
    handle = logs.open("a", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "process", str(run_directory), "--worker"],
        stdin=subprocess.DEVNULL,
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    handle.close()
    set_worker_control(run_directory, "running", process.pid)
    print(json.dumps({"runDirectory": str(run_directory), "background": True, "pid": process.pid, "log": str(logs)}, indent=2))


def captured_command(handler, args):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        handler(args)
    return parse_command_json(buffer.getvalue(), handler.__name__)


def command_run(args):
    chat = chat_configuration()
    probe = None
    if not chat["enabled"]:
        fail("serial foreground extraction is unavailable because connectedServices.chat is disabled; configure it before initializing the run")
    if args.background:
        scheduling = chat["scheduling"]
        if not scheduling["enabled"]:
            fail("background extraction is unavailable because scheduling is disabled; omit --background for serial foreground processing")
        if scheduling["interactiveSlot"] == scheduling["backgroundSlot"]:
            fail("background extraction requires different interactive and background slots")
        probe = probe_background_slot(chat)
        if not probe.get("available"):
            fail(f"background slot {scheduling['backgroundSlot']} is unavailable: {probe.get('detail')}")
    run_directory = Path(args.output).expanduser().resolve()
    init_args = argparse.Namespace(
        inputs=args.inputs,
        output=str(run_directory),
        title=args.title,
        packet_chars=args.packet_chars,
        inbox=args.inbox,
        focus=args.focus,
        team=args.team,
        person=args.person,
        workstream=args.workstream,
        include_source=args.include_source,
        exclude_source=args.exclude_source,
        control_type=args.control_type,
        date_from=args.date_from,
        date_to=args.date_to,
    )
    initialized = captured_command(command_init, init_args)
    if probe is not None:
        config, _ = load_run(run_directory)
        config.setdefault("worker", {})["slotProbe"] = {
            "url": chat["url"],
            "model": chat["model"],
            "slot": chat["scheduling"]["backgroundSlot"],
            "verifiedAt": utc_now(),
        }
        write_json(run_directory / "run_config.json", config)
    inbox = inbox_status_value(run_directory)
    intake = None
    if inbox.get("activeBatch") or inbox.get("pendingCount"):
        intake = captured_command(command_inbox_sync, argparse.Namespace(run_directory=str(run_directory), inbox=args.inbox))
        if not intake.get("synced") and intake.get("nextAction") == "complete_document_ingest_review":
            print(json.dumps({"runDirectory": str(run_directory), "initialized": initialized, "inbox": intake, "complete": False, "exactNextAction": intake.get("commands") or intake["nextAction"]}, indent=2))
            return
    process_output = io.StringIO()
    with contextlib.redirect_stdout(process_output):
        command_process(argparse.Namespace(run_directory=str(run_directory), background=args.background, worker=False))
    if args.background:
        print(json.dumps({"runDirectory": str(run_directory), "initialized": initialized, "inbox": intake, "background": True, "complete": False, "exactNextAction": f"project-extraction.py status {run_directory} --json"}, indent=2))
        return
    status = captured_command(command_status, argparse.Namespace(run_directory=str(run_directory), json=True))
    print(json.dumps({"runDirectory": str(run_directory), "initialized": initialized, "inbox": intake, "status": status, "complete": status.get("status") == "complete", "exactNextAction": status.get("exactNextAction")}, indent=2))


def command_worker_control(args):
    run_directory = require_run_directory(args.run_directory)
    desired = {"pause": "paused", "stop-after-current": "stop_after_current", "resume": "running"}[args.command]
    value = set_worker_control(run_directory, desired)
    if args.command == "resume":
        command_process(argparse.Namespace(run_directory=str(run_directory), background=True, worker=False))
        return
    print(json.dumps({"runDirectory": str(run_directory), **value}, indent=2))


def control_matches_scope(control, scope):
    serialized = json.dumps(control, ensure_ascii=False).lower()
    if scope.get("focus") and scope["focus"].lower() not in serialized:
        return False
    for key in ("teams", "people", "workstreams"):
        values = scope.get(key, [])
        if values and not any(value.lower() in serialized for value in values):
            return False
    control_types = scope.get("controlTypes", [])
    if control_types and control["control_type"] not in control_types:
        return False
    date_value = control.get("date") or control.get("end_date") or control.get("start_date")
    if scope.get("dateFrom") and (not date_value or date_value < scope["dateFrom"]):
        return False
    if scope.get("dateTo") and (not date_value or date_value > scope["dateTo"]):
        return False
    return True


def command_focus(args):
    parent = require_run_directory(args.run_directory)
    state = run_state.load_run_state(parent, "project-extraction")
    if state.get("status") != "complete":
        fail("focus views require a completed parent run")
    output = Path(args.output).expanduser().resolve()
    if output.exists():
        fail(f"focus output already exists: {output}")
    output.mkdir(parents=True)
    scope = scope_options(args)
    controls = read_jsonl(parent / "controls.jsonl")
    selected_ids = {row["control_id"] for row in controls if control_matches_scope(row, scope)}
    changed = True
    while changed:
        changed = False
        for row in controls:
            related = set(row.get("depends_on_control_ids", [])) | set(row.get("parent_control_ids", []))
            if row["control_id"] in selected_ids:
                before = len(selected_ids)
                selected_ids.update(related)
                changed |= len(selected_ids) != before
            elif related & selected_ids:
                selected_ids.add(row["control_id"])
                changed = True
    selected = [{**row, "scope_relation": "direct" if control_matches_scope(row, scope) else "dependency"} for row in controls if row["control_id"] in selected_ids]
    run_state.atomic_write_text(output / "controls.jsonl", "" if not selected else "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in selected) + "\n")
    write_csv(output / "controls.csv", CONTROL_COLUMNS, control_csv_rows(selected))
    status = [row for row in read_csv(parent / "project_status.csv") if row.get("control_id") in selected_ids]
    write_csv(output / "project_status.csv", STATUS_COLUMNS, status)
    write_gantt_outputs(output, selected)
    parent_config, _ = load_run(parent)
    view_config = {**parent_config, "scope": scope, "scopeMode": "focused", "parentRun": str(parent)}
    write_json(output / "view_config.json", view_config)
    coverage_gap = parent_config.get("scopeMode") == "focused"
    write_json(output / "coverage.json", {"completeParentCoverage": not coverage_gap, "warning": "Parent run was focused; this view may not cover excluded source material." if coverage_gap else None})
    author_markdown_briefs(output, view_config, selected, False)
    print(json.dumps({"viewDirectory": str(output), "controls": len(selected), "coverageGap": coverage_gap}, indent=2))


def add_scope_arguments(command):
    command.add_argument("--focus")
    command.add_argument("--team", action="append")
    command.add_argument("--person", action="append")
    command.add_argument("--workstream", action="append")
    command.add_argument("--include-source", action="append")
    command.add_argument("--exclude-source", action="append")
    command.add_argument("--control-type", action="append", choices=ITEM_TYPES)
    command.add_argument("--date-from", type=lambda value: validate_iso_date(value, "--date-from"))
    command.add_argument("--date-to", type=lambda value: validate_iso_date(value, "--date-to"))


def parser():
    root = argparse.ArgumentParser(description="Extract refreshable, source-backed project controls from document corpora.")
    commands = root.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="Report local project-extraction capabilities.")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--probe-slot", action="store_true")
    doctor.set_defaults(handler=command_doctor)

    init = commands.add_parser("init", help="Discover sources, hash them, and create bounded extraction packets.")
    init.add_argument("inputs", nargs="+")
    init.add_argument("--output", required=True)
    init.add_argument("--title")
    init.add_argument("--packet-chars", type=int, default=DEFAULT_PACKET_CHARACTERS)
    init.add_argument("--inbox", help="Explicit Inbox path for multi-root or nonstandard project layouts.")
    add_scope_arguments(init)
    init.set_defaults(handler=command_init)

    next_command = commands.add_parser("next", help="Return exactly one pending source packet as JSON.")
    next_command.add_argument("run_directory")
    next_command.set_defaults(handler=command_next)

    record = commands.add_parser("record", help="Append one packet extraction or explicit disposition.")
    record.add_argument("run_directory")
    record.add_argument("--packet-id", required=True)
    record.add_argument("--items-file")
    record.add_argument("--status", choices=sorted(PACKET_STATUSES), default="extracted")
    record.add_argument("--note")
    record.add_argument("--screening-method", choices=("model", "human"))
    record.add_argument("--screening-finding")
    record.add_argument("--disposition-source", choices=("worker", "manual"), default="manual")
    record.add_argument("--document-role")
    record.set_defaults(handler=command_record)

    validate_extraction = commands.add_parser("validate-extraction", help="Report every schema, date, and quote error without recording a result.")
    validate_extraction.add_argument("run_directory")
    validate_extraction.add_argument("--packet-id", required=True)
    validate_extraction.add_argument("--items-file", required=True)
    validate_extraction.set_defaults(handler=command_validate_extraction)

    reconcile = commands.add_parser("reconcile", help="Build evidence tables and bounded canonical-control review packets.")
    reconcile.add_argument("run_directory")
    reconcile.add_argument("--draft", action="store_true")
    reconcile.set_defaults(handler=command_reconcile)

    next_review = commands.add_parser("next-review", help="Return exactly one pending control reconciliation packet.")
    next_review.add_argument("run_directory")
    next_review.set_defaults(handler=command_next_review)

    record_review = commands.add_parser("record-review", help="Record canonical controls and explicit evidence dispositions.")
    record_review.add_argument("run_directory")
    record_review.add_argument("--review-file", required=True)
    record_review.set_defaults(handler=command_record_review)

    build = commands.add_parser("build", help="Build project registers and scaffold human-facing deliverables.")
    build.add_argument("run_directory")
    build.add_argument("--as-of", type=lambda value: validate_iso_date(value, "--as-of"))
    build.add_argument("--draft", action="store_true")
    build.set_defaults(handler=command_build)

    refresh = commands.add_parser("refresh", help="Rescan inputs and queue only new or changed source revisions.")
    refresh.add_argument("run_directory")
    refresh.set_defaults(handler=command_refresh)

    status = commands.add_parser("status", help="Report durable progress and source drift without changing the run.")
    status.add_argument("run_directory")
    status.add_argument("--json", action="store_true")
    status.set_defaults(handler=command_status)

    retry = commands.add_parser("retry", help="Explicitly requeue permanent packet failures.")
    retry.add_argument("run_directory")
    retry_group = retry.add_mutually_exclusive_group(required=True)
    retry_group.add_argument("--item")
    retry_group.add_argument("--all-failed", action="store_true")
    retry_group.add_argument("--invalid-screenings", action="store_true")
    retry.set_defaults(handler=command_retry)

    validate = commands.add_parser("validate", help="Validate sources, evidence, controls, status, and authored deliverables.")
    validate.add_argument("run_directory")
    validate.add_argument("--fix-hints", action="store_true")
    validate.add_argument("--json", action="store_true")
    validate.set_defaults(handler=command_validate)

    index = commands.add_parser("index", help="Build or incrementally refresh the hybrid project search index.")
    index.add_argument("run_directory")
    index.add_argument("--rebuild", action="store_true")
    index.set_defaults(handler=command_index)

    search = commands.add_parser("search", help="Return hybrid-ranked controls, evidence, and source passages.")
    search.add_argument("run_directory")
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=10)
    search.set_defaults(handler=command_search)

    show = commands.add_parser("show", help="Load one search hit and optionally its complete source documents.")
    show.add_argument("run_directory")
    show.add_argument("--hit-id", required=True)
    show.add_argument("--full-source", action="store_true")
    show.set_defaults(handler=command_show)

    inbox_status = commands.add_parser("inbox-status", help="Report pending Inbox files without modifying them.")
    inbox_status.add_argument("run_directory")
    inbox_status.set_defaults(handler=command_inbox_status)

    inbox_sync = commands.add_parser("inbox-sync", help="Resume Inbox ingestion and refresh the project when publication completes.")
    inbox_sync.add_argument("run_directory")
    inbox_sync.add_argument("--inbox", help="Configure an explicit Inbox path for an existing multi-root run.")
    inbox_sync.set_defaults(handler=command_inbox_sync)

    process = commands.add_parser("process", help="Run the cache-aware extraction worker through completion.")
    process.add_argument("run_directory")
    process.add_argument("--background", action="store_true")
    process.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    process.set_defaults(handler=command_process)

    run = commands.add_parser("run", help="Initialize or resume, sync Inbox, extract, reconcile, build, and validate through durable checkpoints.")
    run.add_argument("inputs", nargs="+")
    run.add_argument("--output", required=True)
    run.add_argument("--title")
    run.add_argument("--packet-chars", type=int, default=DEFAULT_PACKET_CHARACTERS)
    run.add_argument("--inbox", help="Explicit Inbox path for multi-root or nonstandard project layouts.")
    run.add_argument("--background", action="store_true")
    add_scope_arguments(run)
    run.set_defaults(handler=command_run)

    for name in ("pause", "resume", "stop-after-current"):
        control = commands.add_parser(name, help=f"{name.replace('-', ' ').title()} the cache-aware worker.")
        control.add_argument("run_directory")
        control.set_defaults(handler=command_worker_control)

    focus = commands.add_parser("focus", help="Build a focused dependency-closed view from a completed run.")
    focus.add_argument("run_directory")
    focus.add_argument("--output", required=True)
    add_scope_arguments(focus)
    focus.set_defaults(handler=command_focus)
    return root


def main():
    args = parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()

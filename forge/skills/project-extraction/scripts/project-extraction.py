#!/usr/bin/env python3

import argparse
import csv
import hashlib
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path


RUN_SCHEMA_VERSION = 1
DEFAULT_PACKET_CHARACTERS = 60_000
SOURCE_EXTENSIONS = {".md", ".markdown", ".txt", ".csv"}
RESERVED_DIRECTORIES = {"Ingest", "Originals", "Generated"}
PLACEHOLDER = "<!-- TODO: author this section -->"

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
PACKET_STATUSES = {"success", "needs_review", "skipped", "failed"}
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
)

STATUS_COLUMNS = ("control_id", "current_owner", "working_status", "forecast_date", "last_updated", "notes")
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


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def append_jsonl(path, value):
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def read_jsonl(path):
    if not path.exists():
        return []
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as error:
            fail(f"invalid JSONL at {path}:{line_number}: {error}")
    return rows


def write_csv(path, columns, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: csv_value(row.get(column)) for column in columns})


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


def require_new_directory(raw_path):
    path = Path(raw_path).expanduser().resolve()
    if path.exists():
        fail(f"output already exists: {path}")
    path.mkdir(parents=True)
    return path


def require_run_directory(raw_path):
    path = Path(raw_path).expanduser().resolve()
    if not path.is_dir() or not (path / "run_config.json").is_file():
        fail(f"not a project-extraction run: {path}")
    return path


def load_run(run_directory):
    config = json.loads((run_directory / "run_config.json").read_text(encoding="utf-8"))
    manifest = json.loads((run_directory / "source_manifest.json").read_text(encoding="utf-8"))
    return config, manifest


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
                if any(part.startswith(".") or part in RESERVED_DIRECTORIES for part in relative.parts[:-1]):
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
    return item


def command_doctor(args):
    result = {
        "status": "ok",
        "python": sys.version.split()[0],
        "schemaVersion": RUN_SCHEMA_VERSION,
        "sourceExtensions": sorted(SOURCE_EXTENSIONS),
        "defaultPacketCharacters": DEFAULT_PACKET_CHARACTERS,
    }
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Project extraction OK (Python {result['python']})")
        print(f"Source formats: {', '.join(result['sourceExtensions'])}")


def command_init(args):
    if args.packet_chars < 1_000:
        fail("--packet-chars must be at least 1000")
    run_directory = require_new_directory(args.output)
    (run_directory / "working").mkdir()
    sources = discover_sources(args.inputs)
    manifest = initialize_manifest(run_directory, sources, args.packet_chars)
    config = {
        "schemaVersion": RUN_SCHEMA_VERSION,
        "title": args.title or Path(args.inputs[0]).expanduser().stem or "Project",
        "inputs": [str(Path(value).expanduser().resolve()) for value in args.inputs],
        "packetCharacters": args.packet_chars,
        "createdAt": utc_now(),
        "updatedAt": utc_now(),
        "asOfDate": None,
    }
    write_json(run_directory / "run_config.json", config)
    write_json(run_directory / "source_manifest.json", manifest)
    append_source_changes(run_directory, None, manifest)
    print(json.dumps({"runDirectory": str(run_directory), "sources": len(sources), "packets": len(active_packets(manifest))}, indent=2))


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
    if args.status != "success":
        if not args.note:
            fail("non-success dispositions require --note")
        result = {
            "recordedAt": utc_now(),
            "packetId": args.packet_id,
            "sourceId": source["sourceId"],
            "sourceRevision": source["revisionId"],
            "status": args.status,
            "documentRole": "other",
            "items": [],
            "note": args.note,
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
            "status": "success",
            "documentRole": document_role,
            "items": normalized,
            "note": args.note,
        }
    append_jsonl(run_directory / "extraction_results.jsonl", result)
    print(json.dumps({"packetId": args.packet_id, "status": result["status"], "items": len(result["items"])}, indent=2))


def evidence_rows(run_directory, manifest):
    results = current_results(run_directory, manifest)
    source_by_id = {source["sourceId"]: source for source in active_sources(manifest)}
    rows = []
    for packet in active_packets(manifest):
        result = results.get(packet["packetId"])
        if not result or result["status"] != "success":
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
    if missing:
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
    print(json.dumps({"evidenceItems": len(rows), "reviewPackets": len(review_packets), "pending": sum(row["status"] == "pending" for row in review_packets)}, indent=2))


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


def command_build(args):
    run_directory = require_run_directory(args.run_directory)
    config, manifest = load_run(run_directory)
    review_manifest = load_review_manifest(run_directory)
    results = current_review_results(run_directory, review_manifest)
    pending = [row["reviewPacketId"] for row in review_manifest["packets"] if row["reviewPacketId"] not in results]
    if pending:
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
    print(json.dumps({"controls": len(controls), "asOfDate": config["asOfDate"], "proposalChecklist": proposal_present}, indent=2))


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
    print(json.dumps({"runDirectory": str(run_directory), **counts, "pendingPackets": len(active_packets(manifest)) - len(current_results(run_directory, manifest))}, indent=2))


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
    for packet in active_packets(manifest):
        result = results.get(packet["packetId"])
        if result is None:
            issues.append(validation_issue("packet_pending", f"packet has no disposition: {packet['packetId']}", "Run next and record."))
        elif result["status"] != "success":
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
    )
    for filename in required_built:
        if not (run_directory / filename).is_file():
            issues.append(validation_issue("artifact_missing", f"missing required artifact: {filename}", "Run build."))
    evidence_ids = {row["evidence_id"] for row in read_csv(run_directory / "evidence_items.csv")}
    controls = read_jsonl(run_directory / "controls.jsonl")
    control_ids = {row.get("control_id") for row in controls}
    for control in controls:
        unknown_evidence = set(control.get("source_evidence_ids", [])) - evidence_ids
        if unknown_evidence:
            issues.append(validation_issue("unknown_evidence", f"{control['control_id']} cites unknown evidence: {', '.join(sorted(unknown_evidence))}"))
        for field in ("parent_control_ids", "depends_on_control_ids", "satisfies_control_ids", "supersedes_control_ids", "conflicts_with_control_ids"):
            unknown_controls = set(control.get(field, [])) - control_ids
            if unknown_controls:
                issues.append(validation_issue("unknown_control", f"{control['control_id']} {field} cites unknown controls: {', '.join(sorted(unknown_controls))}"))
    status_path = run_directory / "project_status.csv"
    if status_path.is_file():
        for row_number, row in enumerate(read_csv(status_path), 2):
            if row.get("working_status", "") not in WORKING_STATUSES:
                issues.append(validation_issue("status_value", f"project_status.csv:{row_number} has invalid working_status"))
            if not is_iso_date(row.get("forecast_date")) or not is_iso_date(row.get("last_updated")):
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


def parser():
    root = argparse.ArgumentParser(description="Extract refreshable, source-backed project controls from document corpora.")
    commands = root.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="Report local project-extraction capabilities.")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(handler=command_doctor)

    init = commands.add_parser("init", help="Discover sources, hash them, and create bounded extraction packets.")
    init.add_argument("inputs", nargs="+")
    init.add_argument("--output", required=True)
    init.add_argument("--title")
    init.add_argument("--packet-chars", type=int, default=DEFAULT_PACKET_CHARACTERS)
    init.set_defaults(handler=command_init)

    next_command = commands.add_parser("next", help="Return exactly one pending source packet as JSON.")
    next_command.add_argument("run_directory")
    next_command.set_defaults(handler=command_next)

    record = commands.add_parser("record", help="Append one packet extraction or explicit disposition.")
    record.add_argument("run_directory")
    record.add_argument("--packet-id", required=True)
    record.add_argument("--items-file")
    record.add_argument("--status", choices=sorted(PACKET_STATUSES), default="success")
    record.add_argument("--note")
    record.set_defaults(handler=command_record)

    reconcile = commands.add_parser("reconcile", help="Build evidence tables and bounded canonical-control review packets.")
    reconcile.add_argument("run_directory")
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
    build.set_defaults(handler=command_build)

    refresh = commands.add_parser("refresh", help="Rescan inputs and queue only new or changed source revisions.")
    refresh.add_argument("run_directory")
    refresh.set_defaults(handler=command_refresh)

    validate = commands.add_parser("validate", help="Validate sources, evidence, controls, status, and authored deliverables.")
    validate.add_argument("run_directory")
    validate.add_argument("--fix-hints", action="store_true")
    validate.add_argument("--json", action="store_true")
    validate.set_defaults(handler=command_validate)
    return root


def main():
    args = parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()

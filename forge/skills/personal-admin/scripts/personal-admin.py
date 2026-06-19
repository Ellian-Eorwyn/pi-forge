#!/usr/bin/env python3

import argparse
import csv
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path


RUN_SCHEMA_VERSION = 1
SOURCE_EXTENSIONS = {".md", ".markdown", ".txt"}
PLACEHOLDER = "<!-- TODO: author this section -->"

FACT_TYPES = [
    "deadline",
    "action",
    "contact",
    "reference_number",
    "date",
    "fee",
    "requirement",
    "missing_info",
]
FACT_TYPE_SET = set(FACT_TYPES)
CONFIDENCES = {"high", "medium", "low"}
DOCUMENT_STATUSES = {"success", "needs_review", "skipped", "failed"}

# Deliverable name -> (kind, filename). kind is "markdown" (authored) or "csv" (derived).
DELIVERABLES = {
    "admin_summary": ("markdown", "admin_summary.md"),
    "next_steps": ("markdown", "next_steps.md"),
    "message_draft": ("markdown", "message_draft.md"),
    "comparison_table": ("markdown", "comparison_table.md"),
    "call_script": ("markdown", "call_script.md"),
    "deadline_checklist": ("csv", "deadline_checklist.csv"),
    "contact_list": ("csv", "contact_list.csv"),
}
DEFAULT_DELIVERABLES = ["admin_summary", "next_steps", "deadline_checklist", "contact_list"]

EXTRACTED_FACTS_COLUMNS = [
    "document_id",
    "source_title",
    "fact_type",
    "text",
    "value",
    "due_date",
    "locator",
    "confidence",
    "notes",
]
DEADLINE_COLUMNS = [
    "document_id",
    "source_title",
    "due_date",
    "item",
    "fact_type",
    "locator",
    "confidence",
    "notes",
]
CONTACT_COLUMNS = [
    "document_id",
    "source_title",
    "contact",
    "value",
    "locator",
    "confidence",
    "notes",
]

MARKDOWN_TEMPLATES = {
    "admin_summary.md": [
        "# Admin Summary: {title}",
        "",
        PLACEHOLDER,
        "",
        "<!-- Plain-language summary of what each document says. Facts only;",
        "keep suggested actions in next_steps.md. -->",
        "",
        "## What These Documents Are",
        "",
        "## Key Facts",
        "",
        "## Missing or Unclear Information",
        "",
    ],
    "next_steps.md": [
        "# Next Steps: {title}",
        "",
        PLACEHOLDER,
        "",
        "<!-- Suggested plan. This is generated guidance, not professional advice,",
        "and is distinct from the document facts in extracted_facts.csv. -->",
        "",
        "## Prioritized Actions",
        "",
        "## Upcoming Deadlines",
        "",
        "See `deadline_checklist.csv`.",
        "",
        "## Where Professional Advice May Be Warranted",
        "",
    ],
    "message_draft.md": [
        "# Message Draft: {title}",
        "",
        PLACEHOLDER,
        "",
        "<!-- Draft email/letter/message. Note any placeholders the user must fill",
        "(account numbers, dates) rather than inventing them. -->",
        "",
    ],
    "comparison_table.md": [
        "# Comparison: {title}",
        "",
        PLACEHOLDER,
        "",
        "| Option | Cost | Key terms | Deadline | Notes |",
        "|---|---|---|---|---|",
        "",
    ],
    "call_script.md": [
        "# Call Script: {title}",
        "",
        PLACEHOLDER,
        "",
        "## Before You Call",
        "",
        "## What to Say",
        "",
        "## Information to Have Ready",
        "",
        "## Questions to Ask",
        "",
    ],
}


def fail(message, exit_code=1):
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(exit_code)


def utc_now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def blank(value):
    return value is None or (isinstance(value, str) and value.strip() == "")


def require_new_directory(raw_path):
    path = Path(raw_path).expanduser().resolve()
    if path.exists():
        fail(f"output already exists: {path}")
    path.mkdir(parents=True)
    return path


def require_run_directory(raw_path):
    path = Path(raw_path).expanduser().resolve()
    if not path.is_dir():
        fail(f"run directory does not exist: {path}")
    if not (path / "run_config.json").is_file():
        fail(f"run_config.json is missing: {path}")
    return path


def title_from_metadata(metadata_path):
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    fields = metadata.get("fields") if isinstance(metadata, dict) else None
    title = fields.get("title") if isinstance(fields, dict) else None
    value = title.get("value") if isinstance(title, dict) else None
    return value if isinstance(value, str) and value.strip() else None


def discover_sources(raw_inputs):
    seen = set()
    found = []
    for raw in raw_inputs:
        root = Path(raw).expanduser().resolve()
        if not root.exists():
            fail(f"input does not exist: {root}")
        if root.is_symlink():
            fail(f"input is a symlink: {root}")
        if root.is_file():
            if root.suffix.lower() not in SOURCE_EXTENSIONS:
                fail(f"unsupported input format {root.suffix or '(none)'}; expected .md, .markdown, or .txt")
            candidates = [root]
        else:
            candidates = []
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.suffix.lower() not in SOURCE_EXTENSIONS:
                    continue
                relative = path.relative_to(root)
                if any(part.startswith(".") for part in relative.parts):
                    continue
                current = root
                linked = False
                for part in relative.parts:
                    current = current / part
                    if current.is_symlink():
                        linked = True
                        break
                if not linked:
                    candidates.append(path)
        for path in candidates:
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                found.append(resolved)
    if not found:
        fail("no .md, .markdown, or .txt sources found")
    return found


def build_document_records(sources):
    documents = []
    used_ids = set()
    for source in sources:
        digest = sha256(source)
        stem = source.stem or "document"
        base = f"{stem}-{digest[:12]}"
        document_id = base
        suffix = 1
        while document_id in used_ids:
            suffix += 1
            document_id = f"{base}-{suffix}"
        used_ids.add(document_id)
        title = None
        if source.name == "document.md":
            candidate = source.with_name("metadata.json")
            if candidate.is_file():
                title = title_from_metadata(candidate)
        documents.append(
            {
                "documentId": document_id,
                "sourcePath": str(source),
                "sha256": digest,
                "sizeBytes": source.stat().st_size,
                "title": title,
            }
        )
    return documents


def write_sources_md(run_directory, title, documents):
    lines = [
        f"# Sources: {title}",
        "",
        "Deterministically generated from `source_manifest.json`. Keep sensitive",
        "documents local and redact before sharing any output.",
        "",
        "| ID | Title | Size (bytes) | SHA-256 | Path |",
        "|---|---|---:|---|---|",
    ]
    for document in documents:
        lines.append(
            f"| `{document['documentId']}` | {document['title'] or '—'} | "
            f"{document['sizeBytes']} | `{document['sha256'][:12]}…` | `{document['sourcePath']}` |"
        )
    lines.append("")
    (run_directory / "sources.md").write_text("\n".join(lines), encoding="utf-8")


def scaffold_markdown(run_directory, title, deliverables):
    created = []
    for name in deliverables:
        kind, filename = DELIVERABLES[name]
        if kind != "markdown":
            continue
        path = run_directory / filename
        if path.exists():
            continue
        body = "\n".join(line.replace("{title}", title) for line in MARKDOWN_TEMPLATES[filename]) + "\n"
        path.write_text(body, encoding="utf-8")
        created.append(filename)
    return created


def command_doctor(args):
    result = {
        "python": sys.version.split()[0],
        "note": "Convert PDF/DOCX/HTML/RTF with document-ingest first; this skill consumes document.md, .md, and .txt.",
        "advisory": "This skill organizes and summarizes information. It does not provide legal, medical, or financial advice.",
    }
    if args.json:
        print(json.dumps(result, indent=2))
        return
    print(f"Python: {result['python']}")
    print(f"Note: {result['note']}")
    print(f"Advisory: {result['advisory']}")


def parse_deliverables(raw):
    if raw is None:
        return list(DEFAULT_DELIVERABLES)
    names = [item.strip() for item in raw.split(",") if item.strip()]
    if not names:
        fail("--deliverables was empty")
    unknown = [name for name in names if name not in DELIVERABLES]
    if unknown:
        fail(f"unknown deliverables: {', '.join(unknown)}; choose from {', '.join(sorted(DELIVERABLES))}")
    ordered = []
    for name in names:
        if name not in ordered:
            ordered.append(name)
    return ordered


def command_init(args):
    deliverables = parse_deliverables(args.deliverables)
    sources = discover_sources(args.inputs)
    documents = build_document_records(sources)
    title = args.title or "Personal Admin"
    output = require_new_directory(args.output)
    (output / "working").mkdir()
    run = {
        "schemaVersion": RUN_SCHEMA_VERSION,
        "createdAt": utc_now(),
        "title": title,
        "deliverables": deliverables,
        "factTypes": FACT_TYPES,
        "documents": documents,
    }
    (output / "run_config.json").write_text(json.dumps(run, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    manifest = {"schemaVersion": RUN_SCHEMA_VERSION, "createdAt": utc_now(), "documents": documents}
    (output / "source_manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (output / "facts_results.jsonl").write_text("", encoding="utf-8")
    write_sources_md(output, title, documents)
    created = scaffold_markdown(output, title, deliverables)
    print(
        json.dumps(
            {"runDirectory": str(output), "title": title, "documents": len(documents), "deliverables": deliverables, "scaffolded": created},
            ensure_ascii=False,
        )
    )


def load_run(run_directory):
    try:
        run = json.loads((run_directory / "run_config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail(f"could not read run_config.json: {error}")
    if run.get("schemaVersion") != RUN_SCHEMA_VERSION:
        fail(f"unsupported run schema version: {run.get('schemaVersion')}")
    return run


def load_results(run_directory, strict=True):
    path = run_directory / "facts_results.jsonl"
    if not path.is_file():
        fail(f"facts_results.jsonl is missing: {path}")
    results = []
    seen = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            result = json.loads(line)
        except json.JSONDecodeError as error:
            fail(f"invalid JSON on facts_results.jsonl line {line_number}: {error}")
        document_id = result.get("documentId")
        if strict and document_id in seen:
            fail(f"duplicate result for document {document_id}")
        seen.add(document_id)
        results.append(result)
    return results


def document_order(run):
    return [document["documentId"] for document in run["documents"]]


def next_pending(run, results):
    recorded = {result.get("documentId") for result in results}
    for document_id in document_order(run):
        if document_id not in recorded:
            return document_id
    return None


def document_by_id(run, document_id):
    for document in run["documents"]:
        if document["documentId"] == document_id:
            return document
    return None


def command_next(args):
    run_directory = require_run_directory(args.run_directory)
    run = load_run(run_directory)
    results = load_results(run_directory)
    document_id = next_pending(run, results)
    total = len(run["documents"])
    if document_id is None:
        print(json.dumps({"complete": True, "processed": len(results), "total": total}))
        return
    document = document_by_id(run, document_id)
    print(
        json.dumps(
            {
                "complete": False,
                "documentId": document_id,
                "sourcePath": document["sourcePath"],
                "textPath": document["sourcePath"],
                "title": document["title"],
                "factTypes": run["factTypes"],
                "progress": {"processed": len(results), "total": total},
            },
            ensure_ascii=False,
        )
    )


def normalize_facts(raw):
    if not isinstance(raw, list):
        fail("facts file must contain a JSON array of fact objects")
    facts = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            fail(f"fact {index} is not an object")
        fact_type = item.get("fact_type")
        if fact_type not in FACT_TYPE_SET:
            fail(f"fact {index} has invalid fact_type {fact_type!r}; expected one of {', '.join(FACT_TYPES)}")
        if blank(item.get("text")):
            fail(f"fact {index} requires a nonblank text value")
        confidence = item.get("confidence")
        if confidence not in CONFIDENCES:
            fail(f"fact {index} has invalid confidence {confidence!r}; expected high, medium, or low")
        for optional in ("value", "due_date", "locator", "notes"):
            value = item.get(optional)
            if value is not None and not isinstance(value, str):
                fail(f"fact {index} field {optional} must be a string or null")
        facts.append(
            {
                "fact_type": fact_type,
                "text": item["text"],
                "value": item.get("value"),
                "due_date": item.get("due_date"),
                "locator": item.get("locator"),
                "confidence": confidence,
                "notes": item.get("notes"),
            }
        )
    return facts


def command_record(args):
    run_directory = require_run_directory(args.run_directory)
    run = load_run(run_directory)
    results = load_results(run_directory)
    expected = next_pending(run, results)
    if expected is None:
        fail("the run is already complete")
    if args.doc_id != expected:
        fail(f"documents must be recorded sequentially; expected {expected}, received {args.doc_id}")
    if args.status == "success":
        if not args.facts_file:
            fail("successful results require --facts-file")
        facts_path = Path(args.facts_file).expanduser().resolve()
        if not facts_path.is_file():
            fail(f"facts file does not exist: {facts_path}")
        try:
            raw = json.loads(facts_path.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            fail(f"facts file is not valid UTF-8: {facts_path}")
        except json.JSONDecodeError as error:
            fail(f"facts file is not valid JSON: {error}")
        facts = normalize_facts(raw)
        note = args.note
    else:
        if args.facts_file:
            fail("--facts-file is only valid with --status success")
        if not args.note:
            fail(f"--status {args.status} requires --note")
        facts = None
        note = args.note
    result = {
        "documentId": args.doc_id,
        "status": args.status,
        "facts": facts,
        "note": note,
        "recordedAt": utc_now(),
    }
    with (run_directory / "facts_results.jsonl").open("a", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps(result, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    remaining = len(run["documents"]) - len(results) - 1
    print(json.dumps({"recorded": args.doc_id, "status": args.status, "facts": len(facts) if facts is not None else 0, "remaining": remaining}))


def verify_hashes(run):
    for document in run["documents"]:
        source = Path(document["sourcePath"])
        if not source.is_file():
            fail(f"source file is missing: {source}")
        if sha256(source) != document["sha256"]:
            fail(f"source file changed after init; refusing to proceed: {source}")


def write_csv(path, columns, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(columns)
        writer.writerows(rows)


def due_date_sort_key(value):
    # ISO dates sort lexically; undated facts sort last.
    return (0, value) if value else (1, "")


def command_build(args):
    run_directory = require_run_directory(args.run_directory)
    run = load_run(run_directory)
    results = load_results(run_directory)
    pending = next_pending(run, results)
    if pending is not None:
        fail(f"run is incomplete; next pending document is {pending}")
    verify_hashes(run)
    titles = {document["documentId"]: (document["title"] or "") for document in run["documents"]}
    deliverables = run.get("deliverables", [])

    all_rows = []
    deadline_rows = []
    contact_rows = []
    for result in results:
        if result.get("status") != "success":
            continue
        document_id = result["documentId"]
        title = titles.get(document_id, "")
        for fact in result.get("facts") or []:
            all_rows.append(
                [
                    document_id,
                    title,
                    fact["fact_type"],
                    fact["text"],
                    fact.get("value") or "",
                    fact.get("due_date") or "",
                    fact.get("locator") or "",
                    fact["confidence"],
                    fact.get("notes") or "",
                ]
            )
            if fact["fact_type"] == "deadline" or (fact["fact_type"] == "action" and fact.get("due_date")):
                deadline_rows.append(
                    [
                        document_id,
                        title,
                        fact.get("due_date") or "",
                        fact["text"],
                        fact["fact_type"],
                        fact.get("locator") or "",
                        fact["confidence"],
                        fact.get("notes") or "",
                    ]
                )
            if fact["fact_type"] == "contact":
                contact_rows.append(
                    [
                        document_id,
                        title,
                        fact["text"],
                        fact.get("value") or "",
                        fact.get("locator") or "",
                        fact["confidence"],
                        fact.get("notes") or "",
                    ]
                )
    deadline_rows.sort(key=lambda row: due_date_sort_key(row[2]))

    write_csv(run_directory / "extracted_facts.csv", EXTRACTED_FACTS_COLUMNS, all_rows)
    built = ["extracted_facts.csv"]
    if "deadline_checklist" in deliverables:
        write_csv(run_directory / "deadline_checklist.csv", DEADLINE_COLUMNS, deadline_rows)
        built.append("deadline_checklist.csv")
    if "contact_list" in deliverables:
        write_csv(run_directory / "contact_list.csv", CONTACT_COLUMNS, contact_rows)
        built.append("contact_list.csv")
    counts = Counter(result["status"] for result in results)
    print(
        json.dumps(
            {
                "facts": len(all_rows),
                "deadlines": len(deadline_rows),
                "contacts": len(contact_rows),
                "built": built,
                "success": counts["success"],
                "needsReview": counts["needs_review"],
                "skipped": counts["skipped"],
                "failed": counts["failed"],
            }
        )
    )


def command_validate(args):
    run_directory = require_run_directory(args.run_directory)
    run = load_run(run_directory)
    results = load_results(run_directory, strict=False)
    errors = []
    warnings = []
    order = document_order(run)
    valid_ids = set(order)
    seen = []
    for result in results:
        document_id = result.get("documentId")
        seen.append(document_id)
        if document_id not in valid_ids:
            errors.append(f"result references unknown document {document_id}")
        status = result.get("status")
        if status not in DOCUMENT_STATUSES:
            errors.append(f"document {document_id} has invalid status {status}")
        if status == "success":
            if not isinstance(result.get("facts"), list):
                errors.append(f"successful document {document_id} has no facts list")
            else:
                try:
                    normalize_facts(result["facts"])
                except SystemExit:
                    errors.append(f"document {document_id} has invalid facts")
        elif not result.get("note"):
            errors.append(f"non-successful document {document_id} has no note")
    duplicates = sorted(value for value, count in Counter(seen).items() if count > 1)
    if duplicates:
        errors.append(f"duplicate results for documents: {', '.join(str(value) for value in duplicates)}")
    if seen != order[: len(seen)]:
        errors.append("results are not in document order")
    missing = [document_id for document_id in order if document_id not in set(seen)]
    if missing:
        warnings.append(f"run is incomplete; {len(missing)} documents remain, beginning with {missing[0]}")

    if not (run_directory / "sources.md").is_file():
        errors.append("sources.md is missing; re-run init")
    for name in run.get("deliverables", []):
        kind, filename = DELIVERABLES[name]
        path = run_directory / filename
        if kind == "markdown":
            if not path.is_file():
                errors.append(f"deliverable is missing: {filename}")
            elif PLACEHOLDER in path.read_text(encoding="utf-8"):
                errors.append(f"deliverable still has an unresolved placeholder: {filename}")
        elif not missing and not path.is_file():
            errors.append(f"selected output is missing: {filename}; run build")
    if not missing and not (run_directory / "extracted_facts.csv").is_file():
        errors.append("extracted_facts.csv is missing; run build")

    for document in run["documents"]:
        source = Path(document["sourcePath"])
        if not source.is_file():
            errors.append(f"source file is missing: {source}")
        elif sha256(source) != document["sha256"]:
            errors.append(f"source file hash differs from init: {source}")

    counts = Counter(result.get("status") for result in results)
    result = {
        "valid": not errors,
        "complete": not missing,
        "counts": {status: counts[status] for status in sorted(DOCUMENT_STATUSES)},
        "errors": errors,
        "warnings": warnings,
    }
    print(json.dumps(result, indent=2))
    if errors:
        raise SystemExit(1)


def parser():
    root = argparse.ArgumentParser(description="Summarize personal-admin documents and stage structured facts into action plans.")
    subparsers = root.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Report capabilities and usage advisories.")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(handler=command_doctor)

    init = subparsers.add_parser("init", help="Discover documents and scaffold a resumable run.")
    init.add_argument("inputs", nargs="+")
    init.add_argument("--output", required=True)
    init.add_argument("--deliverables")
    init.add_argument("--title")
    init.set_defaults(handler=command_init)

    next_command = subparsers.add_parser("next", help="Return exactly one pending document as JSON.")
    next_command.add_argument("run_directory")
    next_command.set_defaults(handler=command_next)

    record = subparsers.add_parser("record", help="Append one document's facts or an explicit disposition.")
    record.add_argument("run_directory")
    record.add_argument("--doc-id", required=True)
    record.add_argument("--status", choices=sorted(DOCUMENT_STATUSES), default="success")
    record.add_argument("--facts-file")
    record.add_argument("--note")
    record.set_defaults(handler=command_record)

    build = subparsers.add_parser("build", help="Assemble facts, deadline, and contact tables from staged facts.")
    build.add_argument("run_directory")
    build.set_defaults(handler=command_build)

    validate = subparsers.add_parser("validate", help="Validate run state, facts, deliverables, and provenance.")
    validate.add_argument("run_directory")
    validate.set_defaults(handler=command_validate)
    return root


def main():
    args = parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()

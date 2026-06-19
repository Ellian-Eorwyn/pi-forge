#!/usr/bin/env python3

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path


RUN_SCHEMA_VERSION = 1
SOURCE_EXTENSIONS = {".md", ".markdown", ".txt"}
ITEM_TYPES = [
    "claim",
    "method",
    "data_source",
    "finding",
    "limitation",
    "definition",
    "citation",
    "quoted_evidence",
    "variable",
    "population",
    "technology",
    "policy",
    "research_gap",
]
ITEM_TYPE_SET = set(ITEM_TYPES)
INTERPRETATIONS = {"explicit", "inferred", "unclear"}
CONFIDENCES = {"high", "medium", "low"}
DOCUMENT_STATUSES = {"success", "needs_review", "skipped", "failed"}
PLACEHOLDER = "<!-- TODO: author this section -->"

EVIDENCE_COLUMNS = [
    "document_id",
    "source_path",
    "source_title",
    "item_type",
    "item_text",
    "evidence_quote",
    "locator",
    "interpretation",
    "confidence",
    "notes",
]
# Each methods-matrix column after the identifiers maps to exactly one item type
# so assembly stays deterministic.
METHODS_COLUMNS = [
    "document_id",
    "source_title",
    "methods",
    "data_sources",
    "populations",
    "variables",
    "technologies",
    "policies",
    "limitations",
    "research_gaps",
]
METHODS_TYPE_MAP = {
    "methods": "method",
    "data_sources": "data_source",
    "populations": "population",
    "variables": "variable",
    "technologies": "technology",
    "policies": "policy",
    "limitations": "limitation",
    "research_gaps": "research_gap",
}
DOCUMENTS_COLUMNS = [
    "document_id",
    "source_path",
    "source_sha256",
    "source_title",
    "status",
    "item_count",
    "note",
]

MARKDOWN_DELIVERABLES = {
    "literature_summary.md": [
        "# Literature Summary",
        "",
        PLACEHOLDER,
        "",
        "<!-- Author this from the extracted evidence. Keep generated synthesis",
        "separate from quoted source content. Do not overclaim beyond the documents. -->",
        "",
        "## Scope",
        "",
        "## Key Findings Across Sources",
        "",
        "## Points of Agreement and Disagreement",
        "",
        "## Assumptions and Limits",
        "",
        "## Open Questions",
        "",
    ],
    "claims_matrix.md": [
        "# Claims Matrix",
        "",
        PLACEHOLDER,
        "",
        "<!-- One row per claim. Cite the source document and locator. Mark whether",
        "each source states the claim explicitly, it is inferred, or it is unclear. -->",
        "",
        "| Claim | Source(s) | Locator | Interpretation | Supporting / Contradicting |",
        "|---|---|---|---|---|",
        "",
    ],
    "research_gaps.md": [
        "# Research Gaps",
        "",
        PLACEHOLDER,
        "",
        "<!-- List gaps the documents identify explicitly and gaps you infer.",
        "Label each as explicit or inferred. Do not invent gaps. -->",
        "",
        "## Explicitly Stated Gaps",
        "",
        "## Inferred Gaps",
        "",
    ],
    "citation_notes.md": [
        "# Citation Notes",
        "",
        PLACEHOLDER,
        "",
        "<!-- One entry per source document: full reference where available, plus a",
        "short annotation of scope, methods, and relevance. -->",
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


def openpyxl_available():
    return importlib.util.find_spec("openpyxl") is not None


def read_text(path):
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        fail(f"source is not valid UTF-8: {path}")


def title_from_metadata(metadata_path):
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    fields = metadata.get("fields") if isinstance(metadata, dict) else None
    title = fields.get("title") if isinstance(fields, dict) else None
    value = title.get("value") if isinstance(title, dict) else None
    return value if isinstance(value, str) and value.strip() else None


def discover_sources(root):
    if root.is_file():
        if root.suffix.lower() not in SOURCE_EXTENSIONS:
            fail(f"unsupported input format {root.suffix or '(none)'}; expected .md, .markdown, or .txt")
        return [root]
    found = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SOURCE_EXTENSIONS:
            continue
        relative = path.relative_to(root)
        if any(part.startswith(".") for part in relative.parts):
            continue
        # Reject any symlinked component between root and the file.
        current = root
        linked = False
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                linked = True
                break
        if linked:
            continue
        found.append(path)
    if not found:
        fail(f"no .md, .markdown, or .txt sources found under {root}")
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
        metadata_path = None
        title = None
        if source.name == "document.md":
            candidate = source.with_name("metadata.json")
            if candidate.is_file():
                metadata_path = str(candidate)
                title = title_from_metadata(candidate)
        documents.append(
            {
                "documentId": document_id,
                "sourcePath": str(source),
                "metadataPath": metadata_path,
                "sha256": digest,
                "sizeBytes": source.stat().st_size,
                "title": title,
            }
        )
    return documents


def write_documents_csv(run_directory, documents, dispositions):
    path = run_directory / "documents.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(DOCUMENTS_COLUMNS)
        for document in documents:
            disposition = dispositions.get(document["documentId"], {})
            status = disposition.get("status", "pending")
            items = disposition.get("items")
            item_count = len(items) if isinstance(items, list) else 0
            writer.writerow(
                [
                    document["documentId"],
                    document["sourcePath"],
                    document["sha256"],
                    document["title"] or "",
                    status,
                    item_count,
                    disposition.get("note") or "",
                ]
            )


def command_doctor(args):
    available = openpyxl_available()
    version = None
    if available:
        import openpyxl

        version = openpyxl.__version__
    result = {
        "python": sys.version.split()[0],
        "markdownText": True,
        "xlsx": available,
        "openpyxlVersion": version,
        "remediation": None if available else "Install openpyxl for the active Python 3 environment to export XLSX.",
        "note": "Convert PDF, DOCX, HTML, and RTF sources with document-ingest first; this skill consumes document.md, .md, and .txt.",
    }
    if args.json:
        print(json.dumps(result, indent=2))
        return
    print(f"Python: {result['python']}")
    print("Markdown/text sources: available")
    print(f"XLSX export: {'available via openpyxl ' + version if available else 'unavailable (CSV only)'}")
    if result["remediation"]:
        print(f"Action: {result['remediation']}")
    print(f"Note: {result['note']}")


def command_init(args):
    root = Path(args.input).expanduser().resolve()
    if not root.exists():
        fail(f"input does not exist: {root}")
    sources = discover_sources(root)
    documents = build_document_records(sources)
    output = require_new_directory(args.output)
    try:
        (output / "working").mkdir()
        run = {
            "schemaVersion": RUN_SCHEMA_VERSION,
            "createdAt": utc_now(),
            "input": {"path": str(root), "isDirectory": root.is_dir()},
            "itemTypes": ITEM_TYPES,
            "documents": documents,
        }
        (output / "run_config.json").write_text(json.dumps(run, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        (output / "extraction_results.jsonl").write_text("", encoding="utf-8")
        write_documents_csv(output, documents, {})
    except BaseException:
        raise
    print(json.dumps({"runDirectory": str(output), "documents": len(documents)}, ensure_ascii=False))


def load_run(run_directory):
    try:
        run = json.loads((run_directory / "run_config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail(f"could not read run_config.json: {error}")
    if run.get("schemaVersion") != RUN_SCHEMA_VERSION:
        fail(f"unsupported run schema version: {run.get('schemaVersion')}")
    return run


def load_results(run_directory, strict=True):
    path = run_directory / "extraction_results.jsonl"
    if not path.is_file():
        fail(f"extraction_results.jsonl is missing: {path}")
    results = []
    seen = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            result = json.loads(line)
        except json.JSONDecodeError as error:
            fail(f"invalid JSON on extraction_results.jsonl line {line_number}: {error}")
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
                "itemTypes": run["itemTypes"],
                "progress": {"processed": len(results), "total": total},
            },
            ensure_ascii=False,
        )
    )


def normalize_items(raw):
    if not isinstance(raw, list):
        fail("extraction file must contain a JSON array of item objects")
    items = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            fail(f"item {index} is not an object")
        item_type = item.get("item_type")
        if item_type not in ITEM_TYPE_SET:
            fail(f"item {index} has invalid item_type {item_type!r}; expected one of {', '.join(ITEM_TYPES)}")
        if blank(item.get("text")):
            fail(f"item {index} requires a nonblank text value")
        interpretation = item.get("interpretation")
        if interpretation not in INTERPRETATIONS:
            fail(f"item {index} has invalid interpretation {interpretation!r}; expected explicit, inferred, or unclear")
        confidence = item.get("confidence")
        if confidence not in CONFIDENCES:
            fail(f"item {index} has invalid confidence {confidence!r}; expected high, medium, or low")
        for optional in ("evidence_quote", "locator", "notes"):
            value = item.get(optional)
            if value is not None and not isinstance(value, str):
                fail(f"item {index} field {optional} must be a string or null")
        items.append(
            {
                "item_type": item_type,
                "text": item["text"],
                "evidence_quote": item.get("evidence_quote"),
                "locator": item.get("locator"),
                "interpretation": interpretation,
                "confidence": confidence,
                "notes": item.get("notes"),
            }
        )
    return items


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
        if not args.extraction_file:
            fail("successful results require --extraction-file")
        extraction_path = Path(args.extraction_file).expanduser().resolve()
        if not extraction_path.is_file():
            fail(f"extraction file does not exist: {extraction_path}")
        try:
            raw = json.loads(extraction_path.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            fail(f"extraction file is not valid UTF-8: {extraction_path}")
        except json.JSONDecodeError as error:
            fail(f"extraction file is not valid JSON: {error}")
        items = normalize_items(raw)
        note = args.note
    else:
        if args.extraction_file:
            fail("--extraction-file is only valid with --status success")
        if not args.note:
            fail(f"--status {args.status} requires --note")
        items = None
        note = args.note
    result = {
        "documentId": args.doc_id,
        "status": args.status,
        "items": items,
        "note": note,
        "recordedAt": utc_now(),
    }
    with (run_directory / "extraction_results.jsonl").open("a", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps(result, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    remaining = len(run["documents"]) - len(results) - 1
    item_count = len(items) if items is not None else 0
    print(json.dumps({"recorded": args.doc_id, "status": args.status, "items": item_count, "remaining": remaining}))


def verify_hashes(run):
    for document in run["documents"]:
        source = Path(document["sourcePath"])
        if not source.is_file():
            fail(f"source file is missing: {source}")
        if sha256(source) != document["sha256"]:
            fail(f"source file changed after init; refusing to proceed: {source}")


def write_evidence_table(run_directory, run, results_by_id):
    rows = []
    for document in run["documents"]:
        result = results_by_id.get(document["documentId"])
        if not result or result.get("status") != "success":
            continue
        for item in result.get("items") or []:
            rows.append(
                [
                    document["documentId"],
                    document["sourcePath"],
                    document["title"] or "",
                    item["item_type"],
                    item["text"],
                    item.get("evidence_quote") or "",
                    item.get("locator") or "",
                    item["interpretation"],
                    item["confidence"],
                    item.get("notes") or "",
                ]
            )
    csv_path = run_directory / "evidence_table.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(EVIDENCE_COLUMNS)
        writer.writerows(rows)
    xlsx_written = False
    if openpyxl_available():
        import openpyxl

        workbook = openpyxl.Workbook()
        worksheet = workbook.active
        worksheet.title = "evidence"
        worksheet.append(EVIDENCE_COLUMNS)
        for row in rows:
            worksheet.append(row)
        workbook.save(run_directory / "evidence_table.xlsx")
        xlsx_written = True
    return len(rows), xlsx_written


def write_methods_matrix(run_directory, run, results_by_id):
    rows = []
    for document in run["documents"]:
        result = results_by_id.get(document["documentId"])
        if not result or result.get("status") != "success":
            continue
        grouped = defaultdict(list)
        for item in result.get("items") or []:
            grouped[item["item_type"]].append(item["text"])
        row = [document["documentId"], document["title"] or ""]
        for column in METHODS_COLUMNS[2:]:
            texts = grouped.get(METHODS_TYPE_MAP[column], [])
            row.append("; ".join(texts))
        rows.append(row)
    with (run_directory / "methods_matrix.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(METHODS_COLUMNS)
        writer.writerows(rows)
    return len(rows)


def scaffold_markdown(run_directory):
    created = []
    for name, lines in MARKDOWN_DELIVERABLES.items():
        path = run_directory / name
        if path.exists():
            continue
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        created.append(name)
    return created


def command_build(args):
    run_directory = require_run_directory(args.run_directory)
    run = load_run(run_directory)
    results = load_results(run_directory)
    pending = next_pending(run, results)
    if pending is not None:
        fail(f"run is incomplete; next pending document is {pending}")
    verify_hashes(run)
    results_by_id = {result["documentId"]: result for result in results}
    evidence_rows, xlsx_written = write_evidence_table(run_directory, run, results_by_id)
    method_rows = write_methods_matrix(run_directory, run, results_by_id)
    created = scaffold_markdown(run_directory)
    write_documents_csv(run_directory, run["documents"], results_by_id)
    counts = Counter(result["status"] for result in results)
    print(
        json.dumps(
            {
                "evidenceItems": evidence_rows,
                "methodsRows": method_rows,
                "xlsx": xlsx_written,
                "scaffolded": created,
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
            if not isinstance(result.get("items"), list):
                errors.append(f"successful document {document_id} has no items list")
            else:
                try:
                    normalize_items(result["items"])
                except SystemExit:
                    errors.append(f"document {document_id} has invalid extraction items")
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
    for document in run["documents"]:
        source = Path(document["sourcePath"])
        if not source.is_file():
            errors.append(f"source file is missing: {source}")
        elif sha256(source) != document["sha256"]:
            errors.append(f"source file hash differs from init: {source}")
    if not missing:
        for required in ("evidence_table.csv", "methods_matrix.csv"):
            if not (run_directory / required).is_file():
                errors.append(f"built artifact is missing: {required}; run build")
        for required in MARKDOWN_DELIVERABLES:
            path = run_directory / required
            if not path.is_file():
                errors.append(f"deliverable is missing: {required}; run build")
            elif PLACEHOLDER in path.read_text(encoding="utf-8"):
                errors.append(f"deliverable still has an unresolved placeholder: {required}")
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
    root = argparse.ArgumentParser(description="Extract structured evidence from research documents and manage resumable per-document extraction.")
    subparsers = root.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Report local extraction capabilities.")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(handler=command_doctor)

    init = subparsers.add_parser("init", help="Discover sources and scaffold a resumable extraction run.")
    init.add_argument("input")
    init.add_argument("--output", required=True)
    init.set_defaults(handler=command_init)

    next_command = subparsers.add_parser("next", help="Return exactly one pending document as JSON.")
    next_command.add_argument("run_directory")
    next_command.set_defaults(handler=command_next)

    record = subparsers.add_parser("record", help="Append one document's extraction or an explicit disposition.")
    record.add_argument("run_directory")
    record.add_argument("--doc-id", required=True)
    record.add_argument("--status", choices=sorted(DOCUMENT_STATUSES), default="success")
    record.add_argument("--extraction-file")
    record.add_argument("--note")
    record.set_defaults(handler=command_record)

    build = subparsers.add_parser("build", help="Assemble evidence and methods tables and scaffold Markdown deliverables.")
    build.add_argument("run_directory")
    build.set_defaults(handler=command_build)

    validate = subparsers.add_parser("validate", help="Validate run state, provenance, schema, and built artifacts.")
    validate.add_argument("run_directory")
    validate.set_defaults(handler=command_validate)
    return root


def main():
    args = parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Shared forge embeddings client lives at forge/lib; this script is at
# forge/skills/literature-extraction/scripts/literature-extraction.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
import forge_embeddings
import run_state


RUN_SCHEMA_VERSION = 1
META_SCHEMA_VERSION = 1
MAX_ALLOWED_META_CONTEXT = 256000
DEFAULT_META_TARGET_CONTEXT = 128000
DEFAULT_META_MAX_CONTEXT = 256000
DEFAULT_SYNTHESIS_TARGET_CONTEXT = 128000
RUN_STATE_WORKFLOW = "literature-extraction"
META_RUN_STATE_WORKFLOW = "literature-extraction-meta"
DEFAULT_META_BRIDGE_THRESHOLD = 0.78
META_CLUSTER_ITEM_TYPES = ("claim", "finding", "definition", "connection")
META_BRIDGE_ITEM_TYPES = ("claim", "finding", "definition", "connection", "quoted_evidence")
META_ITEM_TEXT_CHARS = 2000

# Cross-document claim clustering groups semantically similar claims and findings
# across sources so the model can author agreement/disagreement synthesis with
# better recall. The worksheet is advisory: it never reconciles, merges, or
# decides contradictions; the model judges each group against the evidence.
CLAIM_CLUSTER_ITEM_TYPES = ("claim", "finding")
DEFAULT_CLAIM_CLUSTER_THRESHOLD = 0.80
# Cap embedding input per claim so one oversized item cannot exceed the endpoint
# payload or token limits and fail the whole batch. The full text is still kept
# for the worksheet; only the embedded sample is truncated.
CLAIM_CLUSTER_TEXT_CHARS = 2000

# Crude lexical negation cues. Used only to hint at possible polarity differences
# within a cluster for the model to examine; never treated as a contradiction
# determination.
NEGATION_CUES = (
    "not",
    "no",
    "never",
    "without",
    "cannot",
    "n't",
    "fails to",
    "failed to",
    "lack",
    "absence of",
    "insignificant",
    "no significant",
    "no effect",
    "did not",
    "does not",
)
SOURCE_EXTENSIONS = {".md", ".markdown", ".txt"}
RESERVED_WORKSPACE_DIRECTORIES = {"Ingest", "Originals", "Generated"}
ITEM_TYPES = [
    "claim",
    "connection",
    "method",
    "data_source",
    "finding",
    "limitation",
    "definition",
    "author",
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
    "direct_quotes",
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
SOURCE_PROFILE_COLUMNS = [
    "document_id",
    "source_path",
    "source_title",
    "status",
    "item_count",
    "claim_count",
    "finding_count",
    "definition_count",
    "connection_count",
    "method_count",
    "limitation_count",
    "research_gap_count",
]
META_SOURCE_COLUMNS = [
    "meta_source_id",
    "corpus_label",
    "run_directory",
    "source_run_id",
    "document_count",
    "item_count",
    "authored_deliverables",
    "warnings",
]
META_BRIDGE_COLUMNS = [
    "bridge_id",
    "similarity",
    "left_item_id",
    "left_corpus_label",
    "left_document_title",
    "left_item_type",
    "right_item_id",
    "right_corpus_label",
    "right_document_title",
    "right_item_type",
    "review_note",
]
META_TOPIC_COLUMNS = [
    "cluster_id",
    "cluster_size",
    "corpus_count",
    "is_representative",
    "similarity_to_representative",
    "item_id",
    "corpus_label",
    "source_title",
    "item_type",
    "item_text",
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
        "each source states the claim explicitly, it is inferred, or it is unclear.",
        "When build produced claim_clusters.md, use it to group related claims across",
        "sources and to examine flagged possible contradictions; judge each yourself. -->",
        "",
        "| Claim | Source(s) | Locator | Interpretation | Supporting / Contradicting |",
        "|---|---|---|---|---|",
        "",
    ],
    "key_terms.md": [
        "# Key Terms",
        "",
        PLACEHOLDER,
        "",
        "<!-- One row per definition or important term. Include source document,",
        "locator, interpretation, and direct quote support when available. -->",
        "",
        "| Term | Definition | Source(s) | Locator | Direct quote(s) |",
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

META_MARKDOWN_DELIVERABLES = {
    "meta_synthesis.md": [
        "# Meta Literature Synthesis",
        "",
        PLACEHOLDER,
        "",
        "<!-- Author from packet memos, bridge_candidates.csv, topic_clusters.csv, and meta_items.jsonl.",
        "Every substantive claim needs item ids, source titles, corpus labels, and locators when available.",
        "Keep primary evidence, secondary interpretation, and generated synthesis distinct. -->",
        "",
        "## Research Question",
        "",
        "## Cross-Corpus Findings",
        "",
        "## Agreements, Tensions, and Silences",
        "",
        "## Interpretive Limits",
        "",
    ],
    "primary_secondary_matrix.md": [
        "# Primary / Secondary Matrix",
        "",
        PLACEHOLDER,
        "",
        "<!-- Link primary-source evidence to secondary-source concepts. Cite item ids and locators.",
        "If corpus labels are not literally primary/secondary, use the labels recorded in meta_sources.csv. -->",
        "",
        "| Primary evidence | Secondary concept | Relationship | Item ids | Locators |",
        "|---|---|---|---|---|",
        "",
    ],
    "concept_register.md": [
        "# Concept Register",
        "",
        PLACEHOLDER,
        "",
        "<!-- Separate source-native terms from analyst/theory terms. Cite item ids and corpus labels. -->",
        "",
        "| Concept | Emic / etic / unclear | Definition or use | Source role | Item ids |",
        "|---|---|---|---|---|",
        "",
    ],
    "negative_cases.md": [
        "# Negative Cases",
        "",
        PLACEHOLDER,
        "",
        "<!-- Surface contradictions, absences, failed fits, and cases that complicate the main pattern.",
        "Do not smooth disagreements into consensus. Cite item ids. -->",
        "",
    ],
    "methods_and_limits.md": [
        "# Methods and Limits",
        "",
        PLACEHOLDER,
        "",
        "<!-- Describe corpus coverage, extraction limits, missing sources, OCR/locator limits inherited from",
        "document-ingest, and how packeted synthesis shaped the result. Cite run and item ids where useful. -->",
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


def atomic_csv(path, columns, rows):
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows(rows)
    run_state.atomic_write_text(path, output.getvalue())


def init_configuration(args, root):
    return {
        "workflow": RUN_STATE_WORKFLOW,
        "command": "init",
        "input": {"path": str(root)},
        "options": {
            "schema": args.schema,
            "itemTypes": args.item_types,
            "customInstructions": args.custom_instructions or "",
            "includeReserved": args.include_reserved,
        },
    }


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


def discover_sources(root, include_reserved=False, allow_empty=False):
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
        if not include_reserved and any(part in RESERVED_WORKSPACE_DIRECTORIES for part in relative.parts):
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
    if not found and not allow_empty:
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
    rows = []
    for document in documents:
        disposition = dispositions.get(document["documentId"], {})
        status = disposition.get("status", "pending")
        items = disposition.get("items")
        item_count = len(items) if isinstance(items, list) else 0
        rows.append(
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
    atomic_csv(run_directory / "documents.csv", DOCUMENTS_COLUMNS, rows)


def command_doctor(args):
    embeddings = forge_embeddings.embeddings_doctor()
    result = {
        "python": sys.version.split()[0],
        "markdownText": True,
        "embeddings": embeddings,
        "evidenceTable": "csv",
        "note": "Convert PDF, DOCX, HTML, and RTF sources with document-ingest first; this skill consumes document.md, .md, and .txt.",
    }
    if args.json:
        print(json.dumps(result, indent=2))
        return
    print(f"Python: {result['python']}")
    print("Markdown/text sources: available")
    print("Evidence table export: CSV")
    reach = "reachable" if embeddings["reachable"] else "unreachable"
    print(f"Embeddings ({embeddings['url']}): {reach} - {embeddings['detail']}")
    print("  Used by build for advisory cross-document claim clustering; build degrades cleanly when unreachable.")
    print(f"Note: {result['note']}")


def command_init(args):
    root = Path(args.input).expanduser().resolve()
    if not root.exists():
        fail(f"input does not exist: {root}")
    configuration = init_configuration(args, root)
    requested_output = Path(args.output).expanduser().resolve()
    if requested_output.exists():
        try:
            state = run_state.load_run_state(requested_output, RUN_STATE_WORKFLOW)
            run_state.assert_compatible_run(state, configuration)
            run = load_run(requested_output)
            results = load_results(requested_output)
            drift = literature_drift(run)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            fail(str(error))
        print(
            json.dumps(
                {
                    "runDirectory": str(requested_output),
                    "resumed": True,
                    "documents": len(run["documents"]),
                    "processed": len(results),
                    "phase": state["phase"],
                    "nextAction": state.get("nextAction"),
                    "complete": state["status"] == "complete",
                    "inputDrift": drift,
                    "refreshRequired": any(drift.values()),
                },
                ensure_ascii=False,
            )
        )
        return
    sources = discover_sources(root, include_reserved=args.include_reserved)
    documents = build_document_records(sources)
    output = require_new_directory(requested_output)

    item_types = ITEM_TYPES
    custom_instructions = args.custom_instructions or ""
    if args.item_types:
        item_types = [value.strip() for value in args.item_types.split(",") if value.strip()]
        if not item_types:
            fail("--item-types requires at least one comma-separated category")
    elif args.schema == "products":
        item_types = ["product", "service", "price", "specification", "target_audience", "competitor", "limitation"]
    elif args.schema == "custom":
        fail("--schema custom requires --item-types")
    elif sys.stdin.isatty():
        print("Select extraction schema:")
        print("1. Academic Literature (claims, methods, findings, etc.)")
        print("2. Products and Services (products, prices, specs, etc.)")
        print("3. Custom (Provide your own categories and instructions)")
        choice = input("> ").strip()
        if choice == "2":
            item_types = ["product", "service", "price", "specification", "target_audience", "competitor", "limitation"]
        elif choice == "3":
            cats = input("Enter comma-separated categories: ").strip()
            item_types = [c.strip() for c in cats.split(",") if c.strip()]
            if not item_types:
                fail("Custom schema requires at least one category.")
            custom_instructions = input("Enter custom instruction for the model (optional): ").strip()

    try:
        (output / "working").mkdir()
        run = {
            "schemaVersion": RUN_SCHEMA_VERSION,
            "createdAt": utc_now(),
            "input": {"path": str(root), "isDirectory": root.is_dir(), "includeReserved": args.include_reserved},
            "itemTypes": item_types,
            "customInstructions": custom_instructions,
            "documents": documents,
        }
        run_state.atomic_write_json(output / "run_config.json", run)
        run_state.atomic_write_text(output / "extraction_results.jsonl", "")
        write_documents_csv(output, documents, {})
        state = run_state.create_run_state(
            RUN_STATE_WORKFLOW,
            "init",
            configuration["input"],
            configuration["options"],
            items=[
                {
                    "id": document["documentId"],
                    "path": document["sourcePath"],
                    "sha256": document["sha256"],
                    "status": "pending",
                    "attempts": 0,
                    "transient": False,
                }
                for document in documents
            ],
            phase="extracting",
            next_action="next",
        )
        run_state.initialize_run_state(output, state)
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
    try:
        results, warnings = run_state.read_jsonl_recover_tail(path, repair=True)
    except ValueError as error:
        fail(str(error))
    if warnings:
        try:
            run_state.update_run_state(
                run_directory,
                lambda state: {**state, "warnings": sorted(set(state.get("warnings", []) + warnings))},
                {"type": "jsonl_tail_recovered", "file": path.name},
            )
        except ValueError:
            pass
    seen = set()
    for result in results:
        document_id = result.get("documentId")
        if strict and document_id in seen:
            fail(f"duplicate result for document {document_id}")
        seen.add(document_id)
    return results


def document_order(run):
    return [document["documentId"] for document in run["documents"]]


def next_pending(run, results):
    recorded = {result.get("documentId") for result in results}
    for document_id in document_order(run):
        if document_id not in recorded:
            return document_id
    return None


def write_results(run_directory, results):
    text = "".join(json.dumps(result, ensure_ascii=False) + "\n" for result in results)
    run_state.atomic_write_text(run_directory / "extraction_results.jsonl", text)


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
                "itemTypes": run.get("itemTypes", ITEM_TYPES),
                "customInstructions": run.get("customInstructions", ""),
                "progress": {"processed": len(results), "total": total},
            },
            ensure_ascii=False,
        )
    )


def normalize_items(raw, item_types):
    if not isinstance(raw, list):
        fail("extraction file must contain a JSON array of item objects")
    items = []
    item_type_set = set(item_types)
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            fail(f"item {index} is not an object")
        item_type = item.get("item_type")
        if item_type not in item_type_set:
            fail(f"item {index} has invalid item_type {item_type!r}; expected one of {', '.join(item_types)}")
        if blank(item.get("text")):
            fail(f"item {index} requires a nonblank text value")
        interpretation = item.get("interpretation")
        if interpretation not in INTERPRETATIONS:
            fail(f"item {index} has invalid interpretation {interpretation!r}; expected explicit, inferred, or unclear")
        confidence = item.get("confidence")
        if confidence not in CONFIDENCES:
            fail(f"item {index} has invalid confidence {confidence!r}; expected high, medium, or low")
        for optional in ("direct_quotes", "locator", "notes"):
            value = item.get(optional)
            if value is not None and not isinstance(value, str):
                fail(f"item {index} field {optional} must be a string or null")
        items.append(
            {
                "item_type": item_type,
                "text": item["text"],
                "direct_quotes": item.get("direct_quotes"),
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
        items = normalize_items(raw, run.get("itemTypes", ITEM_TYPES))
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
    run_state.append_jsonl_fsync(run_directory / "extraction_results.jsonl", result)
    effective = [*results, result]
    by_id = {row["documentId"]: row for row in effective}
    effective = [by_id[document_id] for document_id in document_order(run) if document_id in by_id]
    write_results(run_directory, effective)
    write_documents_csv(run_directory, run["documents"], by_id)
    remaining = len(run["documents"]) - len(effective)
    run_state.update_run_state(
        run_directory,
        lambda state: _record_extraction_state(state, args.doc_id, args.status, remaining),
        {"type": "item_completed", "itemId": args.doc_id, "phase": "extract", "status": args.status},
    )
    item_count = len(items) if items is not None else 0
    print(json.dumps({"recorded": args.doc_id, "status": args.status, "items": item_count, "remaining": remaining}))


def _record_extraction_state(state, document_id, status, remaining):
    for item in state.get("items", []):
        if item.get("id") == document_id:
            item["status"] = status
            item["attempts"] = item.get("attempts", 0) + 1
            item["error"] = None
            break
    state["phase"] = "extracting" if remaining else "building"
    state["nextAction"] = "next" if remaining else "build"
    return state


def verify_hashes(run):
    for document in run["documents"]:
        if document.get("active", True) is False:
            continue
        source = Path(document["sourcePath"])
        if not source.is_file():
            fail(f"source file is missing: {source}")
        if sha256(source) != document["sha256"]:
            fail(f"source file changed after init; refusing to proceed: {source}")


def current_documents(run):
    root = Path(run["input"]["path"])
    if not root.exists():
        return []
    return build_document_records(discover_sources(root, include_reserved=run["input"].get("includeReserved", False), allow_empty=True))


def literature_drift(run):
    snapshot = [
        {"path": document["sourcePath"], "sha256": document["sha256"]}
        for document in run["documents"]
        if document.get("active", True)
    ]
    current = [{"path": document["sourcePath"], "sha256": document["sha256"]} for document in current_documents(run)]
    return run_state.input_drift(snapshot, current)


def command_status(args):
    run_directory = require_run_directory(args.run_directory)
    state = run_state.load_run_state(run_directory, RUN_STATE_WORKFLOW)
    run = load_run(run_directory)
    results = load_results(run_directory)
    counts = Counter(result["status"] for result in results)
    drift = literature_drift(run)
    next_id = next_pending(run, results)
    print(
        json.dumps(
            {
                "runDirectory": str(run_directory),
                "status": state["status"],
                "phase": state["phase"],
                "nextAction": state.get("nextAction"),
                "documents": len(run["documents"]),
                "processed": len(results),
                "nextDocumentId": next_id,
                "counts": {status: counts[status] for status in sorted(DOCUMENT_STATUSES)},
                "inputDrift": drift,
                "refreshRequired": any(drift.values()),
            },
            indent=2,
        )
    )


def command_refresh(args):
    run_directory = require_run_directory(args.run_directory)
    run = load_run(run_directory)
    results = load_results(run_directory)
    recorded_ids = {result["documentId"] for result in results}
    current = current_documents(run)
    current_by_identity = {(document["sourcePath"], document["sha256"]): document for document in current}
    kept_identities = set()
    documents = []
    for document in run["documents"]:
        identity = (document["sourcePath"], document["sha256"])
        if identity in current_by_identity:
            refreshed = {**document, "active": True}
            documents.append(refreshed)
            kept_identities.add(identity)
        elif document["documentId"] in recorded_ids:
            documents.append({**document, "active": False})
    documents.extend(document for identity, document in current_by_identity.items() if identity not in kept_identities)
    by_id = {document["documentId"]: document for document in documents}
    ordered = [by_id[result["documentId"]] for result in results if result["documentId"] in by_id]
    ordered.extend(document for document in documents if document["documentId"] not in recorded_ids)
    drift = literature_drift(run)
    if not any(drift.values()):
        print(json.dumps({"refreshed": False, "runDirectory": str(run_directory), "inputDrift": drift}, indent=2))
        return
    run["documents"] = ordered
    run_state.atomic_write_json(run_directory / "run_config.json", run)
    result_map = {result["documentId"]: result for result in results}
    write_documents_csv(run_directory, ordered, result_map)
    run_state.update_run_state(
        run_directory,
        lambda state: _refresh_extraction_state(state, ordered, result_map),
        {"type": "input_refreshed", "added": len(drift["added"]), "changed": len(drift["changed"]), "removed": len(drift["removed"])},
    )
    print(json.dumps({"refreshed": True, "runDirectory": str(run_directory), "inputDrift": drift, "documents": len(ordered)}, indent=2))


def _refresh_extraction_state(state, documents, results):
    state["items"] = [
        {
            "id": document["documentId"],
            "path": document["sourcePath"],
            "sha256": document["sha256"],
            "status": results.get(document["documentId"], {}).get("status", "pending"),
            "attempts": 1 if document["documentId"] in results else 0,
            "transient": False,
            "active": document.get("active", True),
        }
        for document in documents
    ]
    state["status"] = "running"
    state["phase"] = "extracting"
    state["nextAction"] = "next"
    state.pop("synthesis", None)
    state.pop("deliverables", None)
    return state


def command_retry(args):
    run_directory = require_run_directory(args.run_directory)
    run = load_run(run_directory)
    results = load_results(run_directory)
    targets = {
        result["documentId"]
        for result in results
        if result["status"] == "failed" and (args.all_failed or result["documentId"] == args.item)
    }
    if not targets:
        fail(f"failed item not found: {args.item}" if args.item else "run has no failed items")
    remaining = [result for result in results if result["documentId"] not in targets]
    write_results(run_directory, remaining)
    result_map = {result["documentId"]: result for result in remaining}
    write_documents_csv(run_directory, run["documents"], result_map)
    run_state.update_run_state(
        run_directory,
        lambda state: _retry_extraction_state(state, targets),
        {"type": "items_retried", "itemIds": sorted(targets)},
    )
    print(json.dumps({"runDirectory": str(run_directory), "retried": len(targets), "nextAction": "next"}))


def _retry_extraction_state(state, targets):
    for item in state.get("items", []):
        if item.get("id") in targets:
            item["status"] = "pending"
            item["attempts"] = 0
            item["error"] = None
    state["status"] = "running"
    state["phase"] = "extracting"
    state["nextAction"] = "next"
    return state


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
                    item.get("direct_quotes") or "",
                    item.get("locator") or "",
                    item["interpretation"],
                    item["confidence"],
                    item.get("notes") or "",
                ]
            )
    csv_path = run_directory / "evidence_table.csv"
    atomic_csv(csv_path, EVIDENCE_COLUMNS, rows)
    xlsx_path = run_directory / "evidence_table.xlsx"
    if xlsx_path.exists():
        xlsx_path.unlink()
    return len(rows)


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
    atomic_csv(run_directory / "methods_matrix.csv", METHODS_COLUMNS, rows)
    return len(rows)


def normalized_item_id(prefix, index):
    return f"{prefix}{index:06d}"


def estimate_tokens(text):
    return (len(text) + 3) // 4


def write_item_index(run_directory, run, results_by_id):
    count = 0
    lines = []
    for document in run["documents"]:
        result = results_by_id.get(document["documentId"])
        if not result or result.get("status") != "success":
            continue
        for item in result.get("items") or []:
            count += 1
            row = {
                "itemId": normalized_item_id("i", count),
                "documentId": document["documentId"],
                "sourcePath": document["sourcePath"],
                "sourceSha256": document["sha256"],
                "sourceTitle": document["title"] or "",
                "itemType": item["item_type"],
                "itemText": item["text"],
                "directQuotes": item.get("direct_quotes"),
                "locator": item.get("locator"),
                "interpretation": item["interpretation"],
                "confidence": item["confidence"],
                "notes": item.get("notes"),
            }
            lines.append(json.dumps(row, ensure_ascii=False))
    run_state.atomic_write_text(run_directory / "item_index.jsonl", "" if not lines else "\n".join(lines) + "\n")
    return count


def write_source_profile(run_directory, run, results_by_id):
    rows = []
    for document in run["documents"]:
        result = results_by_id.get(document["documentId"])
        status = result.get("status", "pending") if result else "pending"
        items = result.get("items") if result and result.get("status") == "success" else []
        counts = Counter(item["item_type"] for item in items or [])
        rows.append(
            [
                document["documentId"],
                document["sourcePath"],
                document["title"] or "",
                status,
                len(items or []),
                counts["claim"],
                counts["finding"],
                counts["definition"],
                counts["connection"],
                counts["method"],
                counts["limitation"],
                counts["research_gap"],
            ]
        )
    atomic_csv(run_directory / "source_profile.csv", SOURCE_PROFILE_COLUMNS, rows)
    return len(rows)


def scaffold_markdown(run_directory):
    created = []
    for name, lines in MARKDOWN_DELIVERABLES.items():
        path = run_directory / name
        if path.exists():
            continue
        run_state.atomic_write_text(path, "\n".join(lines) + "\n")
        created.append(name)
    return created


def packetize_blocks(blocks, target_context):
    packets = []
    current = []
    current_tokens = 0
    for block in blocks:
        tokens = estimate_tokens(block)
        if current and current_tokens + tokens > target_context:
            packets.append("\n".join(current))
            current = []
            current_tokens = 0
        current.append(block)
        current_tokens += tokens
    if current:
        packets.append("\n".join(current))
    return packets or ["No extracted evidence items were recorded for this run."]


def create_synthesis_level(run_directory, level, blocks, target_context):
    packet_directory = run_directory / "synthesis_packets" / f"level-{level}"
    packet_directory.mkdir(parents=True, exist_ok=True)
    packets = []
    for index, text in enumerate(packetize_blocks(blocks, target_context), start=1):
        packet_id = f"l{level}-p{index:04d}"
        path = packet_directory / f"{packet_id}.md"
        run_state.atomic_write_text(path, text.rstrip() + "\n")
        packets.append(
            {
                "packetId": packet_id,
                "path": str(path),
                "estimatedTokens": estimate_tokens(text),
                "status": "pending",
                "memoPath": None,
            }
        )
    return {"level": level, "packets": packets}


def initialize_synthesis_state(run_directory, target_context=DEFAULT_SYNTHESIS_TARGET_CONTEXT):
    blocks = (run_directory / "item_index.jsonl").read_text(encoding="utf-8").splitlines()
    first_level = create_synthesis_level(run_directory, 1, blocks, target_context)
    return {"targetContext": target_context, "complete": False, "levels": [first_level]}


def command_synthesis_next(args):
    run_directory = require_run_directory(args.run_directory)
    state = run_state.load_run_state(run_directory, RUN_STATE_WORKFLOW)
    synthesis = state.get("synthesis")
    if not synthesis:
        fail("synthesis packets are not initialized; run build")
    while True:
        level = synthesis["levels"][-1]
        pending = next((packet for packet in level["packets"] if packet["status"] != "complete"), None)
        if pending:
            print(
                json.dumps(
                    {
                        "complete": False,
                        "level": level["level"],
                        **pending,
                        "targetContext": synthesis["targetContext"],
                        "recordCommand": f"literature-extraction.py synthesis-record {run_directory} --packet-id {pending['packetId']} --memo-file <memo.md>",
                    }
                )
            )
            return
        memo_blocks = [Path(packet["memoPath"]).read_text(encoding="utf-8") for packet in level["packets"]]
        if estimate_tokens("\n".join(memo_blocks)) <= synthesis["targetContext"]:
            synthesis["complete"] = True
            run_state.update_run_state(
                run_directory,
                lambda draft: _set_synthesis_state(draft, synthesis, "next-output"),
                {"type": "synthesis_packets_completed", "levels": len(synthesis["levels"])},
            )
            print(json.dumps({"complete": True, "levels": len(synthesis["levels"]), "nextAction": "next-output"}))
            return
        next_level = create_synthesis_level(run_directory, level["level"] + 1, memo_blocks, synthesis["targetContext"])
        synthesis["levels"].append(next_level)
        state = run_state.update_run_state(
            run_directory,
            lambda draft: _set_synthesis_state(draft, synthesis, "synthesis-next"),
            {"type": "synthesis_level_created", "level": next_level["level"], "packets": len(next_level["packets"])},
        )
        synthesis = state["synthesis"]


def _set_synthesis_state(state, synthesis, next_action):
    state["synthesis"] = synthesis
    state["phase"] = "synthesis"
    state["nextAction"] = next_action
    return state


def command_synthesis_record(args):
    run_directory = require_run_directory(args.run_directory)
    state = run_state.load_run_state(run_directory, RUN_STATE_WORKFLOW)
    synthesis = state.get("synthesis")
    if not synthesis or synthesis.get("complete"):
        fail("no synthesis packet is pending")
    level = synthesis["levels"][-1]
    expected = next((packet for packet in level["packets"] if packet["status"] != "complete"), None)
    if not expected or expected["packetId"] != args.packet_id:
        fail(f"expected synthesis packet {expected['packetId'] if expected else '(none)'}, received {args.packet_id}")
    memo_path = Path(args.memo_file).expanduser().resolve()
    if not memo_path.is_file():
        fail(f"memo file does not exist: {memo_path}")
    memo = read_text(memo_path).strip()
    if not memo:
        fail("memo file cannot be blank")
    if estimate_tokens(memo) > synthesis["targetContext"]:
        fail("memo exceeds the configured synthesis context target")
    destination = run_directory / "synthesis_memos" / f"level-{level['level']}" / f"{args.packet_id}.md"
    run_state.atomic_write_text(destination, memo + "\n")
    expected["status"] = "complete"
    expected["memoPath"] = str(destination)
    run_state.update_run_state(
        run_directory,
        lambda draft: _set_synthesis_state(draft, synthesis, "synthesis-next"),
        {"type": "synthesis_packet_completed", "packetId": args.packet_id, "level": level["level"]},
    )
    print(json.dumps({"recorded": args.packet_id, "nextAction": "synthesis-next"}))


def command_next_output(args):
    run_directory = Path(args.run_directory).expanduser().resolve()
    state = run_state.load_run_state(run_directory)
    if state.get("synthesis") and not state["synthesis"].get("complete"):
        fail("synthesis packets remain; run synthesis-next")
    deliverables = state.get("deliverables", [])
    pending = next((item for item in deliverables if item["status"] != "complete"), None)
    if not pending:
        print(json.dumps({"complete": True, "runDirectory": str(run_directory), "nextAction": "validate"}))
        return
    print(
        json.dumps(
            {
                "complete": False,
                "name": pending["name"],
                "targetPath": pending["path"],
                "workingDirectory": str(run_directory / "working"),
                "recordCommand": f"literature-extraction.py record-output {run_directory} --name {pending['name']} --file <authored.md>",
            }
        )
    )


def command_record_output(args):
    run_directory = Path(args.run_directory).expanduser().resolve()
    state = run_state.load_run_state(run_directory)
    deliverables = state.get("deliverables", [])
    expected = next((item for item in deliverables if item["status"] != "complete"), None)
    if not expected or expected["name"] != args.name:
        fail(f"expected deliverable {expected['name'] if expected else '(none)'}, received {args.name}")
    source = Path(args.file).expanduser().resolve()
    if not source.is_file():
        fail(f"authored file does not exist: {source}")
    text = read_text(source)
    if not text.strip() or PLACEHOLDER in text:
        fail("authored deliverable is blank or still contains the placeholder")
    run_state.atomic_write_text(expected["path"], text.rstrip() + "\n")
    expected["status"] = "complete"
    remaining = any(item["status"] != "complete" for item in deliverables)
    run_state.update_run_state(
        run_directory,
        lambda draft: _set_deliverable_state(draft, deliverables, remaining),
        {"type": "deliverable_completed", "name": args.name},
    )
    print(json.dumps({"recorded": args.name, "remaining": sum(item["status"] != "complete" for item in deliverables)}))


def _set_deliverable_state(state, deliverables, remaining):
    state["deliverables"] = deliverables
    state["phase"] = "authoring" if remaining else "validation"
    state["nextAction"] = "next-output" if remaining else "validate"
    return state


def detect_negation(text):
    lowered = text.lower()
    for cue in NEGATION_CUES:
        if re.search(r"(?<![a-z])" + re.escape(cue) + r"(?![a-z])", lowered):
            return True
    return False


def collect_claim_items(run, results_by_id):
    items = []
    for document in run["documents"]:
        result = results_by_id.get(document["documentId"])
        if not result or result.get("status") != "success":
            continue
        for item in result.get("items") or []:
            if item["item_type"] not in CLAIM_CLUSTER_ITEM_TYPES:
                continue
            items.append(
                {
                    "document_id": document["documentId"],
                    "source_title": document["title"] or "",
                    "item_type": item["item_type"],
                    "interpretation": item["interpretation"],
                    "confidence": item["confidence"],
                    "item_text": item["text"],
                    "locator": item.get("locator") or "",
                    "negation_hint": "true" if detect_negation(item["text"]) else "false",
                }
            )
    return items


def write_claim_clusters_csv(path, members):
    fields = [
        "cluster_id",
        "cluster_size",
        "document_count",
        "is_representative",
        "similarity_to_representative",
        "document_id",
        "source_title",
        "item_type",
        "interpretation",
        "confidence",
        "negation_hint",
        "locator",
        "item_text",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for member in members:
            writer.writerow(member)


def claim_clusters_markdown(members, cross_document_ids, possible_contradiction_ids, threshold, model):
    by_cluster = {}
    for member in members:
        by_cluster.setdefault(member["cluster_id"], []).append(member)
    lines = [
        "# Claim Clusters",
        "",
        f"- Similarity threshold: {threshold}",
        f"- Model: `{model}`",
        f"- Clustered items (claims and findings): {len(members)}",
        f"- Cross-document groups: {len(cross_document_ids)}",
        f"- Groups flagged for possible contradiction: {len(possible_contradiction_ids)}",
        "",
        "This is an advisory worksheet, not a deliverable. Each group below collects "
        "claims and findings that are similar in meaning across sources, to help you "
        "author the claims matrix and summary with better recall. Groups flagged for "
        "possible contradiction contain a crude lexical negation difference among "
        "members; treat that only as a prompt to read the evidence and judge for "
        "yourself. Never reconcile or merge claims automatically; record genuine "
        "agreement and disagreement from the sources.",
        "",
    ]
    if not cross_document_ids:
        lines.append("No cross-document groups at this threshold. Claims did not cluster across sources.")
        lines.append("")
        return "\n".join(lines)
    for cluster_id in cross_document_ids:
        group = by_cluster[cluster_id]
        flag = " - possible contradiction" if cluster_id in possible_contradiction_ids else ""
        documents = len({member["document_id"] for member in group})
        lines.append(f"## Group {cluster_id} ({len(group)} items, {documents} documents{flag})")
        lines.append("")
        lines.extend(
            [
                "| Document | Type | Interpretation | Negation hint | Locator | Text |",
                "|---|---|---|---|---|---|",
            ]
        )
        for member in group:
            text_cell = member["item_text"].replace("|", "\\|").replace("\n", " ")
            if len(text_cell) > 100:
                text_cell = text_cell[:97] + "..."
            title = member["source_title"] or member["document_id"]
            title_cell = title.replace("|", "\\|")
            locator_cell = member["locator"].replace("|", "\\|")
            lines.append(
                f"| {title_cell} | {member['item_type']} | {member['interpretation']} | "
                f"{member['negation_hint']} | {locator_cell} | {text_cell} |"
            )
        lines.append("")
    return "\n".join(lines)


def remove_claim_cluster_artifacts(run_directory):
    for name in ("claim_clusters.csv", "claim_clusters.md"):
        path = run_directory / name
        if path.exists():
            path.unlink()


def compute_claim_clusters(run_directory, run, results_by_id, args):
    info = {
        "enabled": False,
        "reason": None,
        "itemCount": 0,
        "crossDocumentGroups": 0,
        "possibleContradictions": 0,
    }
    if args.no_claim_clusters:
        info["reason"] = "disabled with --no-claim-clusters"
        remove_claim_cluster_artifacts(run_directory)
        return info
    items = collect_claim_items(run, results_by_id)
    if len(items) < 2:
        info["reason"] = "fewer than two claims or findings to cluster"
        remove_claim_cluster_artifacts(run_directory)
        return info
    result = forge_embeddings.embed_texts(
        [item["item_text"][:CLAIM_CLUSTER_TEXT_CHARS] for item in items], url=args.embeddings_url
    )
    if not result["ok"]:
        info["reason"] = f"embeddings endpoint unavailable: {result['reason']}"
        remove_claim_cluster_artifacts(run_directory)
        return info
    vectors = [forge_embeddings.normalize(vector) for vector in result["vectors"]]
    components = forge_embeddings.cluster_components(vectors, args.claim_cluster_threshold)

    members = []
    cross_document_ids = []
    possible_contradiction_ids = []
    for cluster_index, component in enumerate(sorted(components, key=lambda part: min(part)), start=1):
        cluster_id = f"k{cluster_index}"
        representative_position = min(component)
        ordered = sorted(component, key=lambda position: (items[position]["document_id"], position))
        document_ids = {items[position]["document_id"] for position in component}
        negation_values = {items[position]["negation_hint"] for position in component}
        if len(document_ids) > 1:
            cross_document_ids.append(cluster_id)
            if len(negation_values) > 1:
                possible_contradiction_ids.append(cluster_id)
        for position in ordered:
            similarity = forge_embeddings.cosine(vectors[position], vectors[representative_position])
            member = dict(items[position])
            member.update(
                {
                    "cluster_id": cluster_id,
                    "cluster_size": len(component),
                    "document_count": len(document_ids),
                    "is_representative": "true" if position == representative_position else "false",
                    "similarity_to_representative": f"{similarity:.3f}",
                }
            )
            members.append(member)
    members.sort(key=lambda member: (member["cluster_id"], member["document_id"]))
    write_claim_clusters_csv(run_directory / "claim_clusters.csv", members)
    (run_directory / "claim_clusters.md").write_text(
        claim_clusters_markdown(
            members, cross_document_ids, possible_contradiction_ids, args.claim_cluster_threshold, result["model"]
        ),
        encoding="utf-8",
    )
    info.update(
        {
            "enabled": True,
            "itemCount": len(items),
            "crossDocumentGroups": len(cross_document_ids),
            "possibleContradictions": len(possible_contradiction_ids),
        }
    )
    return info


def is_literature_run_directory(path):
    return (
        path.is_dir()
        and (path / "run_config.json").is_file()
        and (path / "extraction_results.jsonl").is_file()
        and (path / "evidence_table.csv").is_file()
    )


def discover_literature_runs(path):
    root = Path(path).expanduser().resolve()
    if not root.exists():
        fail(f"meta input does not exist: {root}")
    if is_literature_run_directory(root):
        return [root]
    if not root.is_dir():
        fail(f"meta input is not a literature run or directory: {root}")
    runs = []
    for config_path in sorted(root.rglob("run_config.json")):
        candidate = config_path.parent
        if is_literature_run_directory(candidate):
            runs.append(candidate)
    if not runs:
        fail(f"no completed literature-extraction runs found under {root}")
    return runs


def infer_corpus_label(path):
    resolved = Path(path).expanduser().resolve()
    if resolved.name == "Literature-Extraction" and resolved.parent.name:
        return resolved.parent.name
    if resolved.name.startswith("literature") and resolved.parent.name:
        return resolved.parent.name
    return resolved.name or "corpus"


def parse_group_specs(raw_groups):
    groups = []
    for raw in raw_groups or []:
        if "=" not in raw:
            fail("--group must use label=path")
        label, raw_path = raw.split("=", 1)
        label = label.strip()
        if not label:
            fail("--group requires a nonblank label")
        groups.append((label, raw_path.strip()))
    return groups


def read_research_question(raw_value):
    candidate = Path(raw_value).expanduser()
    if candidate.is_file():
        return read_text(candidate).strip()
    return raw_value.strip()


def authored_deliverable_count(run_directory):
    count = 0
    for name in MARKDOWN_DELIVERABLES:
        path = run_directory / name
        if path.is_file() and PLACEHOLDER not in path.read_text(encoding="utf-8"):
            count += 1
    return count


def quote_probe(source_path, direct_quotes):
    if blank(source_path):
        return {"sourceAvailable": False, "sourceHashStatus": "missing", "quoteVerified": "", "sourceSnippet": None}
    source = Path(source_path)
    if not source.is_file():
        return {"sourceAvailable": False, "sourceHashStatus": "missing", "quoteVerified": "", "sourceSnippet": None}
    if blank(direct_quotes):
        return {"sourceAvailable": True, "sourceHashStatus": "unchecked", "quoteVerified": "", "sourceSnippet": None}
    try:
        text = source.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {"sourceAvailable": True, "sourceHashStatus": "unreadable", "quoteVerified": "false", "sourceSnippet": None}
    needle = str(direct_quotes).strip().splitlines()[0].strip()
    if len(needle) > 240:
        needle = needle[:240]
    if not needle:
        return {"sourceAvailable": True, "sourceHashStatus": "unchecked", "quoteVerified": "", "sourceSnippet": None}
    position = text.find(needle)
    if position < 0:
        return {"sourceAvailable": True, "sourceHashStatus": "unchecked", "quoteVerified": "false", "sourceSnippet": None}
    start = max(0, position - 240)
    end = min(len(text), position + len(needle) + 240)
    snippet = text[start:end].replace("\n", " ").strip()
    return {"sourceAvailable": True, "sourceHashStatus": "unchecked", "quoteVerified": "true", "sourceSnippet": snippet}


def run_source_id(run_directory):
    digest = hashlib.sha256(str(run_directory).encode("utf-8")).hexdigest()
    return f"run-{digest[:12]}"


def collect_meta_source(run_directory, corpus_label, meta_source_id, item_start):
    run = load_run(run_directory)
    results = load_results(run_directory, strict=False)
    results_by_id = {result.get("documentId"): result for result in results}
    warnings = []
    order = document_order(run)
    missing = [document_id for document_id in order if document_id not in results_by_id]
    if missing:
        warnings.append(f"incomplete run: {len(missing)} documents missing dispositions")
    authored_count = authored_deliverable_count(run_directory)
    if authored_count < len(MARKDOWN_DELIVERABLES):
        warnings.append("not all first-pass Markdown deliverables are authored")

    source_run_id = run_source_id(run_directory)
    items = []
    item_index = item_start
    for document in run["documents"]:
        result = results_by_id.get(document["documentId"])
        if not result or result.get("status") != "success":
            continue
        for item in result.get("items") or []:
            item_index += 1
            probe = quote_probe(document["sourcePath"], item.get("direct_quotes"))
            if not probe["sourceAvailable"]:
                warnings.append(f"source missing for {document['documentId']}: {document['sourcePath']}")
            items.append(
                {
                    "itemId": normalized_item_id("m", item_index),
                    "metaSourceId": meta_source_id,
                    "sourceRunId": source_run_id,
                    "runDirectory": str(run_directory),
                    "corpusLabel": corpus_label,
                    "documentId": document["documentId"],
                    "sourcePath": document["sourcePath"],
                    "sourceSha256": document.get("sha256", ""),
                    "sourceTitle": document.get("title") or Path(document["sourcePath"]).stem,
                    "itemType": item.get("item_type"),
                    "itemText": item.get("text") or "",
                    "directQuotes": item.get("direct_quotes"),
                    "locator": item.get("locator"),
                    "interpretation": item.get("interpretation"),
                    "confidence": item.get("confidence"),
                    "notes": item.get("notes"),
                    "quoteVerified": probe["quoteVerified"],
                    "sourceAvailable": probe["sourceAvailable"],
                    "sourceSnippet": probe["sourceSnippet"],
                    "questionScore": "",
                }
            )
    source = {
        "metaSourceId": meta_source_id,
        "corpusLabel": corpus_label,
        "runDirectory": str(run_directory),
        "sourceRunId": source_run_id,
        "documentCount": len(run["documents"]),
        "itemCount": len(items),
        "authoredDeliverables": authored_count,
        "warnings": sorted(set(warnings)),
    }
    return source, items


def write_meta_sources_csv(run_directory, sources):
    with (run_directory / "meta_sources.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(META_SOURCE_COLUMNS)
        for source in sources:
            writer.writerow(
                [
                    source["metaSourceId"],
                    source["corpusLabel"],
                    source["runDirectory"],
                    source["sourceRunId"],
                    source["documentCount"],
                    source["itemCount"],
                    source["authoredDeliverables"],
                    "; ".join(source["warnings"]),
                ]
            )


def write_meta_items(run_directory, items):
    with (run_directory / "meta_items.jsonl").open("w", encoding="utf-8", newline="") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")


def load_meta_items(run_directory):
    path = run_directory / "meta_items.jsonl"
    if not path.is_file():
        fail(f"meta_items.jsonl is missing: {path}")
    items = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError as error:
            fail(f"invalid JSON on meta_items.jsonl line {line_number}: {error}")
    return items


def write_empty_meta_embedding_artifacts(run_directory):
    with (run_directory / "bridge_candidates.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(META_BRIDGE_COLUMNS)
    with (run_directory / "topic_clusters.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(META_TOPIC_COLUMNS)


def compute_meta_embedding_artifacts(run_directory, research_question, items, embeddings_url):
    info = {
        "enabled": False,
        "reason": None,
        "embeddedItems": 0,
        "topicClusters": 0,
        "bridgeCandidates": 0,
    }
    embeddable = [
        item
        for item in items
        if item.get("itemType") in META_CLUSTER_ITEM_TYPES and not blank(item.get("itemText"))
    ]
    if not embeddable:
        info["reason"] = "no embeddable meta items"
        write_empty_meta_embedding_artifacts(run_directory)
        return info
    result = forge_embeddings.embed_texts(
        [research_question] + [item["itemText"][:META_ITEM_TEXT_CHARS] for item in embeddable],
        url=embeddings_url,
    )
    if not result["ok"]:
        info["reason"] = f"embeddings endpoint unavailable: {result['reason']}"
        write_empty_meta_embedding_artifacts(run_directory)
        return info
    vectors = [forge_embeddings.normalize(vector) for vector in result["vectors"]]
    question_vector = vectors[0]
    item_vectors = vectors[1:]
    vector_by_item = {}
    for item, vector in zip(embeddable, item_vectors):
        item["questionScore"] = f"{forge_embeddings.cosine(question_vector, vector):.3f}"
        vector_by_item[item["itemId"]] = vector

    components = forge_embeddings.cluster_components(item_vectors, DEFAULT_CLAIM_CLUSTER_THRESHOLD)
    topic_rows = []
    for cluster_index, component in enumerate(sorted(components, key=lambda part: min(part)), start=1):
        cluster_id = f"t{cluster_index}"
        representative_position = min(component)
        representative_vector = item_vectors[representative_position]
        corpus_count = len({embeddable[position]["corpusLabel"] for position in component})
        for position in sorted(component, key=lambda value: (embeddable[value]["corpusLabel"], embeddable[value]["itemId"])):
            item = embeddable[position]
            topic_rows.append(
                {
                    "cluster_id": cluster_id,
                    "cluster_size": len(component),
                    "corpus_count": corpus_count,
                    "is_representative": "true" if position == representative_position else "false",
                    "similarity_to_representative": f"{forge_embeddings.cosine(item_vectors[position], representative_vector):.3f}",
                    "item_id": item["itemId"],
                    "corpus_label": item["corpusLabel"],
                    "source_title": item["sourceTitle"],
                    "item_type": item["itemType"],
                    "item_text": item["itemText"],
                }
            )
    with (run_directory / "topic_clusters.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=META_TOPIC_COLUMNS)
        writer.writeheader()
        writer.writerows(topic_rows)

    bridge_rows = []
    bridge_index = 0
    for left_index, left in enumerate(embeddable):
        if left.get("itemType") not in META_BRIDGE_ITEM_TYPES:
            continue
        for right in embeddable[left_index + 1 :]:
            if right.get("itemType") not in META_BRIDGE_ITEM_TYPES:
                continue
            if left["corpusLabel"] == right["corpusLabel"]:
                continue
            similarity = forge_embeddings.cosine(vector_by_item[left["itemId"]], vector_by_item[right["itemId"]])
            if similarity < DEFAULT_META_BRIDGE_THRESHOLD:
                continue
            bridge_index += 1
            bridge_rows.append(
                {
                    "bridge_id": f"b{bridge_index:04d}",
                    "similarity": f"{similarity:.3f}",
                    "left_item_id": left["itemId"],
                    "left_corpus_label": left["corpusLabel"],
                    "left_document_title": left["sourceTitle"],
                    "left_item_type": left["itemType"],
                    "right_item_id": right["itemId"],
                    "right_corpus_label": right["corpusLabel"],
                    "right_document_title": right["sourceTitle"],
                    "right_item_type": right["itemType"],
                    "review_note": "candidate cross-corpus connection; verify against evidence before synthesis",
                }
            )
    bridge_rows.sort(key=lambda row: (-float(row["similarity"]), row["left_item_id"], row["right_item_id"]))
    bridge_rows = bridge_rows[:200]
    with (run_directory / "bridge_candidates.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=META_BRIDGE_COLUMNS)
        writer.writeheader()
        writer.writerows(bridge_rows)

    cache_directory = run_directory / "working"
    cache_directory.mkdir(exist_ok=True)
    cache = {
        "model": result["model"],
        "url": result["url"],
        "dimensions": result["dimensions"],
        "items": [
            {"itemId": item["itemId"], "vector": vector_by_item[item["itemId"]]}
            for item in embeddable
        ],
    }
    (cache_directory / "embedding_cache.json").write_text(json.dumps(cache, ensure_ascii=False) + "\n", encoding="utf-8")
    info.update(
        {
            "enabled": True,
            "embeddedItems": len(embeddable),
            "topicClusters": len(components),
            "bridgeCandidates": len(bridge_rows),
            "model": result["model"],
            "url": result["url"],
        }
    )
    return info


def balanced_meta_items(items):
    type_order = list(META_BRIDGE_ITEM_TYPES) + [
        "method",
        "data_source",
        "limitation",
        "research_gap",
        "author",
        "citation",
        "variable",
        "population",
        "technology",
        "policy",
    ]
    buckets = defaultdict(list)
    for item in items:
        buckets[(item["corpusLabel"], item.get("itemType") or "")].append(item)
    for key in buckets:
        buckets[key].sort(key=lambda item: (-(float(item["questionScore"]) if item.get("questionScore") else 0.0), item["itemId"]))
    ordered = []
    labels = sorted({item["corpusLabel"] for item in items})
    remaining = sum(len(values) for values in buckets.values())
    while remaining:
        progressed = False
        for label in labels:
            seen_types = set(type_order)
            item_types = type_order + sorted({key[1] for key in buckets if key[0] == label and key[1] not in seen_types})
            for item_type in item_types:
                bucket = buckets.get((label, item_type))
                if not bucket:
                    continue
                ordered.append(bucket.pop(0))
                remaining -= 1
                progressed = True
        if not progressed:
            break
    return ordered


def render_meta_item(item):
    text = (item.get("itemText") or "").replace("\n", " ").strip()
    quote = (item.get("directQuotes") or item.get("sourceSnippet") or "").replace("\n", " ").strip()
    if len(text) > 1000:
        text = text[:997] + "..."
    if len(quote) > 500:
        quote = quote[:497] + "..."
    parts = [
        f"- item:{item['itemId']}",
        f"corpus:{item['corpusLabel']}",
        f"type:{item.get('itemType')}",
        f"source:{item.get('sourceTitle') or item.get('documentId')}",
        f"locator:{item.get('locator') or 'unknown'}",
        f"interpretation:{item.get('interpretation')}",
        f"confidence:{item.get('confidence')}",
        f"text:{text}",
    ]
    if quote:
        parts.append(f"quote_or_snippet:{quote}")
    if item.get("quoteVerified"):
        parts.append(f"quote_verified:{item['quoteVerified']}")
    return " | ".join(parts)


def write_meta_packets(run_directory, research_question, items, target_context, max_context):
    packets_directory = run_directory / "packets"
    packets_directory.mkdir()
    header = "\n".join(
        [
            "# Meta Literature Extraction Packet",
            "",
            f"Research question: {research_question}",
            "",
            "Use this bounded packet with meta_items.jsonl, bridge_candidates.csv, and topic_clusters.csv.",
            "Write a memo that cites item ids for every substantive analytic claim.",
            "Preserve corpus roles, disagreements, silences, and limits; do not reconcile conflicts silently.",
            "",
            "## Evidence Items",
            "",
        ]
    )
    ordered = balanced_meta_items(items)
    packets = []
    current_lines = [header]
    current_items = []

    def flush_packet():
        if not current_items:
            return
        packet_id = f"packet-{len(packets) + 1:04d}"
        content = "\n".join(current_lines).rstrip() + "\n"
        token_count = estimate_tokens(content)
        if token_count > max_context:
            fail(f"{packet_id} exceeds --max-context after truncation estimate: {token_count}")
        path = packets_directory / f"{packet_id}.md"
        path.write_text(content, encoding="utf-8")
        packets.append(
            {
                "packetId": packet_id,
                "path": str(path),
                "itemCount": len(current_items),
                "estimatedTokens": token_count,
                "itemIds": list(current_items),
            }
        )

    for item in ordered:
        line = render_meta_item(item)
        candidate = "\n".join(current_lines + [line]).rstrip() + "\n"
        if current_items and estimate_tokens(candidate) > target_context:
            flush_packet()
            current_lines = [header]
            current_items = []
        current_lines.append(line)
        current_items.append(item["itemId"])
    flush_packet()
    if not packets:
        fail("no meta packets were generated")
    return packets


def load_meta_config(run_directory):
    try:
        config = json.loads((run_directory / "meta_config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail(f"could not read meta_config.json: {error}")
    if config.get("schemaVersion") != META_SCHEMA_VERSION:
        fail(f"unsupported meta schema version: {config.get('schemaVersion')}")
    return config


def require_meta_run_directory(raw_path):
    path = Path(raw_path).expanduser().resolve()
    if not path.is_dir():
        fail(f"meta run directory does not exist: {path}")
    if not (path / "meta_config.json").is_file():
        fail(f"meta_config.json is missing: {path}")
    return path


def load_packet_memos(run_directory, strict=True):
    path = run_directory / "packet_memos.jsonl"
    if not path.is_file():
        fail(f"packet_memos.jsonl is missing: {path}")
    try:
        memos, _ = run_state.read_jsonl_recover_tail(path, repair=True)
    except ValueError as error:
        fail(str(error))
    seen = set()
    for memo in memos:
        packet_id = memo.get("packetId")
        if strict and packet_id in seen:
            fail(f"duplicate memo for packet {packet_id}")
        seen.add(packet_id)
    return memos


def next_pending_packet(config, memos):
    recorded = {memo.get("packetId") for memo in memos}
    for packet in config.get("packets", []):
        if packet["packetId"] not in recorded:
            return packet
    return None


def command_meta_init(args):
    if args.max_context > MAX_ALLOWED_META_CONTEXT:
        fail(f"--max-context cannot exceed {MAX_ALLOWED_META_CONTEXT}")
    if args.target_context > args.max_context:
        fail("--target-context must be less than or equal to --max-context")
    research_question = read_research_question(args.research_question)
    if not research_question:
        fail("--research-question is required and cannot be blank")
    groups = parse_group_specs(args.group)
    for raw_input in args.inputs:
        groups.append((infer_corpus_label(raw_input), raw_input))
    if not groups:
        fail("meta-init requires at least one input path or --group label=path")
    configuration = {
        "workflow": META_RUN_STATE_WORKFLOW,
        "command": "meta-init",
        "input": {"groups": [{"label": label, "path": str(Path(path).expanduser().resolve())} for label, path in groups]},
        "options": {
            "researchQuestion": research_question,
            "targetContext": args.target_context,
            "maxContext": args.max_context,
            "embeddingsUrl": args.embeddings_url,
        },
    }
    requested_output = Path(args.output).expanduser().resolve()
    if requested_output.exists():
        try:
            state = run_state.load_run_state(requested_output, META_RUN_STATE_WORKFLOW)
            run_state.assert_compatible_run(state, configuration)
            config = load_meta_config(requested_output)
            memos = load_packet_memos(requested_output)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            fail(str(error))
        print(json.dumps({"metaRunDirectory": str(requested_output), "resumed": True, "packets": len(config["packets"]), "processed": len(memos), "phase": state["phase"], "nextAction": state.get("nextAction")}))
        return
    output = require_new_directory(requested_output)
    (output / "working").mkdir()

    sources = []
    items = []
    source_index = 0
    for corpus_label, raw_path in groups:
        for run_directory in discover_literature_runs(raw_path):
            source_index += 1
            source, source_items = collect_meta_source(
                run_directory,
                corpus_label,
                f"s{source_index:04d}",
                len(items),
            )
            sources.append(source)
            items.extend(source_items)
    if not items:
        fail("meta-init found no successful extracted items in the supplied runs")

    embedding_info = compute_meta_embedding_artifacts(output, research_question, items, args.embeddings_url)
    write_meta_items(output, items)
    write_meta_sources_csv(output, sources)
    packets = write_meta_packets(output, research_question, items, args.target_context, args.max_context)
    warnings = sorted({warning for source in sources for warning in source["warnings"]})
    if embedding_info.get("reason"):
        warnings.append(embedding_info["reason"])
    context_budget = {
        "targetContext": args.target_context,
        "maxContext": args.max_context,
        "estimatedTokensMethod": "ceil(characters / 4)",
        "packetCount": len(packets),
        "packets": packets,
        "warnings": warnings,
    }
    run_state.atomic_write_json(output / "context_budget.json", context_budget)
    config = {
        "schemaVersion": META_SCHEMA_VERSION,
        "createdAt": utc_now(),
        "researchQuestion": research_question,
        "targetContext": args.target_context,
        "maxContext": args.max_context,
        "sources": sources,
        "packets": packets,
        "embeddingInfo": embedding_info,
    }
    run_state.atomic_write_json(output / "meta_config.json", config)
    run_state.atomic_write_text(output / "packet_memos.jsonl", "")
    state = run_state.create_run_state(
        META_RUN_STATE_WORKFLOW,
        "meta-init",
        configuration["input"],
        configuration["options"],
        items=[{"id": packet["packetId"], "path": packet["path"], "status": "pending", "attempts": 0, "transient": False} for packet in packets],
        phase="meta-extracting",
        next_action="meta-next",
    )
    run_state.initialize_run_state(output, state)
    print(
        json.dumps(
            {
                "metaRunDirectory": str(output),
                "sources": len(sources),
                "items": len(items),
                "packets": len(packets),
                "embeddingInfo": embedding_info,
                "warnings": warnings,
            },
            ensure_ascii=False,
        )
    )


def command_meta_next(args):
    run_directory = require_meta_run_directory(args.run_directory)
    config = load_meta_config(run_directory)
    memos = load_packet_memos(run_directory)
    packet = next_pending_packet(config, memos)
    total = len(config.get("packets", []))
    if packet is None:
        print(json.dumps({"complete": True, "processed": len(memos), "total": total}))
        return
    print(
        json.dumps(
            {
                "complete": False,
                "packetId": packet["packetId"],
                "packetPath": packet["path"],
                "estimatedTokens": packet["estimatedTokens"],
                "itemCount": packet["itemCount"],
                "researchQuestion": config["researchQuestion"],
                "progress": {"processed": len(memos), "total": total},
            },
            ensure_ascii=False,
        )
    )


def command_meta_status(args):
    run_directory = require_meta_run_directory(args.run_directory)
    state = run_state.load_run_state(run_directory, META_RUN_STATE_WORKFLOW)
    config = load_meta_config(run_directory)
    memos = load_packet_memos(run_directory)
    pending = next_pending_packet(config, memos)
    print(
        json.dumps(
            {
                "metaRunDirectory": str(run_directory),
                "status": state["status"],
                "phase": state["phase"],
                "nextAction": state.get("nextAction"),
                "packets": {"total": len(config.get("packets", [])), "recorded": len(memos)},
                "nextPacketId": pending["packetId"] if pending else None,
                "deliverables": state.get("deliverables", []),
            },
            indent=2,
        )
    )


def command_meta_record(args):
    run_directory = require_meta_run_directory(args.run_directory)
    config = load_meta_config(run_directory)
    memos = load_packet_memos(run_directory)
    expected = next_pending_packet(config, memos)
    if expected is None:
        fail("the meta run is already complete")
    if args.packet_id != expected["packetId"]:
        fail(f"packets must be recorded sequentially; expected {expected['packetId']}, received {args.packet_id}")
    memo_path = Path(args.memo_file).expanduser().resolve()
    if not memo_path.is_file():
        fail(f"memo file does not exist: {memo_path}")
    memo_text = read_text(memo_path).strip()
    if not memo_text:
        fail("memo file cannot be blank")
    record = {
        "packetId": args.packet_id,
        "memoPath": str(memo_path),
        "memoText": memo_text,
        "recordedAt": utc_now(),
    }
    run_state.append_jsonl_fsync(run_directory / "packet_memos.jsonl", record)
    remaining = len(config.get("packets", [])) - len(memos) - 1
    run_state.update_run_state(
        run_directory,
        lambda state: _record_meta_packet_state(state, args.packet_id, remaining),
        {"type": "item_completed", "itemId": args.packet_id, "phase": "meta-extract", "status": "success"},
    )
    print(json.dumps({"recorded": args.packet_id, "remaining": remaining}))


def _record_meta_packet_state(state, packet_id, remaining):
    for item in state.get("items", []):
        if item.get("id") == packet_id:
            item["status"] = "success"
            item["attempts"] = item.get("attempts", 0) + 1
            break
    state["phase"] = "meta-extracting" if remaining else "meta-building"
    state["nextAction"] = "meta-next" if remaining else "meta-build"
    return state


def scaffold_meta_markdown(run_directory):
    created = []
    for name, lines in META_MARKDOWN_DELIVERABLES.items():
        path = run_directory / name
        if path.exists():
            continue
        run_state.atomic_write_text(path, "\n".join(lines) + "\n")
        created.append(name)
    return created


def command_meta_build(args):
    run_directory = require_meta_run_directory(args.run_directory)
    config = load_meta_config(run_directory)
    memos = load_packet_memos(run_directory)
    pending = next_pending_packet(config, memos)
    if pending is not None:
        fail(f"meta run is incomplete; next pending packet is {pending['packetId']}")
    created = scaffold_meta_markdown(run_directory)
    state = run_state.load_run_state(run_directory, META_RUN_STATE_WORKFLOW)
    existing = {item["name"]: item for item in state.get("deliverables", [])}
    deliverables = []
    for name in META_MARKDOWN_DELIVERABLES:
        path = run_directory / name
        complete = path.is_file() and PLACEHOLDER not in path.read_text(encoding="utf-8")
        deliverables.append({"name": name, "path": str(path), "status": "complete" if complete else existing.get(name, {}).get("status", "pending")})
    run_state.update_run_state(
        run_directory,
        lambda draft: _set_deliverable_state(draft, deliverables, any(item["status"] != "complete" for item in deliverables)),
        {"type": "meta_build_completed", "packetMemos": len(memos), "deliverables": len(deliverables)},
    )
    print(json.dumps({"packetMemos": len(memos), "scaffolded": created, "nextAction": "next-output"}))


def command_meta_validate(args):
    run_directory = require_meta_run_directory(args.run_directory)
    config = load_meta_config(run_directory)
    items = load_meta_items(run_directory)
    memos = load_packet_memos(run_directory, strict=False)
    errors = []
    warnings = []
    item_ids = {item["itemId"] for item in items}
    packet_ids = [packet["packetId"] for packet in config.get("packets", [])]
    memo_ids = [memo.get("packetId") for memo in memos]
    valid_packet_ids = set(packet_ids)
    for memo_id in memo_ids:
        if memo_id not in valid_packet_ids:
            errors.append(f"memo references unknown packet {memo_id}")
    duplicates = sorted(value for value, count in Counter(memo_ids).items() if count > 1)
    if duplicates:
        errors.append(f"duplicate memos for packets: {', '.join(str(value) for value in duplicates)}")
    if memo_ids != packet_ids[: len(memo_ids)]:
        errors.append("packet memos are not in packet order")
    missing = [packet_id for packet_id in packet_ids if packet_id not in set(memo_ids)]
    if missing:
        errors.append(f"meta run is incomplete; {len(missing)} packets remain, beginning with {missing[0]}")
    for required in ("meta_sources.csv", "meta_items.jsonl", "context_budget.json", "bridge_candidates.csv", "topic_clusters.csv"):
        if not (run_directory / required).is_file():
            errors.append(f"meta artifact is missing: {required}")
    for source in config.get("sources", []):
        for warning in source.get("warnings", []):
            warnings.append(f"{source['metaSourceId']}: {warning}")
    for item in items:
        if not item.get("sourceAvailable", True):
            warnings.append(f"source unavailable for {item['itemId']}: {item.get('sourcePath')}")
    if not missing:
        for required in META_MARKDOWN_DELIVERABLES:
            path = run_directory / required
            if not path.is_file():
                errors.append(f"meta deliverable is missing: {required}; run meta-build")
                continue
            text = path.read_text(encoding="utf-8")
            if PLACEHOLDER in text:
                errors.append(f"meta deliverable still has an unresolved placeholder: {required}")
            elif item_ids and not any(re.search(r"\b" + re.escape(item_id) + r"\b", text) for item_id in item_ids):
                errors.append(f"meta deliverable has no item citation: {required}")
    result = {
        "valid": not errors,
        "complete": not missing,
        "packets": {"total": len(packet_ids), "recorded": len(memos)},
        "items": len(items),
        "errors": errors,
        "warnings": sorted(set(warnings)),
    }
    if args.fix_hints:
        result["issues"] = [meta_validation_issue(error) for error in errors]
    if not errors and not missing:
        run_state.update_run_state(
            run_directory,
            lambda state: {**state, "status": "complete", "phase": "complete", "nextAction": None},
            {"type": "run_completed"},
        )
    print(json.dumps(result, indent=2))
    if errors:
        raise SystemExit(1)


def meta_validation_issue(message):
    if "unresolved placeholder" in message:
        return {
            "code": "meta_deliverable_unreviewed",
            "message": message,
            "command": "Author the scaffolded meta Markdown from packet memos, bridge candidates, topic clusters, and meta items.",
        }
    if "no item citation" in message:
        return {
            "code": "meta_deliverable_uncited",
            "message": message,
            "command": "Cite item ids such as m000001 in every substantive meta synthesis deliverable.",
        }
    if "incomplete" in message:
        return {
            "code": "meta_packets_incomplete",
            "message": message,
            "command": "Call meta-next, author a packet memo, then record it with meta-record until complete.",
        }
    return {"code": "meta_validation_error", "message": message}


def command_build(args):
    run_directory = require_run_directory(args.run_directory)
    run = load_run(run_directory)
    results = load_results(run_directory)
    pending = next_pending(run, results)
    if pending is not None:
        fail(f"run is incomplete; next pending document is {pending}")
    if not -1.0 <= args.claim_cluster_threshold <= 1.0:
        fail("--claim-cluster-threshold must be between -1 and 1")
    if args.target_context < 1 or args.target_context > MAX_ALLOWED_META_CONTEXT:
        fail(f"--target-context must be between 1 and {MAX_ALLOWED_META_CONTEXT}")
    verify_hashes(run)
    results_by_id = {result["documentId"]: result for result in results}
    evidence_rows = write_evidence_table(run_directory, run, results_by_id)
    method_rows = write_methods_matrix(run_directory, run, results_by_id)
    indexed_items = write_item_index(run_directory, run, results_by_id)
    profiled_sources = write_source_profile(run_directory, run, results_by_id)
    claim_clusters = compute_claim_clusters(run_directory, run, results_by_id, args)
    created = scaffold_markdown(run_directory)
    write_documents_csv(run_directory, run["documents"], results_by_id)
    state = run_state.load_run_state(run_directory, RUN_STATE_WORKFLOW)
    synthesis = state.get("synthesis") or initialize_synthesis_state(run_directory, args.target_context)
    existing_deliverables = {item["name"]: item for item in state.get("deliverables", [])}
    deliverables = []
    for name in MARKDOWN_DELIVERABLES:
        path = run_directory / name
        existing = existing_deliverables.get(name)
        complete = path.is_file() and PLACEHOLDER not in path.read_text(encoding="utf-8")
        deliverables.append({"name": name, "path": str(path), "status": "complete" if complete else existing.get("status", "pending") if existing else "pending"})
    run_state.update_run_state(
        run_directory,
        lambda draft: _initialize_build_state(draft, synthesis, deliverables),
        {"type": "build_completed", "evidenceItems": evidence_rows, "deliverables": len(deliverables)},
    )
    counts = Counter(result["status"] for result in results)
    print(
        json.dumps(
            {
                "evidenceItems": evidence_rows,
                "methodsRows": method_rows,
                "indexedItems": indexed_items,
                "profiledSources": profiled_sources,
                "scaffolded": created,
                "claimClusters": claim_clusters,
                "success": counts["success"],
                "needsReview": counts["needs_review"],
                "skipped": counts["skipped"],
                "failed": counts["failed"],
                "nextAction": "synthesis-next" if not synthesis.get("complete") else "next-output",
            }
        )
    )


def _initialize_build_state(state, synthesis, deliverables):
    state["synthesis"] = synthesis
    state["deliverables"] = deliverables
    state["phase"] = "synthesis" if not synthesis.get("complete") else "authoring"
    state["nextAction"] = "synthesis-next" if not synthesis.get("complete") else "next-output"
    return state


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
                    normalize_items(result["items"], run.get("itemTypes", ITEM_TYPES))
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
        if document.get("active", True) is False:
            continue
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
    if args.fix_hints:
        result["issues"] = [validation_issue(error) for error in errors]
    if not errors and not missing:
        run_state.update_run_state(
            run_directory,
            lambda state: {**state, "status": "complete", "phase": "complete", "nextAction": None},
            {"type": "run_completed"},
        )
    print(json.dumps(result, indent=2))
    if errors:
        raise SystemExit(1)


def validation_issue(message):
    if "invalid status" in message:
        return {
            "code": "invalid_document_status",
            "message": message,
            "allowedValues": sorted(DOCUMENT_STATUSES),
            "command": "Use record with --status success, needs_review, skipped, or failed.",
        }
    if "invalid extraction items" in message:
        return {
            "code": "invalid_extraction_items",
            "message": message,
            "allowedValues": {
                "item_type": ITEM_TYPES,
                "interpretation": sorted(INTERPRETATIONS),
                "confidence": sorted(CONFIDENCES),
            },
            "command": "Call next, rewrite the working extraction JSON to the contract, then record it again in a fresh run.",
        }
    if "source file hash differs" in message:
        return {
            "code": "source_hash_changed",
            "message": message,
            "command": "Re-run init after source files are stable.",
        }
    if "unresolved placeholder" in message:
        return {
            "code": "deliverable_unreviewed",
            "message": message,
            "command": "Author the scaffolded Markdown deliverable from evidence_table.csv, methods_matrix.csv, and claim_clusters.md when present.",
        }
    return {"code": "validation_error", "message": message}


def parser():
    root = argparse.ArgumentParser(description="Extract structured evidence from research documents and manage resumable per-document extraction.")
    subparsers = root.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Report local extraction capabilities.")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(handler=command_doctor)

    init = subparsers.add_parser("init", help="Discover sources and scaffold a resumable extraction run.")
    init.add_argument("input")
    init.add_argument("--output", required=True)
    init.add_argument(
        "--schema",
        choices=("academic", "products", "custom"),
        default="academic",
        help="Extraction schema to use noninteractively. Custom requires --item-types.",
    )
    init.add_argument("--item-types", help="Comma-separated item types for a custom extraction schema.")
    init.add_argument("--custom-instructions", default="", help="Custom model instructions recorded in run_config.json.")
    init.add_argument(
        "--include-reserved",
        action="store_true",
        help="Include Ingest, Originals, and Generated folders instead of skipping them.",
    )
    init.set_defaults(handler=command_init)

    status = subparsers.add_parser("status", help="Report durable run progress and input drift.")
    status.add_argument("run_directory")
    status.add_argument("--json", action="store_true", help="Accepted for symmetry; output is always JSON.")
    status.set_defaults(handler=command_status)

    refresh = subparsers.add_parser("refresh", help="Explicitly reconcile added, changed, and removed sources.")
    refresh.add_argument("run_directory")
    refresh.set_defaults(handler=command_refresh)

    retry = subparsers.add_parser("retry", help="Retry failed document dispositions without discarding later results.")
    retry.add_argument("run_directory")
    retry_group = retry.add_mutually_exclusive_group(required=True)
    retry_group.add_argument("--item")
    retry_group.add_argument("--all-failed", action="store_true")
    retry.set_defaults(handler=command_retry)

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

    build = subparsers.add_parser("build", help="Assemble evidence and methods tables, cluster claims across documents, and scaffold Markdown deliverables.")
    build.add_argument("run_directory")
    build.add_argument(
        "--no-claim-clusters",
        action="store_true",
        help="Skip embedding-based cross-document claim clustering.",
    )
    build.add_argument(
        "--target-context",
        type=int,
        default=DEFAULT_SYNTHESIS_TARGET_CONTEXT,
        help="Target maximum estimated tokens per synthesis packet using ceil(characters / 4).",
    )
    build.add_argument(
        "--claim-cluster-threshold",
        type=float,
        default=DEFAULT_CLAIM_CLUSTER_THRESHOLD,
        help="Cosine similarity at or above which claims and findings group together.",
    )
    build.add_argument(
        "--embeddings-url",
        help="Override the embeddings endpoint (default FORGE_EMBEDDINGS_URL or http://llms:8005/v1/embeddings).",
    )
    build.set_defaults(handler=command_build)

    synthesis_next = subparsers.add_parser("synthesis-next", help="Return one bounded synthesis packet.")
    synthesis_next.add_argument("run_directory")
    synthesis_next.set_defaults(handler=command_synthesis_next)

    synthesis_record = subparsers.add_parser("synthesis-record", help="Atomically record one synthesis packet memo.")
    synthesis_record.add_argument("run_directory")
    synthesis_record.add_argument("--packet-id", required=True)
    synthesis_record.add_argument("--memo-file", required=True)
    synthesis_record.set_defaults(handler=command_synthesis_record)

    next_output = subparsers.add_parser("next-output", help="Return one pending authored deliverable.")
    next_output.add_argument("run_directory")
    next_output.set_defaults(handler=command_next_output)

    record_output = subparsers.add_parser("record-output", help="Atomically install one authored deliverable.")
    record_output.add_argument("run_directory")
    record_output.add_argument("--name", required=True)
    record_output.add_argument("--file", required=True)
    record_output.set_defaults(handler=command_record_output)

    validate = subparsers.add_parser("validate", help="Validate run state, provenance, schema, and built artifacts.")
    validate.add_argument("run_directory")
    validate.add_argument("--fix-hints", action="store_true", help="Include machine-readable repair hints.")
    validate.add_argument("--json", action="store_true", help="Accepted for symmetry; output is always JSON.")
    validate.set_defaults(handler=command_validate)

    meta_init = subparsers.add_parser("meta-init", help="Initialize a context-bounded meta extraction run over prior literature runs.")
    meta_init.add_argument("inputs", nargs="*", help="Prior literature run directories or parent folders containing prior runs.")
    meta_init.add_argument("--output", required=True)
    meta_init.add_argument("--research-question", required=True, help="Research question text, or a path to a UTF-8 text file.")
    meta_init.add_argument(
        "--group",
        action="append",
        default=[],
        help="Explicit corpus label and path as label=path. May be repeated.",
    )
    meta_init.add_argument(
        "--target-context",
        type=int,
        default=DEFAULT_META_TARGET_CONTEXT,
        help="Target maximum estimated tokens per packet using ceil(characters / 4).",
    )
    meta_init.add_argument(
        "--max-context",
        type=int,
        default=DEFAULT_META_MAX_CONTEXT,
        help=f"Hard packet ceiling. Must be <= {MAX_ALLOWED_META_CONTEXT}.",
    )
    meta_init.add_argument(
        "--embeddings-url",
        help="Override the embeddings endpoint (default FORGE_EMBEDDINGS_URL or http://llms:8005/v1/embeddings).",
    )
    meta_init.set_defaults(handler=command_meta_init)

    meta_status = subparsers.add_parser("meta-status", help="Report durable meta-run progress.")
    meta_status.add_argument("run_directory")
    meta_status.add_argument("--json", action="store_true", help="Accepted for symmetry; output is always JSON.")
    meta_status.set_defaults(handler=command_meta_status)

    meta_next = subparsers.add_parser("meta-next", help="Return exactly one pending meta packet as JSON.")
    meta_next.add_argument("run_directory")
    meta_next.set_defaults(handler=command_meta_next)

    meta_record = subparsers.add_parser("meta-record", help="Append one model-authored packet memo.")
    meta_record.add_argument("run_directory")
    meta_record.add_argument("--packet-id", required=True)
    meta_record.add_argument("--memo-file", required=True)
    meta_record.set_defaults(handler=command_meta_record)

    meta_build = subparsers.add_parser("meta-build", help="Scaffold meta synthesis deliverables after all packet memos are recorded.")
    meta_build.add_argument("run_directory")
    meta_build.set_defaults(handler=command_meta_build)

    meta_validate = subparsers.add_parser("meta-validate", help="Validate meta run state, packet memos, provenance warnings, and authored deliverables.")
    meta_validate.add_argument("run_directory")
    meta_validate.add_argument("--fix-hints", action="store_true", help="Include machine-readable repair hints.")
    meta_validate.add_argument("--json", action="store_true", help="Accepted for symmetry; output is always JSON.")
    meta_validate.set_defaults(handler=command_meta_validate)
    return root


def main():
    args = parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()

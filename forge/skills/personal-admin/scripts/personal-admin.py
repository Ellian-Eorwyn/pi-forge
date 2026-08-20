#!/usr/bin/env python3

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
from vault_schema import ensure_workspace_marker  # noqa: E402
import forge_llm
import forge_verify
import run_state


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


def progress(message):
    """Per-document progress on stderr; stdout stays one JSON result."""
    print(message, file=sys.stderr, flush=True)


class UserError(RuntimeError):
    """A document could not be extracted; the run records it and continues."""


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
    # Inside a vault this lands under the workflow root, and the category folder
    # holding it was created by the mkdir above rather than by
    # ``resolveWorkflowRoot`` -- only the web-research and vault-compose
    # extensions go through that. An unmarked run is counted, classified, filed
    # and embedded as notes, so the run marks itself the moment it exists.
    ensure_workspace_marker(path)
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


def discover_sources(raw_inputs, allow_empty=False):
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
    if not found and not allow_empty:
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


def run_configuration(raw_inputs, title, deliverables, snapshot):
    return {
        "workflow": "personal-admin",
        "command": "init",
        "input": {
            "roots": [str(Path(value).expanduser().resolve()) for value in raw_inputs],
            "snapshot": snapshot,
        },
        "options": {"title": title, "deliverables": deliverables},
    }


def item_state(document):
    return {
        "id": document["documentId"],
        "path": document["sourcePath"],
        "sha256": document["sha256"],
        "status": "pending",
        "attempts": 0,
        "transient": False,
        "error": None,
    }


def current_drift(state):
    roots = state.get("input", {}).get("roots", [])
    current = build_document_records(discover_sources(roots, allow_empty=True))
    snapshot = state.get("input", {}).get("snapshot", [])
    before = [{"path": item["sourcePath"], "sha256": item["sha256"]} for item in snapshot]
    after = [{"path": item["sourcePath"], "sha256": item["sha256"]} for item in current]
    return run_state.input_drift(before, after), current


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
    title = args.title or "Personal Admin"
    output = Path(args.output).expanduser().resolve()
    if output.exists():
        try:
            state = run_state.load_run_state(output, "personal-admin")
            expected = run_configuration(args.inputs, title, deliverables, state["input"]["snapshot"])
            run_state.assert_compatible_run(state, expected)
        except (OSError, ValueError, KeyError) as error:
            fail(str(error))
        drift, _ = current_drift(state)
        print(json.dumps({"runDirectory": str(output), "resumed": True, "complete": state.get("status") == "complete", "phase": state.get("phase"), "nextAction": state.get("nextAction"), "drift": drift}, ensure_ascii=False))
        return
    sources = discover_sources(args.inputs)
    documents = build_document_records(sources)
    configuration = run_configuration(args.inputs, title, deliverables, documents)
    output.mkdir(parents=True)
    (output / "working").mkdir()
    run = {
        "schemaVersion": RUN_SCHEMA_VERSION,
        "createdAt": utc_now(),
        "title": title,
        "deliverables": deliverables,
        "factTypes": FACT_TYPES,
        "documents": documents,
    }
    run_state.atomic_write_json(output / "run_config.json", run)
    manifest = {"schemaVersion": RUN_SCHEMA_VERSION, "createdAt": utc_now(), "documents": documents}
    run_state.atomic_write_json(output / "source_manifest.json", manifest)
    run_state.atomic_write_text(output / "facts_results.jsonl", "")
    state = run_state.create_run_state(
        "personal-admin",
        "init",
        configuration["input"],
        configuration["options"],
        items=[item_state(document) for document in documents],
        phase="extracting",
        next_action="next",
    )
    run_state.initialize_run_state(output, state)
    write_sources_md(output, title, documents)
    created = scaffold_markdown(output, title, deliverables)
    print(
        json.dumps(
            {"runDirectory": str(output), "resumed": False, "title": title, "documents": len(documents), "deliverables": deliverables, "scaffolded": created},
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
    try:
        results, warnings = run_state.read_jsonl_recover_tail(path, repair=True)
    except ValueError as error:
        fail(str(error))
    for warning in warnings:
        print(f"Warning: {warning}", file=sys.stderr)
    # One result per document. A later record may replace an earlier one only
    # when it says so: review escalation re-extracts a document deliberately,
    # while an unmarked duplicate is still a bug worth failing on.
    position = {}
    effective = []
    for result in results:
        document_id = result.get("documentId")
        if document_id in position:
            if result.get("supersedes"):
                effective[position[document_id]] = result
                continue
            if strict:
                fail(f"duplicate result for document {document_id}")
        position[document_id] = len(effective)
        effective.append(result)
    return effective


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


def command_status(args):
    run_directory = require_run_directory(args.run_directory)
    try:
        state = run_state.load_run_state(run_directory, "personal-admin")
    except ValueError as error:
        fail(str(error))
    drift, _ = current_drift(state)
    results = load_results(run_directory)
    run = load_run(run_directory)
    result = {
        "runDirectory": str(run_directory),
        "status": state.get("status"),
        "phase": state.get("phase"),
        "nextAction": state.get("nextAction"),
        "processed": len(results),
        "total": len(run["documents"]),
        "drift": drift,
        "refreshRequired": any(drift.values()),
    }
    print(json.dumps(result, indent=2 if args.json else None))


def validate_facts(raw, fact_types=None):
    """Normalize extracted facts, returning ``(facts, errors)``.

    Returns errors rather than exiting so the worker can feed them back to the
    model as a correction; the CLI turns the first one into a failure.
    """
    fact_types = list(fact_types or FACT_TYPES)
    fact_type_set = set(fact_types)
    if not isinstance(raw, list):
        return [], ["facts must be a JSON array of fact objects"]
    facts = []
    errors = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            errors.append(f"fact {index} is not an object")
            continue
        fact_type = item.get("fact_type")
        if fact_type not in fact_type_set:
            errors.append(f"fact {index} has invalid fact_type {fact_type!r}; expected one of {', '.join(fact_types)}")
            continue
        if blank(item.get("text")):
            errors.append(f"fact {index} requires a nonblank text value")
            continue
        confidence = item.get("confidence")
        if confidence not in CONFIDENCES:
            errors.append(f"fact {index} has invalid confidence {confidence!r}; expected high, medium, or low")
            continue
        invalid_optional = [
            optional
            for optional in ("value", "due_date", "locator", "notes")
            if item.get(optional) is not None and not isinstance(item.get(optional), str)
        ]
        if invalid_optional:
            errors.append(f"fact {index} field {invalid_optional[0]} must be a string or null")
            continue
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
    return facts, errors


def normalize_facts(raw, fact_types=None):
    facts, errors = validate_facts(raw, fact_types)
    if errors:
        fail(errors[0])
    return facts


# --------------------------------------------------------------------------- #
# Stateless extraction worker
# --------------------------------------------------------------------------- #

# One document per call, with no conversation carried between them: the whole
# task is this contract plus this document. That keeps the prompt prefix
# byte-stable across every call in a run, so the server reuses its cached prefix
# instead of re-reading a growing conversation.
EXTRACTION_SYSTEM = """You extract structured, actionable facts from one personal-administration document.

Return a JSON array of fact objects and nothing else. Every fact has exactly these fields:
- "fact_type": one of {fact_types}
- "text": the fact in plain language (required, nonblank)
- "value": the normalized detail — the number, amount, phone, email, or address — or null
- "due_date": "YYYY-MM-DD" or null
- "locator": where in the document it appears (page, section, heading), or null
- "confidence": "high", "medium", or "low"
- "notes": optional clarification, or null

Rules:
- Never invent details the document does not support, and never present inference as fact.
- A "value" is copied from the document, not reconstructed. Reference numbers, account numbers,
  phone numbers, and email addresses must appear in the document with the same digits and
  characters written there. If you cannot copy one, use null.
- "due_date" must be a real calendar date written as YYYY-MM-DD. If the document gives a
  relative or partial date you cannot resolve, use null and explain in "notes".
- Organize and record what the document says. Do not give legal, medical, or financial advice.
- Extract what the document actually contains. An empty array is the right answer for a
  document with nothing actionable.
- Work through the fact types in order and ask what the document offers for each one before
  moving on. Do not stop at deadlines, actions, and contacts: administrative documents also
  carry reference numbers, dates, fees, requirements, and missing information you are expected
  to flag.
"""

MAX_DOCUMENT_CHARACTERS = 48000
WHITESPACE_RE = re.compile(r"\s+")
QUOTE_CHARACTERS = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "—": "-", "–": "-"})
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
IDENTIFIER_STRIP_RE = re.compile(r"[^0-9a-z]+")
# Below these, a value is too generic to check without inviting false alarms.
MIN_IDENTIFIER_CHARACTERS = 5
MIN_IDENTIFIER_DIGITS = 4


def extraction_system_prompt(fact_types):
    return EXTRACTION_SYSTEM.format(fact_types=", ".join(fact_types))


def normalize_for_match(text):
    return WHITESPACE_RE.sub(" ", str(text or "").translate(QUOTE_CHARACTERS)).strip().casefold()


def identifier_core(text):
    """The comparable core of an identifier: letters and digits only.

    ``value`` is contractually the *normalized* detail, so it legitimately
    differs from the source in punctuation and spacing — "1234-5678" for
    "1234 5678". Stripping separators on both sides tolerates that while still
    catching a number whose digits are simply not in the document.
    """
    return IDENTIFIER_STRIP_RE.sub("", str(text or "").casefold())


def valid_due_date(value):
    from datetime import date

    if not ISO_DATE_RE.match(str(value or "")):
        return False
    try:
        date.fromisoformat(str(value))
    except ValueError:
        return False
    return True


def fact_violations(facts, source_text):
    """Checks that need no model, run before the one that does.

    Two of them are worth more than any prompt rule. A due date that does not
    parse silently corrupts the deadline checklist, which sorts ISO strings
    lexically. And a reference number, phone number, or email that is not in the
    document is fabricated however plausible it reads — this skill's analogue of
    quote exactness.
    """
    haystack = normalize_for_match(source_text)
    identifiers = identifier_core(source_text)
    violations = []
    for index, fact in enumerate(facts, start=1):
        due_date = fact.get("due_date")
        if due_date and not valid_due_date(due_date):
            violations.append(f"fact {index}: due_date {due_date!r} is not a YYYY-MM-DD calendar date")
        value = fact.get("value")
        if blank(value):
            continue
        candidate = str(value).strip()
        if EMAIL_RE.match(candidate):
            if normalize_for_match(candidate) not in haystack:
                violations.append(f"fact {index}: email {candidate!r} does not appear in the document")
            continue
        # A date-shaped value is a rendering of the source, not a copy of it:
        # "2026-03-15" for "March 15, 2026" is correct and must not be flagged.
        if valid_due_date(candidate):
            continue
        core = identifier_core(candidate)
        if len(core) < MIN_IDENTIFIER_CHARACTERS or sum(character.isdigit() for character in core) < MIN_IDENTIFIER_DIGITS:
            continue
        if core not in identifiers:
            violations.append(f"fact {index}: {fact['fact_type']} value {candidate!r} does not appear in the document")
    return violations


def document_chunks(text, budget=MAX_DOCUMENT_CHARACTERS):
    """Split an oversized document on paragraph boundaries.

    A document too large for one call is covered in pieces rather than
    truncated, so nothing is silently dropped.
    """
    if len(text) <= budget:
        return [text]
    chunks = []
    current = []
    size = 0
    for paragraph in text.split("\n\n"):
        piece = paragraph + "\n\n"
        if size + len(piece) > budget and current:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(piece)
        size += len(piece)
    if current:
        chunks.append("".join(current))
    return chunks


def document_label(document):
    """A human-facing name: the recorded title, else the file name."""
    return document.get("title") or Path(document["sourcePath"]).stem


def extraction_user_prompt(title, instructions, text):
    sections = [f"DOCUMENT TITLE: {title}"]
    if instructions:
        sections.append(f"FOCUS: {instructions}")
    sections.append(f"DOCUMENT:\n{text}")
    return "\n\n".join(sections)


def attempt_extraction(service, messages, fact_types, source_text, args):
    try:
        value, _record = forge_llm.call_json_with_retry(
            service, messages, cache_prompt=args.cache_prompt, timeout=args.request_timeout, task="extract"
        )
    except forge_llm.ChatError as error:
        return [], [str(error)]
    if isinstance(value, dict):
        value = value.get("facts") if isinstance(value.get("facts"), list) else None
    if not isinstance(value, list):
        return [], ["response was not a JSON array of facts"]
    facts, errors = validate_facts(value, fact_types)
    if errors:
        return [], errors
    return facts, fact_violations(facts, source_text)


def extract_document(service, run, document, text, args):
    """Extract one document, with one corrective retry on validation failure."""
    fact_types = run.get("factTypes", FACT_TYPES)
    system = extraction_system_prompt(fact_types)
    instructions = run.get("customInstructions", "")
    facts = []
    for chunk in document_chunks(text):
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": extraction_user_prompt(document_label(document), instructions, chunk)},
        ]
        chunk_facts, errors = attempt_extraction(service, messages, fact_types, chunk, args)
        if errors:
            repair = [
                *messages,
                {"role": "user", "content": "That response was unusable: " + "; ".join(errors[:5]) + ". Return corrected JSON only."},
            ]
            chunk_facts, errors = attempt_extraction(service, repair, fact_types, chunk, args)
            if errors:
                raise UserError("; ".join(errors[:5]))
        facts.extend(chunk_facts)
    return facts


def write_results(run_directory, results):
    text = "".join(json.dumps(result, ensure_ascii=False) + "\n" for result in results)
    run_state.atomic_write_text(run_directory / "facts_results.jsonl", text)


def _record_facts_state(state, document_id, status, note, remaining):
    for item in state.get("items", []):
        if item.get("id") == document_id:
            item["status"] = status
            item["attempts"] = item.get("attempts", 0) + 1
            item["error"] = note if status == "failed" else None
            break
    state["phase"] = "extracting" if remaining else "authoring"
    state["nextAction"] = "next" if remaining else "build"
    return state


def record_facts(run_directory, run, document_id, status, facts, note, supersedes=False):
    """Commit one document's disposition. Shared by the CLI and the worker so
    both go through the same journal and state transition.

    ``supersedes`` marks a deliberate replacement of an earlier result, which is
    how review escalation records a re-extraction.
    """
    result = {
        "documentId": document_id,
        "status": status,
        "facts": facts,
        "note": note,
        "recordedAt": utc_now(),
    }
    if supersedes:
        result["supersedes"] = True
    run_state.append_jsonl_fsync(run_directory / "facts_results.jsonl", result)
    results = load_results(run_directory)
    by_id = {row["documentId"]: row for row in results}
    ordered = [by_id[identifier] for identifier in document_order(run) if identifier in by_id]
    write_results(run_directory, ordered)
    remaining = len(run["documents"]) - len(ordered)
    run_state.update_run_state(
        run_directory,
        lambda state: _record_facts_state(state, document_id, status, note, remaining),
        {"type": "item_recorded", "itemId": document_id, "status": status},
    )
    return remaining


VERIFY_SYSTEM = (
    "You are reviewing facts extracted from personal-administration documents by a faster model\n"
    "without reasoning. For each document you get its source text and every fact extracted from it.\n"
    "Check the facts against the source text.\n"
    "Flag a document when its extraction is actually wrong: an amount, date, or reference that\n"
    "contradicts the document, an obligation or deadline attributed to the wrong party, a value\n"
    "attached to the wrong fact, or inference presented as something the document states.\n"
    "A defensible extraction is 'ok' even if you would have categorized a fact differently; taste is\n"
    "not an error. Do not flag a document for being thin if the source genuinely offers little, and\n"
    "do not flag it for omitting something — you are reviewing what is there, not what is missing."
)
# The reviewer needs the document, not a summary of it: these facts are
# paraphrases with no verbatim quote to check against, so without the source
# text there is nothing to review and every extraction reads as fine.
VERIFY_SOURCE_CHARACTERS = 6000
VERIFY_MAX_FACTS = 60


def verification_payload(document_id, document, result, source_text):
    facts = result.get("facts") or []
    counts = Counter(fact["fact_type"] for fact in facts)
    excerpt = source_text[:VERIFY_SOURCE_CHARACTERS]
    if len(source_text) > VERIFY_SOURCE_CHARACTERS:
        excerpt += "\n[source truncated for review]"
    return {
        "id": document_id,
        "title": document_label(document),
        "source": excerpt,
        "factCount": len(facts),
        "factTypes": dict(counts),
        "facts": [
            {
                "fact_type": fact["fact_type"],
                "text": fact["text"][:300],
                "value": (fact.get("value") or "")[:120],
                "due_date": fact.get("due_date") or "",
                "confidence": fact["confidence"],
            }
            for fact in facts[:VERIFY_MAX_FACTS]
        ],
    }


def verify_extractions(args, run_directory, run):
    """Review every extraction on the thinking model, and redo what it flags."""
    results = {row["documentId"]: row for row in load_results(run_directory)}
    documents = {document["documentId"]: document for document in run["documents"]}
    reviewable = [
        (document_id, result)
        for document_id, result in results.items()
        if result["status"] == "success" and result.get("facts") and document_id in documents
    ]
    if not reviewable:
        return None
    think = forge_llm.resolve_think_or_chat(base_url=args.think_url, model=args.think_model)
    if not think["enabled"]:
        return {"skipped": "no thinking service is configured"}
    sources = {}
    for document_id, _result in reviewable:
        try:
            sources[document_id] = Path(documents[document_id]["sourcePath"]).read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            return {"skipped": f"could not read a source for review: {error}"}
    items = [
        verification_payload(document_id, documents[document_id], result, sources[document_id])
        for document_id, result in reviewable
    ]
    progress(f"verifying {len(items)} extractions on {think['url']}")
    try:
        verdicts = forge_verify.verify_packets(
            think,
            VERIFY_SYSTEM,
            items,
            journal_path=run_directory / "verified.jsonl",
            packet_size=args.verify_packet_size,
            background=getattr(args, "background", False),
            timeout=args.request_timeout,
            progress=progress,
        )
    except forge_verify.VerificationError as error:
        return {"skipped": str(error)}

    flagged = [
        (item, verdicts[item["id"]]["reason"])
        for item in items
        if verdicts.get(item["id"], {}).get("verdict") == forge_verify.VERDICT_FLAG
    ]

    def redo(item, reason):
        document = documents[item["id"]]
        text = Path(document["sourcePath"]).read_text(encoding="utf-8", errors="replace")
        instructions = run.get("customInstructions", "")
        objection = f"A reviewer rejected the previous extraction of this document: {reason}. Extract it again, carefully."
        run_with_note = {**run, "customInstructions": f"{instructions}\n\n{objection}".strip()}
        return extract_document(think, run_with_note, document, text, args)

    escalations = forge_verify.escalate(flagged, redo, journal_path=run_directory / "verified.jsonl", progress=progress)
    for document_id, outcome in escalations.items():
        if outcome.get("resumed"):
            continue  # committed when it was first escalated
        if outcome["ok"]:
            record_facts(run_directory, run, document_id, "success", outcome["value"], "re-extracted with reasoning after review", supersedes=True)
        else:
            record_facts(run_directory, run, document_id, "needs_review", None, f"review flagged this and re-extraction failed: {outcome['detail']}", supersedes=True)
    return forge_verify.summarize(verdicts, escalations)


def command_process(args):
    run_directory = require_run_directory(args.run_directory)
    run = load_run(run_directory)
    verify_hashes(run)
    service = forge_llm.resolve_service("chat", base_url=args.base_url, model=args.model)
    if not service["enabled"]:
        fail("connectedServices.chat is disabled; configure the local chat endpoint before processing")

    processed = 0
    failures = 0
    limit = args.limit if args.limit and args.limit > 0 else None
    while True:
        results = load_results(run_directory)
        document_id = next_pending(run, results)
        if document_id is None or (limit is not None and processed >= limit):
            break
        document = document_by_id(run, document_id)
        position = len(results) + 1
        total = len(run["documents"])
        label = document_label(document)
        progress(f"[{position}/{total}] {label}")
        try:
            text = Path(document["sourcePath"]).read_text(encoding="utf-8", errors="replace")
            facts = extract_document(service, run, document, text, args)
            record_facts(run_directory, run, document_id, "success", facts, None)
            progress(f"[{position}/{total}] {label}: {len(facts)} facts")
        except (UserError, OSError) as error:
            failures += 1
            record_facts(run_directory, run, document_id, "needs_review", None, f"extraction failed: {error}")
            progress(f"[{position}/{total}] {label}: needs review ({error})")
        processed += 1

    verification = verify_extractions(args, run_directory, run) if args.verify else None
    remaining = len(run["documents"]) - len(load_results(run_directory))
    print(
        json.dumps(
            {
                "processed": processed,
                "needsReview": failures,
                "remaining": remaining,
                "verification": verification,
                "nextAction": "process" if remaining else "build",
            },
            ensure_ascii=False,
        )
    )


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
        facts = normalize_facts(raw, run.get("factTypes", FACT_TYPES))
        note = args.note
    else:
        if args.facts_file:
            fail("--facts-file is only valid with --status success")
        if not args.note:
            fail(f"--status {args.status} requires --note")
        facts = None
        note = args.note
    remaining = record_facts(run_directory, run, args.doc_id, args.status, facts, note)
    print(json.dumps({"recorded": args.doc_id, "status": args.status, "facts": len(facts) if facts is not None else 0, "remaining": remaining}))


def verify_hashes(run):
    for document in run["documents"]:
        source = Path(document["sourcePath"])
        if not source.is_file():
            fail(f"source file is missing: {source}")
        if sha256(source) != document["sha256"]:
            fail(f"source file changed after init; refusing to proceed: {source}")


def write_csv(path, columns, rows):
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(columns)
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


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
    def mark_built(state):
        state["phase"] = "authoring"
        state["nextAction"] = "validate"
        return state
    run_state.update_run_state(run_directory, mark_built, {"type": "derived_outputs_built", "files": built})
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
                _facts, fact_errors = validate_facts(result["facts"], run.get("factTypes", FACT_TYPES))
                if fact_errors:
                    errors.append(f"document {document_id} has invalid facts: {fact_errors[0]}")
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
    if result["complete"]:
        def completed(state):
            state["status"] = "complete"
            state["phase"] = "complete"
            state["nextAction"] = None
            return state
        run_state.update_run_state(run_directory, completed, {"type": "run_completed"})


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

    status = subparsers.add_parser("status", help="Report run progress and input drift.")
    status.add_argument("run_directory")
    status.add_argument("--json", action="store_true")
    status.set_defaults(handler=command_status)

    record = subparsers.add_parser("record", help="Append one document's facts or an explicit disposition.")
    record.add_argument("run_directory")
    record.add_argument("--doc-id", required=True)
    record.add_argument("--status", choices=sorted(DOCUMENT_STATUSES), default="success")
    record.add_argument("--facts-file")
    record.add_argument("--note")
    record.set_defaults(handler=command_record)

    process = subparsers.add_parser(
        "process",
        help="Extract every pending document without leaving the script, then have the thinking model review the batch.",
    )
    process.add_argument("run_directory")
    process.add_argument("--limit", type=int, help="stop after this many documents")
    process.add_argument("--base-url", help="chat service (default: connectedServices.chat)")
    process.add_argument("--model")
    process.add_argument("--think-url", help="thinking service used for review (default: connectedServices.think)")
    process.add_argument("--think-model")
    process.add_argument("--no-verify", action="store_true", help="skip the thinking-model review of extractions")
    process.add_argument("--verify-packet-size", type=int, default=15)
    process.add_argument("--no-cache-prompt", action="store_true")
    process.add_argument("--request-timeout", type=float, default=600)
    process.add_argument(
        "--background",
        action="store_true",
        help="run model calls as preemptible background inference; the default is foreground, because a "
        "backgrounded run is preempted by the very interactive session that usually launches it",
    )
    process.set_defaults(handler=command_process)

    build = subparsers.add_parser("build", help="Assemble facts, deadline, and contact tables from staged facts.")
    build.add_argument("run_directory")
    build.set_defaults(handler=command_build)

    validate = subparsers.add_parser("validate", help="Validate run state, facts, deliverables, and provenance.")
    validate.add_argument("run_directory")
    validate.set_defaults(handler=command_validate)
    return root


def main():
    args = parser().parse_args()
    if hasattr(args, "no_cache_prompt"):
        args.cache_prompt = not args.no_cache_prompt
    if hasattr(args, "no_verify"):
        args.verify = not args.no_verify
    args.handler(args)


if __name__ == "__main__":
    main()

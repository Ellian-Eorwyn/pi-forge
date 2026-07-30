#!/usr/bin/env python3

"""Acquire the documents behind a citation list, then name and convert them.

This command owns durable run state, filename derivation, PDF verification, and
publication. Network acquisition lives in `acquire_pdf.mjs` because the settings
ladder and the Playwright endpoint are Node-side; filenames live here because
`vault_schema.safe_title` is the single naming law every vault skill shares and
must not be forked into a second implementation.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))

import citation_naming
import citation_parse
import run_state
from vault_schema import UserError, sha256_file


WORKFLOW = "literature-library"
CONFIG_FILE = "library_config.json"
INDEX_FILE = "library_index.jsonl"
PLAN_FILE = "library_plan.md"
NORMALIZATIONS_FILE = "citation_normalizations.jsonl"
PUBLISH_OPS_FILE = "publish_ops.jsonl"
MANUAL_QUEUE_CSV = "manual_queue.csv"
MANUAL_QUEUE_MD = "manual_queue.md"
ACQUISITION_REPORT = "acquisition_report.md"
PDF_DIR = "pdf"
STAGE_DIR = ".partial"

ACQUIRE_TOOL = Path(__file__).resolve().parent / "acquire_pdf.mjs"

# Calibrated with the owner: runs of 50 or fewer go in one batch, and anything
# larger is split with a pause between batches rather than stopped outright.
DEFAULT_BATCH_SIZE = 50
DEFAULT_BATCH_PAUSE_SECONDS = 30
DEFAULT_HOST_DELAY_MS = 3000

# How many records go to the acquirer before results are verified and published.
# Small on purpose: it bounds how much work an interrupted run repeats, and it is
# what makes progress visible during a long batch.
DEFAULT_CHUNK_SIZE = 8

# Terminal dispositions. `manual` and `deferred-institutional` are terminal on
# purpose: a run that reached them has finished its work honestly, and treating
# them as failures would make a library unimportable from any connection that
# cannot reach the publisher.
TERMINAL_DISPOSITIONS = frozenset({"acquired", "converted", "not-found", "no-candidate", "manual", "deferred-institutional"})
PENDING_DISPOSITIONS = frozenset({"pending", "resolving", "acquiring", "converting"})


def progress(message):
    """Per-record progress on stderr; stdout stays one JSON result."""
    print(message, file=sys.stderr, flush=True)


def emit(payload):
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def record_id(index, stem):
    return f"{index:04d}-{stem[:48].strip()}"


def command_doctor(args):
    """Report what this skill can do here, without touching the network."""
    capabilities = {"pymupdf": False, "pdftotext": False, "node": False}
    try:
        import fitz  # noqa: F401

        capabilities["pymupdf"] = True
    except ImportError:
        pass

    import shutil

    capabilities["pdftotext"] = bool(shutil.which("pdftotext"))
    capabilities["node"] = bool(shutil.which("node"))

    payload = {
        "workflow": WORKFLOW,
        "capabilities": capabilities,
        "formats": {"ris": True, "bibtex": False},
        "notes": [
            "PDF to Markdown is delegated to file-conversion; OCR escalation is delegated to document-ingest.",
            "BibTeX input is not implemented yet; export RIS from your reference manager.",
        ],
        "warnings": [],
    }
    if not capabilities["pymupdf"]:
        payload["warnings"].append("PyMuPDF is unavailable, so downloaded PDFs cannot be structurally verified.")
    if not capabilities["node"]:
        payload["warnings"].append("Node is unavailable, so nothing can be downloaded.")
    emit(payload)


def _plan_markdown(config, records, naming, items):
    """Human-readable plan so the user can see the names before anything runs."""
    lines = [
        f"# Literature library plan: {config['sourceLabel']}",
        "",
        f"- Source: `{config['input']['path']}`",
        f"- Format: {config['input']['format']}, decoded as {config['input']['encodingDetected']}",
        f"- Records: {len(records)}",
        f"- Records with a DOI: {sum(1 for record in records if record['identifiers'].get('doi'))}",
        f"- Records needing review: {sum(1 for item in items if item['needsReview'])}",
        "",
        "Nothing has been downloaded. Filenames below are what acquisition will publish.",
        "",
        "## Planned filenames",
        "",
        "| # | Filename | DOI | Review |",
        "| --- | --- | --- | --- |",
    ]
    for item, built in zip(items, naming):
        doi = item["doi"] or "—"
        review = "; ".join(built["needs_review"]) or ""
        lines.append(f"| {item['recordIndex'] + 1} | `{built['stem']}.pdf` | {doi} | {review} |")

    flagged = [(item, built) for item, built in zip(items, naming) if built["flags"]]
    if flagged:
        lines += ["", "## Naming notes", ""]
        for item, built in flagged:
            lines.append(f"- `{built['stem']}`: {'; '.join(built['flags'])}")
    return "\n".join(lines) + "\n"


def command_parse(args):
    """Parse a citation file and scaffold a resumable acquisition run.

    Deliberately does no network work. The user sees the full set of derived
    filenames, duplicate merges, and review flags before a single request is made.
    """
    source = Path(args.input).expanduser()
    if not source.is_file():
        raise UserError(f"citation file not found: {source}")

    output = Path(args.output).expanduser()
    if (output / "run_state.json").is_file():
        state = run_state.load_run_state(output, WORKFLOW)
        emit({"status": "resumed", "runDirectory": str(output), "phase": state["phase"], "items": len(state["items"])})
        return
    if output.exists() and any(output.iterdir()):
        raise UserError(f"output directory is populated but has no run_state.json: {output}")

    parsed = citation_parse.parse_file(source, repair_replacements=args.repair_replacement_chars)
    records = parsed["records"]
    if not records:
        raise UserError(f"no citation records found in {source}")

    # Duplicate DOIs are the same work cited twice; merging here means the
    # acquisition ladder never fetches one document two times.
    seen_doi = {}
    duplicates = {}
    unique = []
    for record in records:
        doi = record["identifiers"].get("doi")
        if doi and doi in seen_doi:
            duplicates[record["record_index"]] = seen_doi[doi]
            continue
        if doi:
            seen_doi[doi] = record["record_index"]
        unique.append(record)

    naming = citation_naming.derive_stems(unique)

    items = []
    for record, built in zip(unique, naming):
        identifier = record_id(record["record_index"], built["stem"])
        items.append(
            {
                "id": identifier,
                "recordIndex": record["record_index"],
                "stem": built["stem"],
                "doi": record["identifiers"].get("doi"),
                "title": record["canonical_title"],
                "year": built["year"],
                "surname": built["surname"],
                "authorKind": built["author_kind"],
                "accessClass": "unknown",
                "stage": "resolve",
                "disposition": "pending",
                "attempts": 0,
                "titleTruncated": built["title_truncated"],
                "needsReview": built["needs_review"] + record["needs_review"],
                "warnings": record["warnings"],
            }
        )

    # The input snapshot is a list of {path, sha256} because that is the shape
    # `run_state.input_drift` compares; everything descriptive lives in the config.
    snapshot = [{"path": str(source), "sha256": sha256_file(source)}]
    config = {
        "workflow": WORKFLOW,
        "sourceLabel": source.stem,
        "input": {
            "path": str(source),
            "sha256": snapshot[0]["sha256"],
            "format": parsed["format"],
            "encodingDetected": parsed["encoding_detected"],
            "hadBom": parsed["had_bom"],
            "replacementCharCount": parsed["replacement_char_count"],
            "decodeErrorsReplaced": parsed["decode_errors_replaced"],
            "repairedReplacementChars": bool(args.repair_replacement_chars),
        },
        "recordCount": len(records),
        "uniqueRecordCount": len(unique),
        "duplicateRecordCount": len(duplicates),
        "contactEmail": args.contact_email,
    }

    output.mkdir(parents=True, exist_ok=True)
    state = run_state.create_run_state(
        workflow=WORKFLOW,
        command="parse",
        input_config=snapshot,
        options={"contactEmail": args.contact_email, "repairReplacementChars": bool(args.repair_replacement_chars)},
        items=items,
        phase="parsed",
        next_action="acquire",
    )
    state["warnings"] = list(parsed["warnings"])
    run_state.initialize_run_state(output, state)

    run_state.atomic_write_json(output / CONFIG_FILE, config)
    index_path = output / INDEX_FILE
    for record, built, item in zip(unique, naming, items):
        run_state.append_jsonl_fsync(
            index_path,
            {
                "id": item["id"],
                "recordIndex": record["record_index"],
                "stem": built["stem"],
                "pdfFilename": f"{built['stem']}.pdf",
                "markdownFilename": f"{built['stem']}.md",
                "titleFull": record["canonical_title"],
                "titleTruncated": built["title_truncated"],
                "authors": record["authors"],
                "publicationYear": record["publication_year"],
                "publicationDate": record["publication_date"],
                "venueName": record["venue_name"],
                "publisher": record["publisher"],
                "volume": record["volume"],
                "issue": record["issue"],
                "pages": record["pages"],
                "type": record["type"],
                "identifiers": record["identifiers"],
                "urls": record["urls"],
                "keywords": record["keywords"],
                "abstractBest": record["abstract_best"],
                "oaStatus": record["oa_status"],
                "oaLocations": record["oa_locations"],
                "fullTextCandidates": record["full_text_candidates"],
                "duplicateOf": None,
                "namingFlags": built["flags"],
                "needsReview": item["needsReview"],
                "disposition": "pending",
            },
        )
    for duplicate_index, kept_index in duplicates.items():
        run_state.append_jsonl_fsync(
            index_path,
            {
                "recordIndex": duplicate_index,
                "duplicateOf": kept_index,
                "disposition": "duplicate",
                "reason": "another record in this file carries the same DOI",
            },
        )

    for normalization in parsed["normalizations"]:
        run_state.append_jsonl_fsync(output / NORMALIZATIONS_FILE, normalization)

    run_state.atomic_write_text(output / PLAN_FILE, _plan_markdown(config, unique, naming, items))

    emit(
        {
            "status": "parsed",
            "runDirectory": str(output),
            "records": len(records),
            "unique": len(unique),
            "duplicates": len(duplicates),
            "withDoi": sum(1 for record in unique if record["identifiers"].get("doi")),
            "titlesTruncated": sum(1 for built in naming if built["title_truncated"]),
            "needsReview": sum(1 for item in items if item["needsReview"]),
            "replacementCharsRepaired": len(parsed["normalizations"]),
            "warnings": parsed["warnings"],
            "plan": str(output / PLAN_FILE),
            "nextAction": "acquire",
        }
    )


def verify_pdf(path):
    """Confirm a staged download is a usable PDF before it is ever published.

    Checks the magic number first because that is the only trustworthy signal,
    then opens it so a truncated or encrypted file is caught here rather than
    three skills downstream.
    """
    with open(path, "rb") as handle:
        head = handle.read(1024)
    if b"%PDF-" not in head:
        return False, "does not begin with the %PDF magic number"
    try:
        import fitz
    except ImportError:
        return True, "PyMuPDF is unavailable, so only the magic number was checked"
    try:
        with fitz.open(path) as document:
            if document.is_encrypted:
                return False, "the PDF is encrypted"
            if document.page_count < 1:
                return False, "the PDF has no pages"
            pages = document.page_count
    except Exception as error:  # noqa: BLE001 - any failure to open means unusable
        return False, f"PyMuPDF could not open it: {error}"
    return True, f"{pages} page(s)"


def publish_file(output, item_id, staged, destination, staged_sha, kind="pdf"):
    """Move a verified artifact into place under a hash-bound journal entry.

    The operation is recorded before the move and confirmed after, so a restart
    can tell a completed publish from an interrupted one by hashing the
    destination. A destination whose hash does not match is never overwritten.

    `kind` separates the PDF and Markdown operations for one record, so replaying
    one never looks like the other.
    """
    op_id = f"{item_id}:{kind}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if sha256_file(destination) == staged_sha:
            run_state.append_jsonl_fsync(
                output / PUBLISH_OPS_FILE,
                {"opId": op_id, "status": "completed", "to": str(destination), "toSha256": staged_sha, "reconciled": True},
            )
            Path(staged).unlink(missing_ok=True)
            return "already-published", None
        return "blocked", f"{destination.name} already exists with different content"

    run_state.append_jsonl_fsync(
        output / PUBLISH_OPS_FILE,
        {
            "opId": op_id,
            "status": "planned",
            "itemId": item_id,
            "from": str(staged),
            "fromSha256": staged_sha,
            "to": str(destination),
            "toSha256": staged_sha,
            "recordedAt": run_state.utc_now(),
        },
    )
    os.replace(staged, destination)
    run_state.append_jsonl_fsync(
        output / PUBLISH_OPS_FILE,
        {"opId": op_id, "status": "completed", "to": str(destination), "toSha256": staged_sha, "recordedAt": run_state.utc_now()},
    )
    return "published", None


def reconcile_publish_ops(output):
    """Replay an interrupted publish journal without ever overwriting a mismatch."""
    rows, warnings = run_state.read_jsonl_recover_tail(output / PUBLISH_OPS_FILE)
    planned = {}
    completed = set()
    for row in rows:
        if row.get("status") == "planned":
            planned[row["opId"]] = row
        elif row.get("status") == "completed":
            completed.add(row["opId"])

    blocked = []
    for op_id, op in planned.items():
        if op_id in completed:
            continue
        destination = Path(op["to"])
        source = Path(op["from"])
        if destination.is_file():
            if sha256_file(destination) == op["toSha256"]:
                run_state.append_jsonl_fsync(
                    output / PUBLISH_OPS_FILE,
                    {"opId": op_id, "status": "completed", "to": op["to"], "toSha256": op["toSha256"], "reconciled": True},
                )
                source.unlink(missing_ok=True)
                continue
            blocked.append({"opId": op_id, "destination": op["to"], "reason": "destination exists with unexpected content"})
            continue
        if source.is_file() and sha256_file(source) == op["fromSha256"]:
            os.replace(source, destination)
            run_state.append_jsonl_fsync(
                output / PUBLISH_OPS_FILE,
                {"opId": op_id, "status": "completed", "to": op["to"], "toSha256": op["toSha256"], "reconciled": True},
            )
            continue
        warnings.append(f"Publish operation {op_id} was interrupted and its staged file is gone; the record stays pending.")
    return blocked, warnings


def _run_acquire_tool(payload):
    completed = subprocess.run(
        ["node", str(ACQUIRE_TOOL)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )
    if not completed.stdout.strip():
        raise UserError(f"acquirer produced no output: {completed.stderr.strip()[:400]}")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise UserError(f"acquirer returned invalid JSON: {completed.stdout[:300]}") from error
    if result.get("status") != "ok":
        details = "; ".join(entry.get("message", "") for entry in result.get("errors", []))
        raise UserError(f"acquirer failed: {details or 'unknown error'}")
    return result


def _write_index(output, rows):
    """Rewrite the domain manifest atomically after a batch completes."""
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    run_state.atomic_write_text(output / INDEX_FILE, text)


def _manual_queue(output, rows):
    """Write the manual queue as CSV plus a Markdown twin.

    The Markdown twin exists because `vault-connections import-run` only accepts
    `.md` artifacts, and an unacquired-sources list is a deliverable the user
    should be able to import, not a failure log buried in the run directory.
    """
    pending = [row for row in rows if row.get("disposition") in {"manual", "not-found", "no-candidate", "deferred-institutional"}]
    columns = ("id", "disposition", "doi", "title", "accessClass", "stageReached", "bestUrl", "reason")

    def csv_value(value):
        text = "" if value is None else str(value)
        return f'"{text.replace(chr(34), chr(34) * 2)}"' if any(character in text for character in ',"\r\n') else text

    lines = [",".join(columns)]
    for row in pending:
        lines.append(",".join(csv_value(row.get(column)) for column in columns))
    run_state.atomic_write_text(output / MANUAL_QUEUE_CSV, "\n".join(lines) + "\n")

    markdown = [
        "# Sources needing manual retrieval",
        "",
        f"{len(pending)} of {len(rows)} records could not be acquired automatically.",
        "",
    ]
    if pending:
        markdown += ["| Filename | DOI | Why | Best link |", "| --- | --- | --- | --- |"]
        for row in pending:
            doi = row.get("doi") or "—"
            link = row.get("bestUrl") or (f"https://doi.org/{doi}" if row.get("doi") else "—")
            markdown.append(f"| `{row.get('stem')}` | {doi} | {row.get('reason') or row.get('disposition')} | {link} |")
    else:
        markdown.append("Every record was acquired.")
    run_state.atomic_write_text(output / MANUAL_QUEUE_MD, "\n".join(markdown) + "\n")
    return pending


def _acquisition_report(output, rows, hosts, counts):
    lines = [
        "# Acquisition report",
        "",
        f"- Records: {len(rows)}",
    ]
    for disposition, count in sorted(counts.items()):
        lines.append(f"- {disposition}: {count}")
    lines += ["", "## Requests per host", "", "| Host | Requests | Stopped early |", "| --- | --- | --- |"]
    for host in sorted(hosts, key=lambda entry: -entry["requests"]):
        lines.append(f"| {host['host']} | {host['requests']} | {'yes' if host['tripped'] else 'no'} |")
    tripped = [host["host"] for host in hosts if host["tripped"]]
    if tripped:
        lines += [
            "",
            "## Hosts that refused repeatedly",
            "",
            "These hosts returned three consecutive refusals, so this run stopped asking them.",
            "Continuing to retry is what turns a slow run into a blocked institution.",
            "",
        ]
        lines += [f"- {host}" for host in tripped]
    run_state.atomic_write_text(output / ACQUISITION_REPORT, "\n".join(lines) + "\n")


def command_acquire(args):
    """Acquire PDFs for every pending record, in restart-safe batches."""
    output = Path(args.run_directory).expanduser()
    state = run_state.load_run_state(output, WORKFLOW)

    blocked, reconcile_warnings = reconcile_publish_ops(output)
    for warning in reconcile_warnings:
        progress(f"note: {warning}")
    if blocked:
        emit({"status": "blocked", "blockedOperations": blocked, "hint": "resolve the destination conflicts, then rerun acquire"})
        sys.exit(1)

    rows, index_warnings = run_state.read_jsonl_recover_tail(output / INDEX_FILE)
    by_id = {row["id"]: row for row in rows if row.get("id")}

    pending = [item for item in state["items"] if item["disposition"] in PENDING_DISPOSITIONS]
    if args.retry_deferred:
        pending += [item for item in state["items"] if item["disposition"] == "deferred-institutional"]
    if not pending:
        counts, _ = _summarize(state)
        emit({"status": "complete", "runDirectory": str(output), "dispositions": counts, "nextAction": "convert"})
        return

    campus = detect_campus_egress() if args.institutional else {"campusEgress": False, "confidence": "none", "signals": []}
    if args.institutional and not campus["campusEgress"]:
        progress("note: no campus egress detected; closed-access records will be deferred rather than attempted")

    stage_directory = output / STAGE_DIR
    stage_directory.mkdir(parents=True, exist_ok=True)
    pdf_directory = output / PDF_DIR

    batches = [pending[index : index + args.batch_size] for index in range(0, len(pending), args.batch_size)]
    all_hosts = {}
    published = 0

    contact_email = json.loads((output / CONFIG_FILE).read_text(encoding="utf-8"))["contactEmail"]

    for batch_number, batch in enumerate(batches, start=1):
        if batch_number > 1:
            progress(f"pausing {args.batch_pause}s between batches")
            time.sleep(args.batch_pause)
        progress(f"batch {batch_number}/{len(batches)}: {len(batch)} record(s)")

        # The batch is what carries the owner's pause semantics; the chunk is what
        # bounds how much work an interrupted run has to repeat. Committing after
        # every chunk is the run contract's "smallest independently commit-able
        # unit" -- without it, a 49-record batch publishes nothing until the very
        # end and a crash at record 48 redoes all of them.
        chunks = [batch[index : index + args.chunk_size] for index in range(0, len(batch), args.chunk_size)]
        for chunk_number, chunk in enumerate(chunks, start=1):
            progress(f"  chunk {chunk_number}/{len(chunks)} ({len(chunk)} record(s))")
            payload = {
                "contactEmail": contact_email,
                "stageDirectory": str(stage_directory),
                "hostDelayMs": args.host_delay_ms,
                "campusEgress": bool(campus["campusEgress"]),
                "browser": bool(args.allow_browser),
                "records": [
                    {
                        "id": item["id"],
                        "doi": item["doi"],
                        "accessClass": item["accessClass"],
                        "arxivId": by_id.get(item["id"], {}).get("identifiers", {}).get("arxiv_id"),
                        "pmcid": by_id.get(item["id"], {}).get("identifiers", {}).get("pmcid"),
                        "fullTextCandidates": by_id.get(item["id"], {}).get("fullTextCandidates", []),
                    }
                    for item in chunk
                ],
            }
            result = _run_acquire_tool(payload)
            for host in result["data"]["hosts"]:
                existing = all_hosts.setdefault(host["host"], {"host": host["host"], "requests": 0, "tripped": False})
                existing["requests"] += host["requests"]
                existing["tripped"] = existing["tripped"] or host["tripped"]
            published += _record_chunk(output, state, by_id, rows, pdf_directory, result["data"]["results"])
            run_state.update_run_state(
                output,
                lambda current, snapshot=state: {**current, "items": snapshot["items"], "phase": "acquiring"},
                event={"type": "chunk_recorded", "batch": batch_number, "chunk": chunk_number, "records": len(chunk)},
            )

    rows, _ = run_state.read_jsonl_recover_tail(output / INDEX_FILE)
    state = run_state.load_run_state(output, WORKFLOW)
    counts, remaining = _summarize(state)
    hosts = list(all_hosts.values())
    manual = _manual_queue(output, rows)
    _acquisition_report(output, rows, hosts, counts)

    deferred = counts.get("deferred-institutional", 0)
    status = "complete" if remaining == 0 and not deferred else "complete_with_deferrals" if remaining == 0 else "partial"
    run_state.update_run_state(
        output,
        lambda current: {
            **current,
            "phase": "acquired",
            "status": "complete" if remaining == 0 else "running",
            "nextAction": "convert" if remaining == 0 else "acquire",
        },
    )
    emit(
        {
            "status": status,
            "runDirectory": str(output),
            "published": published,
            "dispositions": counts,
            "manualQueue": len(manual),
            "campusEgress": campus,
            "hosts": hosts,
            "trippedHosts": [host["host"] for host in hosts if host["tripped"]],
            "warnings": index_warnings,
            "report": str(output / ACQUISITION_REPORT),
            "nextAction": "convert" if remaining == 0 else "acquire",
        }
    )


def _record_chunk(output, state, by_id, rows, pdf_directory, results):
    """Verify, publish, and commit one chunk of acquisition results.

    Returns how many files were published. Every mutation lands in the domain
    manifest and run state before the caller moves on, so an interrupted run
    repeats at most one chunk.
    """
    published = 0
    for acquired in results:
        item = next(entry for entry in state["items"] if entry["id"] == acquired["id"])
        row = by_id.get(acquired["id"], {})
        item["attempts"] = item.get("attempts", 0) + 1
        item["accessClass"] = acquired["accessClass"]
        row["accessClass"] = acquired["accessClass"]
        row["oaStatus"] = acquired.get("oaStatus")
        row["oaLocations"] = acquired.get("oaLocations", [])
        row["attemptLog"] = acquired.get("attempts", [])
        row["acquisitionWarnings"] = acquired.get("warnings", [])
        row["stageReached"] = acquired.get("stage") or "resolve"
        attempts = acquired.get("attempts", [])
        row["bestUrl"] = next(
            (attempt.get("finalUrl") or attempt.get("url") for attempt in reversed(attempts) if attempt.get("url")),
            None,
        )

        if acquired["disposition"] != "acquired":
            item["disposition"] = acquired["disposition"]
            row["disposition"] = acquired["disposition"]
            row["reason"] = "; ".join(acquired.get("warnings", [])) or acquired["disposition"]
            continue

        staged = Path(acquired["stagedPath"])
        usable, detail = verify_pdf(staged)
        if not usable:
            item["disposition"] = "manual"
            row["disposition"] = "manual"
            row["reason"] = f"downloaded file was not a usable PDF: {detail}"
            staged.unlink(missing_ok=True)
            continue

        outcome, problem = publish_file(
            output, acquired["id"], staged, pdf_directory / row.get("pdfFilename", f"{acquired['id']}.pdf"), acquired["sha256"]
        )
        if outcome == "blocked":
            item["disposition"] = "blocked"
            row["disposition"] = "blocked"
            row["reason"] = problem
            continue
        item["disposition"] = "acquired"
        row["disposition"] = "acquired"
        row["reason"] = None
        row["pdfSha256"] = acquired["sha256"]
        row["pdfBytes"] = acquired["bytes"]
        row["sourceUrl"] = acquired.get("sourceUrl")
        row["finalUrl"] = acquired.get("finalUrl")
        row["pdfPages"] = detail
        published += 1

    # The domain manifest is authoritative for domain data and is rewritten
    # atomically once the chunk's rows are all settled.
    _write_index(output, [by_id.get(row["id"], row) if row.get("id") else row for row in rows])
    return published


MARKDOWN_DIR = "markdown"
CONVERSION_DIR = "conversion"
OCR_DIR = "ocr"

FILE_CONVERSION_TOOL = Path(__file__).resolve().parents[1].parent / "file-conversion" / "scripts" / "file-conversion.py"
DOCUMENT_INGEST_TOOL = Path(__file__).resolve().parents[1].parent / "document-ingest" / "scripts" / "document-ingest.mjs"

# Escalation thresholds. Measured against a real 21-document humanities corpus
# where the sparsest born-digital article still carried ~1450 alphanumeric
# characters per page, so 200 is far below anything that extracts cleanly and
# will not pull a readable PDF into an unnecessary OCR pass.
MIN_ALNUM_CHARS_PER_PAGE = 200
MAX_EMPTY_PAGE_RATIO = 0.25

# Institutional repositories staple a coversheet onto author-accepted
# manuscripts. It is not part of the article, and downstream evidence extraction
# would otherwise quote a university's terms of use as if it were the author.
COVERSHEET_MARKERS = (
    "research portal",
    "repository",
    "eprints",
    "author accepted manuscript",
    "version of record",
    "link to published version",
    "terms of use",
    "citing this paper",
    "downloaded from",
    "this is the peer reviewed version",
    "general rights",
)


def probe_pdf(path):
    """Measure a PDF's extractable text to choose a conversion route.

    Routes on measurement rather than on another skill's warning text, so a
    change to file-conversion's wording cannot silently reroute every document.
    """
    try:
        import fitz
    except ImportError:
        return {"pages": None, "alnumPerPage": None, "emptyRatio": None, "needsOcr": False, "reason": "PyMuPDF unavailable"}
    try:
        with fitz.open(path) as document:
            if document.is_encrypted:
                return {"pages": None, "alnumPerPage": 0, "emptyRatio": 1.0, "needsOcr": True, "reason": "encrypted"}
            pages = document.page_count
            texts = [page.get_text().strip() for page in document]
    except Exception as error:  # noqa: BLE001 - an unopenable PDF is an OCR candidate
        return {"pages": None, "alnumPerPage": 0, "emptyRatio": 1.0, "needsOcr": True, "reason": f"unreadable: {error}"}

    if pages < 1:
        return {"pages": 0, "alnumPerPage": 0, "emptyRatio": 1.0, "needsOcr": True, "reason": "no pages"}
    alnum = sum(sum(character.isalnum() for character in text) for text in texts)
    empty = sum(1 for text in texts if len(text) < 20)
    per_page = alnum / pages
    empty_ratio = empty / pages
    reasons = []
    if per_page < MIN_ALNUM_CHARS_PER_PAGE:
        reasons.append(f"only {per_page:.0f} alphanumeric characters per page")
    if empty_ratio > MAX_EMPTY_PAGE_RATIO:
        reasons.append(f"{empty_ratio:.0%} of pages have no extractable text")
    return {
        "pages": pages,
        "alnumPerPage": round(per_page, 1),
        "emptyRatio": round(empty_ratio, 3),
        "needsOcr": bool(reasons),
        "reason": "; ".join(reasons) or "text extracts cleanly",
    }


def detect_coversheet(body):
    """Report how many leading lines look like a repository coversheet.

    Detection only. The text is never removed: a false positive would delete the
    opening of an article, and the caller can see exactly what was flagged.
    """
    lines = body.split("\n")
    window = min(len(lines), 60)
    hits = []
    for index in range(window):
        lowered = lines[index].lower()
        for marker in COVERSHEET_MARKERS:
            if marker in lowered:
                hits.append((index, marker))
                break
    if len(hits) < 3:
        return None
    return {"lastMarkerLine": hits[-1][0] + 1, "markers": sorted({marker for _, marker in hits})}


def _conversion_warnings(conversion_directory):
    """Map source filename to the warnings file-conversion recorded for it."""
    warnings_path = Path(conversion_directory) / "warnings.md"
    if not warnings_path.is_file():
        return {}
    grouped = {}
    current = None
    for line in warnings_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            current = line[3:].split(" -> ")[0].strip()
            grouped[current] = []
        elif line.startswith("- ") and current:
            grouped[current].append(line[2:].strip())
    return grouped


def _yaml_scalar(value):
    # Integers stay unquoted so a vault reads them as numbers rather than as
    # text that happens to look like a year.
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _markdown_document(row, body, method, probe, warnings, coversheet):
    """Assemble the published Markdown: real metadata, real title, real body.

    file-conversion names its output with `safe_stem`, which turns
    `Author - Year - Title` into `Author---Year---Title` and uses it as the H1.
    That is fine for a conversion run and wrong for a library, so the heading is
    replaced with the work's actual title here.
    """
    authors = [author.get("name") or " ".join(filter(None, (author.get("given"), author.get("family")))) for author in row.get("authors") or []]
    identifiers = row.get("identifiers") or {}
    front = [
        "---",
        f"title: {_yaml_scalar(row.get('titleFull') or row.get('stem'))}",
    ]
    if authors:
        front.append("authors:")
        front += [f"  - {_yaml_scalar(name)}" for name in authors]
    for key, value in (
        ("year", row.get("publicationYear")),
        ("publication_date", row.get("publicationDate")),
        ("venue", row.get("venueName")),
        ("publisher", row.get("publisher")),
        ("doi", identifiers.get("doi")),
        ("source_url", row.get("sourceUrl")),
        ("access_class", row.get("accessClass")),
        ("acquisition_stage", row.get("stageReached")),
        ("oa_status", row.get("oaStatus")),
        ("pdf_sha256", row.get("pdfSha256")),
        ("pdf_pages", probe.get("pages")),
        ("markdown_method", method),
    ):
        if value not in (None, ""):
            front.append(f"{key}: {_yaml_scalar(value)}")
    review = list(warnings)
    if coversheet:
        review.append(
            f"A repository coversheet appears to occupy the first {coversheet['lastMarkerLine']} lines "
            f"({', '.join(coversheet['markers'])}); it was left in place, not removed."
        )
    if review:
        front.append("needs_review:")
        front += [f"  - {_yaml_scalar(entry)}" for entry in review]
    front.append("---")

    # Drop file-conversion's synthesized H1 so the real title is the only one.
    lines = body.split("\n")
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start < len(lines) and lines[start].startswith("# "):
        start += 1
        while start < len(lines) and not lines[start].strip():
            start += 1
    cleaned = "\n".join(lines[start:]).strip()

    heading = row.get("titleFull") or row.get("stem")
    return "\n".join(front) + f"\n\n# {heading}\n\n{cleaned}\n"


def _run_child(command, label):
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise UserError(f"{label} failed: {(completed.stderr or completed.stdout).strip()[:400]}")
    return completed


def command_convert(args):
    """Convert every acquired PDF to Markdown and publish it beside its source."""
    output = Path(args.run_directory).expanduser()
    state = run_state.load_run_state(output, WORKFLOW)

    blocked, reconcile_warnings = reconcile_publish_ops(output)
    if blocked:
        emit({"status": "blocked", "blockedOperations": blocked})
        sys.exit(1)

    rows, index_warnings = run_state.read_jsonl_recover_tail(output / INDEX_FILE)
    by_id = {row["id"]: row for row in rows if row.get("id")}
    pdf_directory = output / PDF_DIR

    pending = [
        item
        for item in state["items"]
        if item["disposition"] == "acquired" and (args.refresh_all or not by_id.get(item["id"], {}).get("markdownSha256"))
    ]
    if not pending:
        emit({"status": "complete", "runDirectory": str(output), "converted": 0, "nextAction": "import"})
        return

    structural = []
    ocr = []
    for item in pending:
        row = by_id.get(item["id"], {})
        source = pdf_directory / row.get("pdfFilename", "")
        if not source.is_file():
            continue
        probe = probe_pdf(source)
        row["textProbe"] = probe
        (ocr if probe["needsOcr"] else structural).append((item, row, source, probe))

    progress(f"{len(structural)} document(s) via file-conversion, {len(ocr)} via document-ingest OCR")
    produced = {}

    if structural:
        conversion_directory = output / CONVERSION_DIR
        _run_child(
            [
                sys.executable,
                str(FILE_CONVERSION_TOOL),
                "convert",
                *[str(source) for _, _, source, _ in structural],
                "--to",
                "md",
                "--output",
                str(conversion_directory),
            ],
            "file-conversion",
        )
        child_state = json.loads((conversion_directory / "run_state.json").read_text(encoding="utf-8"))
        warnings_by_source = _conversion_warnings(conversion_directory)
        for child_item in child_state["items"]:
            if not child_item.get("outputPath"):
                continue
            produced[Path(child_item["path"]).name] = {
                "path": conversion_directory / child_item["outputPath"],
                "method": "file-conversion-structural",
                "warnings": warnings_by_source.get(Path(child_item["path"]).name, []),
                "childStatus": child_item.get("status"),
                "child": str(conversion_directory),
            }

    for _, _, source, _ in ocr:
        target = output / OCR_DIR / source.stem
        # `--ocr-backend local` is mandatory: document-ingest otherwise tries a
        # remote OCR service first, which would ship the document off this machine.
        _run_child(
            [
                "node",
                str(DOCUMENT_INGEST_TOOL),
                "prepare",
                str(source),
                "--output",
                str(target),
                "--ocr",
                "auto",
                "--ocr-backend",
                "local",
            ],
            "document-ingest",
        )
        found = sorted(target.rglob("document.md"))
        if found:
            produced[source.name] = {
                "path": found[0],
                "method": "document-ingest-ocr",
                "warnings": ["Text was recovered by OCR and is not guaranteed verbatim."],
                "childStatus": "ocr",
                "child": str(target),
            }

    converted = 0
    flagged = 0
    markdown_directory = output / MARKDOWN_DIR
    for item, row, source, probe in structural + ocr:
        result = produced.get(source.name)
        if not result or not Path(result["path"]).is_file():
            row["markdownStatus"] = "failed"
            row["reason"] = "conversion produced no Markdown"
            continue

        body = Path(result["path"]).read_text(encoding="utf-8")
        coversheet = detect_coversheet(body)
        document = _markdown_document(row, body, result["method"], probe, result["warnings"], coversheet)

        staged = output / STAGE_DIR / f"{item['id']}.md"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text(document, encoding="utf-8")
        digest = sha256_file(staged)
        outcome, problem = publish_file(
            output, item["id"], staged, markdown_directory / row.get("markdownFilename", f"{item['id']}.md"), digest, kind="md"
        )
        if outcome == "blocked":
            row["markdownStatus"] = "blocked"
            row["reason"] = problem
            continue

        row["markdownStatus"] = "converted"
        row["markdownSha256"] = digest
        row["markdownMethod"] = result["method"]
        row["markdownChildRun"] = result["child"]
        row["conversionWarnings"] = result["warnings"]
        row["coversheetSuspected"] = coversheet
        item["disposition"] = "converted"
        converted += 1
        if coversheet or result["warnings"]:
            flagged += 1

    _write_index(output, [by_id.get(row["id"], row) if row.get("id") else row for row in rows])
    run_state.update_run_state(
        output,
        lambda current, snapshot=state: {
            **current,
            "items": snapshot["items"],
            "phase": "converted",
            "children": {**current.get("children", {}), "conversion": str(output / CONVERSION_DIR)},
            "nextAction": "import",
        },
        event={"type": "conversion_recorded", "converted": converted},
    )
    emit(
        {
            "status": "complete",
            "runDirectory": str(output),
            "converted": converted,
            "viaStructural": len(structural),
            "viaOcr": len(ocr),
            "flaggedForReview": flagged,
            "coversheetsDetected": sum(1 for row in rows if row.get("coversheetSuspected")),
            "markdownDirectory": str(markdown_directory),
            "warnings": index_warnings + reconcile_warnings,
            "nextAction": "import",
        }
    )


RETRYABLE_DISPOSITIONS = frozenset({"manual", "not-found", "no-candidate", "blocked", "deferred-institutional"})


def command_retry(args):
    """Requeue terminal failures so a later attempt can pick them up.

    Deliberately explicit, per the run contract: a permanent failure is never
    retried on its own, because silently re-requesting a host that already
    refused us is the behavior that gets an institution blocked.
    """
    output = Path(args.run_directory).expanduser()
    state = run_state.load_run_state(output, WORKFLOW)

    if args.item:
        targets = [item for item in state["items"] if item["id"] == args.item]
        if not targets:
            raise UserError(f"no item with id {args.item}")
    elif args.disposition:
        targets = [item for item in state["items"] if item["disposition"] == args.disposition]
    else:
        targets = [item for item in state["items"] if item["disposition"] in RETRYABLE_DISPOSITIONS]

    requeued = [item["id"] for item in targets]
    for item in targets:
        item["disposition"] = "pending"

    run_state.update_run_state(
        output,
        lambda current, snapshot=state: {**current, "items": snapshot["items"], "status": "running", "nextAction": "acquire"},
        event={"type": "items_requeued", "count": len(requeued)},
    )
    emit({"status": "requeued", "count": len(requeued), "items": requeued[:20], "nextAction": "acquire"})


def detect_campus_egress():
    """Decide whether this machine currently egresses from the institution.

    Requires two independent signals, because either alone has a false positive
    that matters: a search domain can linger after a tunnel drops, and a default
    route on a virtual interface describes any VPN at all. Tailscale is
    explicitly excluded -- it is a VPN, but not this one, and on this machine it
    holds a default route and a CGNAT prefix that would otherwise read as campus.

    This is a gate, not an oracle. The acquisition result is ground truth: a
    request that returns a landing page instead of a PDF means access is absent
    regardless of what the route table says.
    """
    import re
    import shutil

    domains = []
    signals = []
    institution_domains = ("ucdavis.edu",)
    excluded_domains = (".ts.net", ".local", ".arpa")

    if shutil.which("scutil"):
        completed = subprocess.run(["scutil", "--dns"], capture_output=True, text=True, check=False)
        for match in re.finditer(r"(?:search domain\[\d+\]|domain)\s*:\s*(\S+)", completed.stdout):
            domain = match.group(1).lower()
            if any(domain.endswith(excluded) for excluded in excluded_domains):
                continue
            domains.append(domain)
    institution_domain = next(
        (domain for domain in domains if any(domain == name or domain.endswith(f".{name}") for name in institution_domains)),
        None,
    )
    if institution_domain:
        signals.append(f"DNS search domain {institution_domain}")

    tunnel_default = None
    if shutil.which("netstat"):
        completed = subprocess.run(["netstat", "-rn", "-f", "inet"], capture_output=True, text=True, check=False)
        cgnat_interfaces = set()
        for line in completed.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 4 and fields[0].startswith("100.64"):
                cgnat_interfaces.add(fields[-1])
        for line in completed.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 4 and fields[0] == "default":
                interface = fields[-1]
                # Counting utun interfaces is worthless as a signal: unrelated
                # software leaves many of them behind.
                if interface.startswith("utun") and interface not in cgnat_interfaces:
                    tunnel_default = interface
                    break
    if tunnel_default:
        signals.append(f"default route on tunnel interface {tunnel_default}")

    campus = bool(institution_domain and tunnel_default)
    return {
        "campusEgress": campus,
        "confidence": "high" if campus else "none",
        "signals": signals,
        "checkedAt": run_state.utc_now(),
    }


def command_detect_egress(args):
    emit(detect_campus_egress())


def _summarize(state):
    counts = {}
    for item in state["items"]:
        counts[item["disposition"]] = counts.get(item["disposition"], 0) + 1
    pending = sum(count for disposition, count in counts.items() if disposition in PENDING_DISPOSITIONS)
    return counts, pending


def command_status(args):
    output = Path(args.run_directory).expanduser()
    state = run_state.load_run_state(output, WORKFLOW)
    counts, pending = _summarize(state)
    config = json.loads((output / CONFIG_FILE).read_text(encoding="utf-8"))
    source = Path(config["input"]["path"])
    current = [{"path": str(source), "sha256": sha256_file(source)}] if source.is_file() else []
    drift = run_state.input_drift(state.get("input") or [], current)
    emit(
        {
            "workflow": WORKFLOW,
            "runDirectory": str(output),
            "phase": state["phase"],
            "status": state["status"],
            "sourceLabel": config["sourceLabel"],
            "items": len(state["items"]),
            "dispositions": counts,
            "pending": pending,
            "nextAction": state.get("nextAction"),
            "inputDrift": drift,
            "warnings": state.get("warnings", []),
        }
    )


def command_validate(args):
    """Machine-readable gate, also consumed by vault-connections import-run.

    Reports `complete: true` once every record has reached a terminal
    disposition, which includes `manual` and `deferred-institutional`. That is a
    deliberate reading of the shared validator contract: those two dispositions
    mean the run did everything it could, and refusing to import until every
    paywalled article is on disk would make the library unusable from any
    connection that cannot reach the publisher. The deferrals are carried as
    warnings so the caller still sees them.
    """
    output = Path(args.run_directory).expanduser()
    errors = []
    warnings = []
    try:
        state = run_state.load_run_state(output, WORKFLOW)
    except ValueError as error:
        emit({"valid": False, "complete": False, "errors": [str(error)], "warnings": []})
        sys.exit(1)

    for required in (CONFIG_FILE, INDEX_FILE, PLAN_FILE):
        if not (output / required).is_file():
            errors.append(f"missing required artifact: {required}")

    rows, tail_warnings = run_state.read_jsonl_recover_tail(output / INDEX_FILE)
    warnings.extend(tail_warnings)
    indexed = {row["id"] for row in rows if row.get("id")}
    for item in state["items"]:
        if item["id"] not in indexed:
            errors.append(f"item {item['id']} is missing from {INDEX_FILE}")

    # A published PDF is evidence, and everything downstream cites it by hash.
    # Verify the files still match the journal rather than trusting the state file.
    for row in rows:
        if row.get("pdfSha256"):
            published = output / PDF_DIR / row["pdfFilename"]
            if not published.is_file():
                errors.append(f"{row['pdfFilename']} is recorded as acquired but is missing from {PDF_DIR}/")
            elif sha256_file(published) != row["pdfSha256"]:
                errors.append(f"{row['pdfFilename']} no longer matches the hash recorded when it was published")
        if row.get("markdownSha256"):
            document = output / MARKDOWN_DIR / row["markdownFilename"]
            if not document.is_file():
                errors.append(f"{row['markdownFilename']} is recorded as converted but is missing from {MARKDOWN_DIR}/")
            elif sha256_file(document) != row["markdownSha256"]:
                errors.append(f"{row['markdownFilename']} no longer matches the hash recorded when it was published")

    counts, pending = _summarize(state)
    deferred = counts.get("deferred-institutional", 0)
    manual = counts.get("manual", 0)
    if deferred:
        warnings.append(f"{deferred} record(s) need an institutional connection and were deferred.")
    if manual:
        warnings.append(f"{manual} record(s) could not be acquired automatically and are queued for manual retrieval.")

    complete = not errors and pending == 0
    payload = {
        "valid": not errors,
        "complete": complete,
        "errors": errors,
        "warnings": warnings,
        "dispositions": counts,
        "pending": pending,
    }
    emit(payload)
    if errors:
        sys.exit(1)


def parser():
    root = argparse.ArgumentParser(
        description="Acquire, name, and convert the documents behind a citation list into a reviewable library."
    )
    subparsers = root.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Report local capabilities without touching the network.")
    doctor.add_argument("--json", action="store_true", help="Accepted for symmetry; output is always JSON.")
    doctor.set_defaults(handler=command_doctor)

    parse = subparsers.add_parser("parse", help="Parse a citation file and scaffold a resumable acquisition run.")
    parse.add_argument("input", help="Path to a .ris citation file.")
    parse.add_argument("--output", required=True, help="Run directory to create or resume.")
    parse.add_argument(
        "--contact-email",
        required=True,
        help="Contact address sent to Unpaywall and in the User-Agent. Required by the Unpaywall API.",
    )
    parse.add_argument(
        "--repair-replacement-chars",
        action="store_true",
        help="Guess a right single quote for a letter-flanked U+FFFD, recording every substitution.",
    )
    parse.set_defaults(handler=command_parse)

    acquire = subparsers.add_parser("acquire", help="Acquire PDFs for pending records in restart-safe batches.")
    acquire.add_argument("run_directory")
    acquire.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Records per batch before pausing. Default {DEFAULT_BATCH_SIZE}.",
    )
    acquire.add_argument(
        "--batch-pause",
        type=int,
        default=DEFAULT_BATCH_PAUSE_SECONDS,
        help=f"Seconds to pause between batches. Default {DEFAULT_BATCH_PAUSE_SECONDS}.",
    )
    acquire.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help=f"Records handed to the acquirer before results are published. Default {DEFAULT_CHUNK_SIZE}.",
    )
    acquire.add_argument(
        "--host-delay-ms",
        type=int,
        default=DEFAULT_HOST_DELAY_MS,
        help=f"Minimum milliseconds between requests to one host. Default {DEFAULT_HOST_DELAY_MS}; the acquirer floors it at 2000.",
    )
    acquire.add_argument(
        "--allow-browser",
        action="store_true",
        help="After the direct paths fail, retry open-access records through the browser service. Never used for institutional records.",
    )
    acquire.add_argument(
        "--institutional",
        action="store_true",
        help="Attempt closed-access records too, if this machine egresses from the institution's network.",
    )
    acquire.add_argument(
        "--retry-deferred",
        action="store_true",
        help="Requeue records previously deferred for lack of an institutional connection.",
    )
    acquire.set_defaults(handler=command_acquire)

    convert = subparsers.add_parser("convert", help="Convert acquired PDFs to Markdown with bibliographic frontmatter.")
    convert.add_argument("run_directory")
    convert.add_argument(
        "--refresh-all",
        action="store_true",
        help="Reconvert documents that already have published Markdown.",
    )
    convert.set_defaults(handler=command_convert)

    retry = subparsers.add_parser("retry", help="Requeue terminal failures for another acquisition attempt.")
    retry.add_argument("run_directory")
    retry_group = retry.add_mutually_exclusive_group()
    retry_group.add_argument("--item", help="Requeue one record by id.")
    retry_group.add_argument("--disposition", help="Requeue every record with this disposition.")
    retry_group.add_argument("--all-failed", action="store_true", help="Requeue every retryable failure. The default.")
    retry.set_defaults(handler=command_retry)

    detect = subparsers.add_parser("detect-egress", help="Report whether this machine currently egresses from the institution.")
    detect.add_argument("--json", action="store_true", help="Accepted for symmetry; output is always JSON.")
    detect.set_defaults(handler=command_detect_egress)

    status = subparsers.add_parser("status", help="Report durable run progress and input drift.")
    status.add_argument("run_directory")
    status.add_argument("--json", action="store_true", help="Accepted for symmetry; output is always JSON.")
    status.set_defaults(handler=command_status)

    validate = subparsers.add_parser("validate", help="Machine-readable quality gate for this run.")
    validate.add_argument("run_directory")
    validate.add_argument("--json", action="store_true", help="Accepted for symmetry; output is always JSON.")
    validate.add_argument("--read-only", action="store_true", help="Guarantee no writes; the default behavior.")
    validate.set_defaults(handler=command_validate)

    return root


def main():
    args = parser().parse_args()
    try:
        args.handler(args)
    except UserError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
    except citation_parse.CitationParseError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

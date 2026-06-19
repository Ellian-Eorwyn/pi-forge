#!/usr/bin/env python3

import argparse
import csv
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path


RUN_SCHEMA_VERSION = 1
PLACEHOLDER = "<!-- TODO: author this section -->"
RENDER_FORMATS = {"docx", "html"}

# Run-internal machinery that should not be registered as a deliverable source.
SKIP_FILENAMES = {"run_config.json", "run.json", "source_manifest.json"}
SKIP_SUFFIXES = {".jsonl"}
SKIP_DIRECTORIES = {"working", "converted"}

# Best-effort tagging of recognized forge artifacts: filename -> (type, skill).
KNOWN_ARTIFACTS = {
    "evidence_table.csv": ("evidence_table", "literature-extraction"),
    "evidence_table.xlsx": ("evidence_table", "literature-extraction"),
    "methods_matrix.csv": ("methods_matrix", "literature-extraction"),
    "literature_summary.md": ("literature_summary", "literature-extraction"),
    "claims_matrix.md": ("claims_matrix", "literature-extraction"),
    "research_gaps.md": ("research_gaps", "literature-extraction"),
    "citation_notes.md": ("citation_notes", "literature-extraction"),
    "documents.csv": ("documents_manifest", "literature-extraction"),
    "analysis.md": ("analysis", "spreadsheet-analysis"),
    "data_profile.md": ("data_profile", "spreadsheet-analysis"),
    "cleaned.csv": ("cleaned_data", "spreadsheet-analysis"),
    "cleaned.xlsx": ("cleaned_data", "spreadsheet-analysis"),
    "summary_tables.xlsx": ("summary_tables", "spreadsheet-analysis"),
    "enriched.csv": ("enriched_data", "spreadsheet-analysis"),
    "enriched.xlsx": ("enriched_data", "spreadsheet-analysis"),
    "transform_log.md": ("transform_log", "spreadsheet-analysis"),
    "summary.md": ("summary", "transcript-cleanup"),
    "cleaned_transcript.md": ("cleaned_transcript", "transcript-cleanup"),
    "action_items.md": ("action_items", "transcript-cleanup"),
    "decisions.md": ("decisions", "transcript-cleanup"),
    "open_questions.md": ("open_questions", "transcript-cleanup"),
    "key_quotes.md": ("key_quotes", "transcript-cleanup"),
    "document.md": ("document", "document-ingest"),
    "metadata.json": ("metadata", "document-ingest"),
    "extraction_report.md": ("extraction_report", "document-ingest"),
    "manifest.csv": ("manifest", "document-ingest"),
    "web_manifest.csv": ("web_manifest", "web-collection"),
    "web_manifest.json": ("web_manifest", "web-collection"),
    "collection_report.md": ("collection_report", "web-collection"),
    "failed_downloads.csv": ("failed_downloads", "web-collection"),
}

# Authored deliverables per detail level. sources.md is script-generated and is
# added to every run; assumptions_and_limits.md is authored in every level.
PRESETS = {
    "brief": ["executive_summary.md", "assumptions_and_limits.md"],
    "memo": ["briefing.md", "assumptions_and_limits.md", "review_notes.md"],
    "full": ["report.md", "executive_summary.md", "assumptions_and_limits.md", "review_notes.md"],
    "outline": ["annotated_outline.md", "slide_outline.md", "assumptions_and_limits.md"],
}

DELIVERABLE_TEMPLATES = {
    "report.md": [
        "# {title}",
        "",
        PLACEHOLDER,
        "",
        "## Summary",
        "",
        "## Background",
        "",
        "## Findings",
        "",
        "## Discussion",
        "",
        "## Recommendations",
        "",
        "## Sources",
        "",
        "See `sources.md`. Cite sources by their manifest id.",
        "",
    ],
    "executive_summary.md": [
        "# Executive Summary: {title}",
        "",
        PLACEHOLDER,
        "",
        "## Key Points",
        "",
        "## What This Means",
        "",
        "## Caveats",
        "",
        "See `assumptions_and_limits.md`.",
        "",
    ],
    "briefing.md": [
        "# Briefing: {title}",
        "",
        PLACEHOLDER,
        "",
        "## Situation",
        "",
        "## Key Points",
        "",
        "## Recommended Next Steps",
        "",
        "## Open Questions",
        "",
    ],
    "annotated_outline.md": [
        "# Annotated Outline: {title}",
        "",
        PLACEHOLDER,
        "",
        "<!-- One bullet per section with a sentence on intended content and the",
        "sources (by manifest id) it draws on. -->",
        "",
    ],
    "slide_outline.md": [
        "# Slide Outline: {title}",
        "",
        PLACEHOLDER,
        "",
        "<!-- One '## Slide N: <title>' per slide, with talking-point bullets. -->",
        "",
    ],
    "review_notes.md": [
        "# Review Notes: {title}",
        "",
        PLACEHOLDER,
        "",
        "## Decisions Pending Review",
        "",
        "## Items Needing Verification",
        "",
        "## Known Gaps",
        "",
    ],
    "assumptions_and_limits.md": [
        "# Assumptions and Limits: {title}",
        "",
        PLACEHOLDER,
        "",
        "## Assumptions",
        "",
        "## Limitations",
        "",
        "## Unresolved Questions",
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


def openpyxl_available():
    return importlib.util.find_spec("openpyxl") is not None


def pandoc_version():
    if shutil.which("pandoc") is None:
        return None
    try:
        result = subprocess.run(["pandoc", "--version"], capture_output=True, text=True, check=False)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").splitlines()[0].strip() or "available"


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


def iter_input_files(root):
    if root.is_file():
        yield root
        return
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if any(part in SKIP_DIRECTORIES for part in relative.parts[:-1]):
            continue
        if path.name in SKIP_FILENAMES or path.suffix.lower() in SKIP_SUFFIXES:
            continue
        current = root
        linked = False
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                linked = True
                break
        if linked:
            continue
        yield path


def discover_inputs(raw_inputs):
    seen = set()
    records = []
    used_ids = set()
    for raw in raw_inputs:
        root = Path(raw).expanduser().resolve()
        if not root.exists():
            fail(f"input does not exist: {root}")
        if root.is_symlink():
            fail(f"input is a symlink: {root}")
        for path in iter_input_files(root):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            source_type, producing_skill = KNOWN_ARTIFACTS.get(path.name, ("generic", None))
            digest = sha256(path)
            base = f"{path.stem or 'source'}-{digest[:12]}"
            source_id = base
            suffix = 1
            while source_id in used_ids:
                suffix += 1
                source_id = f"{base}-{suffix}"
            used_ids.add(source_id)
            records.append(
                {
                    "sourceId": source_id,
                    "path": str(resolved),
                    "sha256": digest,
                    "sizeBytes": path.stat().st_size,
                    "sourceType": source_type,
                    "producingSkill": producing_skill,
                }
            )
    if not records:
        fail("no usable input files were found")
    return records


def write_sources_md(run_directory, title, inputs):
    lines = [
        f"# Sources: {title}",
        "",
        "Deterministically generated from `source_manifest.json`. Cite sources by",
        "id in authored deliverables.",
        "",
        "| ID | Type | Producing skill | Size (bytes) | SHA-256 | Path |",
        "|---|---|---|---:|---|---|",
    ]
    for record in inputs:
        skill = record["producingSkill"] or "—"
        lines.append(
            f"| `{record['sourceId']}` | {record['sourceType']} | {skill} | "
            f"{record['sizeBytes']} | `{record['sha256'][:12]}…` | `{record['path']}` |"
        )
    lines.append("")
    (run_directory / "sources.md").write_text("\n".join(lines), encoding="utf-8")


def scaffold_deliverables(run_directory, title, deliverables):
    created = []
    for name in deliverables:
        path = run_directory / name
        if path.exists():
            continue
        template = DELIVERABLE_TEMPLATES[name]
        body = "\n".join(line.replace("{title}", title) for line in template) + "\n"
        path.write_text(body, encoding="utf-8")
        created.append(name)
    return created


def command_doctor(args):
    xlsx = openpyxl_available()
    version = None
    if xlsx:
        import openpyxl

        version = openpyxl.__version__
    pandoc = pandoc_version()
    result = {
        "python": sys.version.split()[0],
        "markdown": True,
        "xlsx": xlsx,
        "openpyxlVersion": version,
        "pandoc": pandoc is not None,
        "pandocVersion": pandoc,
        "remediation": [],
    }
    if not xlsx:
        result["remediation"].append("Install openpyxl for the active Python 3 environment to assemble tables.xlsx.")
    if pandoc is None:
        result["remediation"].append("Install Pandoc to render DOCX and HTML (macOS: brew install pandoc; Debian/Ubuntu: apt install pandoc).")
    if args.json:
        print(json.dumps(result, indent=2))
        return
    print(f"Python: {result['python']}")
    print("Markdown deliverables: available")
    print(f"XLSX tables: {'available via openpyxl ' + version if xlsx else 'unavailable'}")
    print(f"DOCX/HTML rendering: {'available via ' + pandoc if pandoc else 'unavailable'}")
    for action in result["remediation"]:
        print(f"Action: {action}")


def command_init(args):
    if args.detail not in PRESETS:
        fail(f"unknown detail level: {args.detail}")
    inputs = discover_inputs(args.inputs)
    title = args.title or "Untitled Report"
    deliverables = PRESETS[args.detail]
    output = require_new_directory(args.output)
    manifest = {
        "schemaVersion": RUN_SCHEMA_VERSION,
        "createdAt": utc_now(),
        "inputs": inputs,
    }
    (output / "source_manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    run = {
        "schemaVersion": RUN_SCHEMA_VERSION,
        "createdAt": utc_now(),
        "title": title,
        "detail": args.detail,
        "deliverables": deliverables,
        "generated": ["sources.md"],
    }
    (output / "run_config.json").write_text(json.dumps(run, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_sources_md(output, title, inputs)
    created = scaffold_deliverables(output, title, deliverables)
    print(
        json.dumps(
            {
                "runDirectory": str(output),
                "title": title,
                "detail": args.detail,
                "inputs": len(inputs),
                "scaffolded": created,
            },
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


def load_manifest(run_directory):
    try:
        manifest = json.loads((run_directory / "source_manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail(f"could not read source_manifest.json: {error}")
    return manifest


def csv_rows(path):
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.reader(handle))
    except UnicodeDecodeError:
        fail(f"CSV is not valid UTF-8: {path}")


def sheet_name(stem, used):
    cleaned = re.sub(r"[\[\]:*?/\\]", "_", stem).strip().strip("'") or "Sheet"
    cleaned = cleaned[:31]
    candidate = cleaned
    index = 1
    while candidate.lower() in used:
        index += 1
        suffix = f"_{index}"
        candidate = cleaned[: 31 - len(suffix)] + suffix
    used.add(candidate.lower())
    return candidate


def command_tables(args):
    run_directory = require_run_directory(args.run_directory)
    manifest = load_manifest(run_directory)
    if args.from_csv:
        csv_paths = [Path(item).expanduser().resolve() for item in args.from_csv]
        for path in csv_paths:
            if not path.is_file():
                fail(f"CSV does not exist: {path}")
    else:
        csv_paths = [Path(record["path"]) for record in manifest["inputs"] if record["path"].lower().endswith(".csv")]
    if not csv_paths:
        print(json.dumps({"built": False, "reason": "no CSV inputs found", "sheets": []}))
        return
    if not openpyxl_available():
        print(json.dumps({"built": False, "reason": "openpyxl unavailable; install it to assemble tables.xlsx", "sheets": []}))
        return
    import openpyxl

    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    used = set()
    sheets = []
    for path in csv_paths:
        if not path.is_file():
            fail(f"CSV no longer exists: {path}")
        name = sheet_name(path.stem, used)
        worksheet = workbook.create_sheet(title=name)
        for row in csv_rows(path):
            worksheet.append(row)
        sheets.append({"sheet": name, "source": str(path)})
    workbook.save(run_directory / "tables.xlsx")
    print(json.dumps({"built": True, "sheets": sheets}, ensure_ascii=False))


def append_log(run_directory, name, lines):
    path = run_directory / name
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    path.write_text(existing + "\n".join(lines) + "\n", encoding="utf-8")


def command_render(args):
    run_directory = require_run_directory(args.run_directory)
    if args.format not in RENDER_FORMATS:
        fail(f"unsupported format: {args.format}; expected docx or html")
    source = run_directory / (args.input or "report.md")
    if not source.is_file():
        fail(f"source markdown does not exist: {source}")
    if PLACEHOLDER in source.read_text(encoding="utf-8"):
        fail(f"{source.name} still contains the placeholder marker; author it before rendering")
    if pandoc_version() is None:
        fail("Pandoc is required to render DOCX/HTML (macOS: brew install pandoc; Debian/Ubuntu: apt install pandoc)")
    converted = run_directory / "converted"
    converted.mkdir(exist_ok=True)
    output = converted / f"{source.stem}.{args.format}"
    command = ["pandoc", str(source), "-o", str(output)]
    if args.format == "html":
        command.insert(1, "--standalone")
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        append_log(run_directory, "conversion_log.md", [f"- {utc_now()} FAILED {source.name} -> {args.format}: {result.stderr.strip()}"])
        fail(f"pandoc failed: {result.stderr.strip()}")
    append_log(
        run_directory,
        "conversion_log.md",
        [f"- {utc_now()} OK {source.name} -> converted/{output.name} via pandoc"],
    )
    append_log(
        run_directory,
        "warnings.md",
        [
            f"## {output.name}",
            "",
            "Rendered with Pandoc from Markdown. Complex tables, embedded media, and",
            "advanced styling may render imperfectly. Review the output before sharing.",
            "",
        ],
    )
    print(json.dumps({"output": str(output), "format": args.format}, ensure_ascii=False))


def command_validate(args):
    run_directory = require_run_directory(args.run_directory)
    run = load_run(run_directory)
    manifest = load_manifest(run_directory)
    errors = []
    warnings = []
    if not (run_directory / "sources.md").is_file():
        errors.append("sources.md is missing; re-run init")
    for name in run.get("deliverables", []):
        path = run_directory / name
        if not path.is_file():
            errors.append(f"deliverable is missing: {name}")
            continue
        if PLACEHOLDER in path.read_text(encoding="utf-8"):
            errors.append(f"deliverable still has an unresolved placeholder: {name}")
    csv_inputs = [record for record in manifest["inputs"] if record["path"].lower().endswith(".csv")]
    for record in manifest["inputs"]:
        source = Path(record["path"])
        if not source.is_file():
            errors.append(f"source file is missing: {source}")
        elif sha256(source) != record["sha256"]:
            errors.append(f"source file hash differs from init: {source}")
    if csv_inputs and not (run_directory / "tables.xlsx").is_file():
        warnings.append(f"{len(csv_inputs)} CSV inputs present but tables.xlsx not built; run tables")
    converted = sorted(str(path.name) for path in (run_directory / "converted").glob("*")) if (run_directory / "converted").is_dir() else []
    if not converted:
        warnings.append("no DOCX/HTML rendered yet; run render if a document format is needed")
    produced = sorted(name for name in run.get("deliverables", []) if (run_directory / name).is_file())
    result = {
        "valid": not errors,
        "title": run.get("title"),
        "detail": run.get("detail"),
        "deliverables": produced,
        "tables": (run_directory / "tables.xlsx").is_file(),
        "converted": converted,
        "errors": errors,
        "warnings": warnings,
    }
    print(json.dumps(result, indent=2))
    if errors:
        raise SystemExit(1)


def parser():
    root = argparse.ArgumentParser(description="Assemble polished deliverables from processed forge outputs.")
    subparsers = root.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Report local rendering and table capabilities.")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(handler=command_doctor)

    init = subparsers.add_parser("init", help="Register inputs and scaffold deliverables for a detail level.")
    init.add_argument("inputs", nargs="+")
    init.add_argument("--output", required=True)
    init.add_argument("--detail", choices=sorted(PRESETS), required=True)
    init.add_argument("--title")
    init.set_defaults(handler=command_init)

    tables = subparsers.add_parser("tables", help="Assemble tables.xlsx, one sheet per CSV input.")
    tables.add_argument("run_directory")
    tables.add_argument("--from", dest="from_csv", nargs="+")
    tables.set_defaults(handler=command_tables)

    render = subparsers.add_parser("render", help="Render an authored Markdown deliverable to DOCX or HTML via Pandoc.")
    render.add_argument("run_directory")
    render.add_argument("--format", choices=sorted(RENDER_FORMATS), required=True)
    render.add_argument("--input")
    render.set_defaults(handler=command_render)

    validate = subparsers.add_parser("validate", help="Validate deliverables, provenance, and produced artifacts.")
    validate.add_argument("run_directory")
    validate.set_defaults(handler=command_validate)
    return root


def main():
    args = parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()

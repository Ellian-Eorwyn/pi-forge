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
import unicodedata
from collections import Counter
from pathlib import Path


LOW_TEXT_CHARACTERS = 40

MANIFEST_COLUMNS = [
    "source_path",
    "source_sha256",
    "source_format",
    "target_format",
    "status",
    "output_path",
    "warning_count",
    "error",
]

# Source group -> recognized extensions.
GROUP_EXTENSIONS = {
    "docx": {".docx"},
    "md": {".md", ".markdown"},
    "html": {".html", ".htm"},
    "pdf": {".pdf"},
    "csv": {".csv", ".tsv"},
    "xlsx": {".xlsx"},
    "txt": {".txt"},
}
EXTENSION_GROUP = {ext: group for group, exts in GROUP_EXTENSIONS.items() for ext in exts}

# Source group -> allowed --to targets.
ALLOWED_TARGETS = {
    "docx": {"md", "txt"},
    "md": {"docx", "html", "txt"},
    "html": {"md", "txt"},
    "pdf": {"txt", "md"},
    "csv": {"xlsx"},
    "xlsx": {"csv"},
    "txt": {"txt"},
}
ALL_TARGETS = sorted({target for targets in ALLOWED_TARGETS.values() for target in targets})

PANDOC_WRITER = {"md": "gfm", "html": "html5", "docx": "docx", "txt": "plain"}
TARGET_EXTENSION = {"md": "md", "html": "html", "docx": "docx", "txt": "txt", "csv": "csv", "xlsx": "xlsx"}


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


def tool_version(command, args):
    if shutil.which(command) is None:
        return None
    try:
        result = subprocess.run([command, *args], capture_output=True, text=True, check=False)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    combined = f"{result.stdout}\n{result.stderr}".strip()
    return combined.splitlines()[0].strip() if combined else "available"


def pandoc_version():
    return tool_version("pandoc", ["--version"])


def pdftotext_version():
    # pdftotext prints its banner to stderr and exits non-zero for -v; probe presence directly.
    if shutil.which("pdftotext") is None:
        return None
    try:
        result = subprocess.run(["pdftotext", "-v"], capture_output=True, text=True, check=False)
    except OSError:
        return None
    combined = f"{result.stdout}\n{result.stderr}".strip()
    return combined.splitlines()[0].strip() if combined else "available"


def safe_stem(path):
    raw = unicodedata.normalize("NFKC", path.stem).strip()
    safe = re.sub(r"[^\w.-]+", "-", raw, flags=re.UNICODE).strip("-")
    return safe or "file"


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
    if not (path / "conversion_manifest.csv").is_file():
        fail(f"conversion_manifest.csv is missing: {path}")
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
    files = []
    for raw in raw_inputs:
        root = Path(raw).expanduser().resolve()
        if not root.exists():
            fail(f"input does not exist: {root}")
        if root.is_symlink():
            fail(f"input is a symlink: {root}")
        for path in iter_input_files(root):
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                files.append(resolved)
    if not files:
        fail("no input files were found")
    return files


def text_warnings(value):
    warnings = []
    replacement = value.count("�")
    control = len(re.findall(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value))
    non_whitespace = len(re.findall(r"\S", value))
    alphanumeric = len(re.findall(r"[^\W_]", value, flags=re.UNICODE))
    if replacement:
        warnings.append(f"Found {replacement} Unicode replacement characters; encoding may be damaged.")
    if control:
        warnings.append(f"Found {control} unexpected control characters.")
    if non_whitespace > 100 and alphanumeric / non_whitespace < 0.05:
        warnings.append("Output has an unusually low proportion of letters and numbers; review for garbled text.")
    if non_whitespace == 0:
        warnings.append("No readable text was produced.")
    return warnings


def run_pandoc(source, output, target, media_dir):
    if pandoc_version() is None:
        raise RuntimeError("Pandoc is required (macOS: brew install pandoc; Debian/Ubuntu: apt install pandoc)")
    command = ["pandoc", str(source), "-t", PANDOC_WRITER[target], "-o", str(output)]
    if target == "html":
        command.append("--standalone")
    warnings = []
    if media_dir is not None:
        command.append(f"--extract-media={media_dir}")
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"pandoc failed: {result.stderr.strip() or 'unknown error'}")
    if media_dir is not None and media_dir.is_dir() and any(media_dir.rglob("*")):
        warnings.append(f"Embedded media was extracted to {media_dir}; it is referenced from the converted file.")
    if target in {"md", "txt"} and output.is_file():
        warnings.extend(text_warnings(output.read_text(encoding="utf-8", errors="replace")))
    if target in {"docx", "html"}:
        warnings.append("Conversion preserves common structure; complex layout, styles, and footnotes may not survive.")
    return warnings


def run_pdftotext(source, output, target):
    if pdftotext_version() is None:
        raise RuntimeError("pdftotext (Poppler) is required (macOS: brew install poppler; Debian/Ubuntu: apt install poppler-utils)")
    result = subprocess.run(
        ["pdftotext", "-layout", "-enc", "UTF-8", str(source), "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pdftotext failed: {result.stderr.strip() or 'unknown error'}")
    raw = result.stdout.replace("\r\n", "\n")
    warnings = []
    if target == "md":
        pages = [page.strip("\n") for page in raw.split("\f")]
        body = "\n\n".join(page for page in pages if page)
        text = body + "\n" if body else ""
        warnings.append("PDF Markdown is unstructured extracted text; headings and tables are not reconstructed.")
    else:
        text = raw if raw.endswith("\n") or raw == "" else raw + "\n"
    output.write_text(text, encoding="utf-8")
    if len(re.findall(r"[^\W_]", text, flags=re.UNICODE)) < LOW_TEXT_CHARACTERS:
        warnings.append("Very little text was extracted; the PDF may be scanned. Use document-ingest for OCR.")
    warnings.extend(text_warnings(text))
    return warnings


def csv_rows(path):
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.reader(handle, delimiter=delimiter))
    except UnicodeDecodeError:
        raise RuntimeError(f"input is not valid UTF-8: {path}")


def run_csv_to_xlsx(source, output):
    if not openpyxl_available():
        raise RuntimeError("openpyxl is required to write XLSX; install it for the active Python 3 environment")
    import openpyxl

    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "Data"
    for row in csv_rows(source):
        worksheet.append(row)
    workbook.save(output)
    return ["Values were written as text; numeric and date typing was not inferred."]


def sheet_filename(title, used):
    cleaned = re.sub(r"[^\w.-]+", "-", unicodedata.normalize("NFKC", title), flags=re.UNICODE).strip("-") or "sheet"
    candidate = cleaned
    index = 1
    while candidate.lower() in used:
        index += 1
        candidate = f"{cleaned}-{index}"
    used.add(candidate.lower())
    return candidate


def run_xlsx_to_csv(source, converted_dir, stem):
    if not openpyxl_available():
        raise RuntimeError("openpyxl is required to read XLSX; install it for the active Python 3 environment")
    import openpyxl

    workbook = openpyxl.load_workbook(source, read_only=True, data_only=True)
    warnings = []
    try:
        worksheets = list(workbook.worksheets)
        if not worksheets:
            raise RuntimeError("workbook has no sheets")
        if len(worksheets) == 1:
            outputs = [converted_dir / f"{stem}.csv"]
            targets = [(worksheets[0], outputs[0])]
        else:
            sheet_dir = converted_dir / stem
            sheet_dir.mkdir(parents=True, exist_ok=True)
            used = set()
            targets = [(ws, sheet_dir / f"{sheet_filename(ws.title, used)}.csv") for ws in worksheets]
            outputs = [path for _, path in targets]
            warnings.append(f"Workbook had {len(worksheets)} sheets; each was written to a separate CSV under {sheet_dir}.")
        for worksheet, out_path in targets:
            with out_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle, lineterminator="\n")
                for row in worksheet.iter_rows(values_only=True):
                    writer.writerow(["" if cell is None else cell for cell in row])
    finally:
        workbook.close()
    warnings.append("Formulas were exported as their last-computed values; macros, charts, and styling were dropped.")
    return outputs[0], warnings


def run_txt_cleanup(source, output):
    try:
        raw = source.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise RuntimeError(f"input is not valid UTF-8: {source}")
    lines = [line.rstrip() for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n")
    text = text + "\n" if text else ""
    output.write_text(text, encoding="utf-8")
    return text_warnings(text)


def unique_output(converted_dir, stem, extension, used):
    candidate = f"{stem}.{extension}"
    index = 1
    while candidate.lower() in used:
        index += 1
        candidate = f"{stem}-{index}.{extension}"
    used.add(candidate.lower())
    return converted_dir / candidate


def convert_one(source, target, converted_dir, used_names):
    group = EXTENSION_GROUP.get(source.suffix.lower())
    if group is None or target not in ALLOWED_TARGETS.get(group, set()):
        return {"status": "skipped", "output": None, "warnings": [], "error": f"{source.suffix or '(none)'} cannot convert to {target}"}
    stem = safe_stem(source)
    try:
        if group == "csv":
            output = unique_output(converted_dir, stem, "xlsx", used_names)
            warnings = run_csv_to_xlsx(source, output)
        elif group == "xlsx":
            output, warnings = run_xlsx_to_csv(source, converted_dir, stem)
        elif group == "pdf":
            output = unique_output(converted_dir, stem, TARGET_EXTENSION[target], used_names)
            warnings = run_pdftotext(source, output, target)
        elif group == "txt":
            output = unique_output(converted_dir, stem, "txt", used_names)
            warnings = run_txt_cleanup(source, output)
        else:  # docx, md, html via pandoc
            output = unique_output(converted_dir, stem, TARGET_EXTENSION[target], used_names)
            media_dir = (converted_dir / "media" / stem) if group in {"docx", "html"} else None
            warnings = run_pandoc(source, output, target, media_dir)
    except Exception as error:  # handlers may raise tool/library-specific errors; keep the batch going
        message = str(error) or error.__class__.__name__
        return {"status": "failed", "output": None, "warnings": [], "error": message}
    status = "needs_review" if warnings else "success"
    return {"status": status, "output": output, "warnings": warnings, "error": ""}


def command_doctor(args):
    xlsx = openpyxl_available()
    openpyxl_ver = None
    if xlsx:
        import openpyxl

        openpyxl_ver = openpyxl.__version__
    pandoc = pandoc_version()
    pdftotext = pdftotext_version()
    result = {
        "python": sys.version.split()[0],
        "pandoc": pandoc is not None,
        "pandocVersion": pandoc,
        "pdftotext": pdftotext is not None,
        "pdftotextVersion": pdftotext,
        "xlsx": xlsx,
        "openpyxlVersion": openpyxl_ver,
        "remediation": [],
    }
    if pandoc is None:
        result["remediation"].append("Install Pandoc for DOCX/Markdown/HTML conversions (macOS: brew install pandoc; Debian/Ubuntu: apt install pandoc).")
    if pdftotext is None:
        result["remediation"].append("Install Poppler for PDF conversion (macOS: brew install poppler; Debian/Ubuntu: apt install poppler-utils).")
    if not xlsx:
        result["remediation"].append("Install openpyxl for CSV<->XLSX conversion in the active Python 3 environment.")
    if args.json:
        print(json.dumps(result, indent=2))
        return
    print(f"Python: {result['python']}")
    print(f"Pandoc (DOCX/MD/HTML): {pandoc or 'unavailable'}")
    print(f"pdftotext (PDF): {pdftotext or 'unavailable'}")
    print(f"openpyxl (CSV<->XLSX): {'available ' + openpyxl_ver if xlsx else 'unavailable'}")
    for action in result["remediation"]:
        print(f"Action: {action}")


def command_convert(args):
    if args.target not in ALL_TARGETS:
        fail(f"unsupported target: {args.target}; expected one of {', '.join(ALL_TARGETS)}")
    from_extension = None
    if args.from_extension:
        from_extension = args.from_extension if args.from_extension.startswith(".") else f".{args.from_extension}"
        from_extension = from_extension.lower()
        if from_extension not in EXTENSION_GROUP:
            fail(f"--from extension is not a recognized source: {from_extension}")
    files = discover_inputs(args.inputs)
    output = require_new_directory(args.output)
    converted_dir = output / "converted"
    converted_dir.mkdir()
    manifest_path = output / "conversion_manifest.csv"
    used_names = set()
    rows = []
    counts = Counter()
    for source in files:
        if from_extension and source.suffix.lower() != from_extension:
            continue
        outcome = convert_one(source, args.target, converted_dir, used_names)
        counts[outcome["status"]] += 1
        output_rel = str(outcome["output"].relative_to(output)) if outcome["output"] else ""
        rows.append(
            [
                str(source),
                sha256(source),
                source.suffix.lower().lstrip(".") or "(none)",
                args.target,
                outcome["status"],
                output_rel,
                len(outcome["warnings"]),
                outcome["error"],
            ]
        )
        append_log(output, "conversion_log.md", [f"- {utc_now()} {outcome['status'].upper()} {source} -> {output_rel or '(none)'}"])
        if outcome["warnings"]:
            append_log(output, "warnings.md", [f"## {source.name} -> {args.target}", "", *[f"- {warning}" for warning in outcome["warnings"]], ""])
    if not rows:
        fail("no files matched the requested conversion; nothing was written")
    with manifest_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(MANIFEST_COLUMNS)
        writer.writerows(rows)
    print(
        json.dumps(
            {
                "runDirectory": str(output),
                "target": args.target,
                "success": counts["success"],
                "needsReview": counts["needs_review"],
                "skipped": counts["skipped"],
                "failed": counts["failed"],
            }
        )
    )


def append_log(run_directory, name, lines):
    path = run_directory / name
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    path.write_text(existing + "\n".join(lines) + "\n", encoding="utf-8")


def command_validate(args):
    run_directory = require_run_directory(args.run_directory)
    manifest_path = run_directory / "conversion_manifest.csv"
    with manifest_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    errors = []
    warnings = []
    counts = Counter()
    for row in rows:
        counts[row.get("status")] += 1
        source = Path(row.get("source_path", ""))
        if not source.is_file():
            errors.append(f"source file is missing: {source}")
        elif sha256(source) != row.get("source_sha256"):
            errors.append(f"source file hash differs from conversion: {source}")
        if row.get("status") in {"success", "needs_review"}:
            output = run_directory / row.get("output_path", "")
            if not row.get("output_path") or not output.exists():
                errors.append(f"converted output is missing: {row.get('output_path') or '(none)'}")
    if counts["failed"]:
        warnings.append(f"{counts['failed']} files failed conversion; see conversion_manifest.csv error column.")
    if counts["skipped"]:
        warnings.append(f"{counts['skipped']} files were skipped as unsupported for the requested target.")
    result = {
        "valid": not errors,
        "counts": {status: counts[status] for status in ["success", "needs_review", "skipped", "failed"]},
        "errors": errors,
        "warnings": warnings,
    }
    print(json.dumps(result, indent=2))
    if errors:
        raise SystemExit(1)


def parser():
    root = argparse.ArgumentParser(description="Convert files between formats while preserving originals.")
    subparsers = root.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Report local conversion tool availability.")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(handler=command_doctor)

    convert = subparsers.add_parser("convert", help="Convert files or folders to a target format.")
    convert.add_argument("inputs", nargs="+")
    convert.add_argument("--to", dest="target", choices=ALL_TARGETS, required=True)
    convert.add_argument("--output", required=True)
    convert.add_argument("--from", dest="from_extension")
    convert.set_defaults(handler=command_convert)

    validate = subparsers.add_parser("validate", help="Validate a conversion run against its manifest.")
    validate.add_argument("run_directory")
    validate.set_defaults(handler=command_validate)
    return root


def main():
    args = parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3

import argparse
import csv
import hashlib
import importlib.util
import json
import math
import os
import re
import statistics
import sys
from collections import Counter
from copy import copy
from datetime import date, datetime, time, timezone
from pathlib import Path

# Shared forge embeddings client lives at forge/lib; this script is at
# forge/skills/spreadsheet-analysis/scripts/spreadsheet-analysis.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
import forge_embeddings


SUPPORTED_EXTENSIONS = {".csv", ".tsv", ".xlsx"}
RUN_SCHEMA_VERSION = 1
CLUSTER_SCHEMA_VERSION = 1
RESULT_STATUSES = {"completed", "failed", "skipped", "needs_review"}

# Default cosine similarity for grouping rows by an embedded column. Raise it
# (~0.92+) for tight duplicate detection; lower it (~0.6-0.75) for broader
# topical categorization.
DEFAULT_CLUSTER_THRESHOLD = 0.85

# Maximum characters of key text embedded per row.
CLUSTER_KEY_CHARS = 2000


def fail(message, exit_code=1):
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(exit_code)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_value(value):
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return str(value)
        return value
    return str(value)


def display_header(value):
    if value is None:
        return ""
    return str(value)


def blank(value):
    return value is None or (isinstance(value, str) and value.strip() == "")


def require_source(raw_path):
    path = Path(raw_path).expanduser().resolve()
    if not path.exists():
        fail(f"input does not exist: {path}")
    if not path.is_file():
        fail(f"input is not a file: {path}")
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        fail(f"unsupported input format {path.suffix or '(none)'}; expected .csv, .tsv, or .xlsx")
    return path


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
    if not (path / "run.json").is_file():
        fail(f"run.json is missing: {path}")
    return path


def openpyxl_module():
    try:
        import openpyxl
    except ImportError:
        fail("XLSX support requires openpyxl; install it for the active Python 3 environment")
    return openpyxl


def csv_rows(path):
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.reader(handle, delimiter=delimiter))
    except UnicodeDecodeError:
        fail(f"input is not valid UTF-8: {path}")


def load_tables(path, requested_sheet=None, all_xlsx_sheets=False):
    extension = path.suffix.lower()
    if extension in {".csv", ".tsv"}:
        if requested_sheet:
            fail("--sheet is only valid for XLSX input")
        rows = csv_rows(path)
        return [{"name": "Data", "rows": rows}]

    openpyxl = openpyxl_module()
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=False, keep_links=True)
    try:
        if requested_sheet:
            if requested_sheet not in workbook.sheetnames:
                fail(f"sheet not found: {requested_sheet}")
            worksheets = [workbook[requested_sheet]]
        elif all_xlsx_sheets:
            worksheets = list(workbook.worksheets)
        else:
            worksheets = [workbook.active]
        tables = []
        for worksheet in worksheets:
            rows = [list(row) for row in worksheet.iter_rows(values_only=True)]
            tables.append({"name": worksheet.title, "rows": rows})
        return tables
    finally:
        workbook.close()


def normalized_width(rows):
    return max((len(row) for row in rows), default=0)


def pad_rows(rows, width):
    return [row + [None] * (width - len(row)) for row in rows]


def infer_type(values):
    nonblank = [value for value in values if not blank(value)]
    if not nonblank:
        return "empty"
    kinds = set()
    for value in nonblank:
        if isinstance(value, bool):
            kinds.add("boolean")
        elif isinstance(value, int):
            kinds.add("integer")
        elif isinstance(value, float):
            kinds.add("number")
        elif isinstance(value, (datetime, date, time)):
            kinds.add("date")
        elif isinstance(value, str):
            kinds.add(infer_string_type(value))
        else:
            kinds.add("text")
    if kinds <= {"integer"}:
        return "integer"
    if kinds <= {"integer", "number"}:
        return "number"
    if len(kinds) == 1:
        return next(iter(kinds))
    return "mixed"


def infer_string_type(value):
    stripped = value.strip()
    if stripped.startswith("="):
        return "formula"
    if stripped.lower() in {"true", "false"}:
        return "boolean"
    if re.fullmatch(r"[+-]?(?:0|[1-9]\d*)", stripped):
        unsigned = stripped.lstrip("+-")
        if len(unsigned) == 1 or not unsigned.startswith("0"):
            return "integer"
    if re.fullmatch(r"[+-]?(?:(?:0|[1-9]\d*)\.\d+|(?:0|[1-9]\d*)[eE][+-]?\d+|(?:0|[1-9]\d*)\.\d+[eE][+-]?\d+)", stripped):
        try:
            if math.isfinite(float(stripped)):
                return "number"
        except ValueError:
            pass
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ][^\s]+)?", stripped):
        try:
            datetime.fromisoformat(stripped.replace("Z", "+00:00"))
            return "date"
        except ValueError:
            pass
    return "text"


def counter_key(value):
    value = json_value(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def counter_label(key):
    return json.loads(key)


def numeric_distribution(values, inferred):
    if inferred not in {"integer", "number"}:
        return None, []
    numeric = []
    for value in values:
        if blank(value) or isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            numeric.append(number)
    if not numeric:
        return None, []
    ordered = sorted(numeric)
    distribution = {
        "minimum": min(ordered),
        "maximum": max(ordered),
        "mean": statistics.fmean(ordered),
        "median": statistics.median(ordered),
    }
    if len(ordered) < 4:
        return distribution, []
    quartiles = statistics.quantiles(ordered, n=4, method="inclusive")
    lower, upper = quartiles[0], quartiles[2]
    spread = upper - lower
    low_fence = lower - 1.5 * spread
    high_fence = upper + 1.5 * spread
    unusual = [value for value in ordered if value < low_fence or value > high_fence][:10]
    return distribution, unusual


def profile_table(table, max_categories):
    rows = table["rows"]
    width = normalized_width(rows)
    if not rows or width == 0:
        return {
            "name": table["name"],
            "rowCount": 0,
            "dataRowCount": 0,
            "columnCount": 0,
            "duplicateDataRows": 0,
            "columns": [],
            "warnings": ["Sheet or table is empty."],
        }
    padded = pad_rows(rows, width)
    headers = [display_header(value) for value in padded[0]]
    data = padded[1:]
    warnings = []
    if any(header == "" for header in headers):
        warnings.append("One or more header cells are blank.")
    duplicate_headers = sorted(header for header, count in Counter(headers).items() if header and count > 1)
    if duplicate_headers:
        warnings.append(f"Duplicate headers: {', '.join(duplicate_headers)}")
    row_keys = [tuple(counter_key(value) for value in row) for row in data if not all(blank(value) for value in row)]
    duplicate_rows = sum(count - 1 for count in Counter(row_keys).values() if count > 1)
    columns = []
    for index, header in enumerate(headers):
        values = [row[index] for row in data]
        nonblank = [value for value in values if not blank(value)]
        frequencies = Counter(counter_key(value) for value in nonblank)
        top_values = [
            {"value": counter_label(key), "count": count}
            for key, count in frequencies.most_common(max_categories)
        ]
        inferred = infer_type(values)
        numeric, numeric_unusual = numeric_distribution(values, inferred)
        unusual = numeric_unusual
        if numeric is None and len(nonblank) >= 10 and len(frequencies) <= len(nonblank) / 2:
            unusual = [
                counter_label(key)
                for key, count in frequencies.items()
                if count / len(nonblank) <= 0.05
            ][:10]
        columns.append(
            {
                "index": index + 1,
                "header": header,
                "inferredType": inferred,
                "missingCount": len(values) - len(nonblank),
                "nonMissingCount": len(nonblank),
                "uniqueCount": len(frequencies),
                "topValues": top_values,
                "numericDistribution": numeric,
                "unusualValues": unusual,
            }
        )
    return {
        "name": table["name"],
        "rowCount": len(rows),
        "dataRowCount": len(data),
        "columnCount": width,
        "duplicateDataRows": duplicate_rows,
        "columns": columns,
        "warnings": warnings,
    }


def markdown_value(value):
    if value is None:
        return ""
    text = str(value).replace("|", "\\|").replace("\n", " ")
    return text if len(text) <= 80 else f"{text[:77]}..."


def profile_markdown(profile):
    source = profile["source"]
    lines = [
        "# Data Profile",
        "",
        "## Source and Provenance",
        "",
        f"- Path: `{source['path']}`",
        f"- Format: `{source['format']}`",
        f"- SHA-256: `{source['sha256']}`",
        f"- Size: {source['sizeBytes']} bytes",
        f"- Generated: {profile['generatedAt']}",
        "",
    ]
    for sheet in profile["sheets"]:
        lines.extend(
            [
                f"## Sheet: {sheet['name']}",
                "",
                f"- Data rows: {sheet['dataRowCount']}",
                f"- Columns: {sheet['columnCount']}",
                f"- Duplicate data rows beyond first occurrence: {sheet['duplicateDataRows']}",
            ]
        )
        for warning in sheet["warnings"]:
            lines.append(f"- Warning: {warning}")
        lines.extend(
            [
                "",
                "| # | Header | Inferred type | Missing | Unique | Common values | Unusual values |",
                "|---:|---|---|---:|---:|---|---|",
            ]
        )
        for column in sheet["columns"]:
            common = ", ".join(
                f"{markdown_value(item['value'])} ({item['count']})" for item in column["topValues"][:5]
            )
            unusual = ", ".join(markdown_value(value) for value in column["unusualValues"][:5])
            lines.append(
                f"| {column['index']} | {markdown_value(column['header'])} | {column['inferredType']} | "
                f"{column['missingCount']} | {column['uniqueCount']} | {common} | {unusual} |"
            )
        lines.append("")
    lines.extend(
        [
            "## Interpretation Limits",
            "",
            "- Types and unusual values are heuristic and require review before transformation.",
            "- Empty strings and null cells are counted as missing; other missing-value tokens are not assumed.",
            "- Profiles are bounded and do not replace domain-specific validation.",
            "",
        ]
    )
    return "\n".join(lines)


def command_doctor(args):
    available = importlib.util.find_spec("openpyxl") is not None
    version = None
    if available:
        import openpyxl
        version = openpyxl.__version__
    embeddings = forge_embeddings.embeddings_doctor()
    result = {
        "python": sys.version.split()[0],
        "csvTsv": True,
        "xlsx": available,
        "openpyxlVersion": version,
        "embeddings": embeddings,
        "remediation": None if available else "Install openpyxl for the active Python 3 environment.",
    }
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Python: {result['python']}")
        print("CSV/TSV: available")
        print(f"XLSX: {'available via openpyxl ' + version if available else 'unavailable'}")
        reach = "reachable" if embeddings["reachable"] else "unreachable"
        print(f"Embeddings ({embeddings['url']}): {reach} - {embeddings['detail']}")
        print("  Required by the 'cluster' command for fuzzy record linkage and semantic grouping.")
        if result["remediation"]:
            print(f"Action: {result['remediation']}")


def command_inspect(args):
    source = require_source(args.input)
    output = require_new_directory(args.output)
    try:
        tables = load_tables(source, requested_sheet=args.sheet, all_xlsx_sheets=args.sheet is None)
        profile = {
            "schemaVersion": 1,
            "generatedAt": utc_now(),
            "source": {
                "path": str(source),
                "format": source.suffix.lower().lstrip("."),
                "sha256": sha256(source),
                "sizeBytes": source.stat().st_size,
            },
            "sheets": [profile_table(table, args.max_categories) for table in tables],
        }
        (output / "data_profile.json").write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        (output / "data_profile.md").write_text(profile_markdown(profile), encoding="utf-8")
        (output / "transform_log.md").write_text(
            "# Transformation Log\n\nNo data transformations were performed. This run created a bounded profile only.\n",
            encoding="utf-8",
        )
    except BaseException:
        try:
            output.rmdir()
        except OSError:
            pass
        raise
    print(json.dumps({"source": str(source), "output": str(output), "sheets": [table["name"] for table in tables]}))


def validate_headers(headers):
    if not headers:
        fail("the selected table has no header row")
    if any(header == "" for header in headers):
        fail("row enrichment requires every header cell to be nonblank")
    duplicates = sorted(header for header, count in Counter(headers).items() if count > 1)
    if duplicates:
        fail(f"row enrichment requires unique headers; duplicates: {', '.join(duplicates)}")


def command_row_init(args):
    source = require_source(args.input)
    output = Path(args.output).expanduser().resolve()
    tables = load_tables(source, requested_sheet=args.sheet, all_xlsx_sheets=False)
    table = tables[0]
    rows = table["rows"]
    width = normalized_width(rows)
    if not rows or width == 0:
        fail("the selected table is empty")
    padded = pad_rows(rows, width)
    headers = [display_header(value) for value in padded[0]]
    validate_headers(headers)
    if args.column in headers:
        fail(f"output column already exists: {args.column}")
    selected = args.input_columns if args.input_columns else headers
    unknown = [header for header in selected if header not in headers]
    if unknown:
        fail(f"input columns not found: {', '.join(unknown)}")
    if len(set(selected)) != len(selected):
        fail("--input-columns contains duplicates")
    start_row = args.start_row if args.start_row is not None else 2
    end_row = args.end_row if args.end_row is not None else len(padded)
    if start_row < 2:
        fail("--start-row must be 2 or greater because row 1 is the header")
    if end_row < start_row:
        fail("--end-row must be greater than or equal to --start-row")
    end_row = min(end_row, len(padded))
    header_indexes = {header: index for index, header in enumerate(headers)}
    eligible = []
    blank_rows = []
    row_data = {}
    for row_number in range(start_row, end_row + 1):
        row = padded[row_number - 1]
        if all(blank(value) for value in row):
            blank_rows.append(row_number)
            continue
        eligible.append(row_number)
        row_data[str(row_number)] = {header: json_value(row[header_indexes[header]]) for header in selected}
    output = require_new_directory(output)
    source_info = {
        "path": str(source),
        "basename": source.name,
        "format": source.suffix.lower().lstrip("."),
        "sha256": sha256(source),
        "sizeBytes": source.stat().st_size,
    }
    run = {
        "schemaVersion": RUN_SCHEMA_VERSION,
        "createdAt": utc_now(),
        "source": source_info,
        "sheet": table["name"],
        "headerRow": 1,
        "outputColumn": args.column,
        "inputColumns": selected,
        "startRow": start_row,
        "endRow": end_row,
        "eligibleRows": eligible,
        "blankRows": blank_rows,
        "rows": row_data,
    }
    (output / "run.json").write_text(json.dumps(run, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (output / "source_manifest.json").write_text(json.dumps(source_info, indent=2) + "\n", encoding="utf-8")
    (output / "row_results.jsonl").write_text("", encoding="utf-8")
    print(json.dumps({"runDirectory": str(output), "sheet": table["name"], "eligibleRows": len(eligible), "blankRows": len(blank_rows)}))


def load_run(run_directory):
    try:
        run = json.loads((run_directory / "run.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail(f"could not read run.json: {error}")
    if run.get("schemaVersion") != RUN_SCHEMA_VERSION:
        fail(f"unsupported run schema version: {run.get('schemaVersion')}")
    return run


def load_results(run_directory, strict=True):
    path = run_directory / "row_results.jsonl"
    if not path.is_file():
        fail(f"row_results.jsonl is missing: {path}")
    results = []
    seen = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            result = json.loads(line)
        except json.JSONDecodeError as error:
            fail(f"invalid JSON on row_results.jsonl line {line_number}: {error}")
        row_id = result.get("rowId")
        if strict and row_id in seen:
            fail(f"duplicate result for row {row_id}")
        seen.add(row_id)
        results.append(result)
    return results


def next_pending(run, results):
    completed = {result.get("rowId") for result in results}
    for row_id in run["eligibleRows"]:
        if row_id not in completed:
            return row_id
    return None


def command_row_next(args):
    run_directory = require_run_directory(args.run_directory)
    run = load_run(run_directory)
    results = load_results(run_directory)
    row_id = next_pending(run, results)
    if row_id is None:
        print(json.dumps({"complete": True, "processed": len(results), "total": len(run["eligibleRows"])}))
        return
    print(
        json.dumps(
            {
                "complete": False,
                "rowId": row_id,
                "sourceRow": row_id,
                "sheet": run["sheet"],
                "input": run["rows"][str(row_id)],
                "outputColumn": run["outputColumn"],
                "progress": {"processed": len(results), "total": len(run["eligibleRows"])},
            },
            ensure_ascii=False,
        )
    )


def command_row_record(args):
    run_directory = require_run_directory(args.run_directory)
    run = load_run(run_directory)
    results = load_results(run_directory)
    expected = next_pending(run, results)
    if expected is None:
        fail("the run is already complete")
    if args.row_id != expected:
        fail(f"rows must be recorded sequentially; expected row {expected}, received {args.row_id}")
    if args.status == "completed":
        if not args.value_file:
            fail("completed results require --value-file")
        value_path = Path(args.value_file).expanduser().resolve()
        if not value_path.is_file():
            fail(f"value file does not exist: {value_path}")
        try:
            value = value_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            fail(f"value file is not valid UTF-8: {value_path}")
        if not value.strip():
            fail("completed results require a nonblank value; use an explicit non-completed status instead")
    else:
        if args.value_file:
            fail("--value-file is only valid with --status completed")
        if not args.note:
            fail(f"--status {args.status} requires --note")
        value = None
    result = {
        "rowId": args.row_id,
        "status": args.status,
        "value": value,
        "note": args.note,
        "recordedAt": utc_now(),
    }
    with (run_directory / "row_results.jsonl").open("a", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps(result, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    print(json.dumps({"recorded": args.row_id, "status": args.status, "remaining": len(run["eligibleRows"]) - len(results) - 1}))


def result_map(results):
    return {result["rowId"]: result for result in results}


def write_delimited_output(source, output, run, results):
    delimiter = "\t" if source.suffix.lower() == ".tsv" else ","
    rows = csv_rows(source)
    values = result_map(results)
    with output.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter=delimiter, lineterminator="\n")
        for row_number, row in enumerate(rows, start=1):
            if row_number == 1:
                writer.writerow(row + [run["outputColumn"]])
                continue
            result = values.get(row_number)
            value = result["value"] if result and result["status"] == "completed" else ""
            writer.writerow(row + [value])


def write_xlsx_output(source, output, run, results):
    openpyxl = openpyxl_module()
    workbook = openpyxl.load_workbook(source, data_only=False, keep_links=True)
    try:
        if run["sheet"] not in workbook.sheetnames:
            fail(f"source sheet no longer exists: {run['sheet']}")
        worksheet = workbook[run["sheet"]]
        headers = [display_header(worksheet.cell(row=1, column=index).value) for index in range(1, worksheet.max_column + 1)]
        if run["outputColumn"] in headers:
            fail(f"output column now exists in source: {run['outputColumn']}")
        output_column = worksheet.max_column + 1
        source_style_column = max(1, output_column - 1)
        header = worksheet.cell(row=1, column=output_column, value=run["outputColumn"])
        source_header = worksheet.cell(row=1, column=source_style_column)
        if source_header.has_style:
            header._style = copy(source_header._style)
        if source_header.number_format:
            header.number_format = source_header.number_format
        source_letter = openpyxl.utils.get_column_letter(source_style_column)
        output_letter = openpyxl.utils.get_column_letter(output_column)
        if source_letter in worksheet.column_dimensions:
            source_dimension = worksheet.column_dimensions[source_letter]
            worksheet.column_dimensions[output_letter].width = source_dimension.width
            worksheet.column_dimensions[output_letter].hidden = source_dimension.hidden
        for row_id, result in result_map(results).items():
            if result["status"] != "completed":
                continue
            cell = worksheet.cell(row=row_id, column=output_column, value=result["value"])
            source_cell = worksheet.cell(row=row_id, column=source_style_column)
            if source_cell.has_style:
                cell._style = copy(source_cell._style)
            if source_cell.number_format:
                cell.number_format = source_cell.number_format
        temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp.xlsx")
        try:
            workbook.save(temporary)
            os.link(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
    finally:
        workbook.close()


def review_report(run, results):
    lines = [
        "# Row Enrichment Review",
        "",
        f"- Source: `{run['source']['path']}`",
        f"- Sheet: `{run['sheet']}`",
        f"- Output column: `{run['outputColumn']}`",
        f"- Eligible rows: {len(run['eligibleRows'])}",
        f"- Blank rows skipped automatically: {len(run['blankRows'])}",
        "",
        "## Results",
        "",
    ]
    counts = Counter(result["status"] for result in results)
    for status in ["completed", "skipped", "failed", "needs_review"]:
        lines.append(f"- {status}: {counts[status]}")
    lines.extend(["", "## Rows Requiring Attention", ""])
    attention = [result for result in results if result["status"] != "completed"]
    if not attention:
        lines.append("None.")
    else:
        lines.extend(["| Source row | Status | Note |", "|---:|---|---|"])
        for result in attention:
            note = str(result.get("note") or "").replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {result['rowId']} | {result['status']} | {note} |")
    if run["source"]["format"] == "xlsx":
        lines.extend(
            [
                "",
                "## XLSX Preservation Warning",
                "",
                "XLSX output was written through openpyxl. Common cells, formulas, sheets, and basic styles are preserved, but macros, external links, advanced charts, embedded objects, and unsupported Excel features may not survive round-tripping.",
                "",
            ]
        )
    return "\n".join(lines)


def command_row_finalize(args):
    run_directory = require_run_directory(args.run_directory)
    run = load_run(run_directory)
    results = load_results(run_directory)
    pending = next_pending(run, results)
    if pending is not None:
        fail(f"run is incomplete; next pending row is {pending}")
    source = require_source(run["source"]["path"])
    current_hash = sha256(source)
    if current_hash != run["source"]["sha256"]:
        fail("source file changed after row-init; refusing to finalize")
    extension = source.suffix.lower()
    output = run_directory / f"enriched{extension}"
    if output.exists():
        fail(f"output already exists: {output}")
    if extension == ".xlsx":
        write_xlsx_output(source, output, run, results)
    else:
        write_delimited_output(source, output, run, results)
    (run_directory / "review_report.md").write_text(review_report(run, results), encoding="utf-8")
    counts = Counter(result["status"] for result in results)
    (run_directory / "transform_log.md").write_text(
        "\n".join(
            [
                "# Transformation Log",
                "",
                f"- Finalized: {utc_now()}",
                f"- Source: `{source}`",
                f"- Source SHA-256: `{current_hash}`",
                f"- Sheet: `{run['sheet']}`",
                f"- Added column: `{run['outputColumn']}`",
                f"- Completed rows: {counts['completed']}",
                f"- Skipped rows: {counts['skipped']}",
                f"- Failed rows: {counts['failed']}",
                f"- Review-needed rows: {counts['needs_review']}",
                f"- Automatically blank rows: {len(run['blankRows'])}",
                f"- Output: `{output}`",
                "",
                "No source file was modified.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(json.dumps({"output": str(output), "completed": counts["completed"], "skipped": counts["skipped"], "failed": counts["failed"], "needsReview": counts["needs_review"]}))


def output_validation(run_directory, run, results, errors):
    source = Path(run["source"]["path"])
    output = run_directory / f"enriched{source.suffix.lower()}"
    if not output.exists():
        return
    values = result_map(results)
    if source.suffix.lower() in {".csv", ".tsv"}:
        rows = csv_rows(output)
        if not rows or not rows[0] or rows[0][-1] != run["outputColumn"]:
            errors.append("enriched output does not end with the configured output column")
            return
        for row_id, result in values.items():
            if row_id > len(rows):
                errors.append(f"enriched output is missing source row {row_id}")
                continue
            expected = result["value"] if result["status"] == "completed" else ""
            actual = rows[row_id - 1][-1] if rows[row_id - 1] else ""
            if actual != expected:
                errors.append(f"enriched output value differs at source row {row_id}")
        return
    openpyxl = openpyxl_module()
    workbook = openpyxl.load_workbook(output, read_only=True, data_only=False, keep_links=True)
    try:
        if run["sheet"] not in workbook.sheetnames:
            errors.append("enriched output is missing the configured sheet")
            return
        worksheet = workbook[run["sheet"]]
        headers = [display_header(worksheet.cell(row=1, column=index).value) for index in range(1, worksheet.max_column + 1)]
        if not headers or headers[-1] != run["outputColumn"]:
            errors.append("enriched output does not end with the configured output column")
            return
        output_column = len(headers)
        for row_id, result in values.items():
            expected = result["value"] if result["status"] == "completed" else None
            actual = worksheet.cell(row=row_id, column=output_column).value
            if actual != expected:
                errors.append(f"enriched output value differs at source row {row_id}")
    finally:
        workbook.close()


def command_validate(args):
    run_directory = require_run_directory(args.run_directory)
    run = load_run(run_directory)
    results = load_results(run_directory, strict=False)
    errors = []
    warnings = []
    eligible = run.get("eligibleRows", [])
    if len(eligible) != len(set(eligible)):
        errors.append("eligibleRows contains duplicates")
    if any(not isinstance(row_id, int) or row_id < 2 for row_id in eligible):
        errors.append("eligibleRows contains an invalid source row")
    seen = set()
    expected_order = []
    for result in results:
        row_id = result.get("rowId")
        expected_order.append(row_id)
        if row_id in seen:
            errors.append(f"duplicate result for row {row_id}")
        seen.add(row_id)
        if row_id not in eligible:
            errors.append(f"result references ineligible row {row_id}")
        if result.get("status") not in RESULT_STATUSES:
            errors.append(f"row {row_id} has invalid status {result.get('status')}")
        if result.get("status") == "completed" and not isinstance(result.get("value"), str):
            errors.append(f"completed row {row_id} has no string value")
        if result.get("status") != "completed" and not result.get("note"):
            errors.append(f"non-completed row {row_id} has no note")
    if expected_order != eligible[: len(expected_order)]:
        errors.append("results are not in eligible-row order")
    missing = [row_id for row_id in eligible if row_id not in seen]
    if missing:
        warnings.append(f"run is incomplete; {len(missing)} rows remain, beginning with row {missing[0]}")
    source = Path(run.get("source", {}).get("path", ""))
    if not source.is_file():
        errors.append("source file is missing")
    elif sha256(source) != run.get("source", {}).get("sha256"):
        errors.append("source file hash differs from row-init")
    output_validation(run_directory, run, results, errors)
    result = {"valid": not errors, "complete": not missing, "errors": errors, "warnings": warnings}
    print(json.dumps(result, indent=2))
    if errors:
        raise SystemExit(1)


def cluster_key_text(row, header_indexes, columns):
    parts = []
    for column in columns:
        value = json_value(row[header_indexes[column]])
        if blank(value):
            continue
        parts.append(str(value).strip())
    return " | ".join(parts)[:CLUSTER_KEY_CHARS]


def write_clusters_csv(path, members):
    fields = [
        "cluster_id",
        "group_size",
        "is_representative",
        "source_row",
        "similarity_to_representative",
        "key_text",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for member in members:
            writer.writerow(member)


def cluster_groups_markdown(run, members, multi_group_ids):
    by_group = {}
    for member in members:
        by_group.setdefault(member["cluster_id"], []).append(member)
    lines = [
        "# Candidate Groups",
        "",
        f"- Source: `{run['source']['path']}`",
        f"- Sheet: `{run['sheet']}`",
        f"- Grouped columns: {', '.join('`' + column + '`' for column in run['columns'])}",
        f"- Similarity threshold: {run['threshold']}",
        f"- Model: `{run['model']}`",
        f"- Rows grouped: {run['groupedRows']}",
        f"- Rows skipped (blank key): {len(run['blankKeyRows'])}",
        f"- Multi-row groups: {len(multi_group_ids)}",
        "",
        "Each multi-row group is a set of rows whose grouped columns are similar in "
        "meaning. These are candidates for review, not confirmed matches. Raise the "
        "threshold for tighter duplicate detection or lower it for broader topical "
        "grouping. Nothing is merged, deduplicated, or modified; decide what to do "
        "with each group yourself.",
        "",
    ]
    if not multi_group_ids:
        lines.append("No multi-row groups at this threshold. Every grouped row is on its own.")
        lines.append("")
        return "\n".join(lines)
    for cluster_id in multi_group_ids:
        group = by_group[cluster_id]
        lines.append(f"## Group {cluster_id} ({len(group)} rows)")
        lines.append("")
        lines.extend(["| Source row | Representative | Similarity | Key text |", "|---:|---|---:|---|"])
        for member in group:
            representative = "yes" if member["is_representative"] == "true" else ""
            key_cell = member["key_text"].replace("|", "\\|").replace("\n", " ")
            if len(key_cell) > 80:
                key_cell = key_cell[:77] + "..."
            lines.append(
                f"| {member['source_row']} | {representative} | "
                f"{member['similarity_to_representative']} | {key_cell} |"
            )
        lines.append("")
    return "\n".join(lines)


def command_cluster(args):
    source = require_source(args.input)
    if len(set(args.columns)) != len(args.columns):
        fail("--columns contains duplicates")
    threshold = args.threshold
    if not -1.0 <= threshold <= 1.0:
        fail("--threshold must be between -1 and 1")
    tables = load_tables(source, requested_sheet=args.sheet, all_xlsx_sheets=False)
    table = tables[0]
    rows = table["rows"]
    width = normalized_width(rows)
    if not rows or width == 0:
        fail("the selected table is empty")
    padded = pad_rows(rows, width)
    headers = [display_header(value) for value in padded[0]]
    validate_headers(headers)
    unknown = [column for column in args.columns if column not in headers]
    if unknown:
        fail(f"columns not found: {', '.join(unknown)}")
    header_indexes = {header: index for index, header in enumerate(headers)}

    grouped_rows = []
    key_texts = []
    blank_key_rows = []
    for row_number in range(2, len(padded) + 1):
        row = padded[row_number - 1]
        if all(blank(value) for value in row):
            continue
        key = cluster_key_text(row, header_indexes, args.columns)
        if not key:
            blank_key_rows.append(row_number)
            continue
        grouped_rows.append(row_number)
        key_texts.append(key)
    if not grouped_rows:
        fail("no rows had nonblank values in the selected columns")

    output = require_new_directory(args.output)
    try:
        result = forge_embeddings.embed_texts(key_texts, url=args.embeddings_url)
        if not result["ok"]:
            fail(
                "embeddings endpoint unavailable: "
                f"{result['reason']}. Set FORGE_EMBEDDINGS_URL or pass --embeddings-url; "
                "the cluster command requires embeddings."
            )
        vectors = [forge_embeddings.normalize(vector) for vector in result["vectors"]]
        components = forge_embeddings.cluster_components(vectors, threshold)

        members = []
        multi_group_ids = []
        for cluster_index, component in enumerate(
            sorted(components, key=lambda part: min(part)), start=1
        ):
            cluster_id = f"g{cluster_index}"
            representative_position = min(component, key=lambda position: grouped_rows[position])
            if len(component) > 1:
                multi_group_ids.append(cluster_id)
            for position in sorted(component, key=lambda position: grouped_rows[position]):
                similarity = forge_embeddings.cosine(vectors[position], vectors[representative_position])
                members.append(
                    {
                        "cluster_id": cluster_id,
                        "group_size": len(component),
                        "is_representative": "true" if position == representative_position else "false",
                        "source_row": grouped_rows[position],
                        "similarity_to_representative": f"{similarity:.3f}",
                        "key_text": key_texts[position],
                    }
                )

        run = {
            "schemaVersion": CLUSTER_SCHEMA_VERSION,
            "createdAt": utc_now(),
            "source": {
                "path": str(source),
                "format": source.suffix.lower().lstrip("."),
                "sha256": sha256(source),
                "sizeBytes": source.stat().st_size,
            },
            "sheet": table["name"],
            "columns": args.columns,
            "threshold": threshold,
            "model": result["model"],
            "dimensions": result["dimensions"],
            "groupedRows": len(grouped_rows),
            "blankKeyRows": blank_key_rows,
            "clusterCount": len(components),
            "multiRowGroupCount": len(multi_group_ids),
        }
        members.sort(key=lambda member: (member["cluster_id"], member["source_row"]))
        write_clusters_csv(output / "clusters.csv", members)
        (output / "cluster_groups.md").write_text(
            cluster_groups_markdown(run, members, multi_group_ids), encoding="utf-8"
        )
        (output / "cluster_run.json").write_text(
            json.dumps(run, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except BaseException:
        try:
            for child in output.iterdir():
                child.unlink()
            output.rmdir()
        except OSError:
            pass
        raise
    print(
        json.dumps(
            {
                "output": str(output),
                "groupedRows": len(grouped_rows),
                "blankKeyRows": len(blank_key_rows),
                "clusterCount": len(components),
                "multiRowGroupCount": len(multi_group_ids),
            }
        )
    )


def parser():
    root = argparse.ArgumentParser(description="Profile spreadsheets and manage resumable row enrichment.")
    subparsers = root.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Report local spreadsheet capabilities.")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(handler=command_doctor)

    inspect = subparsers.add_parser("inspect", help="Create bounded Markdown and JSON data profiles.")
    inspect.add_argument("input")
    inspect.add_argument("--output", required=True)
    inspect.add_argument("--sheet")
    inspect.add_argument("--max-categories", type=int, default=20)
    inspect.set_defaults(handler=command_inspect)

    cluster = subparsers.add_parser(
        "cluster",
        help="Group rows by embedding-based similarity of one or more columns for fuzzy record linkage or categorization.",
    )
    cluster.add_argument("input")
    cluster.add_argument("--output", required=True)
    cluster.add_argument("--columns", nargs="+", required=True, help="One or more column headers whose combined text is embedded.")
    cluster.add_argument("--sheet")
    cluster.add_argument("--threshold", type=float, default=DEFAULT_CLUSTER_THRESHOLD)
    cluster.add_argument("--embeddings-url", help="Override the embeddings endpoint (default FORGE_EMBEDDINGS_URL or http://llms:8005/v1/embeddings).")
    cluster.set_defaults(handler=command_cluster)

    row_init = subparsers.add_parser("row-init", help="Initialize a resumable one-row-at-a-time enrichment run.")
    row_init.add_argument("input")
    row_init.add_argument("--output", required=True)
    row_init.add_argument("--column", required=True)
    row_init.add_argument("--sheet")
    row_init.add_argument("--input-columns", nargs="+")
    row_init.add_argument("--start-row", type=int)
    row_init.add_argument("--end-row", type=int)
    row_init.set_defaults(handler=command_row_init)

    row_next = subparsers.add_parser("row-next", help="Return exactly one pending row as JSON.")
    row_next.add_argument("run_directory")
    row_next.set_defaults(handler=command_row_next)

    row_record = subparsers.add_parser("row-record", help="Append one generated value or explicit disposition.")
    row_record.add_argument("run_directory")
    row_record.add_argument("--row-id", type=int, required=True)
    row_record.add_argument("--status", choices=sorted(RESULT_STATUSES), default="completed")
    row_record.add_argument("--value-file")
    row_record.add_argument("--note")
    row_record.set_defaults(handler=command_row_record)

    row_finalize = subparsers.add_parser("row-finalize", help="Write a new enriched spreadsheet after all rows are disposed.")
    row_finalize.add_argument("run_directory")
    row_finalize.set_defaults(handler=command_row_finalize)

    validate = subparsers.add_parser("validate", help="Validate run state, provenance, and any finalized output.")
    validate.add_argument("run_directory")
    validate.set_defaults(handler=command_validate)
    return root


def main():
    args = parser().parse_args()
    if getattr(args, "max_categories", 1) < 1:
        fail("--max-categories must be positive")
    args.handler(args)


if __name__ == "__main__":
    main()

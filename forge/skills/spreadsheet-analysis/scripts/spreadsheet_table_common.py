#!/usr/bin/env python3

import csv
import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
from tool_contract import ToolInputError


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
WORKFLOW_SCRIPT = SCRIPT_DIRECTORY / "spreadsheet-analysis.py"
SUPPORTED_EXTENSIONS = {".csv", ".tsv", ".xlsx"}
_IMPLEMENTATION = None


def implementation():
    global _IMPLEMENTATION
    if _IMPLEMENTATION is None:
        spec = importlib.util.spec_from_file_location("spreadsheet_analysis_impl", WORKFLOW_SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _IMPLEMENTATION = module
    return _IMPLEMENTATION


def require_source(raw_path):
    path = Path(raw_path).expanduser().resolve()
    if not path.exists():
        raise ToolInputError("input_not_found", f"Input does not exist: {path}")
    if not path.is_file():
        raise ToolInputError("input_not_file", f"Input is not a file: {path}")
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ToolInputError("unsupported_input_format", f"Unsupported input format: {path.suffix or '(none)'}")
    return path


def ensure_xlsx_support():
    if importlib.util.find_spec("openpyxl") is None:
        raise ToolInputError("openpyxl_missing", "XLSX support requires openpyxl")


def load_tables(path, sheet=None, all_sheets=False):
    if path.suffix.lower() == ".xlsx":
        ensure_xlsx_support()
    try:
        return implementation().load_tables(path, requested_sheet=sheet, all_xlsx_sheets=all_sheets)
    except SystemExit as error:
        raise ToolInputError("spreadsheet_load_failed", str(error)) from error


def normalized_table(table):
    impl = implementation()
    rows = table["rows"]
    width = impl.normalized_width(rows)
    padded = impl.pad_rows(rows, width) if rows else []
    headers = [impl.display_header(value) for value in padded[0]] if padded else []
    data_rows = padded[1:] if padded else []
    return headers, data_rows


def json_value(value):
    return implementation().json_value(value)


def rows_as_objects(headers, rows):
    return [
        {header: json_value(row[index]) for index, header in enumerate(headers)}
        for row in rows
    ]


def selected_column_indexes(headers, columns):
    indexes = []
    for column in columns:
        if column not in headers:
            raise ToolInputError("unknown_column", f"Column not found: {column}")
        indexes.append(headers.index(column))
    return indexes


def write_table(path, headers, rows, sheet_name="Data"):
    output = Path(path).expanduser().resolve()
    if output.exists():
        raise ToolInputError("output_exists", f"Output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    extension = output.suffix.lower()
    if extension in {".csv", ".tsv"}:
        delimiter = "\t" if extension == ".tsv" else ","
        with output.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter=delimiter, lineterminator="\n")
            writer.writerow(headers)
            writer.writerows(rows)
    elif extension == ".json":
        output.write_text(json.dumps(rows_as_objects(headers, rows), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    elif extension == ".xlsx":
        ensure_xlsx_support()
        import openpyxl

        workbook = openpyxl.Workbook()
        worksheet = workbook.active
        worksheet.title = sheet_name[:31] or "Data"
        worksheet.append(headers)
        for row in rows:
            worksheet.append([json_value(value) for value in row])
        workbook.save(output)
    else:
        raise ToolInputError("unsupported_output_format", f"Unsupported output format: {extension or '(none)'}")
    return output

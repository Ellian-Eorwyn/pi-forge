#!/usr/bin/env python3

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
from tool_contract import ToolInputError, ok_result, required_string, run_tool

from spreadsheet_table_common import implementation, load_tables, normalized_table, require_source, selected_column_indexes, write_table


def blank(value):
    return value is None or (isinstance(value, str) and value.strip() == "")


def operation_columns(headers, operation):
    columns = operation.get("columns")
    if columns is None:
        return list(range(len(headers)))
    if not isinstance(columns, list):
        raise ToolInputError("invalid_operation", "operation columns must be an array")
    return selected_column_indexes(headers, columns)


def apply_operations(headers, rows, operations):
    applied = []
    for operation in operations:
        if not isinstance(operation, dict):
            raise ToolInputError("invalid_operation", "each operation must be an object")
        op = operation.get("op")
        if op == "rename":
            source = operation.get("from")
            destination = operation.get("to")
            if source not in headers or not isinstance(destination, str) or not destination:
                raise ToolInputError("invalid_operation", "rename requires from and to headers")
            if destination in headers and destination != source:
                raise ToolInputError("duplicate_header", f"Header already exists: {destination}")
            headers = [destination if header == source else header for header in headers]
        elif op == "trim":
            for index in operation_columns(headers, operation):
                for row in rows:
                    if isinstance(row[index], str):
                        row[index] = row[index].strip()
        elif op == "normalize_blanks":
            tokens = operation.get("tokens")
            if not isinstance(tokens, list):
                raise ToolInputError("invalid_operation", "normalize_blanks requires tokens")
            token_set = {str(token).strip() for token in tokens}
            for index in operation_columns(headers, operation):
                for row in rows:
                    if isinstance(row[index], str) and row[index].strip() in token_set:
                        row[index] = ""
        elif op == "drop_blank_rows":
            rows = [row for row in rows if not all(blank(value) for value in row)]
        elif op == "select_columns":
            columns = operation.get("columns")
            if not isinstance(columns, list) or not columns:
                raise ToolInputError("invalid_operation", "select_columns requires columns")
            indexes = selected_column_indexes(headers, columns)
            headers = [headers[index] for index in indexes]
            rows = [[row[index] for index in indexes] for row in rows]
        elif op == "drop_columns":
            columns = operation.get("columns")
            if not isinstance(columns, list) or not columns:
                raise ToolInputError("invalid_operation", "drop_columns requires columns")
            drop = set(selected_column_indexes(headers, columns))
            keep = [index for index in range(len(headers)) if index not in drop]
            headers = [headers[index] for index in keep]
            rows = [[row[index] for index in keep] for row in rows]
        else:
            raise ToolInputError("unknown_operation", f"Unknown operation: {op}")
        applied.append(op)
    return headers, rows, applied


def main(payload):
    source = require_source(required_string(payload, "input"))
    output = required_string(payload, "output")
    operations = payload.get("operations")
    if not isinstance(operations, list):
        raise ToolInputError("missing_required_field", "operations must be an array")
    tables = load_tables(source, sheet=payload.get("sheet"), all_sheets=False)
    table = tables[0]
    headers, rows = normalized_table(table)
    impl = implementation()
    source_hash = impl.sha256(source)
    headers, rows, applied = apply_operations(headers, rows, operations)
    output_path = write_table(output, headers, rows, sheet_name=table["name"])
    return ok_result(
        artifacts=[{"role": "cleaned_table", "path": str(output_path)}],
        data={
            "source": str(source),
            "sourceSha256": source_hash,
            "output": str(output_path),
            "sheet": table["name"],
            "operations": applied,
            "dataRowCount": len(rows),
            "columnCount": len(headers),
        },
    )


if __name__ == "__main__":
    run_tool(main)

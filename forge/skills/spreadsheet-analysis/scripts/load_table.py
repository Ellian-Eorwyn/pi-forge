#!/usr/bin/env python3

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
from tool_contract import ok_result, required_string, run_tool

from spreadsheet_table_common import json_value, load_tables, normalized_table, require_source


def main(payload):
    source = require_source(required_string(payload, "input"))
    preview_rows = int(payload.get("previewRows", 20))
    if preview_rows < 0:
        preview_rows = 0
    tables = load_tables(source, sheet=payload.get("sheet"), all_sheets=bool(payload.get("allSheets", False)))
    sheets = []
    for table in tables:
        headers, rows = normalized_table(table)
        sheets.append(
            {
                "name": table["name"],
                "rowCount": len(rows) + (1 if headers else 0),
                "dataRowCount": len(rows),
                "columnCount": len(headers),
                "headers": headers,
                "previewRows": [[json_value(value) for value in row] for row in rows[:preview_rows]],
            }
        )
    return ok_result(data={"source": str(source), "sheets": sheets})


if __name__ == "__main__":
    run_tool(main)

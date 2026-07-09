#!/usr/bin/env python3

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
from tool_contract import ok_result, required_string, run_tool

from spreadsheet_table_common import implementation, load_tables, normalized_table, require_source, write_table


def main(payload):
    source = require_source(required_string(payload, "input"))
    output = required_string(payload, "output")
    tables = load_tables(source, sheet=payload.get("sheet"), all_sheets=False)
    table = tables[0]
    headers, rows = normalized_table(table)
    output_path = write_table(output, headers, rows, sheet_name=table["name"])
    return ok_result(
        artifacts=[{"role": "exported_table", "path": str(output_path)}],
        data={
            "source": str(source),
            "sourceSha256": implementation().sha256(source),
            "output": str(output_path),
            "sheet": table["name"],
            "dataRowCount": len(rows),
            "columnCount": len(headers),
        },
    )


if __name__ == "__main__":
    run_tool(main)

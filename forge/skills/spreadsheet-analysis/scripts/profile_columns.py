#!/usr/bin/env python3

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
from tool_contract import ToolInputError, ok_result, required_string, run_tool

from spreadsheet_table_common import implementation, load_tables, require_source


def main(payload):
    source = require_source(required_string(payload, "input"))
    max_categories = int(payload.get("maxCategories", 20))
    if max_categories < 1:
        raise ToolInputError("invalid_max_categories", "maxCategories must be positive")
    tables = load_tables(source, sheet=payload.get("sheet"), all_sheets=bool(payload.get("allSheets", True)))
    impl = implementation()
    profile = {
        "schemaVersion": 1,
        "generatedAt": impl.utc_now(),
        "source": {
            "path": str(source),
            "format": source.suffix.lower().lstrip("."),
            "sha256": impl.sha256(source),
            "sizeBytes": source.stat().st_size,
        },
        "sheets": [impl.profile_table(table, max_categories) for table in tables],
    }
    artifacts = []
    output_directory = payload.get("outputDirectory")
    if output_directory:
        output = Path(output_directory).expanduser().resolve()
        if output.exists():
            raise ToolInputError("output_exists", f"Output directory already exists: {output}")
        output.mkdir(parents=True)
        (output / "data_profile.json").write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        (output / "data_profile.md").write_text(impl.profile_markdown(profile), encoding="utf-8")
        artifacts.extend(
            [
                {"role": "profile_json", "path": str(output / "data_profile.json")},
                {"role": "profile_markdown", "path": str(output / "data_profile.md")},
            ]
        )
    return ok_result(artifacts=artifacts, data={"profile": profile})


if __name__ == "__main__":
    run_tool(main)

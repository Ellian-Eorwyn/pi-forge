#!/usr/bin/env python3
"""Backfill the human-owned ``date`` property from date-prefixed filenames.

A bulk reorganization flattened this vault's file timestamps, so ``file.ctime``
and ``file.mtime`` no longer say when a note's content happened. For the notes
whose filename already carries the date, that fact is recoverable exactly, with
no model involved. Everything else keeps an empty ``date`` until a human sets it.

Dry run by default. ``--apply`` writes, and only after re-reading each file and
confirming its body is untouched by the rewrite.

    python3 scripts/backfill-vault-date.py --vault ~/Documents/Obsidian/Loom
    python3 scripts/backfill-vault-date.py --vault ~/Documents/Obsidian/Loom --apply
"""

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "forge" / "lib"))

from vault_schema import (  # noqa: E402
    UserError,
    compiled_schema_for,
    human_owned_properties,
    parse_frontmatter,
    relative_path,
    resolve_schema_path,
    selected_notes,
    serialize_frontmatter,
    split_frontmatter,
)

# Leading YYYY-MM-DD, optionally followed by a separator. Anchored to the start of
# the basename so a date appearing mid-title is not mistaken for the note's date.
DATE_PREFIX_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?=$|[ _.\-])")


def parsed_prefix_date(stem):
    match = DATE_PREFIX_RE.match(stem)
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        # A real calendar check, so 2026-02-30 is reported rather than written.
        return False


def plan_backfill(vault, schema_path):
    schema, _hash = compiled_schema_for(vault, schema_path)
    if "date" not in schema["property_order"]:
        raise UserError("schema does not define a 'date' property; add it before backfilling")
    if "date" not in human_owned_properties(schema):
        raise UserError("schema does not mark 'date' human-owned; the classifier would overwrite it")

    planned, skipped, invalid = [], [], []
    for path in selected_notes(vault, schema_path, "vault", None):
        rel = relative_path(vault, path)
        value = parsed_prefix_date(path.stem)
        if value is None:
            continue
        if value is False:
            invalid.append({"path": rel, "reason": "filename date is not a real calendar date"})
            continue
        try:
            split = split_frontmatter(path.read_bytes())
        except OSError as error:
            invalid.append({"path": rel, "reason": str(error)})
            continue
        if split["malformed"] or not split["had_frontmatter"]:
            skipped.append({"path": rel, "reason": "no usable frontmatter", "date": value})
            continue
        metadata = parse_frontmatter(split["frontmatter_text"])
        existing = metadata.get("date")
        if existing:
            if existing != value:
                skipped.append({"path": rel, "reason": f"already has date: {existing}", "date": value})
            continue
        planned.append({"path": rel, "date": value})
    return schema, planned, skipped, invalid


def apply_backfill(vault, schema, planned):
    written, failed = [], []
    for item in planned:
        path = vault / item["path"]
        data = path.read_bytes()
        split = split_frontmatter(data)
        metadata = parse_frontmatter(split["frontmatter_text"])
        if metadata.get("date"):
            failed.append({"path": item["path"], "reason": "date appeared since planning"})
            continue
        metadata["date"] = item["date"]
        revised = serialize_frontmatter(metadata, schema) + split["body"]
        encoded = revised.encode("utf-8")
        if split["had_bom"]:
            encoded = b"\xef\xbb\xbf" + encoded
        path.write_bytes(encoded)

        # The body is the note; only the frontmatter block was ours to touch.
        after = split_frontmatter(path.read_bytes())
        if after["body"] != split["body"]:
            path.write_bytes(data)
            failed.append({"path": item["path"], "reason": "body changed; reverted"})
            continue
        written.append(item)
    return written, failed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--schema")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    vault = args.vault.expanduser().resolve()
    if not vault.is_dir():
        raise UserError(f"vault is not a directory: {vault}")
    schema_path = resolve_schema_path(vault, args.schema)

    schema, planned, skipped, invalid = plan_backfill(vault, schema_path)
    if args.limit:
        planned = planned[: args.limit]

    result = {
        "status": "ok",
        "artifacts": [],
        "warnings": [item["reason"] for item in invalid],
        "errors": [],
        "data": {
            "dry_run": not args.apply,
            "vault": str(vault),
            "counts": {
                "planned": len(planned),
                "skipped": len(skipped),
                "invalid": len(invalid),
            },
            "planned": planned,
            "skipped": skipped,
            "invalid": invalid,
        },
    }

    if args.apply:
        written, failed = apply_backfill(vault, schema, planned)
        result["data"]["written"] = len(written)
        result["data"]["failed"] = failed
        if failed:
            result["status"] = "partial"
            result["errors"] = [f"{item['path']}: {item['reason']}" for item in failed]

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except UserError as error:
        print(json.dumps({"status": "error", "errors": [str(error)]}, ensure_ascii=False), file=sys.stdout)
        sys.exit(2)

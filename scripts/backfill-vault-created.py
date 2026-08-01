#!/usr/bin/env python3
"""Recover the derived ``created`` property from the evidence a vault still holds.

A bulk reorganization flattened this vault's file timestamps, so ``file.ctime``
and ``file.mtime`` say "recently touched" and never "made on". The approved
property list is closed, so any ``created`` key a note carried before the schema
migration was stripped the first time it was filed. Both facts are recoverable
for a large share of notes, from evidence that is still on disk -- but only if the
tiers stay distinguishable, because a date read off a filename and a date read off
a flattened mtime are not the same claim.

Tiers, best evidence first:

    backup   an organizer run backed the note up before rewriting it, and that
             copy still carries the pre-migration frontmatter
    git      the vault is a git repository and the note has a first commit
    filename the basename starts with YYYY-MM-DD, written by someone who meant it
    date     the note's own subject date, the closest remaining proxy
    file     birthtime or mtime -- the tier this vault's migration destroyed

There is deliberately no "today" tier. Stamping the run date on a thousand notes
records nothing; those notes are reported instead, and ``vault-organizer`` gives
them a date when it next files them.

Dry run by default. ``--apply`` writes, and only after re-reading each file and
confirming the rewrite left its body untouched.

    python3 scripts/backfill-vault-created.py --vault ~/Documents/Obsidian/Loom
    python3 scripts/backfill-vault-created.py --vault ~/Documents/Obsidian/Loom --min-tier filename
    python3 scripts/backfill-vault-created.py --vault ~/Documents/Obsidian/Loom --apply
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "forge" / "lib"))

from vault_schema import (  # noqa: E402
    UNSTAMPED_TYPES,
    UserError,
    compiled_schema_for,
    derived_properties,
    note_birthtime,
    normalized_date_value,
    parse_frontmatter,
    parsed_prefix_date,
    relative_path,
    resolve_schema_path,
    selected_notes,
    serialize_frontmatter,
    split_frontmatter,
)

# Ordered best evidence first; --min-tier accepts any of them and drops the rest.
TIERS = ("backup", "git", "filename", "date", "file")

TIER_REASONS = {
    "backup": "an organizer run's pre-migration backup of this note",
    "git": "the first commit adding this note to the vault repository",
    "filename": "a YYYY-MM-DD prefix on the note's filename",
    "date": "the note's own date property",
    "file": "file timestamps, which this vault's reorganization flattened",
}

# Frontmatter keys a note may have carried a creation date under before the
# schema closed the property list. The canonical parser only accepts lowercase
# snake_case keys, so the raw text is scanned instead -- these are exactly the
# spellings that would have been dropped rather than normalized.
LEGACY_CREATED_KEYS = ("created", "created_at", "createdat", "date_created", "datecreated", "creation_date")
LEGACY_KEY_RE = re.compile(
    r"^[ \t]*(?P<key>[A-Za-z][A-Za-z0-9_ -]*)[ \t]*:[ \t]*(?P<value>.+?)[ \t]*$",
    re.MULTILINE,
)

RUNS_DIR = ".vault-organizer/runs"


def normalized_legacy_key(key):
    return key.strip().lower().replace("-", "_").replace(" ", "_")


def created_from_frontmatter_text(text):
    """A creation date under any of its historical spellings, or None.

    Reads the raw block rather than ``parse_frontmatter`` because the whole point
    of a backup is that it predates normalization: a note written by hand may say
    ``Created:`` or ``date created:``, neither of which survives the canonical
    key pattern.
    """
    for match in LEGACY_KEY_RE.finditer(text or ""):
        if normalized_legacy_key(match.group("key")) not in LEGACY_CREATED_KEYS:
            continue
        value = normalized_date_value(match.group("value").strip().strip("\"'"))
        if value:
            return value
    return None


def backup_copies(vault, rel):
    """Every organizer-run backup of one note, oldest run first.

    Run directories are named with a sortable UTC timestamp, so lexicographic
    order is chronological order and the first hit is the earliest surviving copy
    -- the one closest to what the note said before any of this started.
    """
    runs = vault / RUNS_DIR
    if not runs.is_dir():
        return []
    found = []
    for run in sorted(path for path in runs.iterdir() if path.is_dir()):
        candidate = run / "backup" / rel
        if candidate.is_file():
            found.append(candidate)
    return found


def created_from_backups(vault, rel):
    for candidate in backup_copies(vault, rel):
        try:
            split = split_frontmatter(candidate.read_bytes())
        except OSError:
            continue
        value = created_from_frontmatter_text(split["frontmatter_text"])
        if value:
            return value
        value = normalized_date_value(parse_frontmatter(split["frontmatter_text"]).get("date"))
        if value:
            return value
    return None


def git_available(vault):
    """Whether the vault is a git working tree we can ask about first commits."""
    try:
        result = subprocess.run(
            ["git", "-C", str(vault), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def created_from_git(vault, rel):
    """The author date of the commit that first added a note, or None.

    ``--follow`` so a note that was renamed keeps the date it was written rather
    than the date it moved, which is the whole failure this backfill exists for.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(vault), "log", "--diff-filter=A", "--follow", "--format=%ad",
             "--date=short", "--", rel],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    dates = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return normalized_date_value(dates[-1]) if dates else None


def evidence_for(vault, path, rel, metadata, use_git):
    """The best creation date available for one note, paired with its tier."""
    value = created_from_backups(vault, rel)
    if value:
        return value, "backup"
    if use_git:
        value = created_from_git(vault, rel)
        if value:
            return value, "git"
    value = parsed_prefix_date(path.stem)
    if value:
        return value, "filename"
    value = normalized_date_value(metadata.get("date"))
    if value:
        return value, "date"
    value = note_birthtime(path)
    if value:
        return value, "file"
    return None, None


def plan_backfill(vault, schema_path, min_tier, use_git, limit=None):
    schema, _hash = compiled_schema_for(vault, schema_path)
    if "created" not in schema["property_order"]:
        raise UserError("schema does not define a 'created' property; add it before backfilling")
    if "created" not in derived_properties(schema):
        raise UserError("schema does not mark 'created' derived; the classifier would overwrite it")

    allowed = set(TIERS[: TIERS.index(min_tier) + 1])
    planned, present, skipped, unresolved = [], 0, [], []
    for path in selected_notes(vault, schema_path, "vault", None):
        rel = relative_path(vault, path)
        try:
            split = split_frontmatter(path.read_bytes())
        except OSError as error:
            skipped.append({"path": rel, "reason": str(error)})
            continue
        if split["malformed"] or not split["had_frontmatter"]:
            skipped.append({"path": rel, "reason": "no usable frontmatter"})
            continue
        metadata = parse_frontmatter(split["frontmatter_text"])
        if metadata.get("type") in UNSTAMPED_TYPES:
            # A template's installed bytes are compared with the shipped copy, so
            # a stamped one is refused as owner-modified by every later install.
            continue
        if normalized_date_value(metadata.get("created")):
            present += 1
            continue
        value, tier = evidence_for(vault, path, rel, metadata, use_git)
        if not value:
            unresolved.append({"path": rel, "reason": "no evidence of a creation date"})
            continue
        if tier not in allowed:
            skipped.append({"path": rel, "reason": f"{tier} evidence is below --min-tier {min_tier}", "created": value})
            continue
        planned.append({"path": rel, "created": value, "tier": tier, "because": TIER_REASONS[tier]})
    if limit:
        planned = planned[:limit]
    return schema, planned, present, skipped, unresolved


def apply_backfill(vault, schema, planned):
    written, failed = [], []
    for item in planned:
        path = vault / item["path"]
        data = path.read_bytes()
        split = split_frontmatter(data)
        metadata = parse_frontmatter(split["frontmatter_text"])
        if metadata.get("created"):
            failed.append({"path": item["path"], "reason": "created appeared since planning"})
            continue
        metadata["created"] = item["created"]
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


def tier_counts(items):
    counts = {}
    for item in items:
        counts[item["tier"]] = counts.get(item["tier"], 0) + 1
    return {tier: counts[tier] for tier in TIERS if tier in counts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--schema")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int)
    # Defaults to excluding the file tier. This vault's timestamps were flattened
    # by the reorganization that lost the dates in the first place, so writing
    # them back would launder one bad date into a thousand confident ones.
    parser.add_argument(
        "--min-tier", choices=TIERS, default="date",
        help="lowest evidence tier to write; the rest are reported and left alone (default: date)",
    )
    parser.add_argument("--no-git", action="store_true", help="skip the git tier even in a versioned vault")
    args = parser.parse_args()

    vault = args.vault.expanduser().resolve()
    if not vault.is_dir():
        raise UserError(f"vault is not a directory: {vault}")
    schema_path = resolve_schema_path(vault, args.schema)
    use_git = not args.no_git and git_available(vault)

    schema, planned, present, skipped, unresolved = plan_backfill(
        vault, schema_path, args.min_tier, use_git, args.limit
    )

    warnings = []
    if not use_git and not args.no_git:
        warnings.append("vault is not a git repository; the git evidence tier was unavailable")
    if unresolved:
        warnings.append(
            f"{len(unresolved)} notes have no evidence of a creation date and were left alone;"
            " vault-organizer will stamp them with its run date when it next files them"
        )

    result = {
        "status": "ok",
        "artifacts": [],
        "warnings": warnings,
        "errors": [],
        "data": {
            "dry_run": not args.apply,
            "vault": str(vault),
            "min_tier": args.min_tier,
            "git_tier_used": use_git,
            "counts": {
                "planned": len(planned),
                "already_present": present,
                "skipped": len(skipped),
                "unresolved": len(unresolved),
            },
            "by_tier": tier_counts(planned),
            "planned": planned,
            "skipped": skipped,
            "unresolved": unresolved,
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

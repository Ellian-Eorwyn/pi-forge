#!/usr/bin/env python3
"""Copy the work material out of a personal vault into a vault of its own.

A one-off. The Loom vault grew a `work` domain subdivided by activity --
`building-energy`, `market-research` -- and the split reorganizes that material
into research areas with projects directly beneath them. Two vaults, two
schemas, and no shared notes except a handful of deliberately duplicated
identity anchors.

Nothing here rewrites a link. Obsidian resolves `[[Note]]` by basename rather
than by path, and the whole work cluster moves together with its people and
organizations, so the internal graph survives the copy untouched. Only links
that cross the boundary change, and those are counted in the report rather than
edited.

Placement is not guessed. Each note's destination is compiled by
`vault_schema.compile_destination` -- the same function `vault-organizer` files
with -- so a note lands exactly where the target schema says it belongs, and a
schema the compiler refuses stops the run before anything is copied.

Three rules decide a note's new domain, in order:

    project    the note names a project the target schema registers, whose
               domain is then authoritative. 262 of 291 work notes.
    legacy     the note's old `subdomain` has an entry in the target schema's
               Legacy normalization map -- `data-centers`, `proposals`.
    inbox      neither applies, so the note is staged in `00 Inbox` with
               `status: raw` for `vault-organizer` to classify against the new
               schema. This is the only place judgment is involved.

`building-energy` and `market-research` deliberately have no legacy entry. They
described what was being done rather than what it was about, which is the reason
for the split, so a note carrying one is classified afresh.

Copies, never moves: the source vault is opened read-only and is still intact
when the run finishes. Verify the target, then delete from the source as a
separate reviewed step.

    python3 scripts/migrate-work-vault.py --source ~/Documents/Obsidian/Loom \\
        --target ~/Documents/Obsidian/Work
    python3 scripts/migrate-work-vault.py --source ~/Documents/Obsidian/Loom \\
        --target ~/Documents/Obsidian/Work --apply
"""

import argparse
import codecs
import datetime
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "forge" / "lib"))

from vault_schema import (  # noqa: E402
    INBOX_DIR,
    UserError,
    compile_destination,
    compiled_schema_for,
    ensure_workspace_marker,
    parse_frontmatter,
    resolve_schema_path,
    revised_note_text,
    split_frontmatter,
)

# Where the work material lives in the source vault. A trailing name means one
# file; everything else is a tree walked recursively.
SOURCE_TREES = ("07 Work", "09 Directory")
# Source notes are spread through the sources tree by kind, so they are found by
# their frontmatter rather than by path.
SOURCE_ROOT = "10 Sources"
# Machine output that cost a model run to make and cannot be rebuilt from the
# vault. Copied verbatim to the same relative path: it is workspace, not notes,
# and is never classified or refiled.
#
# The `.forge-workspace` marker that makes that true sits on the *category*
# folder, one level above the run being copied, so it does not travel with the
# tree. Without it the target vault sees several hundred extraction packets as
# ordinary notes -- indexed, deduped against, and eventually filed. Each tree
# therefore names the ancestor that has to be marked on arrival.
WORKSPACE_TREES = ("99 Meta/99.06 Workflows/Project Extractions/Waste-Heat",)
WORKSPACE_ROOTS = ("99 Meta/99.06 Workflows/Project Extractions",)

# Person notes that are not work. Ellie's own note is the one exception that is
# copied rather than moved -- both vaults need something to point `people` at.
PERSONAL_CONTACTS = ("Gillian Eorwyn", "Sopagna Braje")
DUPLICATED_CONTACTS = ("Ellian Eorwyn",)
# Filed under `domain: work` and about a symphony. Misfiled, and staying -- the
# transcript with it, since a source note follows its parent. The third is a
# recording of website feedback for the same organization, which landed in
# `7.03 Market Research` and is not market research.
MISFILED = (
    "07 Work/2026-04-04 - Meeting - Discovery Of New Chamber Orchestra Works.md",
    "10 Sources/10.03 Transcript/Work/2026-04-04 - Meeting - Discovery Of New Chamber Orchestra Works - Transcript.md",
    "07 Work/7.03 Market Research/20260730 120019-F709DCCE 2026-07-30 12_04_15.md",
)
# Hub notes for folders the new schema does not have. Both describe the old
# activity-based layout and embed a Base that exists only in the source vault,
# so they would arrive stale and pointing at nothing. The work vault gets its
# own hubs instead.
STALE_HUBS = ("07 Work/00 Work.md", "09 Directory/00 Directory.md")

# The old domain everything here carries. A note without it is not work.
LEGACY_DOMAIN = "work"
ENTITY_SUBDOMAIN = {"person": "contacts", "organization": "organizations"}
DIRECTORY_DOMAIN = "directory"


def utc_stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def read_note(path):
    """`(metadata, raw bytes)`, or `(None, raw)` when there is no frontmatter."""
    raw = path.read_bytes()
    try:
        data = split_frontmatter(raw)
    except (UnicodeDecodeError, UserError):
        return None, raw
    text = data.get("frontmatter_text")
    if not text:
        return None, raw
    try:
        metadata = parse_frontmatter(text)
    except (UserError, ValueError):
        return None, raw
    return (metadata if isinstance(metadata, dict) else None), raw


def rewritten_bytes(raw, metadata, schema):
    """The note with new frontmatter and a byte-identical body.

    The body is carried across rather than re-rendered, so line endings and
    trailing whitespace survive the move; only the frontmatter block is rebuilt,
    by the same function every other vault skill writes notes with.
    """
    data = split_frontmatter(raw)
    text = revised_note_text(metadata, schema, data.get("body") or "")
    return (codecs.BOM_UTF8 if data.get("had_bom") else b"") + text.encode("utf-8")


def target_domain(metadata, schema):
    """`(domain, rule)` for a note under the target schema, or `(None, "inbox")`."""
    project = metadata.get("project")
    values = project if isinstance(project, list) else [project] if project else []
    for value in values:
        registered = schema["projects"].get(str(value).strip())
        if registered:
            return registered["domain"], "project"
    legacy = schema.get("legacy") or {}
    mapped = legacy.get(f"subdomain:{metadata.get('subdomain')}")
    if mapped and mapped.get("domain") in schema["domains"]:
        return mapped["domain"], "legacy"
    return None, "inbox"


def unreadable_reason(raw):
    """Why a note has no frontmatter, distinguishing a defect from an absence.

    A note whose `---` is preceded by a space or a tab looks frontmatter-less to
    every reader in the pipeline while looking perfectly normal to a human. That
    is worth naming separately, because the fix is one character and the note has
    been invisible until someone is told.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "inbox-unreadable"
    stripped = text.lstrip("﻿")
    if stripped.lstrip().startswith("---") and not stripped.startswith("---"):
        return "inbox-indented-frontmatter"
    return "inbox-no-frontmatter"


def plan_note(relative, metadata, schema):
    """`(destination folder, new metadata, rule)` for one note."""
    updated = dict(metadata)
    note_type = updated.get("type")

    if note_type in ENTITY_SUBDOMAIN:
        updated["domain"] = DIRECTORY_DOMAIN
        updated["subdomain"] = ENTITY_SUBDOMAIN[note_type]
        updated.pop("project", None)
        return compile_destination(schema, updated), updated, "entity"

    domain, rule = target_domain(updated, schema)
    if domain is None:
        # Staged for the classifier. It replaces frontmatter wholesale, so the
        # old routing values are dropped rather than left to be mistrusted.
        updated["status"] = "raw"
        for key in ("domain", "subdomain"):
            updated.pop(key, None)
        return INBOX_DIR, updated, rule

    updated["domain"] = domain
    # Research areas have no subdomains; the project registry is their structure.
    updated.pop("subdomain", None)
    return compile_destination(schema, updated), updated, rule


def collect(source, target, schema):
    """Every planned copy, plus the notes deliberately left behind."""
    planned = []
    skipped = []
    seen_names = {}

    def consider(path, relative):
        name = path.stem
        if relative in MISFILED:
            skipped.append({"path": relative, "reason": "misfiled in the source; not work"})
            return
        if relative in STALE_HUBS:
            skipped.append({"path": relative, "reason": "hub for a folder the new schema does not have"})
            return
        if name in PERSONAL_CONTACTS:
            skipped.append({"path": relative, "reason": "personal contact; stays in the source vault"})
            return
        metadata, raw = read_note(path)
        if metadata is None:
            if not raw.strip():
                skipped.append({"path": relative, "reason": "empty file; nothing to carry over"})
                return
            # Real content that nothing can classify. Leaving it behind would
            # lose it, so it goes to the inbox byte-for-byte and the classifier
            # gets the first look at it -- which is what the inbox is for.
            destination, updated, rule = INBOX_DIR, None, unreadable_reason(raw)
        else:
            destination, updated, rule = plan_note(relative, metadata, schema)
        entry = {
            "source": relative,
            "destination": f"{destination}/{path.name}" if destination else path.name,
            "rule": rule,
            "type": (updated or {}).get("type"),
            "domain": (updated or {}).get("domain"),
            "project": (updated or {}).get("project"),
            "duplicated": name in DUPLICATED_CONTACTS,
        }
        collision = seen_names.get(name)
        if collision:
            entry["collision_with"] = collision
        seen_names[name] = relative
        entry["_metadata"] = updated
        entry["_raw"] = raw
        planned.append(entry)

    for tree in SOURCE_TREES:
        root = source / tree
        if not root.is_dir():
            raise UserError(f"source tree is missing: {tree}")
        for path in sorted(root.rglob("*.md")):
            consider(path, str(path.relative_to(source)))

    sources_root = source / SOURCE_ROOT
    if sources_root.is_dir():
        for path in sorted(sources_root.rglob("*.md")):
            metadata, _raw = read_note(path)
            if metadata and metadata.get("domain") == LEGACY_DOMAIN:
                consider(path, str(path.relative_to(source)))

    workspace = []
    for tree in WORKSPACE_TREES:
        root = source / tree
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file():
                workspace.append(str(path.relative_to(source)))
    return planned, skipped, workspace


def check_destinations(target, planned):
    """Every problem with the plan, before a byte is written.

    Checked as a whole rather than per file: a run that fails on the four
    hundredth note has already copied three hundred and ninety-nine, and a
    half-migrated vault is worse than an unmigrated one.
    """
    problems = []
    claimed = {}
    for entry in planned:
        destination = entry["destination"]
        if (target / destination).exists():
            problems.append(f"{destination} already exists in the target")
        earlier = claimed.get(destination.lower())
        if earlier:
            problems.append(f"{entry['source']} and {earlier} both want {destination}")
        claimed[destination.lower()] = entry["source"]
    return problems


def apply_plan(source, target, planned, workspace, schema):
    """Copy every planned file. Never overwrites, and never starts unless it can finish."""
    problems = check_destinations(target, planned)
    if problems:
        listed = "\n  ".join(problems[:20])
        more = f"\n  ... and {len(problems) - 20} more" if len(problems) > 20 else ""
        raise UserError(f"the plan cannot be applied as it stands:\n  {listed}{more}")
    written = []
    for entry in planned:
        destination = target / entry["destination"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        if entry["_metadata"] is None:
            # Nothing readable to rewrite, so the bytes travel exactly as they are.
            shutil.copy2(source / entry["source"], destination)
        else:
            destination.write_bytes(rewritten_bytes(entry["_raw"], entry["_metadata"], schema))
            shutil.copystat(source / entry["source"], destination)
        written.append(entry["destination"])
    for relative in workspace:
        destination = target / relative
        if destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, destination)
        written.append(relative)
    # Marked after the files land, so an interrupted copy never leaves a marked
    # empty folder, and before anything reads the vault.
    for relative in WORKSPACE_ROOTS:
        if (target / relative).is_dir():
            written.append(str(ensure_workspace_marker(target / relative).relative_to(target)))
    return written


def report(planned, skipped, workspace, written, run_dir, applied, problems):
    rules = {}
    domains = {}
    for entry in planned:
        rules[entry["rule"]] = rules.get(entry["rule"], 0) + 1
        key = entry["domain"] or INBOX_DIR
        domains[key] = domains.get(key, 0) + 1
    collisions = [entry for entry in planned if "collision_with" in entry]
    lines = [
        "# Work vault migration",
        "",
        f"- notes planned: {len(planned)}",
        f"- workspace files: {len(workspace)}",
        f"- left in the source: {len(skipped)}",
        f"- basename collisions: {len(collisions)}",
        f"- blocking problems: {len(problems)}",
        f"- applied: {'yes' if applied else 'no (dry run)'}",
        "",
        "## How each note was routed",
        "",
    ]
    if problems:
        lines += ["", "## Blocking problems", "", "`--apply` refuses while any of these stands.", ""]
        lines += [f"- {problem}" for problem in problems]
        lines += [""]
    for rule, count in sorted(rules.items(), key=lambda kv: -kv[1]):
        lines.append(f"- `{rule}` — {count}")
    lines += ["", "## Destination domain", ""]
    for domain, count in sorted(domains.items(), key=lambda kv: -kv[1]):
        lines.append(f"- `{domain}` — {count}")
    if collisions:
        lines += ["", "## Basename collisions", "", "Obsidian resolves wikilinks by basename, so two notes sharing one are ambiguous.", ""]
        for entry in collisions:
            lines.append(f"- `{entry['source']}` and `{entry['collision_with']}`")
    lines += ["", "## Left in the source vault", ""]
    for entry in skipped:
        lines.append(f"- `{entry['path']}` — {entry['reason']}")
    (run_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "applied": applied,
                "planned": [{k: v for k, v in entry.items() if not k.startswith("_")} for entry in planned],
                "skipped": skipped,
                "workspace": workspace,
                "written": written,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "planned": len(planned),
        "workspace": len(workspace),
        "skipped": len(skipped),
        "collisions": len(collisions),
        "problems": problems,
        "rules": rules,
        "domains": domains,
        "runDirectory": str(run_dir),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Copy work material into its own vault.")
    parser.add_argument("--source", required=True, help="the personal vault to read")
    parser.add_argument("--target", required=True, help="the work vault to write")
    parser.add_argument("--apply", action="store_true", help="write; otherwise plan only")
    parser.add_argument("--run-dir", help="where the manifest and report go")
    args = parser.parse_args(argv)

    source = Path(args.source).expanduser().resolve()
    target = Path(args.target).expanduser().resolve()
    if source == target:
        raise UserError("source and target are the same vault")

    schema, _hash = compiled_schema_for(target, resolve_schema_path(target, None))
    run_dir = Path(args.run_dir) if args.run_dir else target / ".migration" / utc_stamp()
    run_dir.mkdir(parents=True, exist_ok=True)

    planned, skipped, workspace = collect(source, target, schema)
    problems = check_destinations(target, planned)
    written = apply_plan(source, target, planned, workspace, schema) if args.apply else []
    summary = report(planned, skipped, workspace, written, run_dir, args.apply, problems)
    print(json.dumps({"status": "ok", "data": summary}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except UserError as error:
        print(json.dumps({"status": "error", "errors": [str(error)]}, indent=2))
        raise SystemExit(1)

#!/usr/bin/env python3
"""Resolve a registered project into the closed set of files an agent may use.

Handing an agent a folder works because the folder is the answer to "what may I
read". A vault breaks that: the sources a project cites live once in the sources
tree because two projects cite them, so the folder is no longer the whole
project, and copying them in to make it whole is how a vault stops having one
copy of anything.

This skill keeps the folder handoff and gives up nothing. The project's hub note
carries a `## Corpus` section a person maintains by hand -- links to the sources,
people, organizations, and wiki cards that belong to the work, each with a line
saying why -- and that section is also the machine-readable definition of scope.
`emit` freezes the resolution into `_corpus.json` beside the hub, so an agent
that has never heard of pi-forge can open the folder, read one file, and know
every path it is allowed to read.

Everything here is deterministic: a folder walk, one hub parse, and a header read
per member. No model is called and nothing is fetched. Only `emit --apply` writes
to the vault, and the only file it writes is its own manifest.
"""

import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
import vault_corpus
from vault_schema import (
    WORKSPACE_MARKER,
    UserError,
    compile_destination,
    compiled_schema_for,
    note_title,
    relative_path,
    resolve_schema_path,
    selected_notes,
    serialize_frontmatter,
)

WORKFLOW = "vault-projects"
STATE_DIR = ".vault-projects"
PACK_CATEGORY = "Project Packs"
DEFAULT_BUDGET = 100000
# A rough token estimate is the right precision here. The number exists to keep a
# pack inside a context window with room to work in, and four characters per
# token is close enough for that while costing no tokenizer dependency.
CHARS_PER_TOKEN = 4

# Which `### Subsection` a note is drafted under, by its note type. Only a
# starting point: the headings are the owner's to rename, and the resolver reads
# whatever they are.
DRAFT_SECTIONS = (
    ("Sources", ("source",)),
    ("People", ("person",)),
    ("Organizations", ("organization",)),
    ("Wiki", ("concept", "term", "practice", "work", "place", "event", "animal", "plant", "fungus")),
    ("Working notes", ()),
)


def structured(status, artifacts=None, warnings=None, errors=None, data=None):
    return {
        "status": status,
        "artifacts": artifacts or [],
        "warnings": warnings or [],
        "errors": errors or [],
        "data": data,
    }


def error_entry(code, message):
    return {"code": code, "message": message}


def print_json(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def resolve_vault(raw):
    vault = Path(raw).expanduser().resolve()
    if not vault.is_dir():
        raise UserError(f"vault root does not exist: {vault}")
    return vault


def load_schema(args):
    vault = resolve_vault(args.vault)
    schema_path = resolve_schema_path(vault, args.schema)
    schema, schema_hash = compiled_schema_for(
        vault, schema_path, cache_dir=vault / STATE_DIR / "cache"
    )
    return vault, schema, schema_hash


def run_directory(vault, schema, name):
    """Where generated artifacts go: the vault's workflow root, never a domain.

    The directory is marked as a workspace so the vault skills skip its whole
    tree. A pack is a copy of notes that already exist; letting it be classified,
    refiled, or counted as a corpus member would be the duplication this skill
    exists to avoid.
    """
    try:
        base = vault / compile_destination(schema, {"domain": "meta", "subdomain": "workflows"}) / PACK_CATEGORY
    except (KeyError, UserError):
        base = vault / "forge-output" / WORKFLOW
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    directory = base / name / stamp
    directory.mkdir(parents=True, exist_ok=True)
    marker = base / WORKSPACE_MARKER
    if not marker.exists():
        marker.write_text(
            "This folder holds generated pi-forge run artifacts, not vault notes.\n"
            "Its whole tree is skipped by classification, filing, and corpus resolution.\n",
            encoding="utf-8",
        )
    return directory


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def command_list(args):
    vault, schema, schema_hash = load_schema(args)
    rows = []
    for name, project in sorted(vault_corpus.registered_projects(schema).items()):
        located = vault_corpus.hub_candidates(vault, project)
        existing = vault_corpus.read_manifest(vault, project)
        row = {
            "project": name,
            "value": project["value"],
            "folder": project["folder"],
            "folderExists": located["folder_exists"],
            "hub": located["hub"],
            "manifest": None,
        }
        if existing and not existing.get("unreadable"):
            row["manifest"] = {
                "generated": existing.get("generated"),
                "members": (existing.get("counts") or {}).get("members"),
            }
        elif existing:
            row["manifest"] = {"unreadable": True}
        rows.append(row)
    missing = [row["project"] for row in rows if not row["hub"]]
    warnings = []
    if missing:
        warnings.append(f"{len(missing)} registered projects have no hub note: {', '.join(missing)}")
    return structured(
        "ok",
        warnings=warnings,
        data={"projects": rows, "counts": {"registered": len(rows), "withoutHub": len(missing)}},
    )


def command_resolve(args):
    vault, schema, schema_hash = load_schema(args)
    resolution = vault_corpus.resolve_corpus(vault, schema, schema_hash, args.project, digest=not args.no_hash)
    blocking = vault_corpus.blocking_problems(resolution)
    payload = {
        "project": resolution["project"]["name"],
        "folder": resolution["project"]["folder"],
        "hub": resolution["hub"],
        "counts": resolution["counts"],
        "members": resolution["members"],
        "unresolved": resolution["unresolved"],
        "ambiguous": resolution["ambiguous"],
        "excluded": resolution["excluded"],
        "problems": resolution["problems"],
    }
    if blocking:
        return structured(
            "error",
            errors=[error_entry(problem["code"], problem["message"]) for problem in blocking],
            data=payload,
        )
    warnings = [problem["message"] for problem in resolution["problems"] if problem["severity"] == "warning"]
    return structured("ok", warnings=warnings, data=payload)


def command_doctor(args):
    vault, schema, schema_hash = load_schema(args)
    projects = vault_corpus.registered_projects(schema)
    names = [args.project] if args.project else sorted(projects)
    reports = []
    errors = 0
    for name in names:
        resolution = vault_corpus.resolve_corpus(vault, schema, schema_hash, name, digest=True)
        project = resolution["project"]
        existing = vault_corpus.read_manifest(vault, project)
        fresh = vault_corpus.build_manifest(resolution, generated=None)
        drift = vault_corpus.compare_manifest(existing, fresh)
        problems = list(resolution["problems"])
        if drift["state"] == "stale":
            problems.append(
                {
                    "code": "manifest_stale",
                    "severity": "error",
                    "path": f"{project['folder']}/{vault_corpus.MANIFEST_NAME}",
                    "message": (
                        f"the manifest lists a different corpus than the vault does now "
                        f"(+{len(drift['added'])} -{len(drift['removed'])}); run `emit --apply`"
                    ),
                }
            )
        elif drift["state"] == "unreadable":
            problems.append(
                {
                    "code": "manifest_unreadable",
                    "severity": "error",
                    "path": f"{project['folder']}/{vault_corpus.MANIFEST_NAME}",
                    "message": "the manifest is not valid JSON; run `emit --apply` to rewrite it",
                }
            )
        errors += sum(1 for problem in problems if problem["severity"] == "error")
        reports.append(
            {
                "project": project["name"],
                "hub": resolution["hub"],
                "counts": resolution["counts"],
                "manifest": drift["state"],
                "drift": drift,
                "problems": problems,
            }
        )
    return structured(
        "ok" if not errors else "error",
        errors=[error_entry("corpus_problems", f"{errors} blocking problems across {len(reports)} projects")]
        if errors
        else [],
        data={"projects": reports, "counts": {"checked": len(reports), "errors": errors}},
    )


def command_emit(args):
    vault, schema, schema_hash = load_schema(args)
    resolution = vault_corpus.resolve_corpus(vault, schema, schema_hash, args.project, digest=True)
    project = resolution["project"]
    blocking = vault_corpus.blocking_problems(resolution)
    if blocking:
        return structured(
            "error",
            errors=[error_entry(problem["code"], problem["message"]) for problem in blocking],
            data={
                "project": project["name"],
                "counts": resolution["counts"],
                "problems": resolution["problems"],
            },
        )
    generated = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
    manifest = vault_corpus.build_manifest(resolution, generated=generated)
    existing = vault_corpus.read_manifest(vault, project)
    drift = vault_corpus.compare_manifest(existing, manifest)
    warnings = [problem["message"] for problem in resolution["problems"] if problem["severity"] == "warning"]
    artifacts = []
    if args.apply:
        path = vault_corpus.write_manifest(vault, project, manifest)
        artifacts.append({"path": relative_path(vault, path), "kind": "corpus-manifest"})
    return structured(
        "ok",
        artifacts=artifacts,
        warnings=warnings,
        data={
            "dryRun": not args.apply,
            "project": project["name"],
            "manifestPath": f"{project['folder']}/{vault_corpus.MANIFEST_NAME}",
            "counts": resolution["counts"],
            "drift": drift,
            "manifest": manifest if not args.apply else None,
        },
    )


def command_pack(args):
    vault, schema, schema_hash = load_schema(args)
    resolution = vault_corpus.resolve_corpus(vault, schema, schema_hash, args.project, digest=False)
    project = resolution["project"]
    blocking = vault_corpus.blocking_problems(resolution)
    if blocking:
        return structured(
            "error",
            errors=[error_entry(problem["code"], problem["message"]) for problem in blocking],
            data={"project": project["name"], "problems": resolution["problems"]},
        )

    order = {vault_corpus.ROLE_HUB: 0}
    members = sorted(
        resolution["members"],
        key=lambda record: (order.get(record["role"], 1), record["role"], record["path"]),
    )
    budget_chars = args.budget * CHARS_PER_TOKEN
    chunks = []
    included = []
    skipped = []
    used = 0
    for record in members:
        path = vault / record["path"]
        if not vault_corpus.is_markdown(path):
            skipped.append({"path": record["path"], "reason": "not Markdown"})
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            skipped.append({"path": record["path"], "reason": "unreadable"})
            continue
        header = f"\n\n===== {record['path']} ({record['role']}) =====\n\n"
        if used + len(header) + len(text) > budget_chars:
            skipped.append({"path": record["path"], "reason": "over budget"})
            continue
        chunks.append(header + text)
        used += len(header) + len(text)
        included.append(record["path"])

    directory = run_directory(vault, schema, project["name"])
    pack_path = directory / f"{project['name']} corpus pack.md"
    preamble = (
        f"# {project['name']} — corpus pack\n\n"
        f"Every file below is a member of this project's corpus, resolved from "
        f"`{resolution['hub']}` on {datetime.date.today().isoformat()}. "
        "Answer only from this material; if something needed is absent, say so.\n"
    )
    pack_path.write_text(preamble + "".join(chunks) + "\n", encoding="utf-8")
    warnings = []
    if skipped:
        over = [item for item in skipped if item["reason"] == "over budget"]
        if over:
            warnings.append(
                f"{len(over)} members did not fit the {args.budget}-token budget and are absent from the pack"
            )
    return structured(
        "ok",
        artifacts=[{"path": relative_path(vault, pack_path), "kind": "corpus-pack"}],
        warnings=warnings,
        data={
            "project": project["name"],
            "budgetTokens": args.budget,
            "estimatedTokens": used // CHARS_PER_TOKEN,
            "counts": {"members": len(resolution["members"]), "included": len(included), "skipped": len(skipped)},
            "skipped": skipped,
        },
    )


def draft_link(record):
    """Link by filename, display by heading when a filename says nothing.

    Some sources are filed under whatever the download was called. `[[15]]` is a
    correct link and a useless line in a hub a person is supposed to read, so the
    draft carries the note's own H1 as the display text. Resolution still goes by
    the filename before the pipe, so the alias cannot break the link.
    """
    title = record["title"]
    heading = (record.get("heading") or "").strip()
    if not heading or heading == title or any(character in heading for character in "[]|#^"):
        return f"[[{title}]]"
    return f"[[{title}|{heading}]]"


def command_draft_hub(args):
    vault, schema, schema_hash = load_schema(args)
    project = vault_corpus.find_project(schema, args.project)
    located = vault_corpus.hub_candidates(vault, project)
    prefix = f"{project['folder']}/"

    outside = []
    inside = []
    for path in selected_notes(vault, resolve_schema_path(vault, args.schema), "vault", None):
        relative = path.relative_to(vault).as_posix()
        note = vault_corpus.read_note(vault, relative)
        if note["metadata"].get("project") != project["value"]:
            continue
        record = {
            "path": relative,
            "title": Path(relative).stem,
            "heading": note_title(Path(relative), note["body"]),
            "type": note["metadata"].get("type", ""),
            "source_kind": note["metadata"].get("source_kind", ""),
        }
        (inside if relative.startswith(prefix) else outside).append(record)

    buckets = {heading: [] for heading, _ in DRAFT_SECTIONS}
    for record in sorted(outside, key=lambda item: (item["type"], item["title"])):
        placed = False
        for heading, types in DRAFT_SECTIONS:
            if types and record["type"] in types:
                buckets[heading].append(record)
                placed = True
                break
        if not placed:
            buckets["Working notes"].append(record)

    metadata = {
        "type": "project",
        "status": "active",
        "domain": project["domain"],
        "project": project["value"],
    }
    if project.get("subdomain"):
        metadata["subdomain"] = project["subdomain"]
    lines = [serialize_frontmatter(metadata, schema).rstrip("\n"), "", f"# {project['name']}", ""]
    lines += [
        "> [!summary]",
        f"> {project.get('definition') or 'What this project is and what it is for.'}",
        "",
        "Replace this paragraph with the state of the work: what is done, what is next,",
        "and anything a person picking this up in six months would need to know.",
        "",
        "## Corpus",
        "",
        "Everything in this folder is part of the project already. List below only what",
        "lives elsewhere in the vault, with a line on why it belongs.",
        "",
    ]
    for heading, _ in DRAFT_SECTIONS:
        records = buckets[heading]
        lines.append(f"### {heading}")
        lines.append("")
        if records:
            for record in records:
                lines.append(f"- {draft_link(record)} — ")
        else:
            lines.append(f"<!-- no notes carry project: {project['value']} for this role yet -->")
        lines.append("")
    lines += ["## Notes", ""]
    draft = "\n".join(lines).rstrip() + "\n"

    directory = run_directory(vault, schema, project["name"])
    draft_path = directory / f"{project['name']}.md"
    draft_path.write_text(draft, encoding="utf-8")
    inventory_path = directory / "folder-inventory.json"
    inventory_path.write_text(
        json.dumps({"inFolder": inside, "outsideFolder": outside}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    warnings = []
    if located["hub"]:
        warnings.append(
            f"a hub already exists at {located['hub']}; this draft is a separate file for comparison, "
            "not a replacement"
        )
    if not outside:
        warnings.append(
            f"no notes outside the folder carry project: {project['value']}, so every role section is empty; "
            "add links by hand or file the notes first"
        )
    return structured(
        "ok",
        artifacts=[
            {"path": relative_path(vault, draft_path), "kind": "hub-draft"},
            {"path": relative_path(vault, inventory_path), "kind": "folder-inventory"},
        ],
        warnings=warnings,
        data={
            "project": project["name"],
            "existingHub": located["hub"],
            "counts": {
                "inFolder": len(inside),
                "outsideFolder": len(outside),
                "byRole": {heading: len(buckets[heading]) for heading, _ in DRAFT_SECTIONS},
            },
            "draft": draft if args.print_draft else None,
        },
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def add_common(parser):
    parser.add_argument("--vault", required=True)
    parser.add_argument("--schema")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Resolve a project into the closed set of files an agent may use.")
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="every registered project, its hub, and its manifest")
    add_common(listing)

    resolve = sub.add_parser("resolve", help="resolve one project's corpus and print the members")
    add_common(resolve)
    resolve.add_argument("--project", required=True)
    resolve.add_argument("--no-hash", action="store_true", help="skip member hashing for a faster read")

    doctor = sub.add_parser("doctor", help="check hubs, links, closures, and manifest freshness")
    add_common(doctor)
    doctor.add_argument("--project", help="one project instead of every registered one")

    emit = sub.add_parser("emit", help="write the resolved corpus to _corpus.json beside the hub")
    add_common(emit)
    emit.add_argument("--project", required=True)
    emit.add_argument("--apply", action="store_true", help="write the manifest; without this it is a dry run")

    pack = sub.add_parser("pack", help="concatenate the corpus into one budgeted context file")
    add_common(pack)
    pack.add_argument("--project", required=True)
    pack.add_argument("--budget", type=int, default=DEFAULT_BUDGET, help="token budget; defaults to 100000")

    draft = sub.add_parser("draft-hub", help="draft a hub skeleton from notes that carry this project")
    add_common(draft)
    draft.add_argument("--project", required=True)
    draft.add_argument("--print-draft", action="store_true", help="include the draft text in the JSON payload")

    return parser.parse_args(argv)


COMMANDS = {
    "list": command_list,
    "resolve": command_resolve,
    "doctor": command_doctor,
    "emit": command_emit,
    "pack": command_pack,
    "draft-hub": command_draft_hub,
}


def main(argv=None):
    args = parse_args(argv)
    try:
        payload = COMMANDS[args.command](args)
    except UserError as error:
        print_json(structured("error", errors=[error_entry("user_error", str(error))]))
        return 2
    print_json(payload)
    return 0 if payload["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())

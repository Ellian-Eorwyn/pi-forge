#!/usr/bin/env python3
"""Resolve a registered project into the closed set of notes an agent may use.

A project folder is already a handoff unit for anything filed inside it: point an
agent at the folder and it has the files. What that misses is everything a
project shares -- a book cited by two articles lives once in the sources tree, a
person note is one note however many projects they touch -- and copying those in
to make the folder complete is how a vault stops having one copy of anything.

So membership has two halves. Everything under the project folder is in by
position, and everything else is in because the project's hub note says so, in a
``## Corpus`` section a person maintains by hand and reads for pleasure. The hub
is the human's map of the project and the machine's definition of its scope at
the same time, which is the only arrangement where the two cannot drift apart.

Design rules:

- Standard library only, and no model. Resolution is a folder walk, one hub
  parse, and a header read per member; the same inputs always give the same set.
- Closed world. A link resolves or it is reported unresolved; an ambiguous
  basename is an error rather than a guess, because guessing wrong here silently
  changes what an agent is allowed to read.
- The body is the contract. Corpus semantics live in the hub's ``## Corpus``
  section and in a non-Markdown manifest, never in frontmatter -- the vault's
  approved-property list is closed and a ``corpus:`` key would be deleted on the
  next rewrite.
- Two closures, no expansion. A processed note pulls the transcript it names and
  a note pulls the images it embeds, because those are one document in two files.
  Nothing else follows links, so scope cannot creep down the link graph.

Consumers: ``skills/vault-projects`` (list, resolve, doctor, emit, pack, and hub
drafting).
"""

import hashlib
import json
import os
import re
from pathlib import Path

from vault_schema import (
    UserError,
    compile_destination,
    is_workspace_dir,
    iter_heading_lines,
    link_basename,
    normalize_project_value,
    parse_frontmatter,
    project_name,
    section_bounds,
    split_frontmatter,
)

MANIFEST_NAME = "_corpus.json"
MANIFEST_VERSION = 1
CORPUS_HEADING = "Corpus"
EXCLUDED_HEADING = "Excluded"
TRANSCRIPT_HEADING = "Transcript"
RULES_NOTE = "99 Meta/99.08 Agent Rules/Project corpus rules.md"
MANIFEST_README = (
    "Machine-generated project corpus. Agents: work ONLY from the paths in members[]; "
    "if something you need is not listed, say so instead of searching the rest of the vault. "
    "Paths are vault-relative. Do not hand-edit: regenerate with `vault-projects emit`."
)

# Membership roles that mean something to the resolver. Every other role is a
# `### Subsection` heading in the hub, slugified, so the vocabulary is the
# owner's rather than this file's.
ROLE_HUB = "hub"
ROLE_FOLDER = "folder"
ROLE_TRANSCRIPT = "transcript"
ROLE_ATTACHMENT = "attachment"

VIA_FOLDER = "folder"
VIA_HUB = "hub"
VIA_CLOSURE = "closure"

# ` — ` (space, em dash, space) ends the link and begins the human's note about
# why this thing is in the corpus. Everything after it is carried through
# verbatim and never parsed, so an annotation may mention other notes.
ANNOTATION_SEPARATOR = " — "

BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
EMBED_RE = re.compile(r"!\[\[([^\]\r\n]+)\]\]")
LINK_RE = re.compile(r"(?<!!)\[\[([^\]\r\n]+)\]\]")
BACKTICKED_RE = re.compile(r"`([^`\r\n]+)`")
SKIPPED_FILENAMES = {".DS_Store", "Icon\r"}
MARKDOWN_SUFFIXES = {".md", ".markdown"}


def sha256_path(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(131072), b""):
            digest.update(chunk)
    return digest.hexdigest()


def slugify_role(heading):
    slug = re.sub(r"[^a-z0-9]+", "-", heading.strip().lower()).strip("-")
    return slug or "member"


def is_markdown(path):
    return path.suffix.lower() in MARKDOWN_SUFFIXES


# --------------------------------------------------------------------------- #
# Vault inventory
# --------------------------------------------------------------------------- #


def walk_vault_files(vault):
    """Every real file a link could point at, vault-relative and sorted.

    Unlike ``vault_schema.selected_notes`` this keeps non-Markdown files, because
    an embedded image is a corpus member and an agent handed the project needs
    its path. Dot-directories, symlinks, and marked workspaces are skipped: a
    workspace holds a run's machine artifacts, which are never corpus members.
    """
    vault = Path(vault).resolve()
    files = []
    for directory, dirnames, filenames in os.walk(vault, followlinks=False):
        dirpath = Path(directory)
        kept = []
        for name in sorted(dirnames):
            child = dirpath / name
            if child.is_symlink() or name.startswith(".") or name == "node_modules":
                continue
            if is_workspace_dir(child):
                continue
            kept.append(name)
        dirnames[:] = kept
        for filename in sorted(filenames):
            path = dirpath / filename
            if path.is_symlink() or filename.startswith(".") or filename in SKIPPED_FILENAMES:
                continue
            files.append(path.resolve().relative_to(vault).as_posix())
    return sorted(files)


def build_link_index(vault, files=None):
    """Index the vault the way Obsidian resolves ``[[links]]``.

    Three lookups, tried in order by ``resolve_target``: the full path, the
    basename, and any ``aliases`` a note declares. Basenames are folded to
    lowercase because a link is matched case-insensitively, and a basename owned
    by more than one note is kept as the whole list so the caller can refuse it.
    """
    vault = Path(vault).resolve()
    files = list(files) if files is not None else walk_vault_files(vault)
    by_path = {}
    by_basename = {}
    by_alias = {}
    for relative in files:
        by_path[relative.lower()] = relative
        stem = relative.rsplit("/", 1)[-1]
        by_basename.setdefault(stem.lower(), []).append(relative)
        if is_markdown(Path(relative)):
            stemless = stem[: -len(Path(stem).suffix)] if Path(stem).suffix else stem
            by_basename.setdefault(stemless.lower(), []).append(relative)
            for alias in note_aliases(vault / relative):
                by_alias.setdefault(alias.lower(), []).append(relative)
    return {"files": files, "by_path": by_path, "by_basename": by_basename, "by_alias": by_alias}


def note_aliases(path):
    """``aliases`` from a note's frontmatter, or nothing when it has none.

    Advisory: a vault whose schema does not approve ``aliases`` simply has none,
    and a note this cannot read costs its aliases rather than the run.
    """
    try:
        parts = split_frontmatter(path.read_bytes())
    except (OSError, UnicodeDecodeError):
        return []
    if parts["malformed"] or not parts["had_frontmatter"]:
        return []
    value = parse_frontmatter(parts["frontmatter_text"]).get("aliases")
    if isinstance(value, str):
        return [value] if value else []
    return [item for item in (value or []) if isinstance(item, str) and item]


def read_note(vault, relative):
    """``{metadata, body, title}`` for a Markdown file, tolerant of bad input."""
    path = Path(vault) / relative
    try:
        parts = split_frontmatter(path.read_bytes())
    except (OSError, UnicodeDecodeError):
        return {"metadata": {}, "body": "", "readable": False}
    metadata = {} if parts["malformed"] or not parts["had_frontmatter"] else parse_frontmatter(parts["frontmatter_text"])
    return {"metadata": metadata, "body": parts["body"], "readable": True}


# --------------------------------------------------------------------------- #
# Link resolution
# --------------------------------------------------------------------------- #


def link_target(raw):
    """``Note|alias`` or ``Note#Heading`` -> ``Note``, from a link's inner text."""
    return re.split(r"[|#^]", raw, maxsplit=1)[0].strip()


def resolve_target(index, target):
    """``{status, path, candidates}`` for one wikilink target.

    ``status`` is ``resolved``, ``ambiguous``, or ``unresolved``. Ambiguity is
    never broken by picking one: two notes sharing a basename is a real vault
    problem, and choosing silently would change what an agent may read based on
    which file was walked first.
    """
    text = target.strip()
    if not text:
        return {"status": "unresolved", "path": None, "candidates": []}
    if "/" in text:
        for candidate in (text, f"{text}.md"):
            hit = index["by_path"].get(candidate.lower())
            if hit:
                return {"status": "resolved", "path": hit, "candidates": [hit]}
        return {"status": "unresolved", "path": None, "candidates": []}
    key = link_basename(text).lower()
    matches = sorted(set(index["by_basename"].get(key, []))) or sorted(set(index["by_alias"].get(key, [])))
    if not matches:
        return {"status": "unresolved", "path": None, "candidates": []}
    if len(matches) > 1:
        return {"status": "ambiguous", "path": None, "candidates": matches}
    return {"status": "resolved", "path": matches[0], "candidates": matches}


# --------------------------------------------------------------------------- #
# Hub parsing
# --------------------------------------------------------------------------- #


def parse_corpus_section(body):
    """Read a hub's ``## Corpus`` section into role buckets and exclusions.

    The heading is the ownership boundary, the same way the note format's
    ``## Notes`` is: everything outside ``## Corpus`` is prose a person writes for
    themselves, and no amount of it can widen what an agent may read. Inside,
    each ``###`` subsection names a role and each bullet contributes its first
    link. Embeds are ignored everywhere here, so a Bases embed or a cover image
    in the hub cannot smuggle in scope.
    """
    lines = body.splitlines()
    try:
        start, end = section_bounds(lines, CORPUS_HEADING, level=2)
    except UserError:
        return {"present": False, "entries": [], "exclusions": [], "loose_links": []}

    entries = []
    exclusions = []
    loose_links = []
    role = None
    heading = None
    for offset, line in enumerate(lines[start + 1:end], start=start + 2):
        match = re.match(r"^(#{3,6})\s+(.+?)\s*$", line)
        if match:
            heading = match.group(2).strip()
            role = slugify_role(heading)
            continue
        bullet = BULLET_RE.match(line)
        if not bullet:
            if LINK_RE.search(EMBED_RE.sub("", line)):
                loose_links.append({"line": offset, "text": line.strip()})
            continue
        text = bullet.group(1).strip()
        if not text:
            continue
        # The link is found before the annotation is split off, because a display
        # alias may legitimately contain an em dash -- `[[Tsing - 2015 - …|Tsing —
        # The Mushroom at the End of the World]]` is exactly how a person makes a
        # long filename readable. Splitting first would cut that bullet in half
        # and silently drop the member.
        without_embeds = EMBED_RE.sub("", text)
        link = LINK_RE.search(without_embeds)
        if heading and heading.strip().lower() == EXCLUDED_HEADING.lower():
            head, _, tail = text.partition(ANNOTATION_SEPARATOR)
            exclusions.append(
                {"raw": head.strip(), "annotation": tail.strip(), "line": offset, "heading": heading}
            )
            continue
        if not link:
            continue
        _, _, after_link = without_embeds.partition(link.group(0))
        annotation = after_link.partition(ANNOTATION_SEPARATOR)[2].strip()
        entries.append(
            {
                "target": link_target(link.group(1)),
                "role": role or "member",
                "heading": heading or "",
                "annotation": annotation,
                "line": offset,
            }
        )
    return {"present": True, "entries": entries, "exclusions": exclusions, "loose_links": loose_links}


def transcript_link(body):
    """The single link under a ``# Transcript`` heading, or ``None``.

    Exactly one link is the vault's two-note transcript pattern: a processed note
    ends by naming the verbatim source beside it. Two links means the section is
    being used for something else, and the pair is not closed rather than half
    closed.
    """
    lines = body.splitlines()
    for index, level, title in iter_heading_lines(lines):
        if title.strip().lower() != TRANSCRIPT_HEADING.lower():
            continue
        end = len(lines)
        for offset, found_level, _ in iter_heading_lines(lines[index + 1:]):
            if found_level <= level:
                end = index + 1 + offset
                break
        section = "\n".join(lines[index + 1:end])
        links = LINK_RE.findall(EMBED_RE.sub("", section))
        if len(links) == 1:
            return {"target": link_target(links[0]), "count": 1}
        if links:
            return {"target": None, "count": len(links)}
    return None


def embedded_targets(body):
    return [link_target(raw) for raw in EMBED_RE.findall(body)]


# --------------------------------------------------------------------------- #
# Projects and hubs
# --------------------------------------------------------------------------- #


def registered_projects(schema):
    """``{name: {value, folder, ...}}`` for every project in the schema registry."""
    projects = {}
    for value, project in sorted(schema.get("projects", {}).items()):
        name = project_name(value)
        folder = compile_destination(schema, {"domain": project["domain"], "project": value})
        projects[name] = dict(project, name=name, folder=folder.as_posix())
    return projects


def find_project(schema, requested):
    """Look a project up by name or by its ``[[wikilink]]`` registry value."""
    projects = registered_projects(schema)
    text = normalize_project_value(str(requested).strip())
    name = project_name(text) if re.fullmatch(r"\[\[[^\]\n\r]+\]\]", text) else text
    if name in projects:
        return projects[name]
    lowered = {key.lower(): key for key in projects}
    if name.lower() in lowered:
        return projects[lowered[name.lower()]]
    listed = ", ".join(sorted(projects)) or "none"
    raise UserError(f"no registered project named {name!r}; the schema registers: {listed}")


def hub_candidates(vault, project):
    """Every ``type: project`` note in the project folder, hub first.

    The hub is the note named after the project. Others are ordinary members: a
    folder may hold a timeline or an overview that is also typed as a project
    without there being two definitions of what the project's corpus is.
    """
    folder = Path(vault) / project["folder"]
    if not folder.is_dir():
        return {"folder_exists": False, "hub": None, "others": []}
    expected = f"{project['folder']}/{project['name']}.md"
    hub = None
    others = []
    for path in sorted(folder.rglob("*.md")):
        if path.is_symlink():
            continue
        relative = path.resolve().relative_to(Path(vault).resolve()).as_posix()
        note = read_note(vault, relative)
        if note["metadata"].get("type") != "project":
            continue
        if relative == expected:
            hub = relative
        else:
            others.append(relative)
    if hub is None and (Path(vault) / expected).is_file():
        hub = expected
    return {"folder_exists": True, "hub": hub, "others": others}


def folder_members(vault, project):
    """Everything under the project folder, which is in the corpus by position."""
    vault = Path(vault).resolve()
    folder = vault / project["folder"]
    if not folder.is_dir():
        return []
    prefix = f"{project['folder']}/"
    return [
        relative
        for relative in walk_vault_files(vault)
        if relative.startswith(prefix) and relative.rsplit("/", 1)[-1] != MANIFEST_NAME
    ]


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #


def _member_record(vault, relative, role, via, annotation=None, of=None, digest=True):
    record = {"path": relative, "role": role, "via": via}
    path = Path(vault) / relative
    if is_markdown(path):
        note = read_note(vault, relative)
        metadata = note["metadata"]
        record["title"] = metadata.get("title") or Path(relative).stem
        for key in ("type", "status", "source_kind", "project"):
            if metadata.get(key):
                record[key] = metadata[key]
    else:
        record["title"] = Path(relative).name
    if annotation:
        record["annotation"] = annotation
    if of:
        record["of"] = of
    if digest:
        try:
            record["sha256"] = sha256_path(path)
        except OSError:
            record["sha256"] = None
    return record


def resolve_corpus(vault, schema, schema_hash, requested, digest=True):
    """Resolve one project into its members, problems, and a manifest payload.

    Order matters: folder membership first so an explicit hub listing upgrades a
    file's role rather than duplicating it, then the hub's links, then the two
    closures over everything gathered so far. Closures do not cascade -- a
    transcript pulled in does not get read for further links.
    """
    vault = Path(vault).resolve()
    project = find_project(schema, requested)
    located = hub_candidates(vault, project)
    index = build_link_index(vault)

    problems = []
    if not located["folder_exists"]:
        problems.append(
            {
                "code": "folder_missing",
                "severity": "error",
                "message": f"project folder does not exist: {project['folder']}",
            }
        )
    if not located["hub"]:
        problems.append(
            {
                "code": "hub_missing",
                "severity": "error",
                "message": (
                    f"no hub note at {project['folder']}/{project['name']}.md; "
                    "run `draft-hub` and place the result"
                ),
            }
        )
    for other in located["others"]:
        problems.append(
            {
                "code": "extra_project_note",
                "severity": "warning",
                "path": other,
                "message": f"{other} is typed `project` but is not the hub; it is an ordinary member",
            }
        )

    members = {}
    excluded = []
    unresolved = []
    ambiguous = []

    for relative in folder_members(vault, project):
        members[relative] = {"role": ROLE_FOLDER, "via": VIA_FOLDER, "annotation": None, "of": None}
    if located["hub"]:
        members[located["hub"]] = {"role": ROLE_HUB, "via": VIA_FOLDER, "annotation": None, "of": None}

    parsed = {"present": False, "entries": [], "exclusions": [], "loose_links": []}
    hub_sha = None
    if located["hub"]:
        hub_sha = sha256_path(vault / located["hub"])
        parsed = parse_corpus_section(read_note(vault, located["hub"])["body"])
        if not parsed["present"]:
            problems.append(
                {
                    "code": "corpus_section_missing",
                    "severity": "error",
                    "path": located["hub"],
                    "message": (
                        f"the hub has no `## {CORPUS_HEADING}` section, so nothing outside the "
                        "project folder is in scope"
                    ),
                }
            )
        for loose in parsed["loose_links"]:
            problems.append(
                {
                    "code": "loose_link",
                    "severity": "warning",
                    "path": located["hub"],
                    "line": loose["line"],
                    "message": (
                        f"line {loose['line']} of the Corpus section links a note outside a bullet, "
                        "so it is not a member"
                    ),
                }
            )

    for entry in parsed["entries"]:
        outcome = resolve_target(index, entry["target"])
        if outcome["status"] == "ambiguous":
            ambiguous.append(
                {
                    "target": entry["target"],
                    "role": entry["role"],
                    "line": entry["line"],
                    "candidates": outcome["candidates"],
                }
            )
            problems.append(
                {
                    "code": "ambiguous_link",
                    "severity": "error",
                    "path": located["hub"],
                    "line": entry["line"],
                    "message": (
                        f"[[{entry['target']}]] matches {len(outcome['candidates'])} notes "
                        f"({', '.join(outcome['candidates'])}); qualify the link with its folder"
                    ),
                }
            )
            continue
        if outcome["status"] == "unresolved":
            unresolved.append({"target": entry["target"], "role": entry["role"], "line": entry["line"]})
            problems.append(
                {
                    "code": "unresolved_link",
                    "severity": "error",
                    "path": located["hub"],
                    "line": entry["line"],
                    "message": f"[[{entry['target']}]] does not resolve to a note in this vault",
                }
            )
            continue
        relative = outcome["path"]
        existing = members.get(relative)
        if existing and existing["role"] not in (ROLE_FOLDER,):
            continue
        members[relative] = {
            "role": entry["role"],
            "via": VIA_HUB,
            "annotation": entry["annotation"] or None,
            "of": None,
        }

    for exclusion in parsed["exclusions"]:
        target = exclusion["raw"]
        link = LINK_RE.search(EMBED_RE.sub("", target))
        quoted = BACKTICKED_RE.search(target)
        relative = None
        if link:
            outcome = resolve_target(index, link_target(link.group(1)))
            relative = outcome["path"]
        elif quoted:
            candidate = f"{project['folder']}/{quoted.group(1).strip().lstrip('/')}"
            relative = candidate if (vault / candidate).is_file() else None
        if relative and relative in members:
            members.pop(relative)
            excluded.append(relative)
            continue
        problems.append(
            {
                "code": "dead_exclusion",
                "severity": "error",
                "path": located["hub"],
                "line": exclusion["line"],
                "message": f"the exclusion {target!r} matches nothing in the corpus",
            }
        )

    for relative in sorted(list(members)):
        path = vault / relative
        if not is_markdown(path):
            continue
        body = read_note(vault, relative)["body"]
        pair = transcript_link(body)
        if pair and pair["count"] > 1:
            problems.append(
                {
                    "code": "transcript_ambiguous",
                    "severity": "warning",
                    "path": relative,
                    "message": (
                        f"the Transcript section names {pair['count']} notes, so no transcript was "
                        "pulled in; a pair names exactly one"
                    ),
                }
            )
        elif pair and pair["target"]:
            outcome = resolve_target(index, pair["target"])
            if outcome["status"] == "resolved" and outcome["path"] not in members:
                members[outcome["path"]] = {
                    "role": ROLE_TRANSCRIPT,
                    "via": VIA_CLOSURE,
                    "annotation": None,
                    "of": relative,
                }
            elif outcome["status"] != "resolved":
                problems.append(
                    {
                        "code": "transcript_unresolved",
                        "severity": "warning",
                        "path": relative,
                        "message": f"the Transcript section names [[{pair['target']}]], which does not resolve",
                    }
                )
        for target in embedded_targets(body):
            outcome = resolve_target(index, target)
            if outcome["status"] != "resolved":
                continue
            embedded = outcome["path"]
            if is_markdown(Path(embedded)):
                if embedded not in members:
                    problems.append(
                        {
                            "code": "markdown_embed",
                            "severity": "info",
                            "path": relative,
                            "message": (
                                f"embeds [[{target}]], a note outside the corpus; list it under "
                                f"`## {CORPUS_HEADING}` if it belongs"
                            ),
                        }
                    )
                continue
            if embedded not in members:
                members[embedded] = {
                    "role": ROLE_ATTACHMENT,
                    "via": VIA_CLOSURE,
                    "annotation": None,
                    "of": relative,
                }

    records = [
        _member_record(
            vault,
            relative,
            members[relative]["role"],
            members[relative]["via"],
            annotation=members[relative]["annotation"],
            of=members[relative]["of"],
            digest=digest,
        )
        for relative in sorted(members)
    ]
    by_role = {}
    for record in records:
        by_role[record["role"]] = by_role.get(record["role"], 0) + 1

    return {
        "project": project,
        "hub": located["hub"],
        "hub_sha256": hub_sha,
        "schema_hash": schema_hash,
        "members": records,
        "unresolved": unresolved,
        "ambiguous": ambiguous,
        "excluded": sorted(excluded),
        "problems": problems,
        "counts": {"members": len(records), "by_role": dict(sorted(by_role.items()))},
    }


def blocking_problems(resolution):
    return [problem for problem in resolution["problems"] if problem["severity"] == "error"]


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #


def manifest_path(vault, project):
    return Path(vault) / project["folder"] / MANIFEST_NAME


def build_manifest(resolution, generated):
    project = resolution["project"]
    return {
        "version": MANIFEST_VERSION,
        "readme": MANIFEST_README,
        "rules_note": RULES_NOTE,
        "generated": generated,
        "project": project["name"],
        "project_value": project["value"],
        "hub": resolution["hub"],
        "folder": project["folder"],
        "hub_sha256": resolution["hub_sha256"],
        "schema_hash": resolution["schema_hash"],
        "members": resolution["members"],
        "unresolved": resolution["unresolved"],
        "excluded": resolution["excluded"],
        "counts": resolution["counts"],
    }


def read_manifest(vault, project):
    path = manifest_path(vault, project)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"unreadable": True}


def write_manifest(vault, project, manifest):
    """Write the manifest atomically, so a reader never sees half a corpus."""
    path = manifest_path(vault, project)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


def compare_manifest(existing, fresh):
    """What changed between a written manifest and a fresh resolution.

    Staleness is membership drift: which paths came and went, and whether the
    hub or schema moved underneath. A member whose *content* changed is reported
    separately, because that is the normal state of a vault being worked in and
    calling it stale would make the check cry wolf.
    """
    if not existing or existing.get("unreadable"):
        return {
            "state": "absent" if not existing else "unreadable",
            "added": [record["path"] for record in fresh["members"]],
            "removed": [],
            "changed": [],
            "hub_changed": False,
            "schema_changed": False,
        }
    old = {record.get("path"): record for record in existing.get("members", [])}
    new = {record["path"]: record for record in fresh["members"]}
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    rerole = sorted(
        path for path in set(old) & set(new) if old[path].get("role") != new[path].get("role")
    )
    changed = sorted(
        path
        for path in set(old) & set(new)
        if old[path].get("sha256")
        and new[path].get("sha256")
        and old[path]["sha256"] != new[path]["sha256"]
    )
    hub_changed = bool(
        existing.get("hub_sha256") and fresh["hub_sha256"] and existing["hub_sha256"] != fresh["hub_sha256"]
    )
    schema_changed = bool(
        existing.get("schema_hash") and fresh["schema_hash"] and existing["schema_hash"] != fresh["schema_hash"]
    )
    stale = bool(added or removed or rerole or hub_changed or schema_changed)
    return {
        "state": "stale" if stale else "fresh",
        "added": added,
        "removed": removed,
        "rerole": rerole,
        "changed": changed,
        "hub_changed": hub_changed,
        "schema_changed": schema_changed,
    }

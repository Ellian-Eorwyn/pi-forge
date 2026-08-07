"""Compiling a vault's own orientation skill.

A session already learns *that* it is in a vault from the vault-context
extension, which injects the root, the note count, the schema note's path, the
owner, and which skill answers which question. What no extension can inject is
what this particular vault looks like: which domains it declares and what hangs
beneath them, which of the approved properties the owner holds and code must
never touch, and which notes in the vault carry binding instructions. A session
re-derives that with a scatter of ``ls`` and ``grep`` — or, more often, guesses.

So the vault carries a skill describing itself, at
``.agents/skills/vault-guide/SKILL.md``, which the agent discovers from the vault
root and every directory beneath it. Three things shape the file.

**It is compiled, never written.** Every fact in the generated block comes from
the schema note or from the folders on disk, so the guide cannot claim a
subdomain the schema does not declare. The one thing a generator cannot compile
is judgment, so the guide *indexes* the vault's own prose — the schema notes, the
agent-rules notes — rather than paraphrasing it. Paraphrase is where a compiled
document starts lying: the copy stays confident while the original moves on.

**It carries no timestamp.** Provenance is the schema hash and the folder-tree
hash in the footer, which makes regeneration byte-idempotent — a second run over
an unchanged vault produces the same bytes, so ``--check`` can compare hashes and
the vault's own ``git status`` stays clean. A "generated at" line would turn
every run into a diff and train the owner to ignore them.

**It is short on purpose.** The description costs tokens in every session's skill
menu whether or not the skill ever loads; the body costs them only when a task
actually touches the vault. So the body carries what is expensive to re-derive
and a pointer to everything that is merely long.
"""

import json
import re

from vault_schema import (
    INBOX_DIR,
    UserError,
    compiled_routes,
    count_notes,
    domain_folder,
    existing_folders,
    human_owned_properties,
    parse_frontmatter,
    sha256_text,
    source_kind_routes,
    sources_root_folder,
    sources_routing_enabled,
    split_frontmatter,
    subdomain_folder,
)

SKILL_NAME = "vault-guide"
SKILL_RELATIVE = ".agents/skills/" + SKILL_NAME + "/SKILL.md"
GENERATED_START = "<!-- vault-guide:generated start -->"
GENERATED_END = "<!-- vault-guide:generated end -->"

# The subdomain whose folder holds notes written *at* the agent. Absent from most
# vaults, in which case the conventions table simply has fewer rows.
AGENT_RULES_SUBDOMAIN = "agent-rules"
# The subdomain holding generated run directories. Their contents are machine
# artifacts wearing the .md extension, so every count and every scan here has to
# step around them -- the `.forge-workspace` marker that normally does that is
# written per category folder, and older run trees predate it.
WORKFLOWS_SUBDOMAIN = "workflows"

# Note counts move with every capture, so the map reports a bucket instead. The
# question a bucket answers -- is this a worked area or a stub? -- is the one
# worth answering, and it stays true for months.
SCALE_BUCKETS = ((200, "large"), (50, "medium"), (10, "small"), (1, "sparse"))

# A folder needs this many pre-schema notes before it earns a line. Below it, the
# finding is a handful of stragglers the organizer will pick up anyway.
MIN_LEGACY_NOTES = 10
# When one child holds essentially all of a folder's pre-schema notes, name the
# child: "98 Archive/QE" tells the reader where not to go, "98 Archive" does not.
LEGACY_DESCENT_SHARE = 0.9
# Long enough to show a real gap, short enough that a half-built vault does not
# push the rest of the guide out of the reader's attention.
MAX_MISSING_ROUTES = 12

# The canonical config notes live beside the schema note and are numbered. Their
# titles say what they are, not when to read them, so the trigger is curated per
# number and falls back to the note's own H1 for anything unrecognized. An
# *unnumbered* sibling is not canon -- a vault accumulates drafts and superseded
# design notes in the same folder, and indexing one as a convention is exactly
# the confident wrongness this guide exists to prevent.
CONFIG_NOTE_TRIGGERS = {
    "0.00": "file a note, or set any frontmatter",
    "0.01": "choose wording, register, or tone",
    "0.02": "clean a transcript, or name a speaker",
    "0.03": "address the owner, or draw on personal context",
    "0.04": "write or edit a note's body",
}
SCHEMA_NOTE_TRIGGER = CONFIG_NOTE_TRIGGERS["0.00"]
CONFIG_NOTE_NUMBER_RE = re.compile(r"^(\d+\.\d+)\s")
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)

# Substituted by the reader from the vault coordinates the session already has.
# Baking the absolute path in would make a git-tracked file machine-specific.
VAULT_PLACEHOLDER = "<vault root>"


def scale_label(count):
    """A bucket name for ``count`` notes, or ``empty`` for none."""
    for floor, label in SCALE_BUCKETS:
        if count >= floor:
            return label
    return "empty"


def note_relative_paths(vault, folders):
    """Every Markdown note the organizer would consider, vault-relative posix.

    Built from ``existing_folders`` so the exclusions filing uses -- dotfiles,
    protected directories, marked workspaces -- apply here unchanged, and the
    guide never describes a tree the skills cannot see.
    """
    paths = []
    for folder in [""] + list(folders):
        directory = vault / folder if folder else vault
        try:
            children = sorted(directory.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_symlink() or not child.is_file():
                continue
            if not child.name.lower().endswith(".md"):
                continue
            paths.append(child.relative_to(vault).as_posix())
    return paths


def note_metadata(path):
    """A note's frontmatter as ``{key: value}``; empty when it has none."""
    try:
        parts = split_frontmatter(path.read_bytes())
    except (OSError, UnicodeDecodeError):
        return {}
    if parts["malformed"] or not parts["had_frontmatter"]:
        return {}
    return parse_frontmatter(parts["frontmatter_text"])


def note_title(path):
    """The note's H1, for indexing prose whose filename is not its trigger."""
    try:
        parts = split_frontmatter(path.read_bytes())
    except (OSError, UnicodeDecodeError):
        return None
    match = H1_RE.search(parts["body"])
    return match.group(1).strip() if match else None


def legacy_pockets(vault, notes, workflows=None):
    """Folders holding a body of notes that predate the schema.

    A note with no ``type`` was filed before the vocabulary existed. One or two
    are stragglers; a hundred in one folder is a deliberate archive, and an agent
    that "normalizes" it does damage that looks like tidying. Reported as the
    shallowest qualifying folder, then narrowed while a single child accounts for
    nearly all of it.

    The workflows tree is excluded outright. Run directories are full of files
    that have no frontmatter because they were never notes, and calling a
    thousand extraction packets "pre-schema notes" would be the most confidently
    wrong line in the guide.
    """
    counts = {}
    for relative in notes:
        if workflows and (relative == workflows or relative.startswith(workflows + "/")):
            continue
        if note_metadata(vault / relative).get("type"):
            continue
        parts = relative.split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            folder = "/".join(parts[:depth])
            counts[folder] = counts.get(folder, 0) + 1

    chosen = []
    for folder in sorted(counts, key=lambda name: (name.count("/"), name)):
        if counts[folder] < MIN_LEGACY_NOTES:
            continue
        if any(folder.startswith(parent + "/") for parent in chosen):
            continue
        chosen.append(folder)

    narrowed = []
    for folder in chosen:
        total = counts[folder]
        current = folder
        while True:
            depth = current.count("/") + 1
            children = [
                name
                for name in counts
                if name.startswith(current + "/") and name.count("/") == depth
            ]
            if not children:
                break
            best = max(children, key=lambda name: counts[name])
            if counts[best] < total * LEGACY_DESCENT_SHARE:
                break
            current = best
        narrowed.append((current, total))
    return narrowed


def domain_lines(vault, schema):
    """One line per domain: its folder, its frontmatter value, and its subdomains."""
    lines = []
    domains = sorted(schema["domains"].values(), key=lambda domain: domain["number"])
    for domain in domains:
        folder = domain_folder(domain)
        subdomains = sorted(
            schema["subdomains"].get(domain["value"], {}).values(), key=lambda sub: sub["number"]
        )
        rendered = ", ".join(
            "{0} `{1}`".format(subdomain_folder(domain, sub), sub["value"]) for sub in subdomains
        )
        scale = scale_label(count_notes(vault / folder))
        head = "- `{0}` `{1}` ({2}) — ".format(folder, domain["value"], scale)
        lines.append(head + (rendered if rendered else "no subdomains"))
    return lines


def sources_lines(schema):
    """How the sources tree files, when the schema declares one."""
    if not sources_routing_enabled(schema):
        return []
    root = schema["sources_root"]
    base = sources_root_folder(root)
    kinds = sorted(
        (schema["source_kinds"][value] for value in source_kind_routes(schema)),
        key=lambda kind: kind["number"],
    )
    rendered = ", ".join(
        "{0} `{1}`".format(subdomain_folder(root, kind), kind["value"]) for kind in kinds
    )
    return [
        "",
        "`{0}` is the exception. A note with `type: source` files by its `source_kind`".format(base),
        "rather than by its domain — `{0}/<kind>/<Domain>/<Subdomain>/`, where the tail".format(base),
        "folders are plain labels carrying no numbers. Kinds: " + rendered + ".",
    ]


def project_lines(schema):
    """The registry of approved `project` values, which are wikilinks, not names."""
    projects = schema.get("projects") or {}
    if not projects:
        return []
    ordered = sorted(projects.values(), key=lambda project: (project["domain"], project["number"]))
    rendered = ", ".join("`{0}`".format(project["value"]) for project in ordered)
    return [
        "",
        "Approved `project` values, each nesting a third folder level beneath its",
        "subdomain: " + rendered + ".",
    ]


def property_lines(schema):
    """The approved properties, in order, marked by who owns them."""
    properties = schema["properties"]
    order = schema["property_order"]
    human = set(human_owned_properties(schema))
    required = [name for name in order if properties[name]["required"] == "yes"]
    rendered = " ".join(
        "`{0}`{1}".format(name, "*" if name in human else "") for name in order
    )
    lines = [
        "Approved properties, in the order they must appear. Only these keys may appear;",
        "anything else is stripped on the next filing pass.",
        "",
        rendered,
        "",
        "- Required: " + ", ".join("`{0}`".format(name) for name in required) + ".",
    ]
    if human:
        lines.append(
            "- `*` marks human-owned: never inferred, never written by a skill, carried"
        )
        lines.append(
            "  unchanged across rewrites. An absent one does not exist — do not fill it in."
        )
    return lines


def vocabulary_lines(schema):
    """The controlled values for the properties whose vocabulary is closed."""
    registries = (
        ("type", schema["types"]),
        ("status", schema["statuses"]),
        ("source_kind", schema.get("source_kinds") or {}),
        ("capture_type", schema.get("capture_types") or {}),
    )
    lines = []
    for name, registry in registries:
        if not registry:
            continue
        values = ", ".join("`{0}`".format(value) for value in registry)
        lines.append("- `{0}` — {1}".format(name, values))
    return lines


def config_note_rows(vault, schema_path):
    """Rows for the schema note and the numbered config notes beside it."""
    rows = [(SCHEMA_NOTE_TRIGGER, schema_path.relative_to(vault).as_posix())]
    for path in sorted(schema_path.parent.glob("*.md")):
        if path.is_symlink() or not path.is_file() or path == schema_path:
            continue
        match = CONFIG_NOTE_NUMBER_RE.match(path.name)
        if not match:
            continue
        trigger = CONFIG_NOTE_TRIGGERS.get(match.group(1))
        if trigger is None:
            title = note_title(path)
            if not title:
                continue
            trigger = "work on " + title[0].lower() + title[1:]
        rows.append((trigger, path.relative_to(vault).as_posix()))
    return rows


def subdomain_route(schema, value):
    """The vault-relative folder for a subdomain value, in whichever domain declares it.

    Looked up by value rather than pinned to a domain name so a vault that files
    its workflows or its agent rules somewhere other than `meta` still resolves.
    """
    for domain_value, subdomains in schema["subdomains"].items():
        subdomain = subdomains.get(value)
        if subdomain:
            domain = schema["domains"][domain_value]
            return domain_folder(domain) + "/" + subdomain_folder(domain, subdomain)
    return None


def agent_rules_rows(vault, schema):
    """Rows for the vault's own agent-facing prose, indexed by each note's title."""
    route = subdomain_route(schema, AGENT_RULES_SUBDOMAIN)
    folder = vault / route if route else None
    if folder is None or not folder.is_dir():
        return []
    rows = []
    for path in sorted(folder.glob("*.md")):
        if path.is_symlink() or not path.is_file():
            continue
        title = note_title(path) or path.stem
        rows.append((title[0].lower() + title[1:], path.relative_to(vault).as_posix()))
    return rows


def missing_route_lines(schema, folders):
    """Routes the schema declares that no folder on disk answers to yet."""
    present = set(folders)
    missing = [route for route in compiled_routes(schema) if route not in present]
    if not missing:
        return []
    shown = missing[:MAX_MISSING_ROUTES]
    rendered = ", ".join("`{0}`".format(route) for route in shown)
    if len(missing) > len(shown):
        rendered += ", and {0} more".format(len(missing) - len(shown))
    return [
        "- Declared in the schema with no folder on disk yet: " + rendered + ".",
        "  Filing something there creates it; the absence is not an error to fix.",
    ]


def workflows_lines(workflows):
    """That the workflows tree is machine output, which its `.md` files disguise."""
    if not workflows:
        return []
    return [
        "- `" + workflows + "` holds generated run directories, not notes. The files",
        "  beneath it are machine artifacts that happen to end in `.md`: read them, but do",
        "  not file, edit, link, or count them as notes. To turn a finished run into a real",
        "  note, use vault-connections `import-run` rather than copying it out by hand.",
    ]


def legacy_lines(pockets):
    """The pre-schema pockets, and the instruction not to tidy them."""
    lines = []
    for folder, total in pockets:
        lines.append(
            "- `{0}` holds {1} notes that predate the schema — no `type`, and in".format(folder, total)
        )
        lines.append(
            "  places keys the vocabulary no longer has. That is deliberate. Read them and"
        )
        lines.append("  cite them; do not normalize them unless the owner asks for it.")
    return lines


def tree_fingerprint(folders):
    """A hash of the folder tree, so a new or renamed folder shows as drift."""
    return sha256_text("\n".join(folders))


def build_description(vault_name, schema):
    """The routing metadata, compiled so it names this vault and its real scale."""
    domains = len(schema["domains"])
    return (
        'Orientation for the Obsidian vault "{0}" — its folder map, its frontmatter '
        "vocabulary across {1} numbered domains, and the notes that define its conventions. "
        "Use when reading, writing, filing, or reorganizing notes in this vault, when you need "
        "to know where something belongs or what a property may contain, or before exploring "
        "the vault by hand — it is cheaper than re-deriving the same layout. Do not use it to "
        "search note content, which is vault-connections, or to classify and file notes, which "
        "is vault-organizer.".format(vault_name, domains)
    )


def render(vault, schema, schema_path, schema_hash):
    """The full SKILL.md text: generated frontmatter, then the generated block."""
    vault_name = vault.name
    schema_relative = schema_path.relative_to(vault).as_posix()
    folders = existing_folders(vault)
    workflows = subdomain_route(schema, WORKFLOWS_SUBDOMAIN)
    pockets = legacy_pockets(vault, note_relative_paths(vault, folders), workflows)

    table = ["| Before you… | Read |", "| --- | --- |"]
    for trigger, path in config_note_rows(vault, schema_path) + agent_rules_rows(vault, schema):
        table.append("| {0} | `{1}` |".format(trigger, path))

    lines = [
        "---",
        "name: " + SKILL_NAME,
        "description: " + build_description(vault_name, schema),
        "---",
        "",
        GENERATED_START,
        "# Vault Guide — " + vault_name,
        "",
        "Compiled from `" + schema_relative + "` and the folders on disk. Everything between",
        "the generated markers is overwritten on refresh; anything written after the end",
        "marker is kept.",
        "",
        "## The rule this vault is built on",
        "",
        "A note's folder and its frontmatter are **derived**, never authored. The schema note",
        "holds the numbers and the labels, and the path is compiled from them. So do not invent",
        "a folder, do not hand-write a property, and do not move a note by renaming it — the",
        "vault skills compile the destination and rewrite inbound links. Read the map below to",
        "know where things are, not to build paths from.",
        "",
        "## Folder map",
        "",
        "`<NN Domain>/<N.NN Subdomain>/`. The backticked value after each folder is what goes",
        "into frontmatter; the bucket is how worked that area is.",
        "",
        "- `" + INBOX_DIR + "` — captures that have not been classified yet. Not a domain, and",
        "  nothing should be left here: `vault-organizer inbox` is what empties it.",
    ]
    lines.extend(domain_lines(vault, schema))
    lines.extend(sources_lines(schema))
    lines.extend(project_lines(schema))
    lines.extend(["", "## Frontmatter", ""])
    lines.extend(property_lines(schema))
    lines.append("")
    lines.extend(vocabulary_lines(schema))
    lines.extend(
        [
            "",
            "## Where the conventions live",
            "",
            "This guide is a map, not a rulebook. The rules are the vault's own notes, and they",
            "are the authority wherever the two disagree:",
            "",
        ]
    )
    lines.extend(table)

    local = missing_route_lines(schema, folders) + workflows_lines(workflows) + legacy_lines(pockets)
    if local:
        lines.extend(["", "## Local facts", ""])
        lines.extend(local)

    lines.extend(
        [
            "",
            "## Refreshing this file",
            "",
            "Stale guidance is worse than none, so the fingerprints below are checked rather",
            "than trusted. After any schema edit or folder change, from the vault-organizer",
            "skill directory:",
            "",
            "```bash",
            'python3 scripts/vault-organizer.py guide --vault "'
            + VAULT_PLACEHOLDER
            + '" --check',
            "```",
            "",
            "It exits nonzero when this file no longer matches the vault. Rerun without",
            "`--check` to see what differs, then with `--apply` to write it.",
            "",
            "- schema `" + schema_hash + "`",
            "- tree `" + tree_fingerprint(folders) + "`",
            GENERATED_END,
            "",
        ]
    )
    return "\n".join(lines)


def preserved_tail(text):
    """Whatever the owner added after the generated block, kept across refreshes."""
    index = text.find(GENERATED_END)
    if index == -1:
        return ""
    return text[index + len(GENERATED_END):].lstrip("\n")


def merge(rendered, existing):
    """The freshly compiled file, with any owner-written tail carried over."""
    tail = preserved_tail(existing) if existing else ""
    return rendered + "\n" + tail if tail.strip() else rendered


def fingerprints(text):
    """The schema and tree hashes recorded in a generated file, if it has them."""
    found = {}
    for name in ("schema", "tree"):
        match = re.search("^- " + name + r" `([0-9a-f]{64})`$", text, re.MULTILINE)
        if match:
            found[name] = match.group(1)
    return found


def guide_path(vault):
    return vault / SKILL_RELATIVE


def triggers_path(vault):
    return guide_path(vault).parent / "tests" / "triggers.json"


def render_triggers(vault_name, schema):
    """Worked examples of what should and should not reach this skill.

    Part of the standard's testing story, and the only place the routing
    boundary is written as cases rather than prose: the negatives are the three
    neighbouring vault skills, which is where a self-describing skill is most
    likely to be picked wrongly.
    """
    domains = sorted(schema["domains"].values(), key=lambda domain: domain["number"])
    example = domains[0]["value"] if domains else "personal"
    return json.dumps(
        {
            "positive": [
                "Where does a new note about {0} belong in this vault?".format(example),
                "What values can the `status` property take here?",
                'Which conventions govern writing a note in the "{0}" vault?'.format(vault_name),
                "What are the top-level folders in this vault and what goes in each?",
            ],
            "negative": [
                "Find my notes about dependent origination.",
                "Process the inbox and file everything in it.",
                "Clean up this voice memo and name it.",
            ],
        },
        ensure_ascii=False,
        indent="\t",
    ) + "\n"


def candidate_path(vault):
    """Where a dry run stages the compiled guide.

    Under ``cache/`` because that is the directory the vault's own .gitignore
    already excludes as derivable: a candidate is regenerated from the schema on
    every run, and versioning it would put a second copy of the guide beside the
    one that counts.
    """
    return vault / ".vault-organizer" / "cache" / "guide" / "SKILL.md"


def read_text(path):
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def section_summary(text):
    """Heading -> line count, so a dry run can report shape without printing it."""
    summary = {}
    current = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            summary[current] = 0
        elif current:
            summary[current] += 1
    return summary


def describe_changes(rendered, existing):
    """The headings whose content differs, for a dry run's summary."""
    if not existing:
        return ["no guide installed yet"]
    if rendered == existing:
        return []
    before = section_summary(existing)
    after = section_summary(rendered)
    changes = ["added section: " + heading for heading in after if heading not in before]
    changes.extend("removed section: " + heading for heading in before if heading not in after)
    installed = fingerprints(existing)
    current = fingerprints(rendered)
    for name in ("schema", "tree"):
        if installed.get(name) != current.get(name):
            changes.append(name + " fingerprint changed")
    return changes or ["content changed within existing sections"]


def check(vault, rendered):
    """Whether the installed guide still matches the vault. Writes nothing."""
    path = guide_path(vault)
    if not path.is_file():
        return False, ["no guide installed at " + SKILL_RELATIVE]
    installed = fingerprints(read_text(path))
    if not installed:
        return False, ["the installed guide carries no fingerprints; regenerate it"]
    current = fingerprints(rendered)
    stale = []
    if installed.get("schema") != current.get("schema"):
        stale.append("the schema note changed since this guide was generated")
    if installed.get("tree") != current.get("tree"):
        stale.append("the folder tree changed since this guide was generated")
    return not stale, stale


def write_at(root, rendered, triggers):
    """Write the two files that make up the skill, under ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    skill = root / "SKILL.md"
    skill.write_text(rendered, encoding="utf-8")
    tests = root / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "triggers.json").write_text(triggers, encoding="utf-8")
    return skill


def write_guide(vault, rendered, triggers):
    """Install the compiled skill, creating its directory if needed."""
    return write_at(guide_path(vault).parent, rendered, triggers)


def write_candidate(vault, rendered, triggers):
    """Stage the compiled skill where a dry run can be read without installing it."""
    return write_at(candidate_path(vault).parent, rendered, triggers)


def build(vault, schema, schema_path, schema_hash):
    """Compile the guide and merge in whatever the owner appended to the last one."""
    if not schema.get("domains"):
        raise UserError("the schema declares no domains, so there is no vault shape to compile")
    return merge(render(vault, schema, schema_path, schema_hash), read_text(guide_path(vault)))

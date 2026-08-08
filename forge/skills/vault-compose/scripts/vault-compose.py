#!/usr/bin/env python3
"""Compose one note from material a run is holding, and prove it stayed rooted.

Every other note-writing path here is a closed pipeline over one kind of input:
`vault-capture` splits a braindump, `vault-transcripts` cleans a recording,
`vault-wiki` fills a template. This one takes a *source set* -- any mix of
conversation excerpts, vault notes, and research claims -- and writes a note
assembled in the block order the vault's own `0.04 Note Format.md` declares.

Three shapes reduce to one run spec, differing only in the `kind` of their source
units: research (`web-claim`), synthesis from existing notes (`vault-note`), and a
conversation turned into a note (`chat`). Nothing downstream of `prepare` knows
which one it is.

Unlike capture, this skill **proposes**. Capture's words are the user's and only
the split is the machine's, so writing straight to the inbox is fair. Here the
model chose the content, so the first read of a note should come before it lands
rather than after. Nothing is written until `apply --accept <id>`.

Everything runs on the local LAN endpoints. Drafting is one call per note on the
non-thinking `chat` service; review is a batch on `think`. Deterministic gates run
before either, because a rooted-in-the-sources check beats a model's opinion and
costs nothing.
"""

import argparse
import datetime
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
import forge_llm
import forge_verify
import run_state
import vault_compose
import vault_format
import vault_research
import vault_voice
from vault_schema import (
    INBOX_DIR,
    UserError,
    compiled_schema_for,
    missing_required_properties,
    resolve_schema_path,
    safe_title,
    validate_filename_title,
)

WORKFLOW = "vault-compose"
STATE_DIR = ".vault-compose"
SPEC_VERSION = 1

INTENT_SYNTHESIS = "synthesis"
INTENT_CONVERSATION = "conversation"
INTENT_RESEARCH = "research"
INTENTS = (INTENT_SYNTHESIS, INTENT_CONVERSATION, INTENT_RESEARCH)

# How a note entered the vault, per intent. The vault's schema defines these as
# channels -- `chat` really means "captured from a conversational interface" --
# so the property records how the material arrived and the mandatory provenance
# block records that a machine wrote the note. One property cannot answer both,
# and overloading it leaves a reader unable to tell a research note from a
# conversation without opening it.
INTENT_CAPTURE_TYPE = {
    INTENT_SYNTHESIS: "generated",
    INTENT_CONVERSATION: "chat",
    INTENT_RESEARCH: "generated",
}
INTENT_KINDS = {
    INTENT_SYNTHESIS: (vault_compose.KIND_VAULT_NOTE, vault_compose.KIND_FILE),
    INTENT_CONVERSATION: (vault_compose.KIND_CHAT, vault_compose.KIND_FILE, vault_compose.KIND_VAULT_NOTE),
    INTENT_RESEARCH: (vault_compose.KIND_WEB_CLAIM, vault_compose.KIND_FILE),
}

DEFAULT_MAX_NOTES = 3
DEFAULT_REQUEST_TIMEOUT = 600
# A composed note is a synthesis, not a transcript: past roughly this much the
# note is really several notes, and `--max-notes` is the way to ask for those.
MAX_NOTE_WORDS = 1200
MIN_NOTE_WORDS = 40


def structured(status, artifacts=None, warnings=None, errors=None, data=None):
    return {
        "status": status,
        "artifacts": artifacts or [],
        "warnings": warnings or [],
        "errors": errors or [],
        "data": data,
    }


def progress(message):
    print(message, file=sys.stderr, flush=True)


def chat_service(args):
    return forge_llm.service_from_args(args, "chat")


def unique_run_directory(vault):
    root = vault / STATE_DIR / "runs"
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    candidate = root / f"{stamp}-compose"
    suffix = 2
    while candidate.exists():
        candidate = root / f"{stamp}-compose-{suffix}"
        suffix += 1
    candidate.mkdir(parents=True)
    return candidate


# --------------------------------------------------------------------------- #
# The run spec
# --------------------------------------------------------------------------- #


def load_spec(path):
    """The run spec, validated into the shape every later stage assumes."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise UserError(f"could not read the run spec: {error}") from error
    if not isinstance(raw, dict):
        raise UserError("the run spec is not an object")
    if raw.get("version") != SPEC_VERSION:
        raise UserError(f"run spec version {raw.get('version')}, expected {SPEC_VERSION}")
    intent = raw.get("intent")
    if intent not in INTENTS:
        raise UserError(f"unknown intent: {intent}; expected one of {', '.join(INTENTS)}")
    request = str(raw.get("request") or "").strip()
    if not request:
        raise UserError("the run spec has no request; a note needs to know what it is for")
    units = raw.get("sources")
    warnings = []
    if raw.get("researchRun"):
        # A deep-research run is named rather than transcribed: the claims and
        # the quotes under them are already on disk, and a spec that restated
        # them would be a spec that could restate them wrong.
        if intent != INTENT_RESEARCH:
            raise UserError("researchRun is only for intent 'research'")
        try:
            harvested, warnings = vault_research.claim_source_units(
                raw["researchRun"], limit=raw.get("claimLimit"), include_unsupported=bool(raw.get("includeUnsupported"))
            )
        except ValueError as error:
            raise UserError(str(error)) from error
        units = list(harvested) + list(units or [])
    if not isinstance(units, list) or not units:
        raise UserError("the run spec has no sources")
    permitted = INTENT_KINDS[intent]
    built = []
    for entry in units:
        if not isinstance(entry, dict):
            raise UserError("a source is not an object")
        kind = entry.get("kind")
        if kind not in permitted:
            raise UserError(f"intent '{intent}' does not take a '{kind}' source; expected {', '.join(permitted)}")
        built.append(
            vault_compose.source_unit(
                kind,
                entry.get("label") or "a source",
                entry.get("text") or "",
                origin=entry.get("origin"),
                url=entry.get("url"),
                wikilink=entry.get("wikilink"),
                occurred_at=entry.get("occurredAt"),
                unit_id=entry.get("id"),
            )
        )
    max_notes = raw.get("maxNotes") or DEFAULT_MAX_NOTES
    if not isinstance(max_notes, int) or max_notes < 1:
        raise UserError("maxNotes must be a positive integer")
    date = raw.get("date") or datetime.date.today().isoformat()
    try:
        datetime.date.fromisoformat(date)
    except ValueError as error:
        raise UserError(f"date must be YYYY-MM-DD: {date}") from error
    return {
        "version": SPEC_VERSION,
        "intent": intent,
        "request": request,
        "noteType": raw.get("noteType") or "note",
        "titleHint": raw.get("titleHint") or None,
        "date": date,
        "maxNotes": max_notes,
        "sources": vault_compose.source_set(built),
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- #
# Outline and draft
# --------------------------------------------------------------------------- #


OUTLINE_SYSTEM = """You plan the shape of one note for a personal knowledge vault. You do not write it.

You get a request, and a numbered set of sources the run is holding. Decide how
many notes the material genuinely makes, what each is called, and which blocks
each is built from.

Rules:
- Prefer one note. Split only when the material is about genuinely separate
  things that would be looked up separately.
- Every block you name must be one of the blocks offered. A note that needs only
  a title and prose is a finished note; reaching for blocks it does not need is
  the most common way to make one worse.
- `body` is where the note's actual content goes and almost every note has one.
- A block's `sourceIds` are the sources it rests on. Name only sources that
  genuinely bear on it.
- Titles name the thing, not the act: "Codebook consistency in local models", not
  "Notes on thinking about codebooks".

Return JSON only:
{"notes": [{"title": "...", "blocks": [{"block": "body", "sourceIds": ["s-0001"]}]}]}"""

DRAFT_SYSTEM = """You write one note for a personal knowledge vault, block by block.

You get the note's plan, the sources each block rests on, and the vault's own
style. Write only the blocks you are asked for.

Rules:
- Every specific -- a name, a number, a link, a quoted phrase -- must come from
  the sources given for that block. If it is not there, do not write it. A note
  that is careful and short beats one that is confident and invented.
- Write plain lines. No frontmatter, no `#` headings, no `>` callout syntax:
  those are added afterwards, and a block that writes its own is rejected.
- `body` may use `##` sub-headings when the note genuinely moves between parts.
- Say what is known, likely, contested, or unknown, rather than flattening them.

Return JSON only, one key per block you were asked to write:
{"blocks": {"body": ["line", "line"], "summary": ["line"]}}"""


def offered_blocks(fmt, note_type):
    """Blocks the model may choose from, with what each is for."""
    return [
        {"block": entry["block"], "means": entry["means"]}
        for entry in vault_format.writable_blocks(fmt)
        if entry["block"] not in ("title", "provenance")
    ]


def outline(args, fmt, spec):
    """One call deciding how many notes and what shape each takes."""
    sources = spec["sources"]
    payload = {
        "request": spec["request"],
        "noteType": spec["noteType"],
        "maxNotes": spec["maxNotes"],
        "blocksAvailable": offered_blocks(fmt, spec["noteType"]),
        "sources": [
            {"id": unit["id"], "kind": unit["kind"], "label": unit["label"], "text": unit["text"][:2000]}
            for unit in sources["units"]
        ],
    }
    if spec["titleHint"]:
        payload["titleHint"] = spec["titleHint"]
    shape = fmt.get("shapes", {}).get(spec["noteType"])
    if shape:
        payload["shapeForThisType"] = shape["shape"]
    value, _call = forge_llm.call_json_with_retry(
        chat_service(args),
        [
            {"role": "system", "content": OUTLINE_SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        cache_prompt=args.cache_prompt,
        response_format={"type": "json_object"},
        timeout=args.request_timeout,
        api_key=args.api_key,
        task="compose-outline",
    )
    return validate_outline(value, fmt, spec)


def validate_outline(value, fmt, spec):
    """The outline, or a UserError naming what was wrong with it."""
    if not isinstance(value, dict):
        raise UserError("outline response was not an object")
    raw_notes = value.get("notes")
    if not isinstance(raw_notes, list) or not raw_notes:
        raise UserError("outline response has no notes")
    if len(raw_notes) > spec["maxNotes"]:
        raise UserError(f"outline proposes {len(raw_notes)} notes, more than --max-notes {spec['maxNotes']}")
    declared = vault_format.block_index(fmt)
    known = {unit["id"] for unit in spec["sources"]["units"]}
    notes = []
    seen = set()
    dropped = []
    for entry in raw_notes:
        if not isinstance(entry, dict):
            raise UserError("an outlined note is not an object")
        title = validate_filename_title(str(entry.get("title") or "").strip(), "note title")
        if title.casefold() in seen:
            raise UserError(f"two notes are both called {title!r}")
        seen.add(title.casefold())
        blocks = []
        for block in entry.get("blocks") or []:
            if not isinstance(block, dict):
                raise UserError(f"{title}: a block is not an object")
            name = str(block.get("block") or "").strip().lower()
            if name not in declared:
                # A block the vault does not declare is dropped, not refused: the
                # model reaching for `abstract` where this vault says `summary` is
                # a vocabulary slip, and losing a whole note to it wastes every
                # other block that was right.
                dropped.append(f"{title}: {name or '(unnamed)'}")
                continue
            if name in ("title", "frontmatter"):
                continue
            source_ids = [str(unit_id) for unit_id in block.get("sourceIds") or [] if str(unit_id) in known]
            blocks.append({"block": name, "sourceIds": source_ids})
        if not blocks:
            raise UserError(f"{title}: the outline named no block this vault declares")
        if not any(block["block"] == "body" for block in blocks):
            blocks.append({"block": "body", "sourceIds": [unit["id"] for unit in spec["sources"]["units"]]})
        blocks.sort(key=lambda block: declared[block["block"]])
        notes.append({"title": title, "blocks": blocks})
    return {"notes": notes, "dropped_blocks": dropped}


def draft(args, fmt, spec, note):
    """One call per note, returning plain lines per block."""
    sources = spec["sources"]
    by_id = {unit["id"]: unit for unit in sources["units"]}
    payload = {
        "request": spec["request"],
        "title": note["title"],
        "noteType": spec["noteType"],
        "blocks": [
            {
                "block": block["block"],
                "means": next(
                    (entry["means"] for entry in fmt["blocks"] if entry["block"] == block["block"]), ""
                ),
                "sources": [
                    {"id": unit_id, "label": by_id[unit_id]["label"], "text": by_id[unit_id]["text"]}
                    for unit_id in (block["sourceIds"] or list(by_id))
                ],
            }
            for block in note["blocks"]
        ],
    }
    compiled = vault_voice.compile_voice(
        getattr(args, "compiled_voice", None),
        vault_voice.CONTEXT_OWNER,
        note_type=spec["noteType"],
        material=vault_compose.set_text(sources),
    )
    if compiled["per_type_rule"]:
        payload["styleForThisKind"] = compiled["per_type_rule"]
    if compiled["vocabulary"]:
        payload["relevantVocabulary"] = compiled["vocabulary"]
    system = DRAFT_SYSTEM
    prefix = vault_format.prompt_prefix(fmt, spec["noteType"])
    if prefix:
        system = f"{system}\n\n{prefix}"
    voice_prefix = vault_voice.prompt_prefix(getattr(args, "compiled_voice", None), vault_voice.CONTEXT_OWNER)
    if voice_prefix:
        system = f"{system}\n\n{voice_prefix}"
    value, _call = forge_llm.call_json_with_retry(
        chat_service(args),
        [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        cache_prompt=args.cache_prompt,
        response_format={"type": "json_object"},
        timeout=args.request_timeout,
        api_key=args.api_key,
        task="compose-draft",
    )
    return validate_draft(value, note)


def validate_draft(value, note):
    """Drafted blocks as plain lines, or a UserError."""
    if not isinstance(value, dict):
        raise UserError("draft response was not an object")
    blocks = value.get("blocks")
    if not isinstance(blocks, dict):
        raise UserError("draft response has no blocks")
    wanted = [block["block"] for block in note["blocks"]]
    drafted = {}
    for name in wanted:
        content = blocks.get(name)
        if isinstance(content, str):
            content = content.splitlines()
        if not isinstance(content, list):
            continue
        lines = [str(line).rstrip() for line in content]
        if not any(line.strip() for line in lines):
            continue
        drafted[name] = lines
    if not drafted:
        raise UserError("the draft wrote none of the blocks it was asked for")
    if "body" not in drafted and len(drafted) == 1:
        raise UserError("the draft wrote only apparatus and no content")
    return drafted


# --------------------------------------------------------------------------- #
# Gates
# --------------------------------------------------------------------------- #


def check_drafted_blocks(drafted, note):
    """Structure the drafter is not allowed to author.

    Callout syntax, frontmatter, and `#` titles are added by the renderer once a
    block has passed its checks. A block that writes its own puts every check
    below at the mercy of the model getting `>` prefixes right.
    """
    problems = []
    for name, lines in drafted.items():
        text = "\n".join(lines)
        if any(line.lstrip().startswith(">") for line in lines):
            problems.append(f"{name}: wrote callout syntax; blocks are plain lines")
        if any(line.strip() == "---" for line in lines[:2]):
            problems.append(f"{name}: wrote frontmatter")
        if any(line.lstrip().startswith("# ") for line in lines):
            problems.append(f"{name}: wrote a level-one heading; the title is written in code")
        if name != "body" and any(line.lstrip().startswith("##") for line in lines):
            problems.append(f"{name}: only `body` carries `##` sections")
        if not text.strip():
            problems.append(f"{name}: is empty")
    return problems


def check_note(fmt, spec, note, drafted, text):
    """Every deterministic finding against one composed note."""
    review = list(check_drafted_blocks(drafted, note))
    sources = spec["sources"]
    cited = {block["block"]: block["sourceIds"] for block in note["blocks"]}
    for name, lines in drafted.items():
        prose = "\n".join(lines)
        found = vault_compose.ungrounded_specifics(sources, prose, cited_ids=cited.get(name) or None)
        for value in found["names"]:
            review.append(f"{name}: name not in the sources it cites: {value}")
        for value in found["links"]:
            review.append(f"{name}: link not in the sources it cites: {value}")
        for value in found["wikilinks"]:
            review.append(f"{name}: link to a note no source names: [[{value}]]")
    written = "\n\n".join("\n".join(lines) for lines in drafted.values())
    words = len(written.split())
    if words < MIN_NOTE_WORDS:
        review.append(f"the note is {words} words; too little to be worth finding again")
    if words > MAX_NOTE_WORDS:
        review.append(f"the note is {words} words; this is several notes, so raise --max-notes")
    for severity, message in vault_compose.check_grammar(fmt, text):
        if severity == "error":
            review.append(f"note format: {message}")
    return review


def frontmatter_metadata(schema, spec):
    """The minimal forced block.

    Filing is `vault-organizer`'s job and it reads the note to do it, so this
    writes only what cannot be inferred later: what kind of note it is, that it is
    unfiled, how the material arrived, and the day it is about. `domain` is
    deliberately absent -- guessing one buries a note where nothing looks for it,
    and the organizer refuses to file a note it cannot classify rather than
    silently accepting a wrong guess.
    """
    metadata = {
        "type": spec["noteType"],
        "status": "raw",
        "capture_type": INTENT_CAPTURE_TYPE[spec["intent"]],
        "date": spec["date"],
    }
    if metadata["type"] not in schema["types"]:
        raise UserError(f"schema does not define note type {metadata['type']!r}")
    if metadata["status"] not in schema["statuses"]:
        raise UserError("schema does not define status 'raw'")
    if metadata["capture_type"] not in schema["capture_types"]:
        raise UserError(
            f"schema does not define capture type {metadata['capture_type']!r}; "
            "compose cannot record how this note entered the vault"
        )
    return {key: value for key, value in metadata.items() if key in schema["properties"]}


def provenance_block(spec, run_dir):
    """How the note was made. Written here, never by the model.

    `0.04 Note Format.md` requires this to be accurate about what made a note, and
    a model cannot be accurate about that.
    """
    counts = {}
    for unit in spec["sources"]["units"]:
        counts[unit["kind"]] = counts.get(unit["kind"], 0) + 1
    described = ", ".join(f"{count} {kind}" for kind, count in sorted(counts.items()))
    lines = [
        f"Composed by `vault-compose` ({spec['intent']}) from {described}.",
        "",
        f"Request: {spec['request']}",
        "",
        "Sources:",
    ]
    for unit in spec["sources"]["units"]:
        detail = unit.get("url") or (unit.get("origin") or {}).get("path") or ""
        lines.append(f"- `{unit['id']}` {unit['label']}{f' — {detail}' if detail else ''}")
    lines.extend(["", f"Source set `{spec['sources']['fingerprint'][:12]}`; run `{Path(run_dir).name}`."])
    return {"title": "How this note was made", "lines": lines}


def assemble(args, vault, schema, fmt, spec, note, drafted, run_dir, taken):
    """One composed note, rendered and checked."""
    blocks = {"title": note["title"]}
    for name, lines in drafted.items():
        entry = next((item for item in fmt["blocks"] if item["block"] == name), None)
        if entry is None:
            continue
        if name == "body":
            blocks["body"] = lines
        elif name == "sources":
            blocks["sources"] = [line.lstrip("- ").strip() for line in lines if line.strip()]
        elif name in fmt["callouts"]:
            blocks[name] = {"title": None, "lines": lines}
        else:
            blocks[name] = lines
    blocks["provenance"] = provenance_block(spec, run_dir)
    metadata = frontmatter_metadata(schema, spec)
    text = vault_compose.render_note(fmt, schema, metadata, blocks)
    review = check_note(fmt, spec, note, drafted, text)
    # Not a hold. `domain` is required by the schema and deliberately not written
    # here -- guessing one buries a note where nothing looks for it -- so every
    # composed note is missing it by design, and holding for that would hold
    # every note this skill ever makes. `vault-organizer` reads the note and
    # fills it. Reported so the gap is visible rather than silent.
    unfiled = missing_required_properties(metadata, schema)
    filename = assign_filename(vault, note["title"], taken)
    return {
        "title": note["title"],
        "destination": (Path(INBOX_DIR) / filename).as_posix(),
        "text": text,
        "words": len(text.split()),
        "blocks": [block["block"] for block in note["blocks"]],
        # What `apply` needs to run the same checks over the file on disk: which
        # sources each block claimed, and what the bytes were when the reviewer
        # last saw them.
        "cited": {block["block"]: block["sourceIds"] for block in note["blocks"]},
        "text_sha256": text_digest(text),
        "unfiled_properties": unfiled,
        "review": review,
        "reviewer_review": [],
        "needs_review": bool(review),
    }


def text_digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def all_review(record):
    """Every reason a note is held, deterministic and reviewer alike."""
    return list(record.get("review") or []) + list(record.get("reviewer_review") or [])


def assign_filename(vault, title, taken):
    """A free name in the inbox. Never overwrites; a taken name gets a suffix."""
    stem = safe_title(title)
    suffix = 1
    while True:
        candidate = f"{stem}.md" if suffix == 1 else f"{stem} ({suffix}).md"
        rel = (Path(INBOX_DIR) / candidate).as_posix()
        if rel.casefold() not in taken and not (vault / rel).exists():
            taken.add(rel.casefold())
            return candidate
        suffix += 1


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def compose(args):
    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        raise UserError(f"vault root does not exist: {vault}")
    spec = load_spec(args.spec)
    warnings = list(spec["warnings"])
    schema_path = resolve_schema_path(vault, args.schema)
    schema, schema_hash = compiled_schema_for(vault, schema_path, cache_dir=vault / STATE_DIR / "cache")
    format_path = vault_format.resolve_format_path(vault, args.format, disabled=args.no_format)
    fmt, format_hash = vault_format.compiled_format_for(vault, format_path, cache_dir=vault / STATE_DIR / "cache")
    if not fmt or not fmt.get("blocks"):
        raise UserError(
            "a composed note is assembled from the vault's declared block order, and this vault declares none; "
            f"add a '### {vault_format.GRAMMAR_SUBSECTION}' table to {vault_format.DEFAULT_FORMAT}"
        )
    voice_path = vault_voice.resolve_voice_path(vault, args.voice, disabled=args.no_voice)
    voice, voice_hash = vault_voice.compiled_voice_for(vault, voice_path, cache_dir=vault / STATE_DIR / "cache")
    args.compiled_voice = voice

    run_dir = Path(args.run).expanduser().resolve() if args.run else unique_run_directory(vault)
    configuration = {
        "workflow": WORKFLOW,
        "command": "compose",
        "input": {
            "vault": str(vault),
            "schema_hash": schema_hash,
            "source_fingerprint": spec["sources"]["fingerprint"],
            **vault_format.format_state(format_path, format_hash),
            **vault_voice.voice_state(voice_path, voice_hash, vault_voice.CONTEXT_OWNER),
        },
        "options": {"intent": spec["intent"], "noteType": spec["noteType"], "maxNotes": spec["maxNotes"]},
    }
    if args.run:
        state = run_state.load_run_state(run_dir, workflow=WORKFLOW)
        try:
            run_state.assert_compatible_run(state, configuration)
        except ValueError as error:
            raise UserError(str(error)) from error
    else:
        run_state.initialize_run_state(
            run_dir,
            run_state.create_run_state(
                WORKFLOW, "compose", configuration["input"], configuration["options"], phase="outline"
            ),
        )
    vault_compose.dump_source_set(spec["sources"], run_dir / "sources.json")

    plan = outline(args, fmt, spec)
    for dropped in plan["dropped_blocks"]:
        warnings.append(f"dropped a block this vault does not declare ({dropped})")
    run_state.atomic_write_json(run_dir / "outline.json", plan)
    run_state.update_run_state(
        run_dir,
        lambda draft: draft.update({"phase": "draft"}) or draft,
        event={"type": "phase", "phase": "outline", "notes": len(plan["notes"])},
    )

    proposals = []
    taken = set()
    for position, note in enumerate(plan["notes"], start=1):
        progress(f"[compose {position}/{len(plan['notes'])}] {note['title']}")
        try:
            drafted = draft(args, fmt, spec, note)
        except (forge_llm.ChatError, UserError, ValueError) as error:
            warnings.append(f"{note['title']}: could not be drafted ({type(error).__name__}: {error})")
            continue
        record = assemble(args, vault, schema, fmt, spec, note, drafted, run_dir, taken)
        record["id"] = "n-%03d" % position
        if record["unfiled_properties"]:
            warnings.append(
                f"{record['id']}: leaves {', '.join(record['unfiled_properties'])} for vault-organizer to fill"
            )
        (run_dir / "proposed").mkdir(exist_ok=True)
        (run_dir / "proposed" / f"{record['id']}.md").write_text(record["text"], encoding="utf-8")
        proposals.append(record)

    verification = None
    if args.verify and proposals:
        verification, stage_warnings = verify_proposals(args, spec, proposals, run_dir)
        warnings.extend(stage_warnings)
    run_state.update_run_state(
        run_dir, lambda draft: draft.update({"phase": "propose"}) or draft,
        event={"type": "phase", "phase": "draft", "notes": len(proposals)},
    )

    run_state.atomic_write_json(
        run_dir / "proposals.json",
        {"proposals": [{key: value for key, value in record.items() if key != "text"} for record in proposals]},
    )
    report_path = write_report(run_dir, spec, proposals, warnings, verification)
    run_state.update_run_state(
        run_dir,
        lambda draft: draft.update(
            {
                "phase": "planned",
                "status": "running",
                "nextAction": f"review {report_path.name}, then `apply --run {run_dir} --accept <id>`",
            }
        )
        or draft,
        event={"type": "phase", "phase": "propose", "proposals": len(proposals)},
    )
    return structured(
        "ok",
        artifacts=[str(report_path)],
        warnings=warnings,
        data={
            "vault": str(vault),
            "run_directory": str(run_dir),
            "intent": spec["intent"],
            "counts": {
                "proposed": len(proposals),
                "held": sum(1 for record in proposals if record["needs_review"]),
                "sources": len(spec["sources"]["units"]),
            },
            "verification": verification,
            "proposals": [
                {
                    "id": record["id"],
                    "title": record["title"],
                    "destination": record["destination"],
                    "words": record["words"],
                    "blocks": record["blocks"],
                    "needs_review": record["needs_review"],
                    "review": all_review(record),
                }
                for record in proposals
            ],
        },
    )


def verify_proposals(args, spec, proposals, run_dir):
    """A batch review on the thinking service. Never approval by silence."""
    think = forge_llm.resolve_think_or_chat(base_url=args.think_url, model=args.think_model)
    items = [
        {
            "id": record["id"],
            "title": record["title"],
            "note": record["text"],
            "request": spec["request"],
        }
        for record in proposals
        if not record["needs_review"]
    ]
    if not items:
        return {"reviewed": 0, "flagged": 0, "skipped": "every note was already held"}, []
    try:
        verdicts = forge_verify.verify_packets(
            think,
            VERIFY_SYSTEM,
            items,
            journal_path=run_dir / "verified.jsonl",
            timeout=args.request_timeout,
            progress=progress,
        )
    # OSError included deliberately: a refused connection is the most ordinary way
    # for a reviewer to be down, and it must read as "not reviewed" rather than
    # taking the whole run with it.
    except (forge_verify.VerificationError, forge_llm.ChatError, UserError, ValueError, OSError) as error:
        # An unreachable reviewer is reported as unreviewed, never as approval.
        return {"reviewed": 0, "flagged": 0, "skipped": str(error)}, [
            f"verification did not run ({error}); the notes below were not reviewed"
        ]
    flagged = 0
    by_id = {record["id"]: record for record in proposals}
    for note_id, verdict in verdicts.items():
        record = by_id.get(note_id)
        if record is None or verdict.get("verdict") != forge_verify.VERDICT_FLAG:
            continue
        flagged += 1
        # Kept apart from the deterministic findings because `apply` recomputes
        # those from the file and cannot recompute this one: re-reviewing needs
        # the think service, which an apply must never depend on.
        record["reviewer_review"].append(f"reviewer: {verdict.get('reason') or 'flagged without a reason'}")
        record["needs_review"] = True
    return {"reviewed": len(items), "flagged": flagged}, []


VERIFY_SYSTEM = """You review notes composed for a personal knowledge vault against the request they answer.

Flag a note only for something a reader would have to fix: a claim the note makes
that its own sources do not support, a title that does not describe the note, a
note that answers a different question than the request, or apparatus written as
if it were the author's own thinking.

Do not flag style, length, or wording you would have chosen differently.

Return JSON only:
{"verdicts": [{"id": "n-001", "verdict": "ok"}, {"id": "n-002", "verdict": "flag", "reason": "..."}]}"""


def write_report(run_dir, spec, proposals, warnings, verification):
    lines = [
        "# Composed notes",
        "",
        f"Intent: {spec['intent']}",
        f"Request: {spec['request']}",
        f"Sources: {len(spec['sources']['units'])}",
        "",
        "Nothing is written until you accept an id:",
        "",
        f"```bash\npython3 <skill>/scripts/vault-compose.py apply --vault <vault> --run {run_dir} --accept n-001\n```",
        "",
    ]
    for record in proposals:
        lines.append(f"## {record['id']} — {record['title']}")
        lines.append("")
        lines.append(f"- Destination: `{record['destination']}`")
        lines.append(f"- Blocks: {', '.join(record['blocks'])} ({record['words']} words)")
        if all_review(record):
            lines.append("- **Held for review:**")
            lines.extend(f"  - {line}" for line in all_review(record))
            lines.append(
                "  - Fix the note in `proposed/` and apply again: the deterministic checks "
                "run over the file as it then stands."
            )
        lines.extend(["", "---", "", record["text"], "", "---", ""])
    if verification:
        lines.extend(["## Verification", "", json.dumps(verification), ""])
    if warnings:
        lines.extend(["## Warnings", ""] + [f"- {line}" for line in warnings] + [""])
    path = run_dir / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def format_for_run(vault, state, args):
    """The block grammar this run composed against, so apply checks the same one."""
    recorded = (state.get("input") or {}).get("format_path")
    format_path = (
        Path(recorded)
        if recorded
        else vault_format.resolve_format_path(vault, args.format, disabled=args.no_format)
    )
    fmt, _ = vault_format.compiled_format_for(vault, format_path, cache_dir=vault / STATE_DIR / "cache")
    if not fmt or not fmt.get("blocks"):
        raise UserError(
            f"the note format this run composed against declares no block order ({format_path}); "
            "a note that cannot be parsed cannot be checked, so nothing is written"
        )
    return fmt


def sticky_reviewer_review(record):
    """Reviewer verdicts, which an edit does not clear.

    Held apart from the deterministic findings because they cannot be recomputed
    here: re-reviewing needs the think service, and an apply that reached for a
    model would fail whenever the model was down. Runs composed before the split
    kept both in one list, distinguishable by the prefix the reviewer writes.
    """
    if "reviewer_review" in record:
        return list(record["reviewer_review"] or [])
    return [line for line in (record.get("review") or []) if str(line).startswith("reviewer: ")]


def resolve_destination(vault, relative):
    """The absolute path a proposal names, refused if it leaves the inbox."""
    destination = (vault / relative).resolve()
    if destination.parent != (vault / INBOX_DIR).resolve():
        raise UserError(f"proposal destination is outside {INBOX_DIR} and was not written: {relative}")
    return destination


def apply_notes(args):
    """Write the accepted notes, and nothing else.

    The gate here is recomputed, never read. `proposals.json` records what the
    compose run found, but the bytes reaching the vault are whatever sits in
    `proposed/` at this moment -- so trusting the stored verdict would check one
    note and write another. An edit to a proposal, or to the verdict in the
    manifest, would walk straight past a check that only read what compose left
    behind. So the deterministic checks run again, over the file as it stands.

    That also makes fixing a held note a supported move rather than a dead end:
    correct what was flagged, apply again, and the same checks decide.
    """
    vault = Path(args.vault).expanduser().resolve()
    run_dir = Path(args.run).expanduser().resolve()
    state = run_state.load_run_state(run_dir, workflow=WORKFLOW)
    stored = json.loads((run_dir / "proposals.json").read_text(encoding="utf-8"))["proposals"]
    by_id = {record["id"]: record for record in stored}
    accepted = [value for entry in (args.accept or []) for value in str(entry).split(",") if value.strip()]
    unknown = [value for value in accepted if value not in by_id]
    if unknown:
        raise UserError(f"unknown proposal ids: {', '.join(unknown)}")
    if not accepted:
        raise UserError("apply needs --accept <id>; nothing is written without one")
    fmt = format_for_run(vault, state, args)
    sources = vault_compose.load_source_set(run_dir / "sources.json")
    log_path = run_dir / "apply-log.jsonl"
    prior, _ = run_state.read_jsonl_recover_tail(log_path, repair=True)
    done = {entry.get("id") for entry in prior if entry.get("status") == "ok"}
    written = []
    warnings = []
    rechecked = []
    for note_id in accepted:
        record = by_id[note_id]
        if note_id in done:
            continue
        proposed = run_dir / "proposed" / f"{note_id}.md"
        if not proposed.is_file():
            warnings.append(f"{note_id}: {proposed.name} is not in the run and was not written")
            continue
        text = proposed.read_text(encoding="utf-8")
        review = vault_compose.check_rendered(fmt, sources, record.get("cited"), text)
        reviewer = sticky_reviewer_review(record)
        if reviewer and text_digest(text) != record.get("text_sha256"):
            reviewer = [f"{line} (the reviewer has not seen this edit; recompose to have it reviewed)" for line in reviewer]
        review.extend(reviewer)
        rechecked.append(note_id)
        if review:
            warnings.append(f"{note_id} was held for review and is not written: {'; '.join(review)}")
            continue
        destination = resolve_destination(vault, record["destination"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Exclusive create: a composed note is always new, so a taken name is
            # a collision to report rather than a file to overwrite.
            with open(destination, "xb") as handle:
                handle.write(text.encode("utf-8"))
        except FileExistsError:
            warnings.append(f"{note_id}: {record['destination']} already exists and was not overwritten")
            continue
        run_state.append_jsonl_fsync(
            log_path, {"id": note_id, "status": "ok", "destination": record["destination"]}
        )
        written.append(record["destination"])
    return structured(
        "ok",
        warnings=warnings,
        data={
            "vault": str(vault),
            "run_directory": str(run_dir),
            "written": written,
            # Named so a caller can tell "this passed" from "this was never looked at".
            "rechecked": rechecked,
        },
    )


def doctor(args):
    vault = Path(args.vault).expanduser().resolve()
    checks = {}
    warnings = []
    ok = vault.is_dir()
    checks["vault"] = {"ok": ok, "path": str(vault)}
    inbox = vault / INBOX_DIR
    checks["inbox"] = {"ok": inbox.is_dir() and os.access(inbox, os.W_OK), "path": str(inbox)}
    ok = ok and checks["inbox"]["ok"]
    try:
        schema_path = resolve_schema_path(vault, args.schema)
        schema, schema_hash = compiled_schema_for(vault, schema_path)
        missing = [value for value in ("generated", "chat") if value not in schema["capture_types"]]
        checks["schema"] = {
            "ok": not missing,
            "path": str(schema_path),
            "schema_hash": schema_hash,
            "detail": f"schema does not define capture types: {', '.join(missing)}" if missing else None,
        }
    except UserError as error:
        checks["schema"] = {"ok": False, "detail": str(error)}
    ok = ok and checks["schema"]["ok"]
    try:
        format_path = vault_format.resolve_format_path(vault, args.format, disabled=args.no_format)
        fmt, _hash = vault_format.compiled_format_for(vault, format_path) if format_path else (None, None)
        blocks = len((fmt or {}).get("blocks") or [])
        checks["format"] = {
            "ok": blocks > 0,
            "path": str(format_path) if format_path else None,
            "blocks": blocks,
            "detail": None
            if blocks
            else f"no '### {vault_format.GRAMMAR_SUBSECTION}' table; compose cannot assemble a note without one",
        }
        if format_path:
            findings = vault_format.load_and_check(vault, raw_format=str(format_path))
            for severity, message in findings:
                warnings.append(f"note format [{severity}] {message}")
    except UserError as error:
        checks["format"] = {"ok": False, "detail": str(error)}
    ok = ok and checks["format"]["ok"]
    probe = forge_llm.service_doctor(chat_service(args), expect_non_thinking=True, timeout=min(args.request_timeout, 60))
    checks["chat"] = {"ok": probe["reachable"], "url": probe["url"], "model": probe["model"], "detail": probe.get("detail")}
    if probe.get("warning"):
        warnings.append(probe["warning"])
    ok = ok and probe["reachable"]
    return structured("ok" if ok else "error", warnings=warnings, data={"checks": checks})


def status(args):
    run_dir = Path(args.run).expanduser().resolve()
    state = run_state.load_run_state(run_dir, workflow=WORKFLOW)
    return structured("ok", data={"run_directory": str(run_dir), "state": state})


class TrackingAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        setattr(namespace, f"{self.dest}_provided", True)


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="vault-compose.py")
    parser.add_argument("mode", choices=["compose", "apply", "status", "doctor"])
    parser.add_argument("--vault")
    parser.add_argument("--spec", help="the run spec JSON describing what to compose from what")
    parser.add_argument("--run", help="existing run directory")
    parser.add_argument("--accept", action="append", help="proposal ids to write, repeatable or comma-separated")
    parser.add_argument("--schema", action=TrackingAction)
    parser.add_argument("--format", action=TrackingAction, help="note-format note (default: the vault's)")
    parser.add_argument("--no-format", action="store_true")
    parser.add_argument("--voice", action=TrackingAction, help="voice-and-style note (default: the vault's)")
    parser.add_argument("--no-voice", action="store_true")
    parser.add_argument("--no-verify", action="store_true")
    parser.add_argument("--think-url", action=TrackingAction)
    parser.add_argument("--think-model", action=TrackingAction)
    parser.add_argument("--base-url", action=TrackingAction)
    parser.add_argument("--model", action=TrackingAction)
    parser.add_argument("--api-key")
    parser.add_argument("--request-timeout", type=int, default=DEFAULT_REQUEST_TIMEOUT)
    parser.add_argument("--no-cache-prompt", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.mode == "status":
        if not args.run:
            raise UserError("status requires --run <run-directory>")
        return args
    if not args.vault:
        raise UserError(f"{args.mode} requires --vault")
    if args.mode == "compose" and not args.spec and not args.run:
        raise UserError("compose requires --spec <run-spec.json>")
    if args.mode == "apply" and not args.run:
        raise UserError("apply requires --run <run-directory>")
    args.schema = args.schema or os.environ.get("VAULT_COMPOSE_SCHEMA") or None
    args.format = args.format or os.environ.get("VAULT_COMPOSE_FORMAT") or None
    args.voice = args.voice or os.environ.get("VAULT_COMPOSE_VOICE") or None
    if args.no_format and getattr(args, "format_provided", False):
        raise UserError("--format and --no-format cannot be used together")
    if args.no_voice and getattr(args, "voice_provided", False):
        raise UserError("--voice and --no-voice cannot be used together")
    if args.mode == "apply":
        # Accepted and silently ignored until now, which read as "the check was
        # skipped and held it anyway" to anyone who tried it.
        if args.no_verify:
            raise UserError(
                "--no-verify turns off the reviewer during compose; the checks apply runs are "
                "deterministic and are not optional. Fix what was flagged and apply again."
            )
        return args
    resolved = forge_llm.resolve_service("chat", base_url=args.base_url, model=args.model)
    args.base_url = resolved["url"]
    args.model = resolved["model"]
    args.api_key = args.api_key or os.environ.get("VAULT_COMPOSE_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    args.cache_prompt = not args.no_cache_prompt
    args.verify = not args.no_verify
    return args


def run(argv):
    args = parse_args(argv)
    if args.mode == "status":
        return status(args)
    if args.mode == "doctor":
        return doctor(args)
    if args.mode == "apply":
        return apply_notes(args)
    return compose(args)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        result = run(argv)
    except UserError as error:
        result = structured("error", errors=[{"code": "user_error", "message": str(error)}])
    except forge_llm.ChatError as error:
        result = structured("error", errors=[{"code": "chat_error", "message": str(error)}])
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())

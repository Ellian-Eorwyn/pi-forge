#!/usr/bin/env python3
"""Turn a braindump into schema-valid notes in an Obsidian vault inbox.

The input is thinking, not a document: a paragraph typed into chat, a page of
half-formed plans, a dictated ramble that already came back from transcription.
One dump usually holds more than one note — an idea, a couple of errands, a
question to look into later — and left as a single blob none of them are
findable.

This pipeline splits a dump into its distinct notes, drafts each one as prose,
names it, gives it schema-valid frontmatter, and writes it to ``00 Inbox``. The
braindump itself is preserved verbatim under a ``# Braindump`` heading in the
primary note, because the drafts are a convenience and the user's own words are
the record.

It is the counterpart to ``vault-transcripts``, not a replacement: that skill
preserves a recording and cleans it, this one synthesizes notes out of raw
thinking. Both hand off to ``vault-organizer``, which decides where a note
belongs.

Every note this skill writes carries ``capture_type: generated``. The words are
the user's; the note is the machine's, and the vault says so.

Splitting and drafting are one call per unit on the non-thinking ``chat``
service; the whole batch is then reviewed on ``think`` in a handful of packets.
Deterministic checks run before either, because a byte-exact invariant beats a
model's opinion and costs nothing.
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
import forge_llm
import forge_routing
import forge_verify
import run_state
import vault_compose
import vault_lexicon
import vault_profile
import vault_reflection
import vault_voice
from vault_schema import (
    INBOX_DIR,
    UserError,
    compiled_schema_for,
    parse_frontmatter,
    resolve_schema_path,
    safe_title,
    serialize_frontmatter,
    sha256_bytes,
    sha256_text,
    split_frontmatter,
    validate_filename_title,
)

WORKFLOW = "vault-capture"
PROMPT_VERSION = "vault-capture-v2"
STATE_DIR = ".vault-capture"

# What a braindump can turn into. The kind reaches the note's `type`, so each
# one maps to a schema type and falls back when a vault does not define it.
CAPTURE_KINDS = ("idea", "task", "journal", "question", "reference", "draft", "plan")
KIND_TO_NOTE_TYPE = {
    "idea": "note",
    "task": "task",
    "journal": "journal",
    "question": "note",
    "reference": "note",
    "draft": "draft",
    "plan": "note",
}
FALLBACK_NOTE_TYPE = "note"

# Which reflection sections a kind gets, keyed on kind rather than schema type:
# `idea`, `question`, `reference`, and `plan` all collapse to type `note`, so the
# type cannot tell them apart from each other or from anything else filed there.
# The headings themselves are shared with vault-transcripts; only this mapping
# from kind to heading set is this skill's.
#
# `draft` is the one kind with none: it is prose the person is composing, and
# appending machine commentary to a draft damages the thing being drafted.
JOURNAL_SECTIONS = vault_reflection.JOURNAL_HEADINGS
WORKING_SECTIONS = vault_reflection.WORKING_HEADINGS
KIND_TO_REFLECTION = {
    "idea": WORKING_SECTIONS,
    "task": WORKING_SECTIONS,
    "journal": JOURNAL_SECTIONS,
    "question": WORKING_SECTIONS,
    "reference": WORKING_SECTIONS,
    "draft": (),
    "plan": WORKING_SECTIONS,
}
REFLECTION_HEADINGS = vault_reflection.REFLECTION_HEADINGS
SECTION_GUIDANCE = {
    "Observations": "What the person directly described, stated before any reading of it.",
    "Interpretations": (
        "Tentative readings only. Never diagnose the person, and never claim to know what "
        "their words meant better than they did."
    ),
    "Open questions": (
        "What the material leaves unresolved — a decision not made, a fact not known, a "
        "dependency not named. Only what it actually raises; do not invent doubt."
    ),
    "Context": (
        "What this note belongs to — the project, thread, or earlier note it continues. One "
        "or two lines, and never a restatement of the note."
    ),
    "Next steps": (
        "An action the material implies but did not state as a step. When it already lists "
        "its own steps, this section is empty."
    ),
    "Connections": "Vault notes this relates to, with a few words on why.",
}

FILENAME_PATTERNS = ("topic", "date-topic")
DEFAULT_MAX_NOTES = 8
MAX_TITLE_CHARS = 60
# One dump is one prompt. Past this the split stops being a split and the input
# is really a document, which document-ingest handles better.
MAX_BRAINDUMP_CHARS = 40000
DRAFT_INPUT_CHARS = 24000
VERIFY_SOURCE_CHARS = 6000
MIN_BRAINDUMP_WORDS = 15

# Synthesis compresses, so a low retention floor is normal and only a collapse
# is interesting. This warns rather than holds: a short dump legitimately
# becomes a shorter note.
COVERAGE_WARN_RATIO = 0.4
COVERAGE_MIN_SOURCE_WORDS = 120

# Style examples: enough to show a register, not enough to crowd out the
# braindump the draft is actually working from.
EXEMPLAR_COUNT = 3
EXEMPLAR_CHARS = 1200
EXEMPLAR_TOTAL_CHARS = 4000
EXEMPLAR_MIN_CHARS = 200
EXEMPLAR_SEARCH_LIMIT = 12
EXEMPLAR_SEARCH_TIMEOUT = 120
PREFS_RUN_CONTEXT_CHARS = 6000

BRAINDUMP_HEADING = "# Braindump"
URL_RE = vault_reflection.URL_RE
WIKILINK_RE = re.compile(r"\[\[[^\]]+\]\]")
TIMESTAMP_LINE_RE = re.compile(r"^\*\d{1,2}(?::\d{2}){1,2}\*$")
SPEAKER_LINE_RE = re.compile(r"^\*\*(.+)\*\*$")
# What counts as a section of a drafted body. The checks read the body through
# this and so does the callout rendering, so the two cannot disagree about where
# a section starts.
SECTION_HEADING_RE = re.compile(r"^##+\s+(.*)$")
BULLET_RE = re.compile(r"^[-*+]\s+")

# --------------------------------------------------------------------------- #
# Output plumbing
# --------------------------------------------------------------------------- #


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


def print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def log(args, message):
    if getattr(args, "verbose", False):
        print(message, file=sys.stderr, flush=True)


def progress(message):
    print(message, file=sys.stderr, flush=True)


def utc_timestamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def unique_run_directory(vault):
    base = vault / STATE_DIR / "runs"
    base.mkdir(parents=True, exist_ok=True)
    stamp = utc_timestamp()
    candidate = base / f"{stamp}-capture"
    suffix = 2
    while candidate.exists():
        candidate = base / f"{stamp}-capture-{suffix}"
        suffix += 1
    candidate.mkdir(parents=True)
    return candidate


# --------------------------------------------------------------------------- #
# Input
# --------------------------------------------------------------------------- #


def looks_like_transcript_export(text):
    """A raw transcription dump, which vault-transcripts owns.

    Cheap and shape-only: the exports this vault sees are runs of ``*04:12*``
    timestamp lines and ``**Speaker 1**`` labels. Capture would throw that
    structure away and synthesize over it, losing the recording, so it is held
    for the skill that preserves it instead.
    """
    lines = [line.strip() for line in text.splitlines()]
    timestamps = sum(1 for line in lines if TIMESTAMP_LINE_RE.match(line))
    speakers = sum(1 for line in lines if SPEAKER_LINE_RE.match(line))
    return timestamps >= 3 and speakers >= 2


def read_braindump(path):
    data = path.read_bytes()
    split = split_frontmatter(data)
    return split["body"] if split["had_frontmatter"] and not split["malformed"] else data.decode("utf-8")


def scan_inputs(paths, stdin_text, force):
    """Snapshot every braindump before any model sees one."""
    items = []
    seen = set()
    for index, raw in enumerate(paths):
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            raise UserError(f"input file does not exist: {path}")
        if path in seen:
            continue
        seen.add(path)
        text = read_braindump(path)
        items.append(build_input_item(f"in-{index + 1:03d}", str(path), path.name, text, force))
    if stdin_text is not None:
        items.append(build_input_item(f"in-{len(items) + 1:03d}", "<stdin>", "stdin", stdin_text, force))
    if not items:
        raise UserError("no input given: pass one or more files, or --stdin")
    return items


def build_input_item(identifier, source, label, text, force):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    stripped = text.strip()
    held = None
    if not stripped:
        held = "the braindump is empty"
    elif len(stripped.split()) < MIN_BRAINDUMP_WORDS:
        held = f"the braindump is under {MIN_BRAINDUMP_WORDS} words; write it into the vault by hand"
    elif len(stripped) > MAX_BRAINDUMP_CHARS:
        held = f"the braindump is {len(stripped)} characters, over the {MAX_BRAINDUMP_CHARS} limit; use document-ingest"
    elif looks_like_transcript_export(stripped) and not force:
        held = "this looks like a raw transcription export; run vault-transcripts, or pass --force to synthesize from it"
    return {
        "id": identifier,
        "source": source,
        "label": label,
        "text": text,
        "sha256": sha256_text(text),
        "words": len(stripped.split()),
        "characters": len(stripped),
        "held": held,
    }


# --------------------------------------------------------------------------- #
# Split
# --------------------------------------------------------------------------- #

# The enumeration is the point. A non-thinking model asked "how many notes is
# this" answers "one" almost every time; asked to check for each kind of thing a
# braindump contains, it finds the errand buried in the third paragraph.
SPLIT_SYSTEM = """You read one person's braindump and decide how many notes it should become.

A braindump is unedited thinking: talking through a problem, listing what needs doing, working out a plan, remembering something worth keeping. It is not a document and it has no structure you can trust.

Work through what this dump contains before you answer. Check for each of these separately:

- working ideas, arguments, or things being figured out
- tasks, errands, and commitments, including ones mentioned in passing
- reflection on how the person is doing, feeling, or deciding
- questions to look into later
- facts worth keeping: names, numbers, links, recommendations, references
- drafts of something to be written or sent
- plans with steps, dates, or dependencies

One dump is often one note. It is just as often three, because a person thinking out loud does not stay on one subject. Split when the parts would be looked for separately later; keep together what only makes sense read as one thing. Never split a single line of thinking across two notes.

Every part of the dump belongs to exactly one note. Notes must not overlap: never add a note that summarizes the dump, collects "everything else", or repeats what another note already covers. If a part fits two notes, choose one.

Return exactly one JSON object:

{"notes": [{"kind": "idea|task|journal|question|reference|draft|plan", "title": "What the note is about", "gist": "One or two sentences on what this note covers.", "covers": ["short phrase from the dump", "another"]}], "needs_review": false, "review_reason": null}

Titles name the subject, not the medium: "Espresso Machine Gasket Replacement", never "Braindump about the espresso machine". Keep them under 60 characters, plain text, no punctuation that would break a filename.

"covers" lists the parts of the dump this note is responsible for, in the person's own words, short. Between them the notes must cover everything of substance in the dump.

Set needs_review true only when the dump is too fragmentary to divide honestly."""


def split_payload(item, max_notes):
    return {
        "braindump": item["text"][:DRAFT_INPUT_CHARS],
        "maxNotes": max_notes,
        "sourceName": item["label"],
    }


def validate_split(value, max_notes):
    """The model proposes; this decides. Returns (notes, needs_review, reason)."""
    if not isinstance(value, dict):
        raise UserError("split response is not a JSON object")
    if not isinstance(value.get("notes"), list) or not value["notes"]:
        raise UserError("split response has no notes")
    if len(value["notes"]) > max_notes:
        raise UserError(f"split returned {len(value['notes'])} notes, over --max-notes {max_notes}")
    notes = []
    for position, entry in enumerate(value["notes"], start=1):
        if not isinstance(entry, dict):
            raise UserError(f"note {position} is not an object")
        kind = entry.get("kind")
        if kind not in CAPTURE_KINDS:
            raise UserError(f"note {position} has unknown kind {kind!r}")
        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            raise UserError(f"note {position} has no title")
        title = safe_title(title)[:MAX_TITLE_CHARS].strip()
        if not title:
            raise UserError(f"note {position} title is empty once made filename-safe")
        gist = entry.get("gist")
        if not isinstance(gist, str) or not gist.strip():
            raise UserError(f"note {position} has no gist")
        covers = entry.get("covers")
        covers = [text for text in covers if isinstance(text, str) and text.strip()] if isinstance(covers, list) else []
        notes.append({"kind": kind, "title": title, "gist": gist.strip(), "covers": covers[:12]})
    seen = set()
    for note in notes:
        key = note["title"].casefold()
        if key in seen:
            raise UserError(f"split returned two notes titled {note['title']!r}")
        seen.add(key)
    needs_review = value.get("needs_review")
    if not isinstance(needs_review, bool):
        needs_review = False
    reason = value.get("review_reason")
    return notes, needs_review, reason if isinstance(reason, str) else None


def call_json(args, service, system, payload, task, background=False, extra=None):
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    if extra:
        messages.append({"role": "user", "content": json.dumps(extra, ensure_ascii=False)})
    # A caller that names a service gets it: the escalation path deliberately
    # redoes a flagged note on the thinking service, and routing must not
    # second-guess that. Passing `None` asks for the stage's route instead, where
    # `task` is the routing key — and a stage the table does not name resolves to
    # `chat`, which is what the caller would have passed anyway.
    value, _record = forge_llm.call_json_with_retry(
        service or forge_routing.service_for(task, args),
        messages,
        temperature=0,
        cache_prompt=args.cache_prompt,
        response_format={"type": "json_object"},
        background=background,
        timeout=args.request_timeout,
        api_key=args.api_key,
        task=task,
    )
    return value


def split_items(args, service, items, run_dir):
    """One call per braindump. Journaled, so a resumed run re-splits nothing."""
    journal = run_dir / "split.jsonl"
    done, warnings = run_state.read_jsonl_recover_tail(journal, repair=True)
    by_id = {row["id"]: row for row in done if "id" in row}
    results = []
    pending = [item for item in items if not item["held"] and item["id"] not in by_id]
    for position, item in enumerate(pending, start=1):
        started = time.time()
        record = {"at": run_state.utc_now(), "id": item["id"], "source": item["source"]}
        try:
            # None, not `service`: splitting is routed, and the report put it on
            # the thinking profile (7/8 against 4/8, and it clears a silent
            # failure the bulk model carries on the same fixture).
            value = call_json(args, None, SPLIT_SYSTEM, split_payload(item, args.max_notes), "split-braindump")
            notes, needs_review, reason = validate_split(value, args.max_notes)
            record.update({"status": "ok", "notes": notes, "needs_review": needs_review, "review_reason": reason})
        except (UserError, forge_llm.ChatError) as error:
            record.update({"status": "error", "detail": str(error)})
        record["seconds"] = round(time.time() - started, 3)
        run_state.append_jsonl_fsync(journal, record)
        by_id[item["id"]] = record
        progress(f"[split {position}/{len(pending)}] {item['label']}: {len(record.get('notes') or [])} note(s)")
    for item in items:
        if item["held"]:
            continue
        record = by_id.get(item["id"])
        if record is None:
            continue
        if record["status"] != "ok":
            item["held"] = f"could not be divided into notes: {record['detail']}"
            warnings.append(f"{item['label']}: {record['detail']}")
            continue
        if record.get("needs_review"):
            item["held"] = record.get("review_reason") or "the model asked for review of how this dump divides"
            continue
        results.append((item, record["notes"]))
    return results, warnings


# --------------------------------------------------------------------------- #
# Exemplars
# --------------------------------------------------------------------------- #


def connections_script():
    return Path(__file__).resolve().parents[2] / "vault-connections" / "scripts" / "vault-connections.py"


def search_vault(vault, query, limit=EXEMPLAR_SEARCH_LIMIT, timeout=EXEMPLAR_SEARCH_TIMEOUT):
    """Ask vault-connections for notes about this topic.

    Shelling out rather than importing keeps one implementation of the vector
    store. Search is a nice-to-have here, so every failure degrades to no
    exemplars rather than failing the run.
    """
    script = connections_script()
    if not script.is_file():
        return [], "vault-connections is not installed alongside this skill"
    try:
        completed = subprocess.run(
            [sys.executable, str(script), "search", query, "--vault", str(vault), "--search-limit", str(limit)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return [], f"vault search failed: {error}"
    if completed.returncode != 0:
        return [], "vault search failed; drafting without style examples"
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return [], "vault search returned unreadable output"
    hits = (payload.get("data") or {}).get("hits") or []
    return [row.get("path") for row in hits if isinstance(row, dict) and row.get("path")], None


def exemplar_excerpt(text, limit=EXEMPLAR_CHARS):
    """The head of a note's body, cut at a paragraph boundary.

    Stops before a preserved-source heading: what follows those is raw
    transcription or an unedited braindump, which is the opposite of the
    considered writing this is looking for.
    """
    for marker in (BRAINDUMP_HEADING, "# Transcript"):
        index = text.find(f"\n{marker}")
        if index != -1:
            text = text[:index]
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text.rfind("\n\n", 0, limit)
    return text[: cut if cut > limit // 2 else limit].strip()


PROVENANCE_BLOCK_RE = re.compile(r"^>\s*\[!provenance\]", re.IGNORECASE | re.MULTILINE)


def machine_authored(values, body):
    """Whether a note was written by a pipeline rather than by the vault's owner.

    Frontmatter alone does not answer this. `capture_type` records the *channel* a
    note arrived by, and `vault-transcripts` correctly writes `voice` on a note it
    generated, so the property is true and useless here. `processed_by` was the
    other half of the test and is not an approved property in every vault -- where
    it is not, `serialize_frontmatter` drops it, and every transcript-derived note
    in that vault reads as owner-authored. That is the state this vault is in, so
    the guard against a model learning its own habits has been open for as long as
    transcripts have been processed.

    The `> [!provenance]-` block is the reliable marker: `0.04 Note Format.md`
    says it must be accurate about what made a note, and that a note written by
    hand does not get one. It survives filing, which rewrites frontmatter
    wholesale, and it cannot be dropped by a schema that never approved it.
    """
    if values.get("capture_type") == "generated" or values.get("processed_by"):
        return True
    return bool(PROVENANCE_BLOCK_RE.search(str(body)))


def collect_exemplars(vault, query, wanted=EXEMPLAR_COUNT, note_type=None):
    """Notes by this person that read the way a new note should.

    Anything the pipeline wrote is excluded. A model shown its own output learns
    its own habits, and the point of an exemplar is to sound like the person
    whose vault this is.
    """
    paths, warning = search_vault(vault, query)
    if warning:
        return [], warning
    chosen = []
    fallback = []
    for relative in paths:
        if len(chosen) >= wanted:
            break
        path = vault / relative
        if not path.is_file() or relative.startswith(f"{INBOX_DIR}/"):
            continue
        try:
            split = split_frontmatter(path.read_bytes())
        except (OSError, UnicodeDecodeError):
            continue
        values = {} if split["malformed"] else parse_frontmatter(split["frontmatter_text"])
        if machine_authored(values, split["body"]):
            continue
        excerpt = exemplar_excerpt(split["body"])
        if len(excerpt) < EXEMPLAR_MIN_CHARS:
            continue
        entry = {"note": Path(relative).stem, "excerpt": excerpt}
        if note_type and values.get("type") == note_type:
            chosen.append(entry)
        else:
            fallback.append(entry)
    for entry in fallback:
        if len(chosen) >= wanted:
            break
        chosen.append(entry)
    budget = EXEMPLAR_TOTAL_CHARS
    kept = []
    for entry in chosen:
        if budget - len(entry["excerpt"]) < 0:
            break
        budget -= len(entry["excerpt"])
        kept.append(entry)
    return kept, None


def collect_connection_candidates(vault, query):
    """Return only existing notes, so generated wikilinks resolve."""
    paths, warning = search_vault(vault, query)
    if warning:
        return [], warning
    candidates = []
    for relative in paths:
        path = vault / relative
        if path.is_file() and relative.endswith(".md") and not relative.startswith(f"{INBOX_DIR}/"):
            candidates.append({"path": relative, "wikilink": f"[[{Path(relative).stem}]]"})
    return candidates[:8], None


def collect_outside_sources(vault, braindump, candidates):
    """This skill's half of the shared harvest: the braindump plus its candidates.

    What counts as a citable line, and the refusal to fetch anything, live in
    ``vault_reflection`` because they are the same for a recording. Only the
    label a braindump citation carries is this skill's.
    """
    return vault_reflection.outside_sources(braindump, "this braindump", vault, candidates)


# --------------------------------------------------------------------------- #
# Draft
# --------------------------------------------------------------------------- #

# Part A of references/capture-note-format.md, verbatim. If you change one,
# change both.
DRAFT_FIDELITY = """- Every idea, task, question, decision, and factual detail in the braindump appears in exactly one note.
- Never add a fact, name, date, number, link, or commitment the braindump does not contain.
- Preserve uncertainty. "I think", "maybe", and "I'm not sure" are content, not noise.
- The ideas are the person's; the wording is yours. Write what they meant, in clean prose."""

DRAFT_SYSTEM = """You write one note for a person's Obsidian vault, from a braindump they wrote or spoke.

The braindump is unedited thinking. Your job is to turn the part of it assigned to you into a note that is worth finding again in a year: the substance kept, the false starts and repetition gone.

You see the whole braindump, but you are writing **one** note out of several. `thisNote` is your assignment and `otherNotes` lists what the rest cover. Write your part only. Material that belongs to another note is not yours to include — leaving it out is correct, not a loss — and a note that ends up restating the whole dump is the one thing this must never produce. When your assignment is the only note, it covers everything.

Fidelity rules, which outrank everything below:

{fidelity}

How to write it:

- Open with what this is about, in a sentence or two. No preamble, no "this note covers".
- Then the substance, as prose paragraphs. Use `##` headings only when the note really moves between several parts. Use bullets for things that are genuinely a list — tasks, options, steps — not as a default shape.
- Never write a `#` level-one heading. The note's title is its filename.
- Do not add a title line, a summary callout, frontmatter, or a closing summary.
- Keep the person's own terms for things. If they call it "the gasket thing", it is the gasket thing.
- Say what is unresolved as unresolved. Do not tidy an open question into a conclusion.

When `thisNote.styleForThisKind` is present it is the vault owner's own rule for this kind of note, and it wins over the general guidance above. `relevantVocabulary` contains only policy definitions whose terms occur in this material. When `styleExamples` are present they are notes this person wrote themselves: match their register, sentence length, and vocabulary. Do not copy their subject matter.

`glossary` lists specialist terms and names this person uses that the material appears to contain in mistranscribed or misspelled form, each with the words that were produced instead. Where a passage really is that term — the sound and the sense both fit — write the correct spelling; where it is not, leave the text alone. Never introduce a glossary term into a passage that did not say it. `knownPeople` gives the vault's spelling and wikilink for people this material mentions: use that spelling, and when the note links a person, link them as the `wikilink` given rather than inventing a target.

For a journal note, keep the cleaned authorial account first. Mechanically correct written text without changing its wording or meaning. For spoken material, remove filler, false starts, and accidental repetition while preserving emphasis, meaningful self-correction, uncertainty, wording, and sequence. Do not diagnose the person or override what their words mean.

When `thisNote.reflectionSections` is present, append those sections after the note's own content, as `##` headings, in the order given. `thisNote.sectionGuidance` says what belongs under each one. Omit any section that would be empty, and do not manufacture content to fill one: a short note often gets Connections only, or no sections at all.

Where the material for those sections may come from:

- The vault first. Under Connections, use a `connectionCandidates` wikilink exactly as it is given to you.
- `outsideSources` is the only material from outside the vault available to you. Each entry is text this pipeline actually read, with the URL it came from. Use one only when its excerpt genuinely supports what you write.
- A line drawn from `outsideSources` must begin `Outside vault:` and end with that entry's URL in parentheses. Never cite a URL that is not listed there.
- Never state a fact from outside the vault on your own authority. If you know something relevant and it is not in `outsideSources`, leave it out. When `outsideSources` is absent or empty, every connection is a vault wikilink or there are none.

Return exactly one JSON object:

{{"title": "What the note is about", "body": "The note's Markdown body."}}

The title names the subject and stays under 60 characters. Refine the working title if the braindump justifies it; keep it if it is already right."""


def capture_site():
    """Drafting cannot know where a note will be filed -- that is the organizer's
    later decision -- so no route-gated card reaches it. Only the unrestricted
    always-tier survives, which is the presentation material drafting wants."""
    return vault_profile.profile_site(vault_voice.CONTEXT_OWNER, stage="draft")


def draft_system_prompt(voice_segment="", profile=None):
    """Byte-stable within a run: the fidelity contract, the voice, the background.

    Background goes here and *only* here. It must never reach ``draft_payload``:
    ``check_draft`` treats a name absent from the braindump as a hard problem,
    so a card naming someone would invite the model to write that name and the
    gate would then throw the note away.
    """
    system = DRAFT_SYSTEM.format(fidelity=DRAFT_FIDELITY)
    parts = [system]
    if voice_segment:
        parts.append(voice_segment)
    background = vault_profile.profile_prefix(profile, capture_site())
    if background:
        parts.append(background)
    return "\n\n".join(parts)


def draft_payload(
    item,
    note,
    exemplars=None,
    siblings=None,
    type_style=None,
    connection_candidates=None,
    relevant_vocabulary=None,
    glossary=None,
    known_people=None,
    outside_sources=None,
):
    payload = {
        "braindump": item["text"][:DRAFT_INPUT_CHARS],
        "thisNote": {
            "kind": note["kind"],
            "workingTitle": note["title"],
            "gist": note["gist"],
            "covers": note["covers"],
        },
        # Knowing what the other notes are responsible for is what stops a draft
        # quietly absorbing the whole dump. Per-note variation belongs here, in
        # the user message, so the system prompt stays byte-stable.
        "otherNotes": siblings or [],
    }
    if type_style:
        payload["thisNote"]["styleForThisKind"] = type_style
    # The section list and its guidance vary by kind, so they ride here rather
    # than in the system prompt, which has to stay byte-stable for the cache.
    sections = KIND_TO_REFLECTION.get(note["kind"], ())
    if sections:
        payload["thisNote"]["reflectionSections"] = list(sections)
        payload["thisNote"]["sectionGuidance"] = {name: SECTION_GUIDANCE[name] for name in sections}
    if exemplars:
        payload["styleExamples"] = exemplars
    if connection_candidates:
        payload["connectionCandidates"] = connection_candidates
    if outside_sources:
        payload["outsideSources"] = outside_sources
    if relevant_vocabulary:
        payload["relevantVocabulary"] = relevant_vocabulary
    if glossary:
        payload["glossary"] = glossary
    if known_people:
        payload["knownPeople"] = known_people
    return payload


def validate_draft(value):
    if not isinstance(value, dict):
        raise UserError("draft response is not a JSON object")
    title = value.get("title")
    body = value.get("body")
    if not isinstance(title, str) or not title.strip():
        raise UserError("draft has no title")
    if not isinstance(body, str) or not body.strip():
        raise UserError("draft has no body")
    title = safe_title(title)[:MAX_TITLE_CHARS].strip()
    if not title:
        raise UserError("draft title is empty once made filename-safe")
    return title, body.strip()


def draft_items(args, service, system, planned, run_dir):
    """One call per planned note, all on the bulk service before any review."""
    journal = run_dir / "drafted.jsonl"
    done, warnings = run_state.read_jsonl_recover_tail(journal, repair=True)
    by_id = {row["id"]: row for row in done if "id" in row}
    pending = [entry for entry in planned if entry["id"] not in by_id]
    for position, entry in enumerate(pending, start=1):
        started = time.time()
        record = {"at": run_state.utc_now(), "id": entry["id"], "source": entry["item"]["source"]}
        try:
            value = call_json(
                args,
                service,
                system,
                draft_payload(
                    entry["item"],
                    entry["note"],
                    entry.get("exemplars"),
                    entry.get("siblings"),
                    entry.get("type_style"),
                    entry.get("connection_candidates"),
                    entry.get("relevant_vocabulary"),
                    entry.get("glossary"),
                    entry.get("known_people"),
                    entry.get("outside_sources"),
                ),
                "draft-note",
            )
            title, body = validate_draft(value)
            record.update({"status": "ok", "title": title, "body": body})
        except (UserError, forge_llm.ChatError) as error:
            record.update({"status": "error", "detail": str(error)})
        record["seconds"] = round(time.time() - started, 3)
        run_state.append_jsonl_fsync(journal, record)
        by_id[entry["id"]] = record
        progress(f"[draft {position}/{len(pending)}] {entry['note']['title']}")
    for entry in planned:
        record = by_id[entry["id"]]
        if record["status"] == "ok":
            entry["title"] = record["title"]
            entry["body"] = record["body"]
        else:
            entry["held"] = f"could not be drafted: {record['detail']}"
            warnings.append(f"{entry['note']['title']}: {record['detail']}")
    return warnings


# --------------------------------------------------------------------------- #
# Deterministic checks
# --------------------------------------------------------------------------- #


def invented_specifics(source, body, allowed_urls=()):
    """Specifics in the draft with no root in the braindump.

    Rewording is the job, so most of a draft cannot be checked against its
    source. Names, links, and figures can: they were either in the braindump or
    the model made them up, and that is worth catching exactly.

    ``allowed_urls`` is the code-supplied ``outsideSources`` set -- text this run
    actually read, with the URL it came from. Those URLs are legitimately absent
    from the braindump, so without this the correctly-cited outside connection
    the reflection sections ask for would be held as an invented link. Only that
    set widens the check; a URL from anywhere else is still invention.

    The check itself now lives in `vault_compose.ungrounded_specifics`, which
    takes a set of sources rather than one. This skill has exactly one -- the
    braindump -- so it wraps that with a single-unit set and keeps its own name,
    because "invented" is the right word when there is a single source and the
    draft is supposed to be a rewording of it.

    Returns ``{"names", "uncertain_names", "links", "numbers"}``. Only ``names``
    and ``links`` are strong enough to hold a note back; the rest are handed to
    the reviewer, which reads the braindump anyway.
    """
    sources = vault_compose.source_set(
        [vault_compose.source_unit(vault_compose.KIND_BRAINDUMP, "the braindump", source)]
    )
    found = vault_compose.ungrounded_specifics(sources, body, extra_urls=allowed_urls or ())
    # Wikilinks are checked against `connection_candidates` by `check_reflection`,
    # which knows which notes actually exist; the source-set answer would be "none
    # of them", since a braindump names no vault notes.
    found.pop("wikilinks", None)
    return found


def body_blocks(body):
    """A drafted body as its opening lines and its `##` sections.

    Lines are kept exactly as written, because one caller checks what a section
    says and the other has to render it back out without touching it.
    """
    lead, blocks = [], []
    for line in str(body).splitlines():
        heading = SECTION_HEADING_RE.match(line.strip())
        if heading:
            blocks.append({"heading": heading.group(1).strip(), "line": line, "lines": []})
        elif blocks:
            blocks[-1]["lines"].append(line)
        else:
            lead.append(line)
    return lead, blocks


def body_sections(body):
    """``{heading: [bullet, ...]}`` for the `##` sections of a drafted body."""
    sections = {}
    for block in body_blocks(body)[1]:
        items = sections.setdefault(block["heading"], [])
        items.extend(
            BULLET_RE.sub("", line.strip()).strip() for line in block["lines"] if BULLET_RE.match(line.strip())
        )
    return sections


def check_reflection(entry):
    """Hold a note whose reflection points at something that cannot be checked.

    A hold rather than a notice, and rather than the quiet line-dropping
    ``vault-transcripts`` does: an uncited outside claim is the same class of
    fabrication as the invented link right above it in ``check_draft``, capture
    already has a hold path for that, and its contract is that a result is never
    silently discarded. A held note is reported with its reason and re-runnable.
    """
    problems = []
    kind = (entry.get("note") or {}).get("kind")
    sections = body_sections(entry["body"])
    # Section membership needs to know the kind. Where a caller cannot say --
    # only the re-draft recheck, which works from a record -- the connection
    # rules below still apply; they do not depend on it.
    if kind:
        allowed_sections = KIND_TO_REFLECTION.get(kind, ())
        for heading in sections:
            if heading in REFLECTION_HEADINGS and heading not in allowed_sections:
                problems.append(f"a {kind} note wrote the reflection section {heading!r}, which it does not get")
    allowed_wikilinks = {candidate["wikilink"] for candidate in entry.get("connection_candidates") or []}
    allowed_urls = {source["url"] for source in entry.get("outside_sources") or []}
    for item in sections.get("Connections", []):
        links = WIKILINK_RE.findall(item)
        if links:
            unknown = [link for link in links if link not in allowed_wikilinks]
            if unknown:
                problems.append(f"a connection links {unknown[0]}, which is not a candidate note")
            continue
        if not item.startswith("Outside vault:"):
            problems.append(f"a connection has no vault link and is not labelled 'Outside vault:': {item[:80]}")
            continue
        cited = [url.rstrip(".,;:)") for url in URL_RE.findall(item)]
        if not any(url in allowed_urls for url in cited):
            detail = f"cites {cited[0]}" if cited else "cites no source"
            problems.append(f"an outside connection {detail}, which this run never read: {item[:80]}")
    return problems


def check_draft(entry):
    """Everything that can fail one drafted note. Returns (problems, notices)."""
    problems = []
    notices = []
    body = entry["body"]
    headings = [line.strip() for line in body.splitlines() if re.match(r"^#\s", line.strip())]
    if headings:
        problems.append(f"the draft wrote a level-one heading: {headings[0]!r}")
    if body.lstrip().startswith("---"):
        problems.append("the draft wrote its own frontmatter")
    if BRAINDUMP_HEADING.casefold() in body.casefold():
        problems.append("the draft wrote its own braindump section")
    problems.extend(check_reflection(entry))
    cited_urls = {source["url"] for source in entry.get("outside_sources") or []}
    found = invented_specifics(entry["item"]["text"], body, cited_urls)
    if found["names"]:
        problems.append(f"these names are not in the braindump: {', '.join(found['names'][:6])}")
    if found["links"]:
        problems.append(f"these links are not in the braindump: {', '.join(found['links'][:3])}")
    # Rewording legitimately turns "a couple" into "2" and opens a sentence with
    # a word the braindump never used, so these go to the reviewer rather than
    # throwing away a note that is probably fine.
    if found["numbers"]:
        notices.append(f"numbers not found in the braindump: {', '.join(found['numbers'][:6])}")
    if found["uncertain_names"]:
        notices.append(f"words not found in the braindump: {', '.join(found['uncertain_names'][:6])}")
    return problems, notices


# --------------------------------------------------------------------------- #
# Assemble
# --------------------------------------------------------------------------- #


def note_type_for(kind, schema):
    proposed = KIND_TO_NOTE_TYPE.get(kind, FALLBACK_NOTE_TYPE)
    if proposed in schema["types"]:
        return proposed
    if FALLBACK_NOTE_TYPE in schema["types"]:
        return FALLBACK_NOTE_TYPE
    raise UserError(f"schema does not define note type {proposed!r} or {FALLBACK_NOTE_TYPE!r}")


def frontmatter_metadata(schema, kind, related=None, date=None):
    """Minimal and forced.

    `vault-organizer` replaces this block when it files the note and reads it as
    an advisory hint, so it is accurate rather than complete: domain, subdomain,
    project, and people are its judgment, not this skill's. `capture_type` is
    not a hint. This note was written by a model, and no run of this skill can
    produce one that says otherwise.

    `date` is the day the capture happened, which for a braindump is the day its
    content is about. It is written here rather than left to filing because
    `date` is human-owned: classification is never shown it and cannot fill it,
    so a note that leaves this skill without one never gets one. Filing carries
    it forward untouched, and a `date` already on the note is never replaced.
    """
    metadata = {"type": note_type_for(kind, schema), "status": "raw", "capture_type": "generated"}
    metadata["date"] = date or datetime.date.today().isoformat()
    if related and schema["properties"].get("related", {}).get("shape") == "list":
        metadata["related"] = related
    metadata = {key: value for key, value in metadata.items() if key in schema["properties"]}
    if metadata.get("status") != "raw" or "raw" not in schema["statuses"]:
        raise UserError("schema does not define status 'raw'")
    if metadata.get("capture_type") != "generated" or "generated" not in schema["capture_types"]:
        raise UserError("schema does not define capture type 'generated'; capture cannot mark what it writes")
    return metadata


def existing_inbox_names(vault):
    inbox = vault / INBOX_DIR
    if not inbox.is_dir():
        return set()
    return {path.name.casefold() for path in inbox.rglob("*.md")}


def assign_filename(title, pattern, date, taken_casefold):
    stem = f"{date} - {title}" if pattern == "date-topic" else title
    stem = safe_title(stem)
    candidate = f"{stem}.md"
    suffix = 2
    while candidate.casefold() in taken_casefold:
        candidate = f"{stem} ({suffix}).md"
        suffix += 1
    taken_casefold.add(candidate.casefold())
    return candidate


def reflection_tail(blocks, sections):
    """How many trailing sections of a body are its reflection.

    Read backwards, taking sections while each one comes earlier in the kind's
    order than the one after it. That is exactly the shape drafting was asked
    for -- the reflection appended after the note's own content, in the order
    given -- and it is what keeps the rendering off a note that writes its own
    ``## Next steps`` mid-body: a heading is the reflection's because of where
    it sits, not because of what it is called.
    """
    order = {name: index for index, name in enumerate(sections)}
    count, following = 0, len(sections)
    for block in reversed(blocks):
        position = order.get(block["heading"])
        if position is None or position >= following:
            break
        following = position
        count += 1
    return count


def callout_body(lines):
    """A section's lines as callout content: trailing space and outer blanks gone."""
    trimmed = [line.rstrip() for line in lines]
    while trimmed and not trimmed[0]:
        trimmed.pop(0)
    while trimmed and not trimmed[-1]:
        trimmed.pop()
    return trimmed


def fold_reflection(body, kind):
    """The drafted reflection sections, rewrapped as collapsed callouts.

    Drafting writes the whole note, reflection included, as one Markdown body,
    and the deterministic checks read those sections back out of it -- so the
    model is asked for `##` headings and the callouts are put on afterwards,
    here, once the body has passed. Asking it for callout syntax directly would
    put the checks at the mercy of a model getting `>` prefixes right.

    Only the reflection is touched, and the section's own lines pass through as
    written, so a section the model wrote as prose stays prose. A body with no
    reflection -- a `draft`, or a note short enough that every section was
    legitimately omitted -- comes back unchanged, as does a body whose tail
    cannot be read as one, which is the case a heading collision produces.
    """
    sections = KIND_TO_REFLECTION.get(kind, ())
    if not sections:
        return body
    lead, blocks = body_blocks(body)
    folded = reflection_tail(blocks, sections)
    if not folded:
        return body
    kept = list(lead)
    for block in blocks[: len(blocks) - folded]:
        kept.append(block["line"])
        kept.extend(block["lines"])
    rendered = [
        vault_reflection.render_callout(
            vault_reflection.callout_type_for(block["heading"]), block["heading"], callout_body(block["lines"])
        )
        for block in blocks[len(blocks) - folded :]
    ]
    own = "\n".join(kept).rstrip()
    return "\n\n".join([own, *rendered] if own else rendered)


def build_note_text(schema, metadata, kind, body, braindump=None):
    """The generated body, then the braindump verbatim when this is the primary.

    The kind is here rather than defaulted because it decides how the reflection
    renders, and a call site that forgot it would quietly write the plain
    headings this exists to replace.
    """
    text = serialize_frontmatter(metadata, schema) + "\n" + fold_reflection(body, kind).strip() + "\n"
    if braindump is not None:
        text += f"\n{BRAINDUMP_HEADING}\n\n{braindump}"
        if not text.endswith("\n"):
            text += "\n"
    return text


def primary_of(entries):
    """The note that carries the braindump: the first surviving draft.

    Something has to hold the original, and the split lists notes in the order
    the dump raises them, so the first one is where the person started. Picking
    the longest instead would quietly reward a draft that absorbed more of the
    dump than it was assigned.
    """
    return next((entry for entry in entries if not entry.get("held")), None)


def already_created(run_dir):
    """Notes this run has already written, by id.

    A note keeps the name it was given the first time. Without this a rerun
    would see its own output sitting in the inbox, treat it as a collision, and
    write a second copy under a numbered name.
    """
    rows, _warnings = run_state.read_jsonl_recover_tail(run_dir / "created.jsonl", repair=True)
    return {row["id"]: row for row in rows if row.get("status") == "ok" and row.get("id")}


def assemble_one(args, schema, item, entries, taken, date, warnings, prior=None):
    """Name, gate, and lay out the notes from one braindump."""
    prior = prior or {}
    for entry in entries:
        problems, notices = ([], []) if entry.get("held") else check_draft(entry)
        if problems:
            entry["held"] = "; ".join(problems)
        entry["notices"] = notices
    primary = primary_of(entries)
    if primary is not None and item["words"] >= COVERAGE_MIN_SOURCE_WORDS:
        ratio = vault_compose.coverage_ratio(
            item["text"], [entry["body"] for entry in entries if not entry.get("held")]
        )
        if ratio < COVERAGE_WARN_RATIO:
            message = f"{item['label']}: notes kept {ratio:.0%} of the braindump's distinctive words"
            warnings.append(message)
            for entry in entries:
                entry["notices"].append(message)

    records = []
    for entry in entries:
        record = {
            "id": entry["id"],
            "source": item["source"],
            "source_id": item["id"],
            "kind": entry["note"]["kind"],
            "title": entry.get("title") or entry["note"]["title"],
            "gist": entry["note"]["gist"],
            "notices": entry["notices"],
            # Carried so a re-draft is given the same links and cited excerpts the
            # first attempt had. Without them the redraft is asked for reflection
            # sections with nothing it is allowed to cite, and every connection it
            # writes is then held.
            "connection_candidates": entry.get("connection_candidates") or [],
            "outside_sources": entry.get("outside_sources") or [],
            "is_primary": primary is not None and entry["id"] == primary["id"],
            "status": "review" if entry.get("held") else "ok",
            "held_reason": entry.get("held"),
            "destination": None,
            "verified": "not-verified",
        }
        if record["status"] == "ok":
            written = prior.get(record["id"])
            if written:
                record["destination"] = written["destination"]
            else:
                filename = assign_filename(
                    validate_filename_title(record["title"], "note title"), args.filename_pattern, date, taken
                )
                record["destination"] = f"{INBOX_DIR}/{filename}"
        records.append((record, entry))

    # Siblings link back to the note holding the original, which is only
    # nameable once every note in the group has a filename.
    back_link = None
    for record, _entry in records:
        if record["is_primary"] and record["destination"]:
            back_link = f"[[{Path(record['destination']).stem}]]"
    for record, entry in records:
        if record["status"] != "ok":
            continue
        related = [back_link] if back_link and not record["is_primary"] else []
        record["metadata"] = frontmatter_metadata(schema, record["kind"], related)
        record["text"] = build_note_text(
            schema, record["metadata"], record["kind"], entry["body"], item["text"] if record["is_primary"] else None
        )
        if record["is_primary"] and not record["text"].endswith(item["text"].rstrip("\n") + "\n"):
            record["status"] = "review"
            record["held_reason"] = "the braindump is not preserved verbatim in the primary note"
            record["destination"] = None
    return [record for record, _entry in records]


def assemble(args, vault, schema, results, run_dir):
    """Name, gate, and lay out every drafted note. Nothing is written here."""
    warnings = []
    prior = already_created(run_dir)
    taken = existing_inbox_names(vault) - {Path(row["destination"]).name.casefold() for row in prior.values()}
    date = datetime.date.today().isoformat()
    records = []
    for item, entries in results:
        records.extend(assemble_one(args, schema, item, entries, taken, date, warnings, prior))
    run_state.atomic_write_json(
        run_dir / "assembled.json",
        {"records": [{key: value for key, value in row.items() if key != "text"} for row in records]},
    )
    return records, warnings


# --------------------------------------------------------------------------- #
# Verify
# --------------------------------------------------------------------------- #

VERIFY_NOTES_SYSTEM = """You are reviewing notes a model wrote from one person's braindump, before they are saved to that person's Obsidian vault.

You see the braindump and one note drafted from it. Flag the note only when one of these is true:

- it states a fact, name, date, number, or commitment the braindump does not contain
- it drops something substantial the braindump said and no other note covers
- it turns something the person left open into a settled conclusion
- its title names the medium ("Braindump", "Notes", "Voice memo") instead of the subject, or says nothing about what the note is about
- its kind is plainly wrong: a list of errands filed as reflection, a journal entry filed as a task

A note is a synthesis, not a transcript. Rewording, reordering, compressing, and dropping repetition are the job. Do not flag a note because you would have written it differently, chosen a different title, or kept more detail. `notices` lists deterministic checks that could not confirm something; treat them as places to look, not as findings."""

VERIFY_COVERAGE_SYSTEM = """You are checking whether a set of notes together covers one person's braindump, before they are saved to their Obsidian vault.

You see the braindump and every note drafted from it, with titles and bodies. Flag only when:

- something of substance in the braindump appears in none of the notes
- two notes cover the same material, so the split was wrong
- material that belongs together was split across notes and neither reads correctly alone

Do not flag compression, dropped repetition, or a different division you would have preferred. One note for one dump is a correct answer when the dump is about one thing."""


def verify_payload(record, item):
    return {
        "id": record["id"],
        "braindump": item["text"][:VERIFY_SOURCE_CHARS],
        "note": {
            "title": record["title"],
            "kind": record["kind"],
            "body": record.get("text", "").split(f"\n{BRAINDUMP_HEADING}\n", 1)[0],
        },
        "notices": record.get("notices", []),
    }


def coverage_payload(item, records):
    return {
        "id": item["id"],
        "braindump": item["text"][:VERIFY_SOURCE_CHARS],
        "notes": [
            {
                "title": record["title"],
                "kind": record["kind"],
                "body": record.get("text", "").split(f"\n{BRAINDUMP_HEADING}\n", 1)[0],
            }
            for record in records
        ],
    }


def verify_records(args, schema, system, items_by_id, records, run_dir):
    """Review the batch on the thinking model, and redo what it flags.

    Bulk work runs without reasoning because it is usually right. This is what
    makes "usually" safe: full coverage for a handful of batched calls, with the
    reasoning budget spent on the items that turn out to need it.
    """
    warnings = []
    summary = {"verified": 0, "ok": 0, "flagged": 0, "escalated": 0, "needsReview": 0, "flaggedIds": []}
    candidates = [record for record in records if record["status"] == "ok"]
    if not candidates:
        return summary, warnings
    think = forge_llm.resolve_think_or_chat(base_url=args.think_url, model=args.think_model)
    if not think["enabled"]:
        warnings.append("verification skipped: no thinking service is configured")
        summary["skipped"] = "disabled"
        return summary, warnings

    by_id = {record["id"]: record for record in candidates}
    note_items = [verify_payload(record, items_by_id[record["source_id"]]) for record in candidates]
    grouped = {}
    for record in candidates:
        grouped.setdefault(record["source_id"], []).append(record)
    coverage_items = [coverage_payload(items_by_id[source_id], rows) for source_id, rows in grouped.items()]

    log(args, f"verifying {len(note_items)} notes and {len(coverage_items)} braindumps on {think['url']}")
    try:
        verdicts = forge_verify.verify_packets(
            think,
            VERIFY_NOTES_SYSTEM,
            note_items,
            journal_path=run_dir / "verified.jsonl",
            background=True,
            timeout=args.request_timeout,
            progress=progress,
        )
        coverage_verdicts = forge_verify.verify_packets(
            think,
            VERIFY_COVERAGE_SYSTEM,
            coverage_items,
            journal_path=run_dir / "verified-coverage.jsonl",
            background=True,
            timeout=args.request_timeout,
            progress=progress,
            # Coverage asks whether these notes account for the braindump, which
            # is a review of the split — and the split is routed to the thinking
            # service, the same one reviewing here. Naming the producer lets a
            # clean verdict be recorded as non-independent rather than read as a
            # second opinion it is not.
            produced_by=forge_routing.service_for("split-braindump", args),
        )
    except forge_verify.VerificationError as error:
        # An unreachable reviewer must not read as approval.
        warnings.append(f"verification skipped: {error}")
        summary["skipped"] = str(error)
        return summary, warnings

    # A clean verdict from the model that produced the item is not evidence, and
    # the report must not let it read as one.
    coverage_independence = forge_verify.independence_warning(coverage_verdicts)
    if coverage_independence:
        warnings.append(f"coverage check: {coverage_independence}")
        summary["notIndependentlyVerified"] = sum(
            1 for verdict in coverage_verdicts.values() if not verdict.get("independent", True)
        )

    flagged = [
        (next(entry for entry in note_items if entry["id"] == identifier), verdict["reason"])
        for identifier, verdict in verdicts.items()
        if verdict["verdict"] == forge_verify.VERDICT_FLAG and identifier in by_id
    ]

    def redo(payload, reason):
        record = by_id[payload["id"]]
        item = items_by_id[record["source_id"]]
        value = call_json(
            args,
            think,
            system,
            draft_payload(
                item,
                {"kind": record["kind"], "title": record["title"], "gist": record["gist"], "covers": []},
                type_style=record.get("type_style"),
                connection_candidates=record.get("connection_candidates"),
                outside_sources=record.get("outside_sources"),
            ),
            "redraft-note",
            background=True,
            extra={"reviewerObjection": reason, "previousTitle": record["title"]},
        )
        title, body = validate_draft(value)
        return {"title": title, "body": body}

    escalations = forge_verify.escalate(flagged, redo, journal_path=run_dir / "escalated.jsonl", progress=progress)
    for identifier, outcome in escalations.items():
        if outcome.get("resumed"):
            continue  # committed when it was first escalated
        record = by_id[identifier]
        record["verify_reason"] = next(reason for entry, reason in flagged if entry["id"] == identifier)
        if outcome["ok"]:
            item = items_by_id[record["source_id"]]
            entry = outcome["value"]
            rechecked, notices = check_draft(
                {
                    "item": item,
                    "body": entry["body"],
                    "note": {"kind": record["kind"]},
                    "connection_candidates": record.get("connection_candidates"),
                    "outside_sources": record.get("outside_sources"),
                }
            )
            record["notices"] = notices
            if rechecked:
                record["status"] = "review"
                record["held_reason"] = "; ".join(rechecked)
                record["destination"] = None
                record["verified"] = "needs-review"
                warnings.append(f"{record['title']}: re-drafted note still fails a check: {record['held_reason']}")
                continue
            record["title"] = entry["title"]
            record["text"] = build_note_text(
                schema, record["metadata"], record["kind"], entry["body"], item["text"] if record["is_primary"] else None
            )
            record["verified"] = "escalated"
        else:
            record["status"] = "review"
            record["held_reason"] = f"verification flagged this and re-drafting failed: {outcome['detail']}"
            record["destination"] = None
            record["verified"] = "needs-review"
            warnings.append(f"{record['title']}: {record['held_reason']}")
    for identifier, verdict in verdicts.items():
        if verdict["verdict"] == forge_verify.VERDICT_OK and identifier in by_id:
            by_id[identifier]["verified"] = "ok"
    for source_id, verdict in coverage_verdicts.items():
        if verdict["verdict"] != forge_verify.VERDICT_FLAG:
            continue
        label = items_by_id[source_id]["label"] if source_id in items_by_id else source_id
        warnings.append(f"{label}: the reviewer flagged how this braindump was divided: {verdict['reason']}")
        for record in grouped.get(source_id, []):
            record.setdefault("notices", []).append(f"split flagged: {verdict['reason']}")
    summary = forge_verify.summarize(verdicts, escalations)
    summary["coverageFlagged"] = [
        identifier for identifier, verdict in coverage_verdicts.items() if verdict["verdict"] == forge_verify.VERDICT_FLAG
    ]
    return summary, warnings


# --------------------------------------------------------------------------- #
# Write
# --------------------------------------------------------------------------- #


def write_notes(vault, records, run_dir):
    """Create the notes. New files only: nothing here can touch existing ones.

    A note already written by this run is recognized by its hash and skipped, so
    a resumed run is a no-op rather than a second copy.
    """
    journal = run_dir / "created.jsonl"
    _done, warnings = run_state.read_jsonl_recover_tail(journal, repair=True)
    prior = already_created(run_dir)
    created = 0
    for record in records:
        if record["status"] != "ok" or not record.get("destination"):
            continue
        destination = vault / record["destination"]
        payload = record["text"].encode("utf-8")
        digest = sha256_bytes(payload)
        previous = prior.get(record["id"])
        if previous:
            # Already written by this run. Accept it only when what is on disk
            # is what was written; anything else is the user's edit, and this
            # skill does not touch notes it did not just create.
            if destination.is_file() and sha256_bytes(destination.read_bytes()) == previous.get("sha256"):
                record["status"] = "created"
                continue
            record["status"] = "review"
            record["held_reason"] = (
                f"{record['destination']} was written by this run and has since changed or been moved; "
                "it was left alone"
            )
            warnings.append(f"{record['title']}: {record['held_reason']}")
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(destination, "xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            record["status"] = "review"
            record["held_reason"] = f"a note already exists at {record['destination']}"
            warnings.append(f"{record['title']}: {record['held_reason']}")
            run_state.append_jsonl_fsync(
                journal, {"at": run_state.utc_now(), "id": record["id"], "destination": record["destination"], "status": "collision"}
            )
            continue
        record["status"] = "created"
        created += 1
        run_state.append_jsonl_fsync(
            journal,
            {
                "at": run_state.utc_now(),
                "id": record["id"],
                "destination": record["destination"],
                "sha256": digest,
                "source": record["source"],
                "status": "ok",
            },
        )
    return created, warnings


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def verification_report(verification):
    if not verification:
        return ["## Verification", "", "Verification did not run.", ""]
    if verification.get("skipped"):
        return [
            "## Verification",
            "",
            f"Nothing was reviewed: {verification['skipped']}.",
            "These notes carry no thinking-model review. That is not the same as approval.",
            "",
        ]
    lines = [
        "## Verification",
        "",
        f"- Reviewed by the thinking model: {verification.get('verified', 0)}",
        f"- Accepted: {verification.get('ok', 0)}",
        f"- Flagged: {verification.get('flagged', 0)}",
        f"- Re-drafted with reasoning: {verification.get('escalated', 0)}",
        f"- Left for you: {verification.get('needsReview', 0)}",
    ]
    if verification.get("coverageFlagged"):
        lines.append(f"- Braindumps whose split was flagged: {len(verification['coverageFlagged'])}")
    lines.append("")
    return lines


def write_report(run_dir, items, records, counts, dry_run, vault, options, warnings, verification):
    lines = [
        "# Vault capture",
        "",
        f"- Vault: `{vault}`",
        f"- Run: `{run_dir}`",
        f"- Dry run: `{str(dry_run).lower()}`",
        f"- Braindumps: {counts['braindumps']}",
        f"- Notes written: {counts['created']}",
        f"- Held for you: {counts['review']}",
        f"- Voice note: {options.get('voice_note') or 'none'}",
        "",
        "## Notes",
        "",
    ]
    if not records:
        lines.append("No notes were drafted.")
        lines.append("")
    for record in records:
        marker = {"created": "written", "ok": "ready", "review": "held"}.get(record["status"], record["status"])
        destination = record.get("destination") or "—"
        lines.append(f"### {record['title']} ({record['kind']}, {marker})")
        lines.append("")
        lines.append(f"- Destination: `{destination}`")
        lines.append(f"- From: `{record['source']}`")
        if record["is_primary"]:
            lines.append("- Carries the braindump verbatim")
        lines.append(f"- Verification: {record.get('verified', 'not-verified')}")
        if record.get("held_reason"):
            lines.append(f"- Held: {record['held_reason']}")
        for notice in record.get("notices", []):
            lines.append(f"- Notice: {notice}")
        lines.append("")
        lines.append(f"> {record['gist']}")
        lines.append("")
    held_inputs = [item for item in items if item["held"]]
    if held_inputs:
        lines.extend(["## Braindumps not processed", ""])
        for item in held_inputs:
            lines.append(f"- `{item['label']}` — {item['held']}")
        lines.append("")
    lines.extend(verification_report(verification))
    if warnings:
        lines.extend(["## Warnings", ""])
        lines.extend(f"- {warning}" for warning in warnings)
        lines.append("")
    lines.extend(["## Options", "", "```json", json.dumps(options, indent=2, ensure_ascii=False), "```", ""])
    path = run_dir / "report.md"
    run_state.atomic_write_text(path, "\n".join(lines))
    return path


# --------------------------------------------------------------------------- #
# Preferences
# --------------------------------------------------------------------------- #

PREFS_EDIT_SYSTEM = """You turn one piece of feedback about writing style into proposed edits to a person's voice-and-style note.

That note is what tells the note-writing pipeline how this person's notes should read. You are proposing changes to it, not applying them: a human reads every proposal and accepts or rejects each one.

The note has exactly five sections, and every edit belongs to one of them:

- "global" — how they write, in general: person, tense, tone, hedging.
- "per_type" — one rule for one kind of note. Needs a `type` as well as the text.
- "vocabulary" — a word they use or avoid, written as "term — how they use it".
- "formatting" — headings, bullets, punctuation. Mechanical rules only.
- "never" — things a note must never do. Use this only for real prohibitions.

Return exactly one JSON object:

{"edits": [{"section": "global|per_type|vocabulary|formatting|never", "scope": "universal|owner-authored|source-derived", "operation": "add|amend|remove", "type": "note type, only for per_type", "text": "The rule, written as an instruction.", "replaces": "the existing bullet this amends or removes, exactly", "reason": "why this feedback implies this rule"}], "needs_review": false, "review_reason": null}

Rules for what you propose:

- Write a rule the pipeline can follow. "Be less formal" is not actionable; "Keep contractions" is.
- One rule per edit. Do not combine two pieces of guidance into one bullet.
- Scope owner voice and fidelity rules as `owner-authored`; source description and critique rules as `source-derived`;
  use `universal` only when the rule genuinely applies to both.
- Propose only what the feedback supports. Inventing rules the person did not ask for is how a style note becomes something they no longer recognize.
- `amend` and `remove` must quote the existing bullet exactly in `replaces`.
- An empty `edits` list is a legitimate answer when the feedback is about one note rather than about how notes should read in general. Say why in `review_reason`."""

SECTION_KEYS = {
    "global": "global",
    "per_type": "per_type",
    "vocabulary": "vocabulary",
    "formatting": "formatting",
    "never": "never",
}


def validate_edits(value, voice):
    """The model proposes; this decides what is even representable."""
    if not isinstance(value, dict) or not isinstance(value.get("edits"), list):
        raise UserError("preferences response has no edits list")
    edits = []
    for position, entry in enumerate(value["edits"], start=1):
        if not isinstance(entry, dict):
            raise UserError(f"edit {position} is not an object")
        section = entry.get("section")
        if section not in SECTION_KEYS:
            raise UserError(f"edit {position} names an unknown section {section!r}")
        operation = entry.get("operation")
        if operation not in ("add", "amend", "remove"):
            raise UserError(f"edit {position} has an unknown operation {operation!r}")
        text = entry.get("text")
        if operation != "remove" and (not isinstance(text, str) or not text.strip()):
            raise UserError(f"edit {position} has no text")
        replaces = entry.get("replaces")
        if operation in ("amend", "remove"):
            if not isinstance(replaces, str) or not replaces.strip():
                raise UserError(f"edit {position} is an {operation} without naming what it replaces")
            existing = (
                list(voice.get("per_type", {}).values()) if section == "per_type" else voice.get(section, [])
            )
            if replaces.strip() not in [str(item).strip() for item in existing]:
                raise UserError(f"edit {position} names a bullet that is not in the {section} section: {replaces!r}")
        note_type = entry.get("type")
        if section == "per_type" and operation != "remove" and (not isinstance(note_type, str) or not note_type.strip()):
            raise UserError(f"edit {position} is a per-type rule without a type")
        scope = entry.get("scope") or "universal"
        if scope not in vault_voice.KNOWN_SCOPES:
            raise UserError(f"edit {position} names an unknown scope {scope!r}")
        edits.append(
            {
                "id": f"p-{position:03d}",
                "section": section,
                "operation": operation,
                "text": (text or "").strip(),
                "replaces": (replaces or "").strip(),
                "type": (note_type or "").strip(),
                "scope": scope,
                "reason": entry.get("reason") if isinstance(entry.get("reason"), str) else "",
            }
        )
    return edits


def apply_edits(voice, edits):
    """Apply accepted edits to a parsed voice note, in order."""
    updated = {
        "global": list(voice.get("global", [])),
        "per_type": dict(voice.get("per_type", {})),
        "vocabulary": list(voice.get("vocabulary", [])),
        "formatting": list(voice.get("formatting", [])),
        "never": list(voice.get("never", [])),
        "scope_map": {
            "global": list(voice.get("scope_map", {}).get("global", [])),
            "per_type": dict(voice.get("scope_map", {}).get("per_type", {})),
            "vocabulary": list(voice.get("scope_map", {}).get("vocabulary", [])),
            "formatting": list(voice.get("scope_map", {}).get("formatting", [])),
            "never": list(voice.get("scope_map", {}).get("never", [])),
        },
        "recognized_scopes": list(voice.get("recognized_scopes", [])),
        "unknown_scopes": list(voice.get("unknown_scopes", [])),
    }
    for edit in edits:
        section = edit["section"]
        if section == "per_type":
            if edit["operation"] == "remove":
                for note_type, style in list(updated["per_type"].items()):
                    if style.strip() == edit["replaces"]:
                        del updated["per_type"][note_type]
                        updated["scope_map"]["per_type"].pop(note_type, None)
            else:
                updated["per_type"][edit["type"]] = edit["text"]
                updated["scope_map"]["per_type"][edit["type"]] = edit["scope"]
            continue
        bullets = updated[section]
        scopes = updated["scope_map"][section]
        if not scopes:
            scopes.extend(["universal"] * len(bullets))
        if edit["operation"] == "add":
            if edit["text"] not in bullets:
                bullets.append(edit["text"])
                scopes.append(edit["scope"])
        elif edit["operation"] == "amend":
            for index, bullet in enumerate(bullets):
                if bullet.strip() == edit["replaces"]:
                    bullets[index] = edit["text"]
                    scopes[index] = edit["scope"]
        else:
            kept = [(bullet, scope) for bullet, scope in zip(bullets, scopes) if bullet.strip() != edit["replaces"]]
            updated[section] = [bullet for bullet, _scope in kept]
            updated["scope_map"][section] = [scope for _bullet, scope in kept]
    return updated


def describe_edit(edit):
    where = f"{edit['section']}/{edit['type']}" if edit["section"] == "per_type" and edit["type"] else edit["section"]
    if edit["operation"] == "remove":
        return f"remove from {where}: {edit['replaces']}"
    if edit["operation"] == "amend":
        return f"amend {where}: {edit['replaces']} -> {edit['text']}"
    return f"add to {where}: {edit['text']}"


def preferences(args):
    """Propose voice-note edits from feedback, or apply the ones named.

    This edits a note the user wrote, so it follows the rule every skill that
    touches existing notes follows: propose, show the diff, and change nothing
    without being told which proposals to take.
    """
    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        raise UserError(f"vault root does not exist: {vault}")
    voice_path = vault_voice.resolve_voice_path(vault, args.voice, disabled=args.no_voice)
    if voice_path is None:
        raise UserError(
            f"this vault has no voice note. Create {vault / vault_voice.DEFAULT_VOICE} with at least one of the "
            "sections '## Global voice', '## Per-type style', '## Vocabulary', '## Formatting', '## Never do', "
            "then run this again. This skill does not create it: it is your note, and it should say what you meant."
        )
    current_text = voice_path.read_text(encoding="utf-8")
    voice = vault_voice.parse_voice_note(current_text)
    current_hash = sha256_text(current_text)

    if args.accept or args.reject:
        return apply_preferences(args, vault, voice_path, voice, current_hash)

    if not args.feedback:
        raise UserError("preferences requires --feedback \"<what you want changed>\", or --accept/--reject with --run")
    payload = {"feedback": args.feedback, "currentVoiceNote": voice}
    if args.from_run:
        report = Path(args.from_run).expanduser().resolve() / "report.md"
        if report.is_file():
            payload["recentRun"] = report.read_text(encoding="utf-8")[:PREFS_RUN_CONTEXT_CHARS]
    value = call_json(args, chat_service(args), PREFS_EDIT_SYSTEM, payload, "propose-preferences")
    edits = validate_edits(value, voice)
    # Prove the result is still a readable voice note before offering it. A
    # proposal that would break the note is a bug, not a decision to hand over.
    proposed = apply_edits(voice, edits)
    rendered = vault_voice.render_voice_note(proposed, original_text=current_text)
    try:
        vault_voice.parse_voice_note(rendered)
    except UserError as error:
        raise UserError(f"the proposed voice note would not parse, so nothing was proposed: {error}") from error

    run_dir = unique_run_directory(vault)
    run_state.initialize_run_state(
        run_dir,
        run_state.create_run_state(
            WORKFLOW,
            "preferences",
            {"vault": str(vault), "voice_note": str(voice_path), "voice_hash": current_hash},
            {"feedback": args.feedback, "prompt_version": PROMPT_VERSION},
            phase="proposed",
        ),
    )
    run_state.atomic_write_json(
        run_dir / "proposals.json",
        {"voice_note": str(voice_path), "voice_hash": current_hash, "edits": edits},
    )
    lines = [
        "# Voice and style proposals",
        "",
        f"- Voice note: `{voice_path}`",
        f"- Feedback: {args.feedback}",
        "",
        "## Proposed edits",
        "",
    ]
    if not edits:
        lines.append("Nothing to change: this feedback is about one note rather than about how notes should read.")
        lines.append("")
    for edit in edits:
        lines.append(f"### {edit['id']}")
        lines.append("")
        lines.append(f"- {describe_edit(edit)}")
        if edit["reason"]:
            lines.append(f"- Why: {edit['reason']}")
        lines.append("")
    lines.extend(["## The note as it would read", "", "```markdown", rendered.strip(), "```", ""])
    report_path = run_dir / "report.md"
    run_state.atomic_write_text(report_path, "\n".join(lines))
    return structured(
        "ok",
        artifacts=[str(report_path)],
        warnings=[] if edits else ["no edits were proposed"],
        data={
            "run_directory": str(run_dir),
            "voice_note": str(voice_path),
            "edits": edits,
            "next_action": f"accept with: preferences --vault {vault} --run {run_dir} --accept <ids>",
        },
    )


def apply_preferences(args, vault, voice_path, voice, current_hash):
    if not args.run:
        raise UserError("--accept and --reject need --run <run-directory>")
    run_dir = Path(args.run).expanduser().resolve()
    proposals = json.loads((run_dir / "proposals.json").read_text(encoding="utf-8"))
    if proposals["voice_hash"] != current_hash:
        raise UserError(
            "the voice note changed since these edits were proposed; nothing was applied. "
            "Run preferences again to propose against the note as it reads now."
        )
    accepted_ids = [value.strip() for value in (args.accept or "").split(",") if value.strip()]
    rejected_ids = [value.strip() for value in (args.reject or "").split(",") if value.strip()]
    known = {edit["id"] for edit in proposals["edits"]}
    unknown = sorted((set(accepted_ids) | set(rejected_ids)) - known)
    if unknown:
        raise UserError(f"unknown proposal ids: {', '.join(unknown)}")
    accepted = [edit for edit in proposals["edits"] if edit["id"] in accepted_ids]
    if not accepted:
        run_state.append_jsonl_fsync(
            run_dir / "decisions.jsonl",
            {"at": run_state.utc_now(), "accepted": [], "rejected": rejected_ids},
        )
        return structured(
            "ok",
            warnings=["nothing was accepted, so the voice note is unchanged"],
            data={"voice_note": str(voice_path), "accepted": [], "rejected": rejected_ids},
        )
    updated = apply_edits(voice, accepted)
    original_text = voice_path.read_text(encoding="utf-8")
    rendered = vault_voice.render_voice_note(updated, original_text=original_text)
    vault_voice.parse_voice_note(rendered)  # raises rather than writing an unreadable note
    backup = run_dir / "backup" / voice_path.name
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(voice_path.read_text(encoding="utf-8"), encoding="utf-8")
    run_state.atomic_write_text(voice_path, rendered)
    run_state.append_jsonl_fsync(
        run_dir / "decisions.jsonl",
        {
            "at": run_state.utc_now(),
            "accepted": accepted_ids,
            "rejected": rejected_ids,
            "previous_hash": current_hash,
            "new_hash": sha256_text(rendered),
            "backup": str(backup),
        },
    )
    return structured(
        "ok",
        artifacts=[str(voice_path)],
        data={
            "voice_note": str(voice_path),
            "accepted": accepted_ids,
            "rejected": rejected_ids,
            "backup": str(backup),
            "applied": [describe_edit(edit) for edit in accepted],
        },
    )


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #


def chat_service(args):
    return forge_llm.service_from_args(args, "chat")


def resolved_options(args):
    return {
        "model": args.model,
        "base_url": args.base_url,
        "filename_pattern": args.filename_pattern,
        "max_notes": args.max_notes,
        "prompt_version": PROMPT_VERSION,
        "cache_prompt": args.cache_prompt,
        "schema": args.schema,
        "voice": args.voice,
        "no_voice": args.no_voice,
        "exemplars": args.exemplars,
    }


RESUMABLE_OPTION_FLAGS = {
    "model": "--model",
    "base_url": "--base-url",
    "filename_pattern": "--filename-pattern",
    "max_notes": "--max-notes",
    "schema": "--schema",
    "voice": "--voice",
    "no_voice": "--no-voice",
    "profile": "--profile",
    "no_profile": "--no-profile",
}


def adopt_stored_options(args, state):
    """Resuming keeps the original run's options: changing how notes are named
    or divided halfway through produces a batch that disagrees with itself."""
    stored = state.get("options", {})
    for key, flag in RESUMABLE_OPTION_FLAGS.items():
        if getattr(args, f"{key}_provided", False) and getattr(args, key) != stored.get(key):
            raise UserError(
                f"{flag} differs from the original run ({getattr(args, key)!r} vs {stored.get(key)!r}); "
                "start a new run instead of --run"
            )
        if key in stored:
            setattr(args, key, stored[key])
    args.cache_prompt = stored.get("cache_prompt", args.cache_prompt)


def phase(run_dir, name, event=None):
    run_state.update_run_state(run_dir, lambda draft: draft.update({"phase": name}) or draft, event=event)


def capture(args):
    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        raise UserError(f"vault root does not exist: {vault}")
    stdin_text = sys.stdin.read() if args.stdin else None
    resuming = bool(args.run)
    state = None
    if resuming:
        run_dir = Path(args.run).expanduser().resolve()
        state = run_state.load_run_state(run_dir, workflow=WORKFLOW)
        adopt_stored_options(args, state)
    schema_path = resolve_schema_path(vault, args.schema)
    schema, schema_hash = compiled_schema_for(vault, schema_path, cache_dir=vault / STATE_DIR / "cache")
    voice_path = vault_voice.resolve_voice_path(vault, args.voice, disabled=args.no_voice)
    voice, voice_hash = vault_voice.compiled_voice_for(vault, voice_path, cache_dir=vault / STATE_DIR / "cache")
    lexicon, _lexicon_hash = vault_lexicon.load_lexicon(
        vault,
        vault_lexicon.resolve_lexicon_path(vault),
        schema=schema,
        cache_dir=vault / STATE_DIR / "cache",
        dictionary_path=vault_lexicon.default_dictionary_path(),
    )
    profile_path, profile_warnings = vault_profile.resolve_profile_or_warn(vault, args.profile, disabled=args.no_profile)
    profile, profile_hash, compile_warnings = vault_profile.compiled_profile_for(
        vault, profile_path, cache_dir=vault / STATE_DIR / "cache"
    )
    args.compiled_profile = profile
    warnings = profile_warnings + compile_warnings

    with run_state.run_lock(vault / STATE_DIR):
        if resuming:
            items = json.loads((run_dir / "scan.json").read_text(encoding="utf-8"))["items"]
        else:
            items = scan_inputs(args.inputs, stdin_text, args.force)
        configuration = {
            "workflow": WORKFLOW,
            "command": "capture",
            "input": {
                "vault": str(vault),
                "schema_hash": schema_hash,
                # A changed voice note means a resumed run would draft its
                # remaining notes to different rules than the ones it started
                # with, so it is part of what makes a run compatible.
                **vault_voice.voice_state(voice_path, voice_hash, vault_voice.CONTEXT_OWNER),
                **vault_profile.profile_state(profile_path, profile_hash, capture_site()),
                "sources": [{"source": item["source"], "sha256": item["sha256"]} for item in items],
            },
            "options": resolved_options(args),
        }
        if resuming:
            try:
                run_state.assert_compatible_run(state, configuration)
            except ValueError as error:
                raise UserError(str(error)) from error
        else:
            run_dir = unique_run_directory(vault)
            run_state.initialize_run_state(
                run_dir,
                run_state.create_run_state(
                    WORKFLOW, "capture", configuration["input"], configuration["options"], phase="scan"
                ),
            )
            run_state.atomic_write_json(run_dir / "scan.json", {"items": items})

        items_by_id = {item["id"]: item for item in items}
        for item in items:
            if item["held"]:
                warnings.append(f"{item['label']}: {item['held']}")

        service = chat_service(args)
        # Splitting is routed off the bulk service, so a dead target has to be
        # found here rather than mid-run: one probe, and the run continues on
        # `chat` with the substitution stated instead of dying at the first note.
        warnings.extend(
            forge_routing.disable_unreachable(
                args, ["split-braindump"], timeout=min(args.request_timeout, 60)
            )
        )
        phase(run_dir, "split")
        results, split_warnings = split_items(args, service, items, run_dir)
        warnings.extend(split_warnings)

        planned = []
        for item, notes in results:
            for position, note in enumerate(notes, start=1):
                planned.append(
                    {
                        "id": f"{item['id']}-{position:02d}",
                        "item": item,
                        "note": note,
                        "siblings": [
                            {"title": other["title"], "gist": other["gist"]}
                            for index, other in enumerate(notes)
                            if index != position - 1
                        ],
                    }
                )
        phase(run_dir, "draft", event={"type": "phase", "phase": "draft", "counts": {"notes": len(planned)}})
        seen_warnings = set()
        for entry in planned:
            note_type = note_type_for(entry["note"]["kind"], schema)
            compiled = vault_voice.compile_voice(
                voice,
                vault_voice.CONTEXT_OWNER,
                note_type=note_type,
                material=f"{entry['note']['gist']}\n{entry['item']['text']}",
            )
            entry["type_style"] = compiled["per_type_rule"]
            entry["relevant_vocabulary"] = compiled["vocabulary"]
            material = f"{entry['note']['gist']}\n{entry['item']['text']}"
            entry["glossary"] = vault_lexicon.near_miss_terms(material, vault_lexicon.term_candidates(lexicon))
            entry["known_people"] = [
                {"name": person["name"], "wikilink": person["link"] or f"[[{person['name']}]]"}
                for person in vault_lexicon.candidate_speakers(material, (lexicon or {}).get("speakers", []))
            ]
            if KIND_TO_REFLECTION.get(entry["note"]["kind"]):
                candidates, warning = collect_connection_candidates(vault, entry["note"]["gist"])
                entry["connection_candidates"] = candidates
                entry["outside_sources"] = collect_outside_sources(vault, entry["item"]["text"], candidates)
                if warning and warning not in seen_warnings:
                    seen_warnings.add(warning)
                    warnings.append(warning)
            if not args.exemplars:
                continue
            exemplars, warning = collect_exemplars(vault, entry["note"]["gist"], note_type=note_type)
            entry["exemplars"] = exemplars
            if warning and warning not in seen_warnings:
                seen_warnings.add(warning)
                warnings.append(warning)
        if args.exemplars and planned:
            # Journal which of the user's own notes each draft was shown, so
            # they can see what the pipeline read on their behalf.
            run_state.atomic_write_json(
                run_dir / "exemplars.json",
                {entry["id"]: [row["note"] for row in entry.get("exemplars") or []] for entry in planned},
            )
        # One voice segment per run, not per note: the per-type row would change
        # the system prompt between calls and throw away the prefix cache. Type
        # guidance rides in the user message with everything else that varies.
        system = draft_system_prompt(
            vault_voice.prompt_prefix(voice, vault_voice.CONTEXT_OWNER),
            getattr(args, "compiled_profile", None),
        )
        warnings.extend(draft_items(args, service, system, planned, run_dir))

        grouped = {}
        for entry in planned:
            grouped.setdefault(entry["item"]["id"], []).append(entry)
        phase(run_dir, "assemble")
        records, assemble_warnings = assemble(
            args, vault, schema, [(items_by_id[key], entries) for key, entries in grouped.items()], run_dir
        )
        warnings.extend(assemble_warnings)

        verification = None
        if args.verify:
            phase(run_dir, "verify")
            verification, verify_warnings = verify_records(args, schema, system, items_by_id, records, run_dir)
            warnings.extend(verify_warnings)
        else:
            warnings.append("verification was skipped with --no-verify; nothing was reviewed")

        created = 0
        if not args.dry_run:
            phase(run_dir, "write")
            created, write_warnings = write_notes(vault, records, run_dir)
            warnings.extend(write_warnings)

        counts = {
            "braindumps": len(items),
            "braindumps_held": sum(1 for item in items if item["held"]),
            "notes": len(records),
            "created": created,
            "ready": sum(1 for record in records if record["status"] == "ok"),
            "review": sum(1 for record in records if record["status"] == "review"),
        }
        report_path = write_report(
            run_dir,
            items,
            records,
            counts,
            args.dry_run,
            vault,
            {
                **resolved_options(args),
                **vault_voice.voice_state(voice_path, voice_hash, vault_voice.CONTEXT_OWNER),
                "voice_applied": bool(voice),
                "voice_reason": "vault-capture input is owner-authored braindump material",
            },
            warnings,
            verification,
        )
        run_state.atomic_write_json(
            run_dir / "plan.json",
            {"records": [{key: value for key, value in row.items() if key != "text"} for row in records]},
        )
        final_phase = "complete" if not args.dry_run else "planned"
        run_state.update_run_state(
            run_dir,
            lambda draft: draft.update(
                {
                    "phase": final_phase,
                    "status": "complete" if final_phase == "complete" else "running",
                    "nextAction": None
                    if final_phase == "complete"
                    else f"review {report_path.name}, then rerun without --dry-run using --run {run_dir}",
                }
            )
            or draft,
            event={"type": "phase", "phase": final_phase, "counts": counts},
        )
    return structured(
        "ok",
        artifacts=[str(report_path), str(run_dir / "plan.json")],
        warnings=warnings,
        data={
            "dry_run": args.dry_run,
            "vault": str(vault),
            "run_directory": str(run_dir),
            "options": resolved_options(args),
            "counts": counts,
            "verification": verification,
            "notes": [
                {
                    "title": record["title"],
                    "kind": record["kind"],
                    "destination": record.get("destination"),
                    "status": record["status"],
                    "held_reason": record.get("held_reason"),
                }
                for record in records
            ],
        },
    )


def status(args):
    run_dir = Path(args.run).expanduser().resolve()
    state = run_state.load_run_state(run_dir, workflow=WORKFLOW)
    split, _ = run_state.read_jsonl_recover_tail(run_dir / "split.jsonl", repair=False)
    drafted, _ = run_state.read_jsonl_recover_tail(run_dir / "drafted.jsonl", repair=False)
    created, _ = run_state.read_jsonl_recover_tail(run_dir / "created.jsonl", repair=False)
    return structured(
        "ok",
        data={
            "run_directory": str(run_dir),
            "phase": state.get("phase"),
            "status": state.get("status"),
            "braindumps_split": len(split),
            "notes_drafted": sum(1 for row in drafted if row.get("status") == "ok"),
            "notes_created": sum(1 for row in created if row.get("status") == "ok"),
            "next_action": state.get("nextAction"),
        },
    )


def doctor(args):
    vault = Path(args.vault).expanduser().resolve()
    checks = {}
    warnings = []
    ok = True
    if vault.is_dir() and os.access(vault, os.W_OK):
        checks["vault"] = {"ok": True, "path": str(vault)}
    else:
        checks["vault"] = {"ok": False, "path": str(vault), "detail": "vault root missing or not writable"}
        ok = False
    inbox = vault / INBOX_DIR
    checks["inbox"] = {"ok": inbox.is_dir() and os.access(inbox, os.W_OK), "path": str(inbox)}
    ok = ok and checks["inbox"]["ok"]
    schema = {}
    schema_check = {"ok": False}
    if checks["vault"]["ok"]:
        try:
            schema_path = resolve_schema_path(vault, args.schema)
            schema, schema_hash = compiled_schema_for(vault, schema_path, cache_dir=vault / STATE_DIR / "cache")
            for kind in CAPTURE_KINDS:
                frontmatter_metadata(schema, kind)
            schema_check = {"ok": True, "path": str(schema_path), "schema_hash": schema_hash}
        except UserError as error:
            schema_check = {"ok": False, "detail": str(error)}
    checks["schema"] = schema_check
    ok = ok and schema_check["ok"]
    # A vault with no voice note is healthy: notes are drafted the way this
    # skill would have drafted them anyway. Only a note that cannot be read is
    # a problem, because that is a rule the user wrote and nothing is applying.
    voice_check = {
        "ok": True,
        "configured": False,
        "stages": {"draft": "owner", "split": "none", "verification": "none"},
    }
    if checks["vault"]["ok"]:
        try:
            voice_path = vault_voice.resolve_voice_path(vault, args.voice, disabled=args.no_voice)
            if voice_path is None:
                voice_check["detail"] = f"no voice note; the default path is {vault_voice.DEFAULT_VOICE}"
            else:
                voice, _voice_hash = vault_voice.compiled_voice_for(
                    vault, voice_path, cache_dir=vault / STATE_DIR / "cache"
                )
                segment = vault_voice.prompt_prefix(voice, vault_voice.CONTEXT_OWNER)
                unknown_types = sorted(set(voice.get("per_type", {})) - set(schema.get("types", {})))
                voice_check = {
                    "ok": True,
                    "configured": True,
                    "path": str(voice_path),
                    "prompt_characters": len(segment),
                    "compiler_version": vault_voice.COMPILED_VOICE_VERSION,
                    "recognized_scopes": voice.get("recognized_scopes", []),
                    "types_with_style": sorted(voice.get("per_type", {})),
                    "unknown_scopes": voice.get("unknown_scopes", []),
                    "unknown_schema_types": unknown_types,
                    "stages": {"draft": "owner", "split": "none", "verification": "none"},
                }
                if unknown_types:
                    warnings.append("voice note has unknown schema note types: " + ", ".join(unknown_types))
        except UserError as error:
            voice_check = {"ok": False, "configured": True, "detail": str(error)}
            warnings.append(f"voice note could not be read: {error}")
    checks["voice"] = voice_check
    ok = ok and voice_check["ok"]

    # Never fatal, and never in the draft payload: see draft_system_prompt.
    try:
        profile_path = vault_profile.resolve_profile_path(vault, args.profile, disabled=args.no_profile)
        profile, profile_hash, profile_warnings = vault_profile.compiled_profile_for(
            vault, profile_path, cache_dir=vault / STATE_DIR / "cache"
        )
        checks["profile"] = {
            "ok": True,
            "configured": profile is not None,
            "path": str(profile_path) if profile_path else None,
            "profile_hash": profile_hash,
            "compiler_version": vault_profile.COMPILED_PROFILE_VERSION,
            "cards": vault_profile.profile_digest(profile),
            "stages": {"draft system prompt": "owner", "draft payload": "never — fidelity gate"},
        }
        warnings.extend(profile_warnings)
    except UserError as error:
        checks["profile"] = {"ok": True, "configured": True, "detail": str(error)}
        warnings.append(f"personal context note could not be read: {error}")

    checks["exemplars"] = {
        "ok": True,
        "enabled": args.exemplars,
        "search_available": connections_script().is_file(),
    }
    if args.exemplars and not checks["exemplars"]["search_available"]:
        warnings.append("vault-connections is not installed alongside this skill; drafts get no style examples")
    # Splitting and drafting are one call each per unit, so a backend that
    # reasons first costs hundreds of hidden tokens on every one of them.
    chat_probe = forge_llm.service_doctor(
        chat_service(args), expect_non_thinking=True, timeout=min(args.request_timeout, 60)
    )
    checks["chat"] = {
        "ok": chat_probe["reachable"],
        "url": chat_probe["url"],
        "model": chat_probe["model"],
        "detail": chat_probe.get("detail"),
    }
    for key in ("thinking", "hiddenTokens", "modelMismatch", "servedModels"):
        if key in chat_probe:
            checks["chat"][key] = chat_probe[key]
    ok = ok and chat_probe["reachable"]
    if chat_probe.get("warning"):
        warnings.append(chat_probe["warning"])
    think = forge_llm.resolve_think_or_chat(base_url=args.think_url, model=args.think_model)
    think_probe = forge_llm.service_doctor(think, timeout=min(args.request_timeout, 60))
    checks["think"] = {
        "ok": think_probe["reachable"],
        "url": think_probe["url"],
        "model": think_probe["model"],
        "detail": think_probe.get("detail"),
    }
    if think.get("fallback"):
        checks["think"]["fallback"] = think["fallback"]
        warnings.append("no thinking service is configured; verification would run on the bulk service")
    if not think_probe["reachable"]:
        warnings.append("thinking service is unreachable; runs would report that nothing was verified")
    return structured("ok" if ok else "error", warnings=warnings, data={"checks": checks})


class TrackingAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        setattr(namespace, f"{self.dest}_provided", True)


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Turn a braindump into schema-valid notes in an Obsidian vault inbox.")
    parser.add_argument("mode", choices=["capture", "preferences", "status", "doctor"])
    parser.add_argument("inputs", nargs="*", help="braindump files to capture")
    parser.add_argument("--feedback", help="what you want changed about how notes read (preferences)")
    parser.add_argument("--from-run", help="a capture run this feedback is about (preferences)")
    parser.add_argument("--accept", help="comma-separated proposal ids to apply (preferences)")
    parser.add_argument("--reject", help="comma-separated proposal ids to record as rejected (preferences)")
    parser.add_argument("--vault")
    parser.add_argument("--schema", action=TrackingAction)
    parser.add_argument("--voice", action=TrackingAction, help="voice-and-style note (default: the vault's, when it has one)")
    parser.add_argument("--no-voice", action="store_true", help="disable the vault voice policy for this run")
    parser.add_argument("--profile", action=TrackingAction, help="personal-context register note (default: the vault's, when it has one)")
    parser.add_argument("--no-profile", action="store_true", help="disable personal context for this run")
    parser.add_argument(
        "--no-exemplars",
        action="store_true",
        help="draft without showing the model the user's own notes as style examples",
    )
    parser.add_argument("--stdin", action="store_true", help="read one braindump from standard input")
    parser.add_argument("--run", help="existing run directory to resume")
    parser.add_argument("--dry-run", action="store_true", help="plan and verify without writing notes")
    parser.add_argument("--force", action="store_true", help="synthesize from input that looks like a transcript export")
    parser.add_argument("--max-notes", type=int, action=TrackingAction, help=f"notes per braindump (default {DEFAULT_MAX_NOTES})")
    parser.add_argument("--filename-pattern", choices=FILENAME_PATTERNS, action=TrackingAction)
    parser.add_argument("--base-url", action=TrackingAction)
    parser.add_argument("--model", action=TrackingAction)
    parser.add_argument("--api-key")
    parser.add_argument("--request-timeout", type=float, default=600)
    parser.add_argument("--no-cache-prompt", action="store_true")
    parser.add_argument("--no-verify", action="store_true", help="skip the thinking-model review")
    parser.add_argument("--think-url", help="thinking service used for verification (default: connectedServices.think)")
    parser.add_argument("--think-model")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    for key in RESUMABLE_OPTION_FLAGS:
        if not hasattr(args, f"{key}_provided"):
            setattr(args, f"{key}_provided", False)
    args.filename_pattern = args.filename_pattern or FILENAME_PATTERNS[0]
    if args.max_notes is None:
        args.max_notes = DEFAULT_MAX_NOTES
    if args.max_notes < 1:
        raise UserError("--max-notes must be at least 1")
    if args.mode == "status":
        if not args.run:
            raise UserError("status requires --run <run-directory>")
        return args
    if not args.vault:
        raise UserError(f"{args.mode} requires --vault")
    if args.mode == "capture" and not args.inputs and not args.stdin and not args.run:
        raise UserError("capture requires one or more input files, or --stdin")
    if args.mode == "preferences" and not (args.feedback or args.accept or args.reject):
        raise UserError('preferences requires --feedback "<what you want changed>", or --accept/--reject with --run')
    resolved = forge_llm.resolve_service("chat", base_url=args.base_url, model=args.model)
    args.base_url = resolved["url"]
    args.model = resolved["model"]
    args.api_key = args.api_key or os.environ.get("VAULT_CAPTURE_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    args.schema = args.schema or os.environ.get("VAULT_CAPTURE_SCHEMA") or None
    args.cache_prompt = not args.no_cache_prompt
    args.verify = not args.no_verify
    args.exemplars = not args.no_exemplars
    args.voice = args.voice or os.environ.get("VAULT_CAPTURE_VOICE") or None
    if args.no_voice and args.voice and args.voice_provided:
        raise UserError("--voice and --no-voice cannot be used together")
    return args


def run(argv):
    args = parse_args(argv)
    if args.mode == "status":
        return status(args)
    if args.mode == "doctor":
        return doctor(args)
    if args.mode == "preferences":
        return preferences(args)
    return capture(args)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        result = run(argv)
    except UserError as error:
        print_json(structured("error", errors=[error_entry("user_error", str(error))]))
        return 2
    except ValueError as error:
        print_json(structured("error", errors=[error_entry("run_state_error", str(error))]))
        return 2
    except forge_llm.ChatError as error:
        print_json(structured("error", errors=[error_entry("chat_error", str(error))]))
        return 2
    except KeyboardInterrupt:
        print_json(structured("error", errors=[error_entry("interrupted", "interrupted")]))
        return 130
    print_json(result)
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())

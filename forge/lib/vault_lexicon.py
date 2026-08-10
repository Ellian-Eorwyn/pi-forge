#!/usr/bin/env python3
"""Parse and compile a vault-owned lexicon: specialist terms, and the people who speak.

Two things a transcript pipeline cannot work out on its own. Speech-to-text
renders "Bodhicitta" as "Buddhic chitta", and no amount of surrounding context
recovers the spelling. A diarizer emits "Speaker 2" for a voice the vault owner
would recognize instantly. Both are personal knowledge, so both live in a note
the owner edits, next to the schema and voice policy.

Corrections apply in two tiers. Variants recorded here are replaced in code
before any model sees the text: exact, free, and logged. Canonical spellings
that merely sound close to something in the text are *offered* to the model,
which decides whether the passage really meant them. The second tier is what
catches a mistranscription nobody has written down yet.
"""

import difflib
import json
import os
import re
import unicodedata
from pathlib import Path

from vault_schema import (
    INBOX_DIR,
    PROTECTED_DIRS,
    WORKSPACE_MARKER,
    UserError,
    compile_destination,
    link_basename,
    parse_frontmatter,
    sha256_file,
    sha256_text,
    split_frontmatter,
    strip_inline_code,
    table_after,
    wikilink_target,
)

DEFAULT_LEXICON = "99 Meta/99.02 Schemas/0.02 Speakers and Terms.md"
LEXICON_BASENAME = "0.02 Speakers and Terms.md"
COMPILED_LEXICON_VERSION = 1

TERMS_SECTION = "Terms"
SPEAKERS_SECTION = "Speakers"

APPEARS_ALWAYS = "always"
APPEARS_SOMETIMES = "sometimes"
APPEARS_NEVER = "never"
APPEARS_VALUES = (APPEARS_ALWAYS, APPEARS_SOMETIMES, APPEARS_NEVER)
DEFAULT_APPEARS = APPEARS_SOMETIMES

TERM_CATEGORIES = ("name", "acronym", "term")
DEFAULT_CATEGORY = "term"

# The conventional route for person notes, and only a convention: the schema
# routes by domain and subdomain, and which of those holds people is a prose
# decision rule the parser never sees. A vault declaring this route gets the
# cheap answer; one that files people anywhere else is found by scanning for
# the note type instead, which is what actually makes a note a person note.
PEOPLE_DOMAIN = "directory"
PEOPLE_SUBDOMAIN = "contacts"
PERSON_TYPE = "person"

# Calibrated against the 44 real mistranscriptions in the transcription
# dictionary. Requiring the first folded letter to match keeps every one of
# them and is what makes a low ratio safe: an engine mangles the vowels and
# endings of a specialist term, but almost never its opening consonant. Without
# that constraint "Lojong" matches the ordinary word "jong" at 0.80 -- higher
# than ten of the true pairs -- and no threshold separates them.
NEAR_MISS_RATIO = 0.72
# Names are shorter and collide with common words more easily, so they need a
# higher bar: "Marge" reaches 0.73 against "margin" but only 0.67 against it here.
NAME_MATCH_RATIO = 0.85
MAX_WINDOW_TOKENS = 3
DEFAULT_TERM_LIMIT = 8
DEFAULT_SPEAKER_LIMIT = 10
MAX_ROLE_CHARS = 80

TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
BOLD_LINE_RE = re.compile(r"^\*\*(.+?)\*\*$")
HEADING_RE = re.compile(r"^#{1,6}\s+")


# --------------------------------------------------------------------------
# Folding and fuzzy comparison
# --------------------------------------------------------------------------

def _strip_accents(value):
    text = unicodedata.normalize("NFKD", str(value))
    return "".join(character for character in text if not unicodedata.combining(character))


def fold(value):
    """Accent-stripped, lowercase letters only. ``Nāgārjuna`` and ``Nagarjuna``
    fold to the same string, which is most of what diacritics cost us here."""
    return re.sub(r"[^a-z]", "", _strip_accents(value).casefold())


def similarity(left, right):
    """Ratio over folded forms, with the cheap length bound applied first.

    ``SequenceMatcher`` can never exceed ``2 * min / (len + len)``, so a pair
    that cannot reach the threshold is rejected without the quadratic match.
    """
    if not left or not right:
        return 0.0
    if left[0] != right[0]:
        return 0.0
    bound = 2 * min(len(left), len(right)) / (len(left) + len(right))
    if bound < NEAR_MISS_RATIO:
        return 0.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def _tokens(text):
    return TOKEN_RE.findall(str(text))


def _windows(tokens, max_tokens=MAX_WINDOW_TOKENS):
    """Folded joins of every 1..n token run, so a term the engine split across
    words (``Madhya Maka``) is comparable to the single-token canonical."""
    seen = {}
    for size in range(1, max_tokens + 1):
        for start in range(len(tokens) - size + 1):
            run = tokens[start:start + size]
            key = fold("".join(run))
            if key and key not in seen:
                seen[key] = " ".join(run)
    return seen


def folded_windows(text, max_tokens=MAX_WINDOW_TOKENS):
    """``{folded key: original phrase}`` for every 1..n token run in ``text``.

    The public form of the window builder, so other vault modules can test
    whether a phrase occurs without caring how it was spaced, accented, or
    cased. Exact membership only -- callers wanting fuzzy matching should reach
    for :func:`similarity`, which has different failure costs.
    """
    return _windows(_tokens(text), max_tokens=max_tokens)


# --------------------------------------------------------------------------
# Dictionary entries and correction
#
# Moved here from the transcription skill so the vault pipeline can reach them.
# --------------------------------------------------------------------------

def normalize_entry(entry):
    if not isinstance(entry, dict):
        return None
    correct = str(entry.get("correct", "")).strip()
    if not correct:
        return None
    variants = [str(value).strip() for value in entry.get("variants", []) if str(value).strip()]
    return {
        "correct": correct,
        "variants": sorted(set(variants), key=lambda value: (-len(value), value.lower())),
        "category": entry.get("category") or DEFAULT_CATEGORY,
        "case_sensitive": bool(entry.get("case_sensitive", False)),
        "whole_word": bool(entry.get("whole_word", True)),
        "note": str(entry.get("note") or "").strip(),
    }


def load_dictionary(path):
    path = Path(path)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise UserError(f"could not read dictionary {path}: {error}") from error
    entries = data.get("entries", data) if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise UserError(f"dictionary {path} must contain a list of entries")
    return [entry for entry in (normalize_entry(item) for item in entries) if entry]


def merge_dictionaries(base_entries, overriding_entries):
    merged = {entry["correct"].lower(): entry for entry in base_entries}
    for entry in overriding_entries:
        merged[entry["correct"].lower()] = entry
    return sorted(merged.values(), key=lambda entry: entry["correct"].lower())


def compile_corrections(entries):
    compiled = []
    for entry in entries:
        for variant in entry["variants"]:
            flags = 0 if entry["case_sensitive"] else re.IGNORECASE
            pattern = re.escape(variant)
            pattern = re.sub(r"\\\s+|\\ ", r"\\s+", pattern)
            if entry["whole_word"]:
                pattern = rf"(?<!\w){pattern}(?!\w)"
            compiled.append((variant, entry["correct"], entry["category"], re.compile(pattern, flags)))
    compiled.sort(key=lambda item: -len(item[0]))
    return compiled


def apply_corrections(text, entries):
    """Replace every recorded variant. Returns ``(text, rows)``.

    Longest variant first, so a short variant never eats the front of a longer
    phrase that was about to match.
    """
    log = {}
    for variant, correct, category, regex in compile_corrections(entries):
        offsets = []

        def record(match):
            offsets.append(match.start())
            return correct

        text, count = regex.subn(record, text)
        if count:
            key = (correct, variant)
            existing = log.get(key, {"category": category, "count": 0, "offsets": []})
            existing["count"] += count
            existing["offsets"].extend(offsets)
            log[key] = existing
    rows = [
        {
            "correct": correct,
            "variant": variant,
            "category": value["category"],
            "count": value["count"],
            "offsets": ";".join(str(offset) for offset in value["offsets"][:50]),
        }
        for (correct, variant), value in log.items()
    ]
    rows.sort(key=lambda row: (-row["count"], row["correct"].lower()))
    return text, rows


# --------------------------------------------------------------------------
# Note parsing
# --------------------------------------------------------------------------

def default_dictionary_path(env=None):
    """The standalone transcription dictionary, resolved the way that skill does."""
    environment = env if env is not None else os.environ
    override = environment.get("PI_FORGE_TRANSCRIPTION_HOME")
    if override:
        return Path(override).expanduser() / "dictionary.json"
    root = environment.get("PI_FORGE_HOME")
    home = Path(root).expanduser() if root else Path.home() / ".pi-forge"
    return home / "transcription" / "dictionary.json"


def resolve_lexicon_path(vault, raw_lexicon=None, disabled=False):
    """Return the selected lexicon note, or ``None`` when disabled or absent."""
    if disabled:
        return None
    vault = Path(vault)
    if raw_lexicon:
        path = Path(raw_lexicon).expanduser()
        if not path.is_absolute():
            path = vault / path
        if not path.is_file():
            raise UserError(f"lexicon note does not exist: {path}")
        return path.resolve()
    default = (vault / DEFAULT_LEXICON).resolve()
    if default.is_file():
        return default
    matches = []
    for candidate in vault.rglob(LEXICON_BASENAME):
        parts = candidate.resolve().relative_to(vault.resolve()).parts
        if any(part.startswith(".") or part in PROTECTED_DIRS for part in parts):
            continue
        if parts and parts[0] == INBOX_DIR:
            continue
        matches.append(candidate.resolve())
    if len(matches) > 1:
        listed = ", ".join(str(path) for path in sorted(matches))
        raise UserError(f"more than one '{LEXICON_BASENAME}' in the vault ({listed}); pass --lexicon")
    return matches[0] if matches else None


def _has_section(lines, heading):
    return any(re.fullmatch(rf"##\s+{re.escape(heading)}\s*", line) for line in lines)


def _cell_values(cell):
    """A table cell holding a comma-separated list of optionally backticked values.

    Split before unwrapping: a cell of several backticked values is itself
    wrapped in backticks end to end, so unwrapping first eats the wrong pair.
    """
    parts = (strip_inline_code(part) for part in str(cell).split(","))
    return [part for part in parts if part]


def _parse_terms(lines):
    rows = table_after(lines, TERMS_SECTION, ["Term", "Variants"])
    terms = []
    seen = {}
    for row in rows:
        correct = strip_inline_code(row["Term"])
        if not correct:
            raise UserError(f"{TERMS_SECTION}: a row has no term")
        key = correct.lower()
        if key in seen:
            raise UserError(f"{TERMS_SECTION}: duplicate term {correct}")
        seen[key] = True
        category = strip_inline_code(row.get("Kind", "")) or DEFAULT_CATEGORY
        if category not in TERM_CATEGORIES:
            raise UserError(
                f"{TERMS_SECTION}: {correct} has kind {category!r}; use one of {', '.join(TERM_CATEGORIES)}"
            )
        entry = normalize_entry(
            {
                "correct": correct,
                "variants": _cell_values(row["Variants"]),
                "category": category,
                "note": row.get("Notes", ""),
            }
        )
        terms.append(entry)
    return terms


def _parse_speakers(lines):
    rows = table_after(lines, SPEAKERS_SECTION, ["Person", "Appears"])
    speakers = []
    seen = {}
    for row in rows:
        raw = strip_inline_code(row["Person"])
        link = raw if raw.startswith("[[") else ""
        name = link_basename(wikilink_target(raw)) if link else raw
        if not name:
            raise UserError(f"{SPEAKERS_SECTION}: a row has no person")
        key = name.lower()
        if key in seen:
            raise UserError(f"{SPEAKERS_SECTION}: duplicate person {name}")
        seen[key] = True
        appears = (strip_inline_code(row["Appears"]) or DEFAULT_APPEARS).lower()
        if appears not in APPEARS_VALUES:
            raise UserError(
                f"{SPEAKERS_SECTION}: {name} has appears {appears!r}; use one of {', '.join(APPEARS_VALUES)}"
            )
        speakers.append(
            {
                "name": name,
                "link": link,
                "appears": appears,
                "aliases": _cell_values(row.get("Aliases", "")),
                "cue": strip_inline_code(row.get("Cue", "")),
                "role": "",
            }
        )
    return speakers


def parse_lexicon_note(text):
    """Parse the owner's lexicon note. Both sections are optional; an empty note
    is an error, because a note that defines nothing is a mistake, not a policy."""
    lines = str(text).splitlines()
    lexicon = {"terms": [], "speakers": []}
    present = []
    if _has_section(lines, TERMS_SECTION):
        lexicon["terms"] = _parse_terms(lines)
        present.append(TERMS_SECTION)
    if _has_section(lines, SPEAKERS_SECTION):
        lexicon["speakers"] = _parse_speakers(lines)
        present.append(SPEAKERS_SECTION)
    if not present:
        raise UserError(
            f"lexicon note has no '## {TERMS_SECTION}' or '## {SPEAKERS_SECTION}' section"
        )
    return lexicon


# --------------------------------------------------------------------------
# Directory notes
# --------------------------------------------------------------------------

def people_folder(schema):
    """The conventional people folder, or ``None`` when this schema has no such route."""
    if not schema:
        return None
    try:
        return compile_destination(schema, {"domain": PEOPLE_DOMAIN, "subdomain": PEOPLE_SUBDOMAIN})
    except (KeyError, UserError):
        return None


def _role_summary(body, metadata):
    """The identifying line under a person note's heading.

    These notes open with the name as a heading and then one or more bold lines
    carrying role and affiliation, which is exactly the one-line identity a
    roster entry needs. Frontmatter covers the notes that skip them.
    """
    parts = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or HEADING_RE.match(stripped):
            if parts:
                break
            continue
        match = BOLD_LINE_RE.match(stripped)
        if not match:
            break
        parts.append(match.group(1).strip())
    if not parts:
        organization = metadata.get("organization")
        if isinstance(organization, str):
            target = link_basename(wikilink_target(organization)) or organization
            if target:
                parts.append(target)
    summary = ", ".join(part for part in parts if part)
    # These land in a JSON payload and in the run report's tables, so a pipe or
    # a newline carried out of the note would break the row it is rendered into.
    summary = re.sub(r"\s+", " ", summary.replace("|", "/"))
    return summary[:MAX_ROLE_CHARS].strip()


def _read_person(path, require_type=False):
    try:
        data = split_frontmatter(path.read_bytes())
    except (OSError, UnicodeDecodeError, UserError):
        return None
    try:
        metadata = parse_frontmatter(data.get("frontmatter_text") or "")
    except (UserError, ValueError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    if require_type and metadata.get("type") != PERSON_TYPE:
        return None
    return {"name": path.stem, "role": _role_summary(data.get("body") or "", metadata)}


def _people_by_scan(vault):
    """Person notes found by their own `type`, for a vault with no people route.

    Slower than reading one folder, so it is the fallback rather than the rule --
    but it is the only answer that survives a vault reorganising its domains,
    and an empty roster is worse than a slow one. A silent `[]` here means the
    transcript pipeline stops naming voices it has always named.
    """
    vault = Path(vault).resolve()
    workspace_roots = {marker.parent.resolve() for marker in vault.rglob(WORKSPACE_MARKER)}
    people = []
    for path in sorted(vault.rglob("*.md")):
        resolved = path.resolve()
        parts = resolved.relative_to(vault).parts
        if any(part.startswith(".") or part in PROTECTED_DIRS for part in parts):
            continue
        if parts and parts[0] == INBOX_DIR:
            continue
        # A run directory holds backup copies of notes; a person note backed up
        # there is the same person, and would join the roster twice.
        if any(root in resolved.parents for root in workspace_roots):
            continue
        person = _read_person(resolved, require_type=True)
        if person:
            people.append(person)
    return people


def directory_people(vault, schema):
    """Compile a one-line entry for every person note in the vault.

    Reusing the notes means the roster carries current roles without anyone
    maintaining a second copy of them. Where those notes live is asked of the
    schema first and worked out from the notes themselves when the schema
    declares no such route -- see `PEOPLE_DOMAIN`.

    No schema at all is a different thing from a schema that files people
    elsewhere: the caller has told us nothing, so we scan nothing.
    """
    if not schema:
        return []
    folder = people_folder(schema)
    root = Path(vault) / folder if folder else None
    if root is None or not root.is_dir():
        return _people_by_scan(vault)
    people = []
    for path in sorted(root.glob("*.md")):
        if path.name.startswith("."):
            continue
        person = _read_person(path)
        if person:
            people.append(person)
    return people


def _merge_directory(speakers, people):
    """Overlay rows win; everyone else joins at the default tier.

    An overlay row carries what a note cannot -- a nickname, a relationship, a
    judgment about whether this person is ever in a recording -- so it only has
    to say that much, and takes its role from the note.
    """
    by_name = {entry["name"].lower(): entry for entry in speakers}
    for person in people:
        existing = by_name.get(person["name"].lower())
        if existing:
            if not existing["role"]:
                existing["role"] = person["role"]
            continue
        entry = {
            "name": person["name"],
            "link": f"[[{person['name']}]]",
            "appears": DEFAULT_APPEARS,
            "aliases": [],
            "cue": "",
            "role": person["role"],
        }
        speakers.append(entry)
        by_name[entry["name"].lower()] = entry
    speakers.sort(key=lambda entry: entry["name"].lower())
    return speakers


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def _directory_fingerprint(people):
    return sha256_text(json.dumps(people, ensure_ascii=False, sort_keys=True))


def compiled_lexicon_for(vault, lexicon_path, schema=None, cache_dir=None):
    """Parse the note and fold in the directory notes, caching by content hash."""
    people = directory_people(vault, schema)
    note_hash = sha256_file(Path(lexicon_path)) if lexicon_path else "none"
    lexicon_hash = sha256_text(f"{note_hash}:{_directory_fingerprint(people)}")
    cache_path = Path(cache_dir) / "compiled-lexicon.json" if cache_dir else None
    if cache_path and cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("version") == COMPILED_LEXICON_VERSION and cached.get("lexicon_hash") == lexicon_hash:
                return cached["lexicon"], lexicon_hash
        except (OSError, json.JSONDecodeError, KeyError):
            pass
    if lexicon_path:
        lexicon = parse_lexicon_note(Path(lexicon_path).read_text(encoding="utf-8"))
    else:
        lexicon = {"terms": [], "speakers": []}
    lexicon["speakers"] = _merge_directory(lexicon["speakers"], people)
    if cache_path:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    {"version": COMPILED_LEXICON_VERSION, "lexicon_hash": lexicon_hash, "lexicon": lexicon},
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass
    return lexicon, lexicon_hash


def load_lexicon(vault, lexicon_path, schema=None, cache_dir=None, dictionary_path=None):
    """The lexicon a run should use: the note, the directory notes, and the
    standalone transcription dictionary merged underneath.

    The dictionary is tuned to a different engine's mistakes than the vault
    inbox sees, and a variant one engine never produces costs nothing, so
    merging is strictly more coverage. The note wins on any shared term.
    """
    lexicon, lexicon_hash = compiled_lexicon_for(vault, lexicon_path, schema=schema, cache_dir=cache_dir)
    dictionary_hash = "none"
    if dictionary_path and Path(dictionary_path).is_file():
        entries = load_dictionary(dictionary_path)
        lexicon = dict(lexicon)
        lexicon["terms"] = merge_dictionaries(entries, lexicon["terms"])
        dictionary_hash = sha256_file(Path(dictionary_path))
    if not lexicon["terms"] and not lexicon["speakers"]:
        return None, None
    return lexicon, sha256_text(f"{lexicon_hash}:{dictionary_hash}")


def lexicon_fingerprint(lexicon_hash):
    return lexicon_hash[:16] if lexicon_hash else None


def lexicon_state(lexicon_path, lexicon_hash, dictionary_path=None):
    """Serializable lexicon identity used by resumable workflows."""
    return {
        "lexicon_path": str(lexicon_path) if lexicon_path else None,
        "lexicon_hash": lexicon_fingerprint(lexicon_hash),
        "lexicon_dictionary": str(dictionary_path) if dictionary_path else None,
        "lexicon_compiler_version": COMPILED_LEXICON_VERSION,
    }


def lexicon_digest(lexicon):
    """Counts for the doctor report."""
    if not lexicon:
        return {"terms": 0, "variants": 0, "speakers": 0, "always": 0, "sometimes": 0, "never": 0}
    speakers = lexicon.get("speakers", [])
    digest = {
        "terms": len(lexicon.get("terms", [])),
        "variants": sum(len(entry["variants"]) for entry in lexicon.get("terms", [])),
        "speakers": len(speakers),
    }
    for value in APPEARS_VALUES:
        digest[value] = sum(1 for entry in speakers if entry["appears"] == value)
    return digest


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------

def term_candidates(lexicon):
    """Canonical spellings the model may be offered, terms and people alike.

    A person's name is mistranscribed exactly as a specialist term is, and the
    roster already holds the correct spelling, so it feeds this too -- including
    people marked ``never``, who are spoken about rather than speaking.
    """
    if not lexicon:
        return []
    candidates = [
        {"correct": entry["correct"], "category": entry["category"], "note": entry.get("note", "")}
        for entry in lexicon.get("terms", [])
    ]
    known = {entry["correct"].lower() for entry in lexicon.get("terms", [])}
    for entry in lexicon.get("speakers", []):
        # People are named in passing by one part of their name far more often
        # than in full, so each spoken form is its own candidate. They carry the
        # stricter name bar: a short given name collides with ordinary words
        # that a distinctive multi-word term never would.
        for form in _spoken_name_forms(entry):
            if form.lower() in known:
                continue
            candidates.append(
                {
                    "correct": form,
                    "category": "name",
                    "note": entry.get("role", ""),
                    "min_ratio": NAME_MATCH_RATIO,
                }
            )
            known.add(form.lower())
    return candidates


def _spoken_name_forms(entry):
    """The written-out forms of one person's name worth correcting toward.

    Short forms are dropped: below about five letters a name is closer to an
    ordinary word than to its own misspellings.
    """
    forms = [entry["name"], *entry.get("aliases", [])]
    parts = _tokens(entry["name"])
    if len(parts) > 1:
        forms.extend(parts)
    return [form for form in forms if len(fold(form)) >= 5]


def _present_verbatim(text, term):
    """Whether the text already spells this term, accents and casing aside.

    Spacing has to match too. A term the engine split into "Bodhi citta" folds
    to the canonical spelling but is not how the owner writes it, so it is still
    worth offering.
    """
    parts = [re.escape(part) for part in _tokens(term)]
    if not parts:
        return False
    spacing = r"\s+"
    pattern = rf"(?<!\w){spacing.join(parts)}(?!\w)"
    return re.search(pattern, _strip_accents(text), re.IGNORECASE) is not None


def near_miss_terms(text, candidates, limit=DEFAULT_TERM_LIMIT):
    """Canonical spellings that something in this text sounds like but is not.

    Terms already spelled correctly are excluded: the model has nothing to fix,
    and every offered term is prompt weight that has to earn its place.
    """
    if not candidates or not text:
        return []
    tokens = _tokens(text)
    if not tokens:
        return []
    windows = _windows(tokens)
    canonical = {fold(candidate["correct"]) for candidate in candidates}
    buckets = {}
    for key, phrase in windows.items():
        buckets.setdefault(key[0], []).append((key, phrase))
    offers = []
    for candidate in candidates:
        folded = fold(candidate["correct"])
        if not folded or _present_verbatim(text, candidate["correct"]):
            continue
        best = (0.0, "")
        for key, phrase in buckets.get(folded[0], ()):
            # A window that is itself some other canonical spelling is not a
            # mistranscription of anything. Without this, "Bodhicitta" in the
            # text draws an offer to rewrite it as "Bodhisattva".
            if key in canonical and key != folded:
                continue
            score = similarity(folded, key)
            if score > best[0]:
                best = (score, phrase)
        if best[0] >= candidate.get("min_ratio", NEAR_MISS_RATIO):
            offer = {"term": candidate["correct"], "heardAs": best[1], "score": round(best[0], 3)}
            if candidate.get("note"):
                offer["note"] = candidate["note"]
            offers.append(offer)
    offers.sort(key=lambda offer: (-offer["score"], offer["term"].lower()))
    for offer in offers:
        offer.pop("score", None)
    return offers[:limit]


def _name_forms(entry):
    """The spoken forms that identify one person, longest first."""
    forms = [entry["name"], *entry.get("aliases", [])]
    parts = _tokens(entry["name"])
    if len(parts) > 1:
        if len(parts[0]) >= 3:
            forms.append(parts[0])
        if len(parts[-1]) >= 4:
            forms.append(parts[-1])
    seen = {}
    for form in forms:
        folded = fold(form)
        if folded and folded not in seen:
            seen[folded] = form
    return seen


def _name_mentions(windows, buckets, entry):
    """How many of this person's name forms the text contains, near-misses included."""
    hits = 0
    for folded in _name_forms(entry):
        if folded in windows:
            hits += 1
            continue
        for key, _phrase in buckets.get(folded[0], ()):
            if difflib.SequenceMatcher(None, folded, key).ratio() >= NAME_MATCH_RATIO:
                hits += 1
                break
    return hits


def candidate_speakers(text, speakers, limit=DEFAULT_SPEAKER_LIMIT):
    """The roster entries worth showing the model for this recording.

    ``always`` is for the handful of people whose voice recurs -- a partner, a
    therapist, a standing one-to-one -- and goes in unconditionally, because the
    whole point is naming a voice the transcript never names. Everyone else has
    to be mentioned somewhere in the recording to be worth the tokens.
    """
    if not speakers:
        return []
    tokens = _tokens(text or "")
    windows = _windows(tokens)
    buckets = {}
    for key, phrase in windows.items():
        buckets.setdefault(key[0], []).append((key, phrase))
    always = []
    mentioned = []
    for entry in speakers:
        if entry["appears"] == APPEARS_NEVER:
            continue
        if entry["appears"] == APPEARS_ALWAYS:
            always.append(entry)
            continue
        hits = _name_mentions(windows, buckets, entry)
        if hits:
            mentioned.append((hits, entry))
    mentioned.sort(key=lambda item: (-item[0], item[1]["name"].lower()))
    selected = always + [entry for _hits, entry in mentioned]
    return selected[:limit]


def speaker_offers(entries):
    """Roster entries as compact prompt rows, dropping what is empty."""
    offers = []
    for entry in entries:
        offer = {"name": entry["name"]}
        if entry.get("role"):
            offer["role"] = entry["role"]
        if entry.get("cue"):
            offer["cue"] = entry["cue"]
        if entry.get("aliases"):
            offer["alsoCalled"] = entry["aliases"]
        if entry.get("appears") == APPEARS_ALWAYS:
            offer["recurring"] = True
        offers.append(offer)
    return offers


def _lookup_person(lexicon, name):
    if not lexicon or not name:
        return None
    target = fold(name)
    if not target:
        return None
    for entry in lexicon.get("speakers", []):
        if target in _name_forms(entry):
            return entry
    return None


def canonical_name(lexicon, name):
    """The roster's spelling of a name the model returned, if it knows one.

    A nickname and a mangled surname both resolve to the one name the vault
    already files this person under.
    """
    entry = _lookup_person(lexicon, name)
    return entry["name"] if entry else None


def canonical_link(lexicon, name):
    """The wikilink a resolved speaker should be written as, if the roster knows
    one. Keeps ``[[Gillian]]`` and ``[[Gillian Reid]]`` from both existing."""
    entry = _lookup_person(lexicon, name)
    if not entry:
        return None
    return entry.get("link") or f"[[{entry['name']}]]"

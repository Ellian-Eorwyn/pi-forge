#!/usr/bin/env python3
"""Structural cleanup of a badly extracted document, with an exact invariant.

`document-ingest` cleans the Markdown structure of one chunk at a time: fix
headings, paragraphs, lists, and tables; change no wording. That contract has an
exact check behind it — a word in the output either appeared in the chunk or was
invented — and the fixture is chosen to put real pressure on it. It is a chiller
O&M manual whose PDF extraction left ligature corruption (`certiﬁ ed`,
`beneﬁts`), sentences hard-wrapped mid-clause, and `## Page N` scaffolding
running through the body. A model that "helpfully" repairs `certiﬁ ed` to
`certified` has added a word, which is exactly the judgment call the prompt
tells it not to make: *leave text that is garbled or uncertain visible rather
than repairing it by guessing.*

The prompt is read out of `document-ingest.mjs` rather than copied, so it cannot
drift from the skill. `added_words` is reimplemented here because the JavaScript
one is module-private and that file runs its CLI on import; the algorithm is
small and exactly specified, and `tests/test_evals.py` pins its semantics.
"""

import re

import _common

DIMENSION = "document-cleanup"
SKILL = "document-ingest"
JUDGE = True

FIXTURES = ["manual-ocr-chunk"]

# Small on purpose, for two reasons that happen to have one fix. Twelve-thousand
# character chunks gave this case two items — too few to decide anything — and
# produced 20 KB replies no grader would read, so it was the one judged case that
# never got graded. Smaller windows give both enough items for a verdict and
# output a person can hold in their head.
CHUNK_CHARS = 3000
MAX_CHUNKS = 8

INGEST_SCRIPT = _common.harness.SKILLS / "document-ingest" / "scripts" / "document-ingest.mjs"

# Mirrors WORD_PATTERN in document-ingest.mjs: three or more letters, so page
# numbers and single-letter artifacts are not treated as vocabulary.
WORD_PATTERN = re.compile(r"[a-z][a-z'-]{2,}")


def cleanup_system():
    """`CHUNK_CLEANUP_SYSTEM`, read from the skill that owns it."""
    source = INGEST_SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"const CHUNK_CLEANUP_SYSTEM = `(.*?)`;", source, re.DOTALL)
    if not match:
        raise _common.harness.EvalError(
            f"could not find CHUNK_CLEANUP_SYSTEM in {INGEST_SCRIPT}; the constant moved or was renamed"
        )
    return match.group(1)


def added_words(source, cleaned):
    """Words in the cleaned chunk that were not in the source.

    Multiset, not set: a word the source uses once and the output uses twice has
    one occurrence added. Dropped words are deliberately not checked — removing a
    running header or a page number is legitimate cleanup.

    Port of `addedWords` in document-ingest.mjs (near the CHUNK_CLEANUP_SYSTEM
    constant). Keep the two in step.
    """
    counts = {}
    for word in WORD_PATTERN.findall(source.lower()):
        counts[word] = counts.get(word, 0) + 1
    added = []
    for word in WORD_PATTERN.findall(cleaned.lower()):
        remaining = counts.get(word, 0)
        if remaining <= 0:
            added.append(word)
        else:
            counts[word] = remaining - 1
    return list(dict.fromkeys(added))


def _chunks(fixture_id, budget=CHUNK_CHARS):
    """Split the excerpt on blank lines, under the same rough budget the skill uses."""
    schema_lib = _common.harness.load_lib("vault_schema")
    text = schema_lib.split_frontmatter(_common.fixture(fixture_id).encode("utf-8"))["body"].strip()
    chunks, current, size = [], [], 0
    for block in text.split("\n\n"):
        if current and size + len(block) > budget:
            chunks.append("\n\n".join(current))
            current, size = [], 0
        current.append(block)
        size += len(block) + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def items():
    system = cleanup_system()
    built = []
    for fixture_id in FIXTURES:
        for index, chunk in enumerate(_chunks(fixture_id)[:MAX_CHUNKS], start=1):
            built.append(
                {
                    "id": fixture_id if index == 1 else f"{fixture_id}#{index}",
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": _common.json.dumps({"chunk": chunk}, ensure_ascii=False)},
                    ],
                    "response_format": {"type": "json_object"},
                    "max_tokens": 4096,
                    "source": chunk,
                }
            )
    return built


def score(item, content, record=None):
    parsed = _common.parse_json(content)
    if not isinstance(parsed, dict):
        # This fixture is the one most likely to send a model into a repetition
        # loop — the source itself contains a runaway OCR loop — so a reply that
        # filled its whole budget is a distinct finding from one that came back
        # malformed and short.
        if _common.truncated(record):
            return _common.truncation_failure(record)
        return _common.failure("reply was not a JSON object")
    markdown = parsed.get("markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        return _common.failure("reply carried no markdown", {"parsed": True, "noAddedWords": False})

    source = item["source"]
    invented = added_words(source, markdown)
    source_words = _common.word_count(source)
    kept = _common.word_count(markdown) / source_words if source_words else 0.0
    page_markers = len(re.findall(r"^#+\s*Page\s+\d+\s*$", markdown, re.MULTILINE))
    headings = len(re.findall(r"^#{1,6} ", markdown, re.MULTILINE))

    gates = {
        "parsed": True,
        # The invariant the skill enforces: a chunk with any added word is
        # rejected outright, no tolerance.
        "noAddedWords": not invented,
        # Structural cleanup condenses at most a little. Losing a third of the
        # words means it summarized instead.
        "contentKept": kept >= 0.7,
        "producedStructure": headings > 0,
    }
    notes = []
    if invented:
        notes.append(f"words not in the chunk: {', '.join(invented[:10])}")
    if kept < 0.7:
        notes.append(f"kept {kept:.0%} of the words: this is condensing, not restructuring")

    return {
        "ok": all(gates.values()),
        "gates": gates,
        "metrics": {
            "addedWords": len(invented),
            "wordRatio": round(kept, 4),
            "headings": headings,
            "pageMarkersLeft": page_markers,
        },
        "notes": notes,
        "output": markdown,
    }


def judge_context(item_id):
    built = next((entry for entry in items() if entry["id"] == item_id), None)
    return {
        "instruction": (
            "Fix the Markdown structure of this extracted chunk without changing any wording. "
            "Garbled text should stay visible rather than be repaired by guessing."
        ),
        "source": built["source"] if built else None,
        "reference": None,
    }

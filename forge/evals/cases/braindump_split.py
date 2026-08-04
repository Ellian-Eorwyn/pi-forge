#!/usr/bin/env python3
"""How many notes is this braindump?

`vault-capture`'s own contract calls the split "the weakest joint in the
pipeline", and the comment above `SPLIT_SYSTEM` says why: a non-thinking model
asked how many notes a dump contains answers "one" almost every time, so the
prompt enumerates the kinds of thing a dump can hold instead of asking for a
count. That makes this a direct measurement of whether shipped enumeration works
on a given model.

`validate_split` is the gate. Beyond it there is no single right count, so the
expectation is a range: a band of defensible answers read off the fixture, with
the two failures that are unambiguous scored separately. Returning **one** note
is the documented failure the enumeration exists to prevent. Returning exactly
`--max-notes` is the opposite one — a model that hit the cap stopped counting
and started listing, and the cap rather than the material decided the answer.
"""

import _common

DIMENSION = "segmentation"
SKILL = "vault-capture"
JUDGE = True

# Ranges are hand-read from the frozen fixtures and deliberately wide. An early
# run of this case scored an eight-way split as wrong against a hand-read count
# of two; reading what it actually proposed, most of those notes were real. The
# band is what a careful reader would accept, not what one reader chose.
EXPECTED = {
    "braindump-brainstorm": {
        "min": 2,
        "max": 6,
        "kinds": ["plan", "task", "idea"],
        "why": (
            "An interaction-point outline, then a dictated wall covering navigation, column "
            "behaviour, the source-details panel, citation metadata, and settings. Grouping "
            "those into two notes or into five are both defensible readings."
        ),
    },
    "raw-asr-piforge": {
        "min": 1,
        "max": 4,
        "kinds": ["idea", "plan", "question"],
        "why": (
            "One continuous train of thought: a research extension for qualitative coding, plus "
            "the speaker working through how it might handle segmentation. Splitting past about "
            "four is cutting one line of thinking into pieces."
        ),
    },
    "braindump-todo": {
        "min": 2,
        "max": 5,
        "kinds": ["task", "plan"],
        "why": "A spoken to-do list for one day, running through several unrelated jobs.",
    },
    "braindump-merge": {
        "min": 1,
        "max": 2,
        "kinds": ["idea", "plan"],
        "why": (
            "One feature, described start to finish: merge same-day transcripts into a single "
            "note before cleanup. There is no second subject in it."
        ),
    },
    "braindump-voice": {
        "min": 1,
        "max": 2,
        "kinds": ["idea", "plan"],
        "why": "One idea — move the voice-note transcription pipeline off the Mac and onto Linux.",
    },
    "braindump-speakers": {
        "min": 3,
        "max": 7,
        "kinds": ["task", "note", "question"],
        "why": (
            "A walkthrough of separate defects found while testing a site: account creation, "
            "the confirmation link, and several others, each independent of the rest."
        ),
    },
    "braindump-weather": {
        "min": 1,
        "max": 3,
        "kinds": ["reference", "note", "idea"],
        "why": (
            "Already a written research note with headings, not dictation: one subject with "
            "three options under it. Splitting it much past the options is cutting up a document "
            "that was already organised."
        ),
    },
    "braindump-requirements": {
        "min": 2,
        "max": 5,
        "kinds": ["plan", "task", "idea"],
        "why": "Dictated feature requirements for the vault manager, covering several separable features.",
    },
}

# Deliberately well above any hand-read maximum in the table. `--max-notes` used
# to be 8 and both models returned exactly 8, so the cap was choosing the answer
# rather than the material — the case was measuring a constant. Raised until it
# cannot bind, and `didNotMaxOut` stays as the check that it never does again.
MAX_NOTES = 20


def _skill():
    return _common.harness.load_skill("vault-capture")


def _text(fixture_id):
    """The braindump as the capture stage would receive it: body only."""
    schema_lib = _common.harness.load_lib("vault_schema")
    return schema_lib.split_frontmatter(_common.fixture(fixture_id).encode("utf-8"))["body"].strip()


def items():
    capture = _skill()
    built = []
    for fixture_id, expectation in EXPECTED.items():
        item = {"text": _text(fixture_id), "label": fixture_id}
        payload = capture.split_payload(item, MAX_NOTES)
        built.append(
            {
                "id": fixture_id,
                "messages": [
                    {"role": "system", "content": capture.SPLIT_SYSTEM},
                    {"role": "user", "content": _common.json.dumps(payload, ensure_ascii=False)},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": 2048,
                "expected": expectation,
            }
        )
    return built


def score(item, content, record=None):
    capture = _skill()
    parsed = _common.parse_json(content)
    if not isinstance(parsed, dict):
        return _common.failure("reply was not a JSON object")

    notes = None
    error = None
    try:
        notes, needs_review, reason = capture.validate_split(parsed, MAX_NOTES)
    except Exception as failure:
        error = str(failure)
        needs_review, reason = True, error

    gates = {"parsed": True, "validates": notes is not None}
    if notes is None:
        return {"ok": False, "gates": gates, "metrics": {}, "notes": [error], "output": parsed}

    expected = item["expected"]
    count = len(notes)
    kinds = sorted({note["kind"] for note in notes})
    overlap = len(set(kinds) & set(expected["kinds"]))

    gates["countInRange"] = expected["min"] <= count <= expected["max"]
    # The documented failure: asked how many notes a dump is, a model that does
    # not enumerate answers "one".
    gates["splitAtAll"] = count > 1 or expected["min"] == 1
    # And its mirror: a split that lands exactly on --max-notes was decided by
    # the cap, not by the material.
    gates["didNotMaxOut"] = count < MAX_NOTES
    # Every note has to carry something the dump actually covers, or the split
    # has invented sections rather than found them.
    gates["everyNoteCovers"] = all(note.get("covers") for note in notes)

    notes_out = []
    if error:
        notes_out.append(error)
    if not gates["countInRange"]:
        notes_out.append(
            f"proposed {count} notes, outside the defensible {expected['min']}-{expected['max']}: {expected['why']}"
        )
    if not gates["didNotMaxOut"]:
        notes_out.append(f"returned exactly --max-notes ({MAX_NOTES}); the cap decided the count")
    if reason:
        notes_out.append(f"flagged for review: {reason}")

    return {
        "ok": all(gates.values()),
        "gates": gates,
        "metrics": {
            "noteCount": count,
            # Distance outside the band, so a run inside it scores zero rather
            # than being penalised for picking one end.
            "outsideRangeBy": max(0, expected["min"] - count, count - expected["max"]),
            "kindOverlap": overlap / len(expected["kinds"]),
        },
        "notes": notes_out,
        "output": [{"kind": note["kind"], "title": note["title"], "gist": note.get("gist")} for note in notes],
    }


def judge_context(item_id):
    expectation = EXPECTED[item_id]
    return {
        "instruction": (
            "Split this braindump into the separate notes it actually contains. A careful reading "
            f"accepts {expectation['min']}-{expectation['max']}: {expectation['why']}"
        ),
        "source": _text(item_id),
        "reference": None,
    }

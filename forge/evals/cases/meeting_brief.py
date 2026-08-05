#!/usr/bin/env python3
"""Reading a whole meeting and reporting what happened in it.

The gap this fills: `summary-transcript` drives the skill's real summary stage,
and that stage never reads a long meeting. Above 24,000 characters
`vault-transcripts.summarize_items` swaps the transcript out for its chunk
summaries plus a 6,000-character head, so what the existing case measures is a
map-reduce over pre-digested pieces. Nothing in pi-forge asks a model to hold a
two-hour meeting in its head and say what was decided.

Scored against a reference key written by a stronger model, one meeting at a
time, at length. The key is not prose to be matched: it is a list of facts, each
carrying the verbatim line from the transcript that supports it, so the key
itself is checkable rather than merely trusted — `tests/test_evals.py` asserts
every quote really is in its fixture. Comparing prose to prose would measure
whether two summaries chose the same sentences, which is not the question.

The deterministic half is what decides routing. `factRecall` says how much of the
meeting survived; `inventedNumbers` says whether anything was made up. The second
matters more: a brief that misses a decision is incomplete and looks incomplete,
while a brief with a plausible wrong cost in it looks finished and is worse than
nothing.
"""

import _common
import _meeting

DIMENSION = "long-form-reasoning"
SKILL = "vault-transcripts"
JUDGE = True
TIER = "standard"

# Five public-webinar meetings whose keys are committed, and three internal ones
# whose keys are not. The split is privacy, not difficulty: the internal meetings
# are the better test, because a panel discussion has opinions where a project
# meeting has owners and deadlines. See `_meeting.load_key`.
FIXTURES = (
    "meeting-vpp-intro",
    "meeting-vpp-tech",
    "meeting-brattle",
    "meeting-vpp-panel",
    "meeting-vpp-dr",
    "meeting-kickoff",
    "meeting-aio",
    "meeting-lbnl",
)

# What this case measures when every key is present. `items()` builds only what
# has a key, so on a machine without the private ones it yields five — enough to
# run and read, not enough for a routing verdict. Declared separately so the
# size test checks the design rather than the local filesystem, and so a case
# that is genuinely too small is still caught.
EXPECTED_ITEMS = len(FIXTURES)

# Enough for a full brief on a two-hour meeting without inviting an essay. The
# longest reference key is 21 facts; at roughly 25 tokens a fact plus the JSON
# scaffolding, 2,048 leaves better than twice the room needed.
MAX_TOKENS = 2048

# Recall below this is not a brief, whatever else it gets right.
#
# Measured, not chosen. The first full baseline on `chat-27b` scored 0.18 to
# 0.42 with a median of 0.29, and a floor of 0.35 failed five items of eight —
# which says the floor was wrong, not that the model cannot write a brief. The
# reference keys are exhaustive inventories of everything notable in a two-hour
# meeting (15 to 30 facts each); a 300-word brief is selective by design and
# cannot carry all of them, so `factRecall` is a coverage ratio against an
# upper bound rather than a score out of a target.
#
# 0.15 sits below everything the baseline produced and well above nothing,
# which is what a floor is for: it catches a brief that collapsed without
# punishing ordinary selectivity. Same shape as `enumeration-breadth`'s
# `breadthAboveFloor` (more than 4 of 15 item types). The instrument is the
# metric, compared against the baseline by the report; this is only the rail.
RECALL_FLOOR = 0.15

REQUIRED_KEYS = ("decisions", "actions", "dates", "figures", "open_questions")


def items():
    built = []
    for fixture_id, _key in _meeting.available_keys(FIXTURES):
        body = _common.fixture(fixture_id)
        built.append(
            {
                "id": fixture_id,
                "messages": [
                    {"role": "system", "content": _meeting.BRIEF_SYSTEM},
                    {"role": "user", "content": body},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": MAX_TOKENS,
                "source": body,
            }
        )
    return built


def score(item, content, record=None):
    if _common.truncated(record):
        return _common.truncation_failure(record)
    brief = _common.parse_json(content)
    if not isinstance(brief, dict):
        return _common.failure("reply was not a JSON object")

    key = _meeting.load_key(item["id"])
    if key is None:
        # Unreachable through `items()`, which only builds what has a key. Kept
        # so a key deleted between building and scoring says so instead of
        # crediting the model with a perfect score over zero facts.
        return _common.failure(f"no reference key for {item['id']}", gates={"hasKey": False})

    shape_valid = all(isinstance(brief.get(name), list) for name in REQUIRED_KEYS)
    actions = [entry for entry in (brief.get("actions") or []) if isinstance(entry, dict)]
    written = _meeting.brief_text(brief)

    facts = key.get("facts", [])
    source = item["source"]
    matched = [fact for fact in facts if _meeting.fact_matched(fact, written, source)]
    recall = len(matched) / len(facts) if facts else 0.0

    invented = _meeting.invented_numbers(written, item["source"])

    # A trap is something the meeting floated and never settled. Asserting one
    # as a decision is the failure mode a fluent model has and a weak one does
    # not: it reads the discussion, finds the shape of an agreement, and reports
    # the agreement. Matched against decisions only — naming the same thing as
    # an open question is the correct answer, not a trap hit.
    decisions_text = "\n".join(str(entry) for entry in (brief.get("decisions") or []))
    traps_hit = [trap for trap in key.get("traps", []) if _meeting.fact_matched(trap, decisions_text, source)]

    # Where the key says nobody was assigned, did the brief say so, or did it
    # pick someone? Only counted over actions the model actually produced for
    # those items; inventing an owner is caught here, and staying silent about
    # the action entirely is caught by recall.
    unassigned = key.get("notStated", [])
    abstentions, abstained_right = 0, 0
    for entry in unassigned:
        relevant = [a for a in actions if _meeting.fact_matched(entry, str(a.get("what", "")), source)]
        if not relevant:
            continue
        abstentions += 1
        if all(_meeting.abstained(a.get(entry.get("field", "owner"))) for a in relevant):
            abstained_right += 1

    gates = {
        "parsed": True,
        "shapeValid": shape_valid,
        "noInventedNumbers": not invented,
        "trapsAvoided": not traps_hit,
        "recallAboveFloor": recall >= RECALL_FLOOR,
    }
    notes = []
    if not shape_valid:
        notes.append(f"missing or non-list keys: {[n for n in REQUIRED_KEYS if not isinstance(brief.get(n), list)]}")
    if invented:
        notes.append(f"{len(invented)} number(s) not in the transcript: {', '.join(invented[:6])}")
    if traps_hit:
        notes.append(f"reported as decided: {'; '.join(t['canonical'] for t in traps_hit)}")
    if recall < RECALL_FLOOR:
        notes.append(f"covered {len(matched)} of {len(facts)} reference facts")

    return {
        "ok": all(gates.values()),
        "gates": gates,
        "metrics": {
            "factRecall": round(recall, 4),
            "factsCovered": len(matched),
            "factsInKey": len(facts),
            "inventedNumbers": len(invented),
            "trapsHit": len(traps_hit),
            "abstainedCorrectly": round(abstained_right / abstentions, 4) if abstentions else None,
            "decisions": len(brief.get("decisions") or []),
            "actions": len(actions),
            "briefWords": _common.word_count(written),
        },
        "notes": notes,
        "output": written,
        "keepRaw": True,
    }


def judge_context(item_id):
    key = _meeting.load_key(item_id) or {}
    reference = ["What a careful reader took out of this meeting:", ""]
    for fact in key.get("facts", []):
        reference.append(f"- [{fact['kind']}] {fact['canonical']}")
    if key.get("traps"):
        reference.append("")
        reference.append("Raised but never agreed — reporting these as decisions is wrong:")
        reference.extend(f"- {trap['canonical']}" for trap in key["traps"])
    return {
        "instruction": (
            "Judge the brief as a record of the meeting. Coverage is whether the things that "
            "mattered are here; faithfulness is whether everything here was actually said. The "
            "reference is one reader's list, not a mark scheme — a brief that catches something "
            "the reference missed is better, not worse."
        ),
        "source": _common.fixture(item_id),
        "reference": "\n".join(reference),
    }

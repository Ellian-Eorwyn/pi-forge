#!/usr/bin/env python3
"""Does the review-fix pass clear a faithful cleanup and refuse an unfaithful one?

`vault-transcripts` replaced the utterance spot-check, the whole-note
meaning-judge, and the chat-repair loop with one thinking pass per note
(`REVIEW_FIX_SYSTEM`): read the raw and the note together, then sign off, fix in
place, or hold. This case pins the judgment half of that pass on the same
seeded pairs `fidelity_meaning_judge` used — seven cleanups where meaning
survived exactly the transformations the old word-overlap floors punished, and
five where a point was dropped, a claim inverted, a fact misattributed, a
number changed, or a negation lost (plus the reorder trap and its kin). "ok" on
the faithful seven; anything but "ok" — a fix or a hold both count, since either
stops the unfaithful text shipping as-is — on the defective five.

Precision and recall are scored separately because the two failures cost Ellie
differently: a rubber-stamp reviewer ships an unfaithful note; a flag-everything
reviewer recreates the false holds this overhaul removed.

Run under q38-medium and q38-xhigh and `compare`; the review pass runs at
medium (`FIDELITY_REVIEW_EFFORT`) with xhigh reserved for the structural
re-ask, so medium matching xhigh's gate is the evidence that stands.
"""

import _common

DIMENSION = "verification"
SKILL = "vault-transcripts"
JUDGE = False
TIER = "standard"

SEED = "fidelity_meaning_judge"


def _seeded():
    return _common.harness.load_json(_common.harness.EVALS_ROOT / "expectations" / f"{SEED}.json")


def items():
    transcripts = _common.harness.load_skill("vault-transcripts")
    built = []
    for entry in _seeded()["items"]:
        payload = {
            "id": entry["id"],
            "rawTranscript": entry["item"]["sourceUtterance"],
            "note": entry["item"]["cleanedPassage"],
            "summary": None,
            "classification": {"recordingType": "memo", "title": "Seeded passage", "speakers": {}},
            "register": "spoken-to-written",
            "advisories": [f"utterance containment {entry['item']['containment']}"],
        }
        built.append(
            {
                "id": entry["id"],
                "messages": [
                    {"role": "system", "content": transcripts.REVIEW_FIX_SYSTEM},
                    {"role": "user", "content": _common.json.dumps(payload, ensure_ascii=False)},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": 4096,
                "key": {"defect": entry["defect"]},
                "why": entry["why"],
            }
        )
    return built


def score(item, content, record=None):
    if _common.truncated(record):
        return _common.truncation_failure(record)
    parsed = _common.parse_json(content)
    if not isinstance(parsed, dict):
        return _common.failure("reply was not a JSON object")
    verdict = parsed.get("verdict")
    contract = []
    if verdict not in {"ok", "fixed", "hold"}:
        contract.append(f"verdict {verdict!r} is outside the contract")
    if verdict == "fixed" and not str(parsed.get("body") or "").strip():
        contract.append("fixed with no body")
    if verdict == "hold" and not str(parsed.get("reason") or "").strip():
        contract.append("held with no reason")
    defect = item["key"]["defect"]
    escaped = bool(defect) and verdict == "ok"
    false_flag = not defect and verdict != "ok"
    gates = {
        "parsed": True,
        "contractClean": not contract,
        # A reviewer that signs off a planted defect is the failure that matters
        # most, because it reads as approval.
        "noDefectMissed": not escaped,
        # And its mirror: fixing or holding sound work recreates the false holds
        # this pass replaced.
        "noFalseFlags": not false_flag,
    }
    notes = list(contract)
    if escaped:
        notes.append(f"missed {defect}: {item['why']}")
    if false_flag:
        notes.append(f"false flag ({verdict}): {item['why']}")
    return {
        "ok": all(gates.values()),
        "gates": gates,
        "metrics": {
            "caught": int(bool(defect) and not escaped),
            "missed": int(escaped),
            "falseFlags": int(false_flag),
        },
        "notes": notes,
        "output": {"verdict": verdict},
    }

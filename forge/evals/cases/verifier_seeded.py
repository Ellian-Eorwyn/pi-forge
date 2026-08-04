#!/usr/bin/env python3
"""Can this model do the *other* job — reviewing, rather than producing?

Every skill in the repo splits work the same way: bulk on `chat`, review on
`think`. Asking whether a smaller model can take over bulk work only answers
half the question, and the half that matters less — a verifier that rubber-stamps
removes the quality net the whole design rests on, and one that flags everything
makes a human read every item by hand.

So the set is seeded. Ten extraction items over a document the model is shown,
five sound and five with a planted defect: a fabricated quote, a number the
document never gives, a reversed attribution, an inverted definition, and a
locator pointing at a page that does not exist. Precision and recall are scored
separately, because failing in either direction defeats the purpose differently.

One of the sound items is a trap. Its quote is verbatim but two adjacent clauses
appear in swapped order — a habit `skill-tuner` measured in small models and
explicitly does not treat as fabrication. Flagging it is a false positive, and
the item is there to catch a verifier that pattern-matches on surface mismatch
rather than reading.

The payload carries the document. `docs/service-split-handoff.md` §7.4 records
what happens without it: `personal-admin`'s first review pass approved a seeded
extraction that tripled a balance and invented a deadline, because the reviewer
was shown paraphrases and no source.
"""

import _common

DIMENSION = "verification"
SKILL = "forge_verify"
JUDGE = False

SEED = "verifier_seeded"

# The review call is the unit of work, so items are sent in packets rather than
# one at a time: a verifier that loses track of ids across a packet fails in a
# way per-item calls would never show. The assignment lives in the expectations
# file beside the answer key, and every packet mixes sound items with planted
# defects — a packet that is mostly defects lets a flag-everything verifier score
# well on it.
def _packets():
    return _seeded()["packets"]

# The bar is that no planted defect escapes. Elsewhere in this suite scoring is
# lenient because fixtures have more than one defensible answer; here they do
# not. There is no reading under which a fabricated quote or a reversed
# attribution is acceptable, so one getting through is the failure, and `recall`
# is reported as a metric to show how close it came.


def _seeded():
    return _common.harness.load_json(_common.harness.EVALS_ROOT / "expectations" / f"{SEED}.json")


def _document():
    schema_lib = _common.harness.load_lib("vault_schema")
    return schema_lib.split_frontmatter(_common.fixture(_seeded()["source"]).encode("utf-8"))["body"].strip()


def items():
    verify = _common.harness.load_lib("forge_verify")
    extraction = _common.harness.load_skill("literature-extraction")
    seeded = _seeded()
    document = _document()

    system = f"{extraction.VERIFY_SYSTEM}\n\n{verify.VERDICT_CONTRACT}"
    by_id = {entry["id"]: entry for entry in seeded["items"]}
    packets = _packets()
    unassigned = sorted(set(by_id) - {i for ids in packets.values() for i in ids})
    if unassigned:
        raise _common.harness.EvalError(f"seeded items in no packet: {', '.join(unassigned)}")
    built = []
    for packet_id, ids in packets.items():
        group = [by_id[identifier] for identifier in ids]
        user = _common.json.dumps(
            {
                "document": document[: extraction.MAX_DOCUMENT_CHARACTERS],
                "items": [{"id": entry["id"], **entry["item"]} for entry in group],
            },
            ensure_ascii=False,
        )
        built.append(
            {
                "id": packet_id,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "response_format": {"type": "json_object"},
                "max_tokens": 4096,
                "key": {entry["id"]: entry["defect"] for entry in group},
                "why": {entry["id"]: entry["why"] for entry in group},
            }
        )
    return built


def score(item, content, record=None):
    verify = _common.harness.load_lib("forge_verify")
    parsed = _common.parse_json(content)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("verdicts"), list):
        return _common.failure("reply did not carry a verdicts array")

    key = item["key"]
    verdicts = {}
    malformed = []
    for entry in parsed["verdicts"]:
        if not isinstance(entry, dict):
            malformed.append("a verdict was not an object")
            continue
        identifier = entry.get("id")
        verdict = entry.get("verdict")
        if verdict not in {verify.VERDICT_OK, verify.VERDICT_FLAG}:
            malformed.append(f"{identifier}: verdict {verdict!r} is outside the contract")
            continue
        if verdict == verify.VERDICT_FLAG and not str(entry.get("reason") or "").strip():
            malformed.append(f"{identifier}: flagged with no reason")
        verdicts[identifier] = verdict

    missing = sorted(set(key) - set(verdicts))
    extra = sorted(set(verdicts) - set(key))

    caught = [i for i, defect in key.items() if defect and verdicts.get(i) == verify.VERDICT_FLAG]
    escaped = [i for i, defect in key.items() if defect and verdicts.get(i) != verify.VERDICT_FLAG]
    false_flags = [i for i, defect in key.items() if not defect and verdicts.get(i) == verify.VERDICT_FLAG]
    defects = sum(1 for defect in key.values() if defect)
    sound = len(key) - defects

    flagged = len(caught) + len(false_flags)
    recall = len(caught) / defects if defects else 1.0
    precision = len(caught) / flagged if flagged else (1.0 if not defects else 0.0)

    gates = {
        "parsed": True,
        # What `_parse_verdicts` enforces in production. A packet that fails
        # these never reaches the quality question at all.
        "idCoverage": not missing and not extra,
        "contractClean": not malformed,
        # A verifier that lets a planted defect through is the failure mode that
        # matters most, because it reads as approval.
        "noDefectMissed": not escaped,
        # And its mirror: flagging sound work makes a human read every item by
        # hand, which is the cost the review tier exists to avoid.
        "noFalseFlags": not false_flags,
    }
    notes = list(malformed)
    if missing:
        notes.append(f"no verdict for: {', '.join(missing)}")
    if extra:
        notes.append(f"invented ids: {', '.join(extra)}")
    for identifier in escaped:
        notes.append(f"missed {key[identifier]} in {identifier}: {item['why'][identifier]}")
    for identifier in false_flags:
        notes.append(f"false flag on {identifier}: {item['why'][identifier]}")

    return {
        "ok": all(gates.values()),
        "gates": gates,
        "metrics": {
            "recall": round(recall, 4),
            "precision": round(precision, 4),
            "defectsCaught": len(caught),
            "defectsMissed": len(escaped),
            "falseFlags": len(false_flags),
            "soundItems": sound,
        },
        "notes": notes,
        "output": verdicts,
    }

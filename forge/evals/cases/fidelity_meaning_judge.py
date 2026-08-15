#!/usr/bin/env python3
"""Does the fidelity meaning-judge clear a faithful restructure and catch real loss?

`vault-transcripts` no longer holds a note just because a word-overlap floor —
length ratio, rare-word retention, the utterance locator — fired. A note that
trips those alone is *provisional*: its meaning is judged by the thinking model,
whose verdict, not the floor's score, decides it. This measures that yes/no
meaning judgment directly, at the utterance grain (`VERIFY_FIDELITY_SYSTEM`, the
spot check that still runs on notes that passed the floor). The provisional path
itself judges the *whole* note (`VERIFY_FIDELITY_NOTE_SYSTEM`) and can hand a real
loss to `chat` to repair, but it rests on the same premise this case pins down —
that `medium` reasoning suffices for a meaning yes/no — so the two stand or fall
together here; a dedicated whole-note case is a reasonable follow-up.

The set is seeded, the way `verifier_seeded` is. Twelve utterance/passage pairs:
seven where meaning was preserved through exactly the cleanup the floor punishes —
filler removed, a roundabout phrasing condensed, a distinctive word swapped for a
synonym, a lecture regrouped — and five where a point was dropped, a claim
inverted, a fact misattributed, a number changed, or a negation lost. `ok` on the
seven and `flag` on the five is the only correct reading; precision and recall are
scored separately because the two ways to be wrong fail Ellie differently. A
rubber-stamp judge clears the losses and a note reaches the vault unfaithful; a
flag-everything judge holds the faithful ones and the deferral buys nothing.

One faithful item (`fmj-05`) is the trap: a lecture passage whose three points are
reordered but all present. Flagging it is a false positive, and it is there to
catch a judge that pattern-matches on surface order rather than reading — the very
failure the deterministic locator makes on a regrouped source.

The premise under test is Ellie's: `medium` reasoning is enough for this yes/no
call, and `xhigh` (the note-redo budget) buys nothing here. Run it under
q38-medium and q38-xhigh and `compare`; matching gates on both is the evidence.
"""

import _common

DIMENSION = "verification"
SKILL = "vault-transcripts"
JUDGE = False
TIER = "standard"

SEED = "fidelity_meaning_judge"


def _seeded():
    return _common.harness.load_json(_common.harness.EVALS_ROOT / "expectations" / f"{SEED}.json")


def _packets():
    return _seeded()["packets"]


def items():
    verify = _common.harness.load_lib("forge_verify")
    transcripts = _common.harness.load_skill("vault-transcripts")
    seeded = _seeded()

    system = f"{transcripts.VERIFY_FIDELITY_SYSTEM}\n\n{verify.VERDICT_CONTRACT}"
    by_id = {entry["id"]: entry for entry in seeded["items"]}
    packets = _packets()
    unassigned = sorted(set(by_id) - {i for ids in packets.values() for i in ids})
    if unassigned:
        raise _common.harness.EvalError(f"seeded items in no packet: {', '.join(unassigned)}")

    built = []
    for packet_id, ids in packets.items():
        group = [by_id[identifier] for identifier in ids]
        user = _common.json.dumps(
            {"items": [{"id": entry["id"], **entry["item"]} for entry in group]},
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
    if _common.truncated(record):
        return _common.truncation_failure(record)
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

    # A defect is a lost/misstated/misattributed meaning that must be flagged; a
    # null defect is a faithful passage that must be cleared.
    caught = [i for i, defect in key.items() if defect and verdicts.get(i) == verify.VERDICT_FLAG]
    escaped = [i for i, defect in key.items() if defect and verdicts.get(i) != verify.VERDICT_FLAG]
    false_flags = [i for i, defect in key.items() if not defect and verdicts.get(i) == verify.VERDICT_FLAG]
    losses = sum(1 for defect in key.values() if defect)
    faithful = len(key) - losses

    flagged = len(caught) + len(false_flags)
    recall = len(caught) / losses if losses else 1.0
    precision = len(caught) / flagged if flagged else (1.0 if not losses else 0.0)

    gates = {
        "parsed": True,
        # What `_parse_verdicts` enforces in production; a packet failing these
        # never reaches the meaning question.
        "idCoverage": not missing and not extra,
        "contractClean": not malformed,
        # A judge that clears a real loss lets an unfaithful note reach the vault:
        # the failure the deferral must not introduce.
        "noLossMissed": not escaped,
        # Its mirror: flagging a faithful restructure holds a note that should
        # finish, so the floor's false positives just move to the judge.
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
            "lossesCaught": len(caught),
            "lossesMissed": len(escaped),
            "falseFlags": len(false_flags),
            "faithfulItems": faithful,
        },
        "notes": notes,
        "output": verdicts,
    }

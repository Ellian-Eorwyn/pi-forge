#!/usr/bin/env python3
"""Thinking-model review of work produced by the non-thinking model.

Bulk per-file work runs on the non-thinking backend because reasoning about
each file separately costs hundreds of hidden tokens and usually changes
nothing. "Usually" is the problem, so every batch is reviewed afterwards by the
thinking backend, and anything it flags is redone with reasoning.

The economics only work because review is batched: one call carries ~20 items,
so a run of 500 notes buys full coverage for ~25 thinking calls instead of 500.
Escalation is per-item and rare, and that is where the reasoning budget goes.

Order matters. Deterministic checks (schema validity, quote exactness, path
derivation) belong *before* this: they are free and exact, and running them
first means the thinking model spends its budget on judgment rather than on
catching malformed JSON.

The stages are deliberately contiguous. Interleaving a verification call
between two classification calls would swap the prompt prefix on both servers
every time; running all the bulk work, then all the review, keeps each server's
prefix cache warm.

Verification is advisory about *quality*, never about safety: it can flag and
escalate, and it can hand something to a human, but it never silently drops a
result.
"""

import json

import forge_llm
import run_state

DEFAULT_PACKET_SIZE = 20
DEFAULT_PACKET_CHARACTERS = 24000
VERDICT_OK = "ok"
VERDICT_FLAG = "flag"

VERDICT_CONTRACT = """Return exactly one JSON object and nothing else:
{"verdicts": [{"id": "<item id>", "verdict": "ok" | "flag", "reason": "<why, only when flagged>"}]}

Include one verdict for every id you were given, and no ids you were not given.
Flag an item only when it is actually wrong or unjustifiable on the evidence
shown. Do not flag an item merely because you would have phrased it differently.
"""


class VerificationError(RuntimeError):
    """The verifier could not produce a usable set of verdicts."""


def build_packets(items, packet_size=DEFAULT_PACKET_SIZE, budget_characters=DEFAULT_PACKET_CHARACTERS):
    """Group items into review packets bounded by count and serialized size."""
    packets = []
    current = []
    current_characters = 0
    for item in items:
        size = len(json.dumps(item, ensure_ascii=False))
        too_many = len(current) >= packet_size
        too_large = current and current_characters + size > budget_characters
        if too_many or too_large:
            packets.append(current)
            current, current_characters = [], 0
        current.append(item)
        current_characters += size
    if current:
        packets.append(current)
    return packets


def load_verdicts(journal_path):
    """Verdicts already recorded, so a resumed run does not re-review them."""
    if journal_path is None or not journal_path.exists():
        return {}
    rows, _warnings = run_state.read_jsonl_recover_tail(journal_path, repair=True)
    verdicts = {}
    for row in rows:
        identifier = row.get("id")
        if identifier is not None and "verdict" in row:
            verdicts[identifier] = row
    return verdicts


def _parse_verdicts(value, expected_ids):
    if not isinstance(value, dict) or not isinstance(value.get("verdicts"), list):
        raise VerificationError('response must be an object with a "verdicts" array')
    seen = {}
    for entry in value["verdicts"]:
        if not isinstance(entry, dict):
            raise VerificationError("every verdict must be an object")
        identifier = entry.get("id")
        verdict = entry.get("verdict")
        if identifier not in expected_ids:
            raise VerificationError(f"verdict for unknown id {identifier!r}")
        if verdict not in {VERDICT_OK, VERDICT_FLAG}:
            raise VerificationError(f"verdict for {identifier!r} must be {VERDICT_OK!r} or {VERDICT_FLAG!r}")
        seen[identifier] = {"verdict": verdict, "reason": (entry.get("reason") or "").strip()}
    missing = [identifier for identifier in expected_ids if identifier not in seen]
    if missing:
        raise VerificationError(f"missing verdicts for: {', '.join(str(value) for value in missing[:5])}")
    for identifier, entry in seen.items():
        if entry["verdict"] == VERDICT_FLAG and not entry["reason"]:
            raise VerificationError(f"flagged {identifier!r} without a reason")
    return seen


def verify_packets(
    service,
    system_prompt,
    items,
    journal_path=None,
    packet_size=DEFAULT_PACKET_SIZE,
    budget_characters=DEFAULT_PACKET_CHARACTERS,
    background=True,
    timeout=forge_llm.DEFAULT_TIMEOUT,
    progress=None,
):
    """Review every item and return ``{id: {"verdict", "reason"}}``.

    Items already present in the journal are returned from it rather than
    re-reviewed, so an interrupted run resumes where it stopped.
    """
    recorded = load_verdicts(journal_path)
    verdicts = {identifier: {"verdict": row["verdict"], "reason": row.get("reason", "")} for identifier, row in recorded.items()}
    pending = [item for item in items if item["id"] not in verdicts]
    packets = build_packets(pending, packet_size, budget_characters)
    for position, packet in enumerate(packets, start=1):
        expected = [item["id"] for item in packet]
        messages = [
            {"role": "system", "content": f"{system_prompt}\n\n{VERDICT_CONTRACT}"},
            {"role": "user", "content": json.dumps({"items": packet}, ensure_ascii=False)},
        ]
        try:
            parsed = _verify_one(service, messages, expected, background, timeout)
        except forge_llm.ChatError as error:
            raise VerificationError(str(error)) from error
        for identifier in expected:
            verdicts[identifier] = parsed[identifier]
            if journal_path is not None:
                run_state.append_jsonl_fsync(journal_path, {"at": run_state.utc_now(), "id": identifier, **parsed[identifier]})
        if progress:
            flagged = sum(1 for identifier in expected if parsed[identifier]["verdict"] == VERDICT_FLAG)
            progress(f"[verify {position}/{len(packets)}] {len(expected)} reviewed, {flagged} flagged")
    return verdicts


def _verify_one(service, messages, expected, background, timeout):
    """One packet, with a single corrective retry that shows the model its own
    contract violation."""
    try:
        value, _record = forge_llm.call_json(
            service, messages, background=background, timeout=timeout, task="verify", response_format={"type": "json_object"}
        )
        return _parse_verdicts(value, expected)
    except (VerificationError, forge_llm.ChatError) as error:
        repair = [
            *messages,
            {
                "role": "user",
                "content": f"That response was unusable: {error}. Return corrected JSON only, "
                f"with exactly one verdict for each of these ids: {json.dumps(expected)}",
            },
        ]
        value, _record = forge_llm.call_json(
            service, repair, background=background, timeout=timeout, task="verify-repair", response_format={"type": "json_object"}
        )
        return _parse_verdicts(value, expected)


def escalate(flagged, redo, journal_path=None, progress=None):
    """Redo each flagged item on the thinking model.

    ``redo(item, reason)`` returns the corrected result, or raises to leave the
    item for a human. The escalated result always wins over the original: it was
    produced with reasoning, by the stronger configuration, knowing what the
    reviewer objected to.
    """
    results = {}
    for position, (item, reason) in enumerate(flagged, start=1):
        identifier = item["id"]
        record = {"at": run_state.utc_now(), "id": identifier, "reason": reason}
        try:
            results[identifier] = {"ok": True, "value": redo(item, reason)}
            record["escalated"] = True
        except Exception as error:  # noqa: BLE001 - any failure becomes a human review item
            results[identifier] = {"ok": False, "detail": f"{type(error).__name__}: {error}"}
            record["escalated"] = False
            record["detail"] = results[identifier]["detail"]
        if journal_path is not None:
            run_state.append_jsonl_fsync(journal_path, record)
        if progress:
            state = "redone" if results[identifier]["ok"] else "needs review"
            progress(f"[escalate {position}/{len(flagged)}] {identifier}: {state}")
    return results


def summarize(verdicts, escalations=None):
    """Counts for the run report."""
    flagged = [identifier for identifier, entry in verdicts.items() if entry["verdict"] == VERDICT_FLAG]
    escalations = escalations or {}
    return {
        "verified": len(verdicts),
        "ok": len(verdicts) - len(flagged),
        "flagged": len(flagged),
        "escalated": sum(1 for entry in escalations.values() if entry["ok"]),
        "needsReview": sum(1 for entry in escalations.values() if not entry["ok"]),
        "flaggedIds": flagged,
    }

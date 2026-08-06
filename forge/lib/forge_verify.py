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

The evidence shown is the only evidence. What you happen to know about the
subject is not part of it: an item that matches what you would have expected is
not thereby supported, and one that surprises you is not thereby wrong. Judge
each item against what it was given, and where that is not enough to tell, flag
it and say so rather than deciding from memory.
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


def same_backend(left, right):
    """Whether two services are the same weights answering the same way.

    Compared on endpoint and model rather than on the service name, because the
    names are a routing concept and the backend is what decides whether a review
    is independent. Two names can point at one server, and on this deployment
    they usually do.
    """
    if not left or not right:
        return False
    return (
        forge_llm.normalize_base_url(left.get("url")) == forge_llm.normalize_base_url(right.get("url"))
        and left.get("model") == right.get("model")
    )


def _producer_for(item_id, produced_by):
    """The service that made one item, from either a batch-wide service or a map."""
    if not produced_by:
        return None
    if isinstance(produced_by, dict) and "url" in produced_by:
        return produced_by
    if isinstance(produced_by, dict):
        return produced_by.get(item_id)
    return None


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
    produced_by=None,
):
    """Review every item and return ``{id: {"verdict", "reason", "independent"}}``.

    Items already present in the journal are returned from it rather than
    re-reviewed, so an interrupted run resumes where it stopped.

    ``produced_by`` is the service that made the items — one service for the
    batch, or ``{item id: service}`` when a stage routed per item. Where it
    matches the verifier, the item is marked ``independent: False``.

    That mark exists because stage routing made self-review reachable. Every
    skill verifies on ``think``; the moment a bulk stage is *also* routed there,
    the reviewer is the model that wrote the thing, and an "ok" from it is not
    evidence. A flag still is — a reasoning pass over its own output can catch a
    contract violation, and anything it flags is escalated as before. So the
    verdict is kept and only its standing changes: flags act, "ok" stops reading
    as approval. This is the same rule as "an unreachable verifier must never
    read as approval", applied to a verifier that is present but not impartial.
    """
    recorded = load_verdicts(journal_path)
    verdicts = {
        identifier: {
            "verdict": row["verdict"],
            "reason": row.get("reason", ""),
            "independent": row.get("independent", True),
        }
        for identifier, row in recorded.items()
    }
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
            independent = not same_backend(service, _producer_for(identifier, produced_by))
            verdicts[identifier] = {**parsed[identifier], "independent": independent}
            if journal_path is not None:
                run_state.append_jsonl_fsync(
                    journal_path, {"at": run_state.utc_now(), "id": identifier, **verdicts[identifier]}
                )
        if progress:
            flagged = sum(1 for identifier in expected if parsed[identifier]["verdict"] == VERDICT_FLAG)
            unverified = sum(1 for identifier in expected if not verdicts[identifier]["independent"])
            note = f", {unverified} not independently reviewed" if unverified else ""
            progress(f"[verify {position}/{len(packets)}] {len(expected)} reviewed, {flagged} flagged{note}")
    return verdicts


def independence_warning(verdicts):
    """One line for the run report when the reviewer reviewed its own work.

    Returns ``None`` when every clean verdict came from a different backend than
    produced the item, which is the ordinary case and needs no comment.
    """
    unverified = [
        identifier
        for identifier, verdict in (verdicts or {}).items()
        if not verdict.get("independent", True) and verdict.get("verdict") == VERDICT_OK
    ]
    if not unverified:
        return None
    return (
        f"{len(unverified)} item(s) were reviewed by the same model that produced them, so their "
        f"clean verdicts are not independent evidence. Anything flagged was still escalated. "
        f"Route the producing stage off the verifier's service to restore the split."
    )


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


def load_escalations(journal_path):
    """Escalations already attempted, so a resumed run does not redo them."""
    if journal_path is None or not journal_path.exists():
        return {}
    rows, _warnings = run_state.read_jsonl_recover_tail(journal_path, repair=True)
    attempts = {}
    for row in rows:
        identifier = row.get("id")
        if identifier is not None and "escalated" in row:
            attempts[identifier] = row
    return attempts


def escalate(flagged, redo, journal_path=None, progress=None):
    """Redo each flagged item on the thinking model.

    ``redo(item, reason)`` returns the corrected result, or raises to leave the
    item for a human. The escalated result always wins over the original: it was
    produced with reasoning, by the stronger configuration, knowing what the
    reviewer objected to.

    A flag verdict stays in the journal forever, so an item already escalated is
    returned from the journal rather than redone. Without this every resumed run
    pays a fresh reasoning-model extraction for every item ever flagged. Resumed
    outcomes carry ``resumed: True``; a caller that commits results must skip
    them, because the corrected value was committed when it was first produced.
    """
    attempted = load_escalations(journal_path)
    results = {}
    pending = []
    for item, reason in flagged:
        row = attempted.get(item["id"])
        if row is None:
            pending.append((item, reason))
            continue
        results[item["id"]] = {"ok": bool(row.get("escalated")), "detail": row.get("detail", ""), "resumed": True}
        if progress:
            progress(f"[escalate] {item['id']}: already escalated, keeping the recorded outcome")
    for position, (item, reason) in enumerate(pending, start=1):
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
            progress(f"[escalate {position}/{len(pending)}] {identifier}: {state}")
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

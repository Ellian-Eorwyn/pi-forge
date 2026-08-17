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
DEFAULT_REPAIR_ROUNDS = 2
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
    reasoning_effort=None,
):
    """Review every item and return ``{id: {"verdict", "reason", "independent"}}``.

    Items already present in the journal are returned from it rather than
    re-reviewed, so an interrupted run resumes where it stopped.

    ``reasoning_effort`` sets the review's reasoning depth; ``None`` leaves the
    service at its own default. A caller that wants a yes/no judgment thought
    through explicitly — the fidelity meaning-judge, say — passes ``"medium"``.

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
            parsed = _verify_one(service, messages, expected, background, timeout, reasoning_effort)
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


def _verify_one(service, messages, expected, background, timeout, reasoning_effort=None):
    """One packet, with a single corrective retry that shows the model its own
    contract violation."""
    try:
        value, _record = forge_llm.call_json(
            service, messages, background=background, timeout=timeout, task="verify",
            response_format={"type": "json_object"}, reasoning_effort=reasoning_effort,
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
            service, repair, background=background, timeout=timeout, task="verify-repair",
            response_format={"type": "json_object"}, reasoning_effort=reasoning_effort,
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


def repair_until_verified(
    items,
    verdicts,
    *,
    fix,
    reverify,
    is_serious=None,
    fingerprint=None,
    max_rounds=DEFAULT_REPAIR_ROUNDS,
    progress=None,
):
    """Diagnose → fix on the bulk tier → re-verify on the thinking tier, looping
    until each item passes or a bounded number of rounds is spent.

    This generalizes the single-round fix-and-re-verify path (``vault-transcripts``
    already does exactly one round of it for fidelity). The thinking model's
    objection is handed to ``fix`` — which the caller runs on the *non-thinking*
    tier — and the result is re-checked by ``reverify`` on the thinking tier, with
    the producer declared so the second verdict is independent (``chat`` fixed it,
    ``think`` approves it, and neither blesses its own edit). A still-flagged item
    is fixed again against the *new* objection, up to ``max_rounds``.

    ``fix``/``reverify`` are callbacks so the caller owns which tier, prompt, and
    effort each uses; this function owns only the loop, its termination, and the
    independence rule. It carries no journal: resume runs through the journals the
    caller's ``reverify`` (and its first-pass diagnosis) already keep — a settled
    round returns its recorded verdict without a model call, and the ``fix`` is
    redone from the item's current on-disk state, which is safe because a grounded
    restore is idempotent. A loop-level "already passed" journal was deliberately
    *not* added: it would let a resumed pass point at an artifact a re-run of an
    earlier stage had since overwritten.

    Arguments:
      ``items``    — the diagnosed payloads, each a dict with an ``id``.
      ``verdicts`` — the caller's first-pass result
                     ``{id: {"verdict","reason","independent"}}`` (what
                     ``verify_packets`` returns). An ``ok`` item that was
                     independently reviewed passes at round 0 with no fix.
      ``fix(item, objection, round_idx)`` → the corrected payload to re-verify
                     (same ``id``), or ``None`` to hand the item to a human.
      ``reverify(fixed_items, round_idx)`` → verdicts for the batch.
      ``is_serious(item, verdict)`` — optional; ``True`` sends a flag straight to a
                     human with no fix attempt (a structural or unsafe failure, not
                     a quality one). "Advisory about quality, never about safety."
      ``fingerprint(item)`` — optional; guards a fix that makes no progress. When
                     a round's fix leaves the fingerprint unchanged from the item
                     it was handed, the item is held rather than re-reviewed to no
                     effect, so it must apply to a diagnosed payload as well as a
                     fixed one (default: canonical JSON of the payload).

    Returns ``{id: {"status": "passed"|"held", "rounds", "reason", "independent",
    "item"?}}``. ``item`` is the last corrected payload, present only for a pass
    that took a fix (a caller that must commit it reads it there); a round-0 pass
    and every hold omit it.
    """
    if fingerprint is None:
        def fingerprint(payload):
            return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def passed(rounds, independent, item):
        result = {"status": "passed", "rounds": rounds, "reason": "", "independent": independent}
        if item is not None:
            result["item"] = item
        return result

    def held(rounds, reason, independent):
        return {"status": "held", "rounds": rounds, "reason": reason, "independent": independent}

    by_id = {item["id"]: item for item in items}
    results = {}
    pending = []  # (id, objection) still needing work

    for item in items:
        identifier = item["id"]
        verdict = verdicts.get(identifier)
        if verdict is None:
            # An item the caller never verified must not be invented into a pass.
            results[identifier] = held(0, "no verdict was produced for this item", True)
        elif verdict["verdict"] == VERDICT_OK:
            if verdict.get("independent", True):
                results[identifier] = passed(0, True, None)
            else:
                results[identifier] = held(
                    0, "reviewed by the model that produced it, so a clean verdict is not independent evidence", False
                )
        elif is_serious and is_serious(item, verdict):
            results[identifier] = held(0, verdict["reason"], verdict.get("independent", True))
        else:
            pending.append((identifier, verdict["reason"]))

    # A fix that leaves the note unchanged has made no progress: re-verifying it
    # would only reproduce the same objection. The mark starts from each item's
    # diagnosed state, so even a first round that changes nothing is caught before
    # a wasted re-review — which is why ``fingerprint`` must apply to the diagnosed
    # payload as well as a fixed one (they are the same kind of payload).
    current_fp = {identifier: fingerprint(by_id[identifier]) for identifier, _ in pending}
    for round_idx in range(1, max_rounds + 1):
        if not pending:
            break
        fixed_items = []
        objections = {}
        for identifier, objection in pending:
            try:
                fixed = fix(by_id[identifier], objection, round_idx)
            except Exception as error:  # noqa: BLE001 - any failure becomes a human review item
                results[identifier] = held(round_idx, f"{type(error).__name__}: {error}", True)
                continue
            if fixed is None:
                results[identifier] = held(round_idx, objection, True)
                continue
            mark = fingerprint(fixed)
            if mark == current_fp[identifier]:  # the fix changed nothing
                results[identifier] = held(round_idx, objection, True)
                continue
            current_fp[identifier] = mark
            by_id[identifier] = fixed  # the next round fixes the latest state, not the original
            fixed_items.append(fixed)
            objections[identifier] = objection

        if not fixed_items:
            pending = []
            break
        try:
            round_verdicts = reverify(fixed_items, round_idx)
        except Exception as error:  # noqa: BLE001 - a failed review holds the whole round for a human
            detail = f"{type(error).__name__}: {error}"
            for fixed in fixed_items:
                identifier = fixed["id"]
                results[identifier] = held(
                    round_idx, f"the re-review failed ({detail}); the objection stands: {objections[identifier]}", True
                )
            pending = []
            break

        next_pending = []
        for fixed in fixed_items:
            identifier = fixed["id"]
            verdict = round_verdicts.get(identifier)
            if verdict is None:
                results[identifier] = held(round_idx, "the re-review returned no verdict for this item", True)
            elif verdict["verdict"] == VERDICT_OK and verdict.get("independent", True):
                results[identifier] = passed(round_idx, True, by_id[identifier])
            elif verdict["verdict"] == VERDICT_OK:
                results[identifier] = held(
                    round_idx, "the fix was reviewed by the model that produced it, so its clean verdict is not independent", False
                )
            else:
                next_pending.append((identifier, verdict["reason"]))
            if progress and identifier in results:
                progress(f"[repair {round_idx}/{max_rounds}] {identifier}: {results[identifier]['status']}")
        pending = next_pending

    # Anything still flagged after the last round is held with its final objection.
    for identifier, objection in pending:
        results[identifier] = held(max_rounds, objection, True)

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

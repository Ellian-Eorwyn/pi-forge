#!/usr/bin/env python3
"""Read a completed deep-research run into the shape a vault note can be made of.

`web-research deep` leaves a run directory holding a claim register, the evidence
items behind each claim, and an index of the sources those quotes came from. Two
skills now want to turn that into notes -- `vault-connections import-run --notes`
renders it into fixed sections, and `vault-compose` composes from it -- and both
need the same three joins: claim to evidence, evidence to quote, quote to URL.

The joins live here rather than in either skill because they encode facts about
the *run format*, not about notes. A change to what `web-research` writes should
break one file.

Nothing here fetches anything. A quote is bytes the research run already read and
recorded, which is what makes it citable at all.
"""

import json
from pathlib import Path

# A claim with no quote behind it is a claim the run asserted and did not show.
# It can still be worth reading, so it is kept and marked rather than dropped.
UNSUPPORTED = "unsupported"


def read_jsonl(path):
    """Rows from a JSONL file, skipping anything unparseable."""
    path = Path(path)
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def deep_run_sources(run_directory):
    """Source id -> {url, title}, so a note can say where a quote came from."""
    path = Path(run_directory) / "source_index.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    sources = {}
    for source in payload.get("sources") or []:
        source_id = source.get("sourceId")
        if source_id:
            sources[source_id] = {
                "url": source.get("finalUrl") or source.get("sourceUrl") or "",
                "title": source.get("title") or "",
            }
    return sources


def deep_run_records(run_directory):
    """The run's claims and evidence, in the flat record shape both skills read."""
    run_directory = Path(run_directory)
    records = []
    for row in read_jsonl(run_directory / "claim_register.jsonl"):
        if not row.get("claimId"):
            continue
        records.append(
            {
                "id": row["claimId"],
                "kind": "claim",
                "text": row.get("text") or "",
                "quote": "",
                "sourceIds": row.get("sourceIds") or [],
                "evidenceIds": row.get("evidenceIds") or [],
                "source": "",
                "confidence": row.get("confidence") or "",
                # The source run's own reviewer already judged this. Its verdict
                # travels with the record so a note can leave a doubted claim out
                # and say that it did.
                "verification": row.get("verification") or None,
            }
        )
    for row in read_jsonl(run_directory / "evidence_items.jsonl"):
        if not row.get("evidenceId"):
            continue
        records.append(
            {
                "id": row["evidenceId"],
                "kind": "evidence",
                "text": row.get("text") or "",
                "quote": row.get("directQuote") or "",
                "sourceIds": [value for value in [row.get("sourceId")] if value],
                "source": "",
                "confidence": row.get("confidence") or "",
                "verification": row.get("verification") or None,
            }
        )
    return records


def deep_run_claims(records):
    claims = {record["id"]: record for record in records if record.get("kind") == "claim"}
    evidence = {record["id"]: record for record in records if record.get("kind") == "evidence"}
    return claims, evidence


def flagged_ids(records):
    """Records the source run's own reviewer rejected."""
    return {
        record["id"]
        for record in records
        if (record.get("verification") or {}).get("verdict") == "flag"
    }


def claim_detail(claim, evidence, sources, flagged=frozenset()):
    """One claim with the quotes and URLs behind it, for a prompt or a note.

    Evidence the source run's reviewer rejected is dropped here rather than
    carried into a note. A flag on an evidence item is a finding about the claim
    that cites it: the claim itself may have passed review only because the
    reviewer was judging its wording, not the extraction underneath it.
    """
    quotes = []
    dropped = []
    for evidence_id in claim.get("evidenceIds") or []:
        item = evidence.get(evidence_id)
        if not item:
            continue
        if evidence_id in flagged:
            dropped.append(evidence_id)
            continue
        quote = (item.get("quote") or "").strip()
        source_id = (item.get("sourceIds") or [None])[0]
        quotes.append(
            {
                "evidenceId": evidence_id,
                "quote": quote or (item.get("text") or "").strip(),
                "exact": bool(quote),
                "url": sources.get(source_id, {}).get("url", ""),
                "sourceId": source_id or "",
            }
        )
    return {
        "claimId": claim["id"],
        "text": claim.get("text") or "",
        "quotes": quotes,
        "droppedEvidenceIds": dropped,
    }


def claim_source_units(run_directory, limit=None, include_unsupported=False):
    """A deep run's claims as source units a note can be composed from.

    One unit per claim, its ``text`` being the claim followed by the quotes that
    support it. That pairing is what makes the grounding check meaningful for
    research: a composed note may use a claim's own wording *and* the wording of
    the evidence under it, and nothing else.

    A unit carries the URL of its first quote, which is the link the note is then
    allowed to cite. A claim whose every quote was rejected by the source run's
    reviewer carries no URL and, by default, is not offered at all -- a claim with
    nothing behind it is exactly what a research note should not repeat.

    Returns ``(units, warnings)`` where a unit is the dict `vault_compose.source_unit`
    takes, so the caller decides the ordering and ids.
    """
    run_directory = Path(run_directory)
    records = deep_run_records(run_directory)
    if not records:
        raise ValueError(f"no claim register or evidence items in {run_directory}")
    sources = deep_run_sources(run_directory)
    claims, evidence = deep_run_claims(records)
    flagged = flagged_ids(records)
    units = []
    warnings = []
    for claim_id, claim in sorted(claims.items()):
        if claim_id in flagged:
            warnings.append(f"{claim_id}: dropped, the research run's own reviewer flagged it")
            continue
        detail = claim_detail(claim, evidence, sources, flagged)
        if detail["droppedEvidenceIds"]:
            warnings.append(
                f"{claim_id}: dropped {len(detail['droppedEvidenceIds'])} quote(s) the research run flagged"
            )
        if not detail["quotes"]:
            if not include_unsupported:
                warnings.append(f"{claim_id}: dropped, no quote survives to support it")
                continue
            warnings.append(f"{claim_id}: kept with no supporting quote")
        body = [detail["text"]]
        for quote in detail["quotes"]:
            body.append("")
            body.append(f'"{quote["quote"]}"' if quote["exact"] else quote["quote"])
            if quote["url"]:
                body.append(quote["url"])
        url = next((quote["url"] for quote in detail["quotes"] if quote["url"]), None)
        title = next(
            (sources.get(quote["sourceId"], {}).get("title") for quote in detail["quotes"] if quote["sourceId"]),
            None,
        )
        units.append(
            {
                "kind": "web-claim",
                "label": title or claim_id,
                "text": "\n".join(body).strip(),
                "url": url,
                "origin": {
                    "runDirectory": str(run_directory),
                    "claimId": claim_id,
                    "evidenceIds": [quote["evidenceId"] for quote in detail["quotes"]],
                    "sourceIds": [quote["sourceId"] for quote in detail["quotes"] if quote["sourceId"]],
                    "confidence": claim.get("confidence") or "",
                },
            }
        )
        if limit is not None and len(units) >= limit:
            break
    if not units:
        raise ValueError(
            f"every claim in {run_directory} was dropped; "
            "the run's own reviewer flagged them or nothing survives to support them"
        )
    return units, warnings


__all__ = [
    "UNSUPPORTED",
    "claim_detail",
    "claim_source_units",
    "deep_run_claims",
    "deep_run_records",
    "deep_run_sources",
    "flagged_ids",
    "read_jsonl",
]

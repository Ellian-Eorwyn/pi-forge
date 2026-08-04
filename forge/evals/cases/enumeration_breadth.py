#!/usr/bin/env python3
"""The failure this repo measured as the biggest cost of moving off a thinking model.

`docs/service-split-handoff.md` §2.1: same corpus, same contract, same
temperature. The thinking model returned 17 items across 8 item types; the
non-thinking one returned 17 items across **4**, settling for claims, findings,
methods, and limitations and never considering definitions, data sources,
variables, populations, technologies, policies, cited works, or research gaps.
Volume was identical. Breadth was not. Appending four lines that name the
categories a model skips took it to 41 items across 12.

So the headline metric here is not how many items came back, it is how many of
the fifteen item types the document could support actually appear. A model that
returns thirty claims and nothing else has failed this case while looking busy.

`quote_violations` runs alongside, because breadth bought with invented quotes is
not breadth. In the measurement above, quote fidelity was already identical
across tiers — if that stops being true for a model, this is where it shows.
"""

import _common

DIMENSION = "enumeration"
SKILL = "literature-extraction"
JUDGE = False

FIXTURES = [
    "report-calnext",
    "report-datacenter",
    "report-arpae-q6",
    "report-arpae-q8",
    "report-claude-dc",
    "report-gemini-dc",
    "report-claude-work",
    "report-claude-work2",
]


def _skill():
    return _common.harness.load_skill("literature-extraction")


def _text(fixture_id):
    schema_lib = _common.harness.load_lib("vault_schema")
    return schema_lib.split_frontmatter(_common.fixture(fixture_id).encode("utf-8"))["body"].strip()


def items():
    extraction = _skill()
    system = extraction.extraction_system_prompt(extraction.ITEM_TYPES)
    built = []
    for fixture_id in FIXTURES:
        text = _text(fixture_id)
        # One chunk per call, as the skill does. The frozen excerpts are sized
        # to sit inside the task model's 32k ceiling as well as the chat slot,
        # so a small model is measured on the task rather than on its context.
        for index, chunk in enumerate(extraction.document_chunks(text), start=1):
            built.append(
                {
                    "id": fixture_id if index == 1 else f"{fixture_id}#{index}",
                    "messages": [
                        {"role": "system", "content": system},
                        {
                            "role": "user",
                            "content": extraction.extraction_user_prompt(fixture_id, "", chunk),
                        },
                    ],
                    # Breadth is the point, so the budget has to be generous
                    # enough that running out of it is a real finding rather
                    # than the harness capping the answer. The baseline hit
                    # 8192 mid-array on the CalNEXT excerpt.
                    "max_tokens": 12288,
                    "source": chunk,
                }
            )
    return built


def score(item, content, record=None):
    extraction = _skill()
    truncated = _common.truncated(record)
    parsed = _common.parse_json(content)
    if not isinstance(parsed, list):
        # Told apart deliberately. A model that filled its whole output budget
        # and got cut off mid-array was enumerating enthusiastically, which is
        # the opposite of the failure this case looks for — and `literature-
        # extraction` handles it in production by splitting and retrying.
        if truncated:
            return _common.truncation_failure(record)
        return _common.failure("reply was not a JSON array of items")

    valid = [entry for entry in parsed if isinstance(entry, dict) and entry.get("item_type") in extraction.ITEM_TYPE_SET]
    unknown = sorted({str(entry.get("item_type")) for entry in parsed if isinstance(entry, dict)} - extraction.ITEM_TYPE_SET)
    covered = sorted({entry["item_type"] for entry in valid})
    violations = extraction.quote_violations(valid, item["source"])
    quoted = [entry for entry in valid if entry.get("direct_quotes")]

    gates = {
        "parsed": True,
        "notTruncated": not truncated,
        "itemTypesKnown": not unknown,
        "quotesVerbatim": not violations,
        # Four types is what the pre-fix non-thinking run produced. Clearing it
        # is the floor this case exists to test, not a good score.
        "breadthAboveFloor": len(covered) > 4,
        "extractedSomething": bool(valid),
    }
    notes = []
    if unknown:
        notes.append(f"item types outside the contract: {', '.join(unknown[:8])}")
    if violations:
        notes.append(f"{len(violations)} quote(s) not found in the document: {violations[0][:120]}")
    notes.append(f"covered {len(covered)}/{len(extraction.ITEM_TYPES)} types: {', '.join(covered)}")

    return {
        "ok": all(gates.values()),
        "gates": gates,
        "metrics": {
            "itemTypesCovered": len(covered),
            "items": len(valid),
            "quotedItems": len(quoted),
            "fabricatedQuotes": len(violations),
        },
        "notes": notes,
        "output": {"typesCovered": covered, "items": len(valid)},
    }

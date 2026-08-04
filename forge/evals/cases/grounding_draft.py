#!/usr/bin/env python3
"""Does the model write things the source did not say?

`vault-capture`'s draft stage asks for a note in the speaker's own words, and
`invented_specifics` checks the two things that can be checked: names and links.
Most of a draft is a rewording and cannot be verified — but a capitalised name
mid-sentence with no root in the braindump, or a URL that was never given, was
either in the source or the model made it up.

Most of these braindumps name no person at all, so *any* name in a draft is
provably invented. One exception is deliberate: `braindump-weather` is a written
research note carrying real URLs, which makes it the case where a model can cite
correctly and the check has to let it — a fixture where the allowed set is
non-empty stops the link check from being trivially satisfied by never linking.

The stage is given no `outsideSources`, which is the only thing that legitimately
widens the allowed set beyond the braindump itself. A clean run here is a model
that stayed inside its material.

Note plans are hand-written rather than taken from the split stage, so a bad
split cannot show up as a fabricating drafter.
"""

import _common

DIMENSION = "grounding"
SKILL = "vault-capture"
JUDGE = True

PLANS = {
    "braindump-brainstorm": {
        "kind": "plan",
        "title": "Browser As Primary Interaction Point",
        "gist": "Rework the manifest page into the primary browser for the app.",
        "covers": ["manifest becomes the Browser", "column behaviour", "source details panel"],
    },
    "raw-asr-piforge": {
        "kind": "idea",
        "title": "Research Extension For Qualitative Coding",
        "gist": "A research-specific extension that helps with qualitative coding of interviews and documents.",
        "covers": ["what it would code", "why interviews are the primary case", "uncertainty about the approach"],
    },
    "braindump-todo": {
        "kind": "task",
        "title": "Things To Get Working Today",
        "gist": "A day's worth of jobs, starting with the bird-listening app on the mini PC.",
        "covers": ["bird listening app", "microphone setup", "the rest of the list"],
    },
    "braindump-merge": {
        "kind": "idea",
        "title": "Merge Same-Day Transcripts Before Cleanup",
        "gist": "Combine transcripts that arrive the same day into one note rather than a dozen fragments.",
        "covers": ["why the fragments are useless separately", "timestamp separators", "cleanup runs on the merged note"],
    },
    "braindump-voice": {
        "kind": "idea",
        "title": "Move Voice-Note Transcription Onto Linux",
        "gist": "Make the phone-to-transcript pipeline independent of any one vendor's tooling.",
        "covers": ["how it works now", "what should change", "where the files should land"],
    },
    "braindump-speakers": {
        "kind": "task",
        "title": "Defects Found While Testing Account Creation",
        "gist": "A walkthrough of separate problems found while exercising the site as a new user.",
        "covers": ["the missing confirmation link", "what happens next", "the other problems found"],
    },
    "braindump-weather": {
        "kind": "reference",
        "title": "Local Data Access For The Weather Station",
        "gist": "Ways to read the weather station without depending on the vendor's cloud.",
        "covers": ["the RF capture option", "what each option gets you", "what each option misses"],
    },
    "braindump-requirements": {
        "kind": "plan",
        "title": "Vault Manager Feature Requirements",
        "gist": "Features to add to the vault manager, starting with whole-note transformation.",
        "covers": ["reorganising notes without losing material", "transcript handling", "the rest of the features"],
    },
}


def _skill():
    return _common.harness.load_skill("vault-capture")


def _text(fixture_id):
    schema_lib = _common.harness.load_lib("vault_schema")
    return schema_lib.split_frontmatter(_common.fixture(fixture_id).encode("utf-8"))["body"].strip()


def items():
    capture = _skill()
    built = []
    for fixture_id, note in PLANS.items():
        source = _text(fixture_id)
        payload = capture.draft_payload({"text": source, "label": fixture_id}, note)
        built.append(
            {
                "id": fixture_id,
                "messages": [
                    # No voice segment and no profile: both splice per-vault
                    # configuration into the prompt, and a prompt that changes
                    # when a preferences note is edited is not a benchmark.
                    {"role": "system", "content": capture.draft_system_prompt()},
                    {"role": "user", "content": _common.json.dumps(payload, ensure_ascii=False)},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": 3000,
                "source": source,
            }
        )
    return built


def score(item, content, record=None):
    capture = _skill()
    parsed = _common.parse_json(content)
    if not isinstance(parsed, dict):
        return _common.failure("reply was not a JSON object")
    body = parsed.get("body")
    if not isinstance(body, str) or not body.strip():
        return _common.failure("reply carried no body", {"parsed": True, "noInventedNames": False})

    source = item["source"]
    invented = capture.invented_specifics(source, body, allowed_urls=())
    coverage = capture.coverage_ratio(source, [body])

    gates = {
        "parsed": True,
        # The two that hold a note back in production.
        "noInventedNames": not invented["names"],
        "noInventedLinks": not invented["links"],
        # A drafter that writes its own H1 or frontmatter is fighting the
        # assembler, which owns both.
        "noOwnHeading": not body.lstrip().startswith("# ") and not body.lstrip().startswith("---"),
        "coverageHeld": coverage >= capture.COVERAGE_WARN_RATIO,
    }
    notes = []
    if invented["names"]:
        notes.append(f"names not in the braindump: {', '.join(invented['names'][:8])}")
    if invented["links"]:
        notes.append(f"links not in the braindump: {', '.join(invented['links'][:5])}")
    if invented["uncertain_names"]:
        # Sentence-opening capitals: reported, never held, because a real
        # sentence can legitimately start with a word that looks like a name.
        notes.append(f"possible names (sentence-initial, advisory): {', '.join(invented['uncertain_names'][:5])}")
    if invented["numbers"]:
        notes.append(f"figures not in the braindump (advisory): {', '.join(invented['numbers'][:5])}")
    if coverage < capture.COVERAGE_WARN_RATIO:
        notes.append(f"coverage {coverage:.2f} below {capture.COVERAGE_WARN_RATIO}: the draft dropped most of the dump")

    return {
        "ok": all(gates.values()),
        "gates": gates,
        "metrics": {
            "inventedNames": len(invented["names"]),
            "inventedLinks": len(invented["links"]),
            "inventedNumbers": len(invented["numbers"]),
            "coverage": round(coverage, 4),
            "bodyWords": _common.word_count(body),
        },
        "notes": notes,
        "output": {"title": parsed.get("title"), "body": body},
    }


def judge_context(item_id):
    plan = PLANS[item_id]
    return {
        "instruction": (
            f"Draft the {plan['kind']} note '{plan['title']}' from this braindump, in the speaker's own words. "
            "Neither braindump names a person or carries a URL, so any name or link in the draft is invented."
        ),
        "source": _text(item_id),
        "reference": None,
    }

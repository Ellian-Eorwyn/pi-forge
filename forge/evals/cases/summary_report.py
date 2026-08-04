#!/usr/bin/env python3
"""The same 120-word paragraph, over a document forty times longer.

`summary-transcript` summarizes a few hundred words of speech. This summarizes
an institutional report excerpt of several thousand — the same contract and the
same gate, with the compression ratio as the only thing that changed. A model
that holds the shape on a memo and loses it here is telling you where its useful
input length actually ends, which is not the same number as its context window.
"""

import _common

DIMENSION = "summarization"
SKILL = "vault-transcripts"
JUDGE = True

FIXTURES = {
    "report-calnext": "CalNEXT 2025 HVAC Technology Priority Map",
    "report-datacenter": "Datacenter Cooling Market Study — Preliminary Findings",
    "report-arpae-q6": "ARPA-E Coolerchips Q6 Report",
    "report-arpae-q8": "ARPA-E Coolerchips Q8 Report",
    "report-claude-dc": "Data Centers — Research Report",
    "report-gemini-dc": "Data Centers — Research Report (second)",
    "report-claude-work": "Work — Research Report",
    "report-claude-work2": "Work — Research Report (second)",
}


def _skill():
    return _common.harness.load_skill("vault-transcripts")


def _text(fixture_id):
    schema_lib = _common.harness.load_lib("vault_schema")
    return schema_lib.split_frontmatter(_common.fixture(fixture_id).encode("utf-8"))["body"].strip()


def items():
    transcripts = _skill()
    built = []
    for fixture_id, title in FIXTURES.items():
        body = _text(fixture_id)
        payload = {
            # `source` role rather than `owner`: this is somebody else's
            # document, and the prompt treats the two differently.
            "recordingType": "meeting",
            "materialRole": "source",
            "title": title,
            "cleaned": body[: transcripts.SUMMARY_INPUT_CHARS],
        }
        built.append(
            {
                "id": fixture_id,
                "messages": [
                    {"role": "system", "content": transcripts.SUMMARY_SYSTEM},
                    {"role": "user", "content": _common.json.dumps(payload, ensure_ascii=False)},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": 1024,
                "title": title,
                # `summary_transcript.score` reads this for the term-coverage
                # metric; borrowing a scorer means matching its item shape.
                "source": body,
            }
        )
    return built


def score(item, content, record=None):
    import summary_transcript

    return summary_transcript.score(item, content, record)


def judge_context(item_id):
    return {
        "instruction": f"One paragraph, at most 120 words, on what '{FIXTURES[item_id]}' is about.",
        "source": _text(item_id)[:12000],
        "reference": None,
    }

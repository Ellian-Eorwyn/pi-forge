#!/usr/bin/env python3
"""Summarization with a hard shape constraint.

`vault-transcripts` asks for one paragraph of at most 120 words that opens on
the substance rather than on "this recording". `check_summary` decides all three
of those without a model, which makes this the cheapest instruction-following
measurement in the suite: a model that cannot hold a word budget shows up here
before it costs anything anywhere else.

Whether the summary is any *good* — whether it says what the recording was
actually about — is the judge's question, so the summaries go into the bundle.

The sources deliberately span two orders of magnitude in length, because a case
where every model passes is measuring nothing.
"""

import _common
import _transcripts

DIMENSION = "summarization"
SKILL = "vault-transcripts"
JUDGE = True

# Six short memos and two long multi-speaker meetings. The spread is the point:
# an earlier version was three memos of a few hundred words each, both models
# scored 3/3, and the case stopped telling them apart. Compressing a 7,000-word
# meeting into 120 words is a different task from compressing a 340-word memo,
# and it is the one where models come apart.
FIXTURES = [
    "transcript-context",
    "transcript-retrieval",
    "raw-asr-piforge",
    "transcript-export",
    "transcript-deepsearch",
    "transcript-knowledgebase",
    "transcript-vpp-chunk",
    "transcript-brattle",
]


def _cleaned_source(fixture_id):
    """The recording as prose, and *only* the prose.

    Summarization runs on cleaned text in production. Feeding it the raw
    transcript instead would fold the cleanup stage's failures into this one, so
    each fixture is summarized from the note the pipeline already produced where
    there is one, and from the rendered turns where there is not.

    The callouts have to come off. The finished note opens with the summary a
    previous run already wrote, and handing that to a summarizer measures
    whether it can copy an answer rather than whether it can produce one — an
    early version of this case did exactly that, and all three models echoed the
    existing callout's phrasing back.
    """
    record = _transcripts.RECORDS[fixture_id]
    schema_lib = _common.harness.load_lib("vault_schema")
    if record.get("reference"):
        import _cleanup

        body = schema_lib.split_frontmatter(_common.fixture(record["reference"]).encode("utf-8"))["body"]
        return _cleanup.cleaned_prose(body)
    transcripts = _transcripts.skill()
    turns = transcripts.collapse_turns(_transcripts.blocks(fixture_id), {})
    return transcripts.render_turns(turns)


def _term_coverage(source, summary):
    """Fraction of the source's distinctive terms that reached the summary.

    Not a gate. A 120-word summary of a 7,000-word meeting cannot carry most of
    them, and the right number is not 1.0 — it is whatever a good summary of that
    source scores. It is here to vary between models where the shape check does
    not.
    """
    transcripts = _transcripts.skill()
    terms = transcripts.rare_words(source)
    if not terms:
        return 1.0
    present = set(transcripts.content_words(str(summary or "")))
    return round(len(terms & present) / len(terms), 4)


def items():
    transcripts = _transcripts.skill()
    built = []
    for fixture_id in FIXTURES:
        record = _transcripts.RECORDS[fixture_id]
        body = _cleaned_source(fixture_id)
        payload = {
            "recordingType": record["recording_type"],
            "materialRole": record["material_role"],
            "title": record["title"],
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
                "source": body,
            }
        )
    return built


def score(item, content, record=None):
    transcripts = _transcripts.skill()
    parsed = _common.parse_json(content)
    if not isinstance(parsed, dict):
        return _common.failure("reply was not a JSON object")
    summary = parsed.get("summary")
    problems = transcripts.check_summary(summary)
    words = _common.word_count(summary)
    return {
        "ok": not problems,
        "gates": {
            "parsed": True,
            "summaryClean": not problems,
            "underWordLimit": words <= transcripts.SUMMARY_MAX_WORDS,
            "oneParagraph": isinstance(summary, str) and "\n\n" not in summary.strip(),
        },
        "metrics": {
            "summaryWords": words,
            # The prompt asks for 90 and the gate rejects over 120. How close a
            # model lands to the target says more than whether it squeaked under.
            "wordsOverTarget": max(0, words - transcripts.SUMMARY_TARGET_WORDS),
            # What the gate cannot see. Every model clears "one paragraph, under
            # 120 words" — this case scored 8/8 for two very different models —
            # so the shape check is a floor and this is the instrument: how much
            # of what the source was distinctively *about* survived into the
            # summary. Reuses the skill's own rare-word notion rather than a new
            # one, so it means the same thing here as in the cleanup gate.
            "sourceTermsCovered": _term_coverage(item["source"], summary),
        },
        "notes": problems,
        "output": summary,
    }


def judge_context(item_id):
    return {
        "instruction": "One paragraph, at most 120 words, on what this recording was about.",
        "source": _cleaned_source(item_id),
        "reference": None,
    }

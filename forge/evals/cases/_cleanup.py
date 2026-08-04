#!/usr/bin/env python3
"""The cleanup stage, shared by the memo and meeting cases.

`check_chunk` is the gate production runs, and it is where most of the value is:
it rejects a chunk containing content words the source did not, a surviving
timestamp, a level-one heading, or speaker labels on a single-speaker recording.
Passing it is necessary and not sufficient — a chunk can keep every word and
still flatten the speaker's register — so the cleaned text also goes to the
judge with the note the pipeline already produced as the reference.
"""

import _common
import _transcripts

DIMENSION = "faithful-cleanup"
SKILL = "vault-transcripts"
JUDGE = True


def build(fixture_ids):
    transcripts = _transcripts.skill()
    built = []
    for fixture_id in fixture_ids:
        record = _transcripts.RECORDS[fixture_id]
        chunks = _transcripts.chunks(fixture_id)
        mapping = _transcripts.speaker_map(fixture_id)
        tiny = _transcripts.is_tiny(fixture_id)
        for index, chunk in enumerate(chunks, start=1):
            payload, source = transcripts.cleanup_payload(
                record,
                chunk,
                index,
                len(chunks),
                headings=[],
                previous_tail="",
                speaker_map=mapping,
                drop_labels=record["drop_labels"],
                tiny=tiny,
            )
            built.append(
                {
                    "id": fixture_id if len(chunks) == 1 else f"{fixture_id}#{index}",
                    "messages": [
                        {"role": "system", "content": transcripts.CLEANUP_SYSTEM},
                        {"role": "user", "content": _common.json.dumps(payload, ensure_ascii=False)},
                    ],
                    "response_format": {"type": "json_object"},
                    # Cleanup condenses, so the reply is never much longer than
                    # its input; the headroom is for the chunk summary.
                    "max_tokens": 6000,
                    "fixture": fixture_id,
                    "source": source,
                    "tiny": tiny,
                    "dropLabels": record["drop_labels"],
                    "speakerMap": mapping,
                }
            )
    return built


def score(item, content, record=None):
    transcripts = _transcripts.skill()
    parsed = _common.parse_json(content)
    if not isinstance(parsed, dict):
        return _common.failure("reply was not a JSON object")
    cleaned = parsed.get("cleaned")
    if not isinstance(cleaned, str) or not cleaned.strip():
        return _common.failure("reply carried no cleaned text", {"parsed": True, "chunkClean": False})

    source = item["source"]
    problems = transcripts.check_chunk(
        cleaned, source, item["speakerMap"], item["dropLabels"], item["tiny"], glossary=()
    )
    invented = transcripts.added_words(source, cleaned, [])
    # Returns (ratio, missing). A source under RARE_WORD_MIN_SOURCE_WORDS scores
    # 1.0 with an empty list, because on a short memo the "rare" words are just
    # ordinary vocabulary and the check says nothing.
    retention, dropped_rare = transcripts.rare_word_retention(source, cleaned)
    source_words = _common.word_count(source)
    cleaned_words = _common.word_count(cleaned)
    ratio = cleaned_words / source_words if source_words else 0.0

    gates = {
        "parsed": True,
        "chunkClean": not problems,
        # The chunk gate tolerates up to MAX_INVENTED_WORDS; nothing invented at
        # all is the standard worth reporting separately.
        "noInventedWords": not invented,
        # The note-level gate applies these to the whole recording. Applied per
        # chunk they say the same thing: prose was condensed, not rewritten or
        # summarized away.
        "ratioInBand": transcripts.CLEANED_RATIO_MIN <= ratio <= transcripts.CLEANED_RATIO_MAX,
        "rareWordsKept": retention >= transcripts.RARE_WORD_RETENTION,
    }
    notes = list(problems)
    if invented:
        notes.append(f"words not in the chunk: {', '.join(invented[:8])}")
    if not gates["ratioInBand"]:
        notes.append(f"cleaned/source word ratio {ratio:.2f} outside [{transcripts.CLEANED_RATIO_MIN}, {transcripts.CLEANED_RATIO_MAX}]")
    if dropped_rare:
        notes.append(f"distinctive words dropped ({retention:.2f} retained): {', '.join(dropped_rare[:8])}")

    return {
        "ok": all(gates.values()),
        "gates": gates,
        "metrics": {
            "inventedWords": len(invented),
            "wordRatio": round(ratio, 4),
            "rareWordRetention": round(retention, 4),
            "chunkSummaryWords": _common.word_count(parsed.get("chunk_summary")),
        },
        "notes": notes,
        "output": cleaned,
    }


def repair(item, content, scored):
    """The one corrective retry `clean_one_chunk` gives a failed chunk.

    Production does not discard a chunk that fails `check_chunk`; it names the
    violation and asks again, and only refuses if that fails too. Scoring the
    first reply alone would understate what the pipeline delivers, so both
    numbers are recorded: `chunkClean` is what the model is like, `repairedOk`
    is what a run would actually produce.

    The wording is the skill's, including its branch on which check failed — a
    retry that only says "unusable" gets the same answer back.
    """
    problems = scored.get("notes") or []
    if not problems:
        return None
    first = str(problems[0])
    if first.startswith("these words are not in the chunk") or first.startswith("words not in the chunk"):
        guidance = (
            "Those words are yours, not the speaker's. Put their wording back: condense by "
            "dropping words, never by replacing one with a word you prefer. Every content "
            "word in your answer must be one they said."
        )
    else:
        guidance = (
            "Stay inside the speaker's own words and their own voice. Clean and condense "
            "how they said it; do not restate, describe, or explain what they said, and do "
            "not drop any point they made."
        )
    return [
        *item["messages"],
        {"role": "user", "content": f"That response was unusable: {first}. Return corrected JSON only. {guidance}"},
    ]


def cleaned_prose(body):
    """The cleanup output alone, pulled out of the finished note around it.

    The published note is summary callout, reflection callouts, connections, cue
    words, the cleaned prose, and a link to the verbatim source. Only the prose
    is what this stage produced, and handing the grader all of it invites a
    comparison against work three other stages did.
    """
    lines = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# Transcript"):
            break
        if stripped.startswith(">") or stripped.startswith("[["):
            continue
        if stripped.startswith("Summary:") or stripped.startswith("Cue words:"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def context(item_id):
    """Source and reference for the judge bundle."""
    fixture_id = item_id.split("#", 1)[0]
    record = _transcripts.RECORDS[fixture_id]
    built = next((entry for entry in build([fixture_id]) if entry["id"] == item_id), None)
    reference = None
    if record.get("reference"):
        schema_lib = _common.harness.load_lib("vault_schema")
        body = schema_lib.split_frontmatter(_common.fixture(record["reference"]).encode("utf-8"))["body"]
        reference = cleaned_prose(body)
    return {
        "instruction": "Clean this transcript chunk into readable prose without losing content or voice.",
        "source": built["source"] if built else None,
        "reference": reference,
    }

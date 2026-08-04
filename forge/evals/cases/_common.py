#!/usr/bin/env python3
"""Helpers every case shares.

A case module exposes two functions and three constants:

    DIMENSION  what capability this measures, for the report's grouping
    SKILL      the skill whose stage is under test
    JUDGE      whether its outputs go into the blind comparison bundle

    items()             -> [{id, messages, max_tokens?, response_format?, ...}]
    score(item, content) -> {ok, gates, metrics, notes, output?, keepRaw?}

`gates` are the skill's own deterministic checks, expressed as name -> bool.
`metrics` are numbers the report averages. `output` is what the judge reads, so
it must be the finished artifact rather than the raw reply.
"""

import json
import sys
from pathlib import Path

EVALS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVALS_ROOT))

import harness  # noqa: E402


def fixture(fixture_id):
    return harness.frozen_text(fixture_id)


def note_parts(fixture_id):
    """A frozen note split into ``(frontmatter_text, body, parsed_frontmatter)``."""
    schema_lib = harness.load_lib("vault_schema")
    split = schema_lib.split_frontmatter(fixture(fixture_id).encode("utf-8"))
    return split["frontmatter_text"], split["body"], schema_lib.parse_frontmatter(split["frontmatter_text"])


def parse_json(content):
    """The reply as JSON, or ``None``. Malformed output is a result, not a crash."""
    forge_llm = harness.forge_llm
    try:
        return forge_llm.parse_json_content(content)
    except (forge_llm.ChatError, ValueError):
        return None


def wikilink_target(value):
    """``"[[Pi Forge]]"`` -> ``"Pi Forge"``; anything else unchanged."""
    text = str(value or "").strip()
    if text.startswith("[[") and text.endswith("]]"):
        return text[2:-2].split("|", 1)[0].strip()
    return text


def word_count(text):
    return len(str(text or "").split())


def truncated(record):
    """Whether the reply stopped because it hit its output budget.

    Worth telling apart from every other unparseable reply: a model cut off
    mid-JSON was answering, at length, and the fix is a bigger budget or a
    smaller chunk. Reporting that as "could not produce JSON" would blame the
    model for the harness's ceiling.
    """
    return (record or {}).get("finishReason") == "length"


def truncation_failure(record):
    generated = (record or {}).get("generatedTokens")
    return {
        "ok": False,
        "gates": {"parsed": False, "notTruncated": False},
        "metrics": {},
        "notes": [f"ran out of output budget at {generated} tokens, mid-reply"],
        "keepRaw": True,
    }


def failure(note, gates=None):
    """A result the scorer could not make sense of.

    `keepRaw` is always set: a parse failure nobody can look at is not a finding,
    it is a shrug, and the reply is exactly the evidence needed to tell a model
    that cannot follow the contract from a harness that asked for the wrong thing.
    """
    return {"ok": False, "gates": gates or {"parsed": False}, "metrics": {}, "notes": [note], "keepRaw": True}

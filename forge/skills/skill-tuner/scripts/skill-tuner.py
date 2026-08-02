#!/usr/bin/env python3
"""Mine a completed pi session log for pain points and author a bounded report.

The session logs this reads were produced by a small local model working
through forge skills. The friction in them — errors, silent failures, output
truncation, ambiguity the model had to reason through, knowledge it should not
have needed — is exactly the material for making those skills clearer, more
decomposed, and less dependent on model reasoning. The output is a single
Markdown report, capped at a fixed token budget, that a model which never saw
the session can act on: every claim cites evidence ids resolvable to exact log
entries, literature-extraction style.

Division of labor, per the service-split handoff:

- The script detects everything detectable deterministically (error flags,
  truncation stop reasons, envelope warnings, compaction, stalls, retry loops,
  re-pasted errors, skill attribution) and hands those findings to the chat
  model as seeds. The model's job is cause, attribution, and consequence — plus
  the categories no scan can see (ambiguity, knowledge reliance, wasted work),
  which live in thinking blocks and narrative.
- All bulk extraction runs on ``chat``, then one batched review on ``think``,
  then authoring on ``think``. Stages never interleave.
- Quotes are verified byte-exact against the rendered timeline before anything
  is recorded. A fabricated quote costs one corrective retry, then the chunk is
  marked for review. Locators the model fumbles are corrected deterministically
  when the quote itself is real.
"""

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

# Shared forge libraries live at forge/lib; this script is at
# forge/skills/skill-tuner/scripts/skill-tuner.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
import forge_embeddings
import forge_llm
import forge_verify
import run_state

RUN_STATE_WORKFLOW = "skill-tuner"
RUN_SCHEMA_VERSION = 1
SUPPORTED_SESSION_VERSION = 3

# House report budget. Tokens are estimated as ceil(characters / 4), the same
# deliberately conservative heuristic literature-extraction records as
# estimatedTokensMethod, so 16,384 tokens is a 65,536-character hard cap.
DEFAULT_REPORT_BUDGET_TOKENS = 16384

# Timeline rendering keeps enough of every block to quote from while making the
# 400KB tool-result payloads uncitable by construction. Thinking blocks keep
# the most: they carry the ambiguity and knowledge-reliance signal no
# deterministic scan can see. Each triple is (threshold, head, tail).
ELIDE_TEXT = (2000, 1500, 300)
ELIDE_THINKING = (4000, 3000, 600)
ELIDE_TOOL_RESULT = (1600, 1000, 300)
# The brackets never occur in real session content, so an elided middle can be
# detected in quotes and excluded from citation.
ELIDE_MARKER = "⟦ELIDED {n} OF {total} CHARS⟧"
ELIDE_MARKER_RE = re.compile(r"⟦ELIDED \d+ OF \d+ CHARS⟧")

DEFAULT_CHUNK_CHARS = 48000
OPEN_THREADS_MAX = 12
OPEN_THREAD_CHARS = 200
CHUNK_SUMMARY_CHARS = 500
SEED_EXCERPT_CHARS = 300

GAP_SECONDS = 60
REPEAT_MIN_WITH_ERROR = 2
REPEAT_MIN_UNCONDITIONAL = 3
REPEATED_TEXT_MIN_CHARS = 80
REPASTED_ERROR_PROBE_CHARS = 120

ITEM_TYPES = [
    "tool_error",
    "silent_failure",
    "output_truncation",
    "context_loss",
    "ambiguity",
    "knowledge_reliance",
    "retry_loop",
    "wasted_work",
    "backend_limit",
    "environment_mismatch",
    "missing_guardrail",
    "user_correction",
]
ITEM_TYPE_SET = set(ITEM_TYPES)
SEVERITIES = ["blocker", "major", "minor", "papercut"]
SEVERITY_WEIGHT = {"blocker": 8, "major": 4, "minor": 2, "papercut": 1}
LAYERS = ["skill", "backend", "harness", "crosscutting", "unknown"]
CHANGE_TYPES = [
    "instruction_clarification",
    "decomposition",
    "deterministic_guard",
    "contract_tightening",
    "backend_config",
    "new_reference",
    "new_tool",
]
INTERPRETATIONS = {"explicit", "inferred", "unclear"}
CONFIDENCES = {"high", "medium", "low"}
MAX_ITEMS_PER_CHUNK = 20
# The live gate showed the small model echoing seed kinds as item_type values.
# The two with one honest translation are normalized deterministically; stall
# kinds stay errors because their friction class genuinely depends on cause.
ITEM_TYPE_ALIASES = {"compaction": "context_loss", "repeated_user_text": "user_correction"}

QUOTE_MIN_MATCH_CHARACTERS = 12
WHITESPACE_RE = re.compile(r"\s+")
QUOTE_CHARACTERS = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "—": "-", "–": "-"})

EVIDENCE_ID_RE = re.compile(r"\bp\d{6}\b")

# Synthesis and authoring budgets, in estimated tokens. One slot of the shared
# llama-server is 131,072 tokens; the target sits below it with a reserve for
# instructions and output, matching literature-extraction's meta defaults.
AUTHORING_TARGET_CONTEXT = 98304
AUTHORING_RESERVED_CONTEXT = 32000
SECTION_FLOOR_CHARS = 1200
EXEC_SUMMARY_MIN_CHARS = 800
EXEC_SUMMARY_MAX_CHARS = 4000
EXEC_SUMMARY_SHARE = 0.12
EXEC_SUMMARY_TOP_GROUPS = 12
SECTION_ATTEMPTS_MAX = 4
ASSEMBLY_SHRINK_PASSES = 2
APPENDIX_QUOTE_CHARS = 120
EMBED_GROUP_THRESHOLD = 0.90
EMBED_TEXT_CHARS = 1500

# Dense chunks can carry 20 items with quotes, and a report section can carry
# several issues with citations, so extraction and authoring deliberately
# deviate from the backgroundOutputTokens convention. Verify and reduce, whose
# outputs are bounded by their contracts, stay at 4,096.
EXTRACT_MAX_TOKENS = 8192
AUTHOR_MAX_TOKENS = 8192
DEFAULT_MAX_TOKENS = 4096
# A section budget the model cannot physically emit in one call buys a
# truncated response, a failed gate, and a wasted retry - measured on the
# example session, where a 30,086-character section budget met a 4,096-token
# cap. Density on this deployment is ~3.42 characters per token
# (forge_llm.PROMPT_CHARACTERS_PER_TOKEN); 3.0 keeps the clamp conservative for
# punctuation-dense Markdown.
SECTION_CHARACTERS_PER_OUTPUT_TOKEN = 3.0
MAX_SECTION_CHARS = int(AUTHOR_MAX_TOKENS * SECTION_CHARACTERS_PER_OUTPUT_TOKEN)
DEFAULT_REQUEST_TIMEOUT = 600.0

CITABLE_ITEM_STATUSES = {"extracted", "verified", "escalated"}

SKILL_PATH_RE = re.compile(r"skills/([a-z0-9][a-z0-9-]*)/")
SKILL_SCRIPT_RE = re.compile(r"scripts/([a-z0-9][a-z0-9-]*)\.(?:py|mjs)\b")

KNOWN_ENTRY_TYPES = {"session", "model_change", "thinking_level_change", "message", "custom_message", "compaction"}
KNOWN_MESSAGE_ROLES = {"user", "assistant", "toolResult"}
KNOWN_ASSISTANT_BLOCKS = {"text", "thinking", "toolCall"}

# --------------------------------------------------------------------------- #
# Prompts. Byte-stable across a run: per-chunk and per-section variation goes
# only in the user message, so the server's prefix cache survives every call.
# references/extraction-contract.md mirrors these verbatim - if you change one,
# change both.
# --------------------------------------------------------------------------- #

EXTRACT_SYSTEM = """You mine one chunk of a rendered agent-session timeline for pain points that made the
session slower, wronger, or more confusing than it needed to be, so that skill
instructions can be improved for a small local model.

Return exactly one JSON object and nothing else:
{"items": [...], "open_threads": [...], "chunk_summary": "<= 2 sentences"}

Every item has exactly these fields:
- "item_type": one of tool_error, silent_failure, output_truncation, context_loss,
  ambiguity, knowledge_reliance, retry_loop, wasted_work, backend_limit,
  environment_mismatch, missing_guardrail, user_correction
- "severity": "blocker", "major", "minor", or "papercut"
- "attribution": {"skill": <a skill named in sessionBrief.skillsSeen, or null>,
  "layer": "skill", "backend", "harness", "crosscutting", or "unknown"}
- "text": what happened and why it was friction (required, nonblank)
- "direct_quotes": a short verbatim quote copied character-for-character from the
  timeline chunk, or null. Never quote from inside an ELIDED marker.
- "locator": {"line": <the L-number of the timeline entry the quote or event is in>}
- "interpretation": "explicit" when the timeline shows it directly, "inferred" when you
  concluded it from the timeline, "unclear" when the timeline is ambiguous
- "confidence": "high", "medium", or "low"
- "seed_ids": ids of the deterministic findings in this chunk this item explains, or []
- "change_type": one of instruction_clarification, decomposition, deterministic_guard,
  contract_tightening, backend_config, new_reference, new_tool
- "recommendation_hint": one sentence on the fix, or null
- "notes": optional clarification, or null

Rules:
- The deterministic findings under "seeds" were already detected by a script. Your job
  with them is context and attribution - what caused them, which skill they belong to,
  what they cost - not rediscovery. Do not restate a seed without adding cause,
  attribution, or consequence.
- Work through the item types in order and ask what this chunk shows for each one
  before moving on. Do not stop at the tool errors, truncations, and retry loops the
  seeds already name: chunks also carry ambiguity the model had to reason through,
  knowledge_reliance where it used world knowledge a reference file could encode,
  wasted_work, missing_guardrail, and user_correction - these live in the thinking
  blocks and the narrative, and no seed will point at them.
- "direct_quotes" must be copied from the timelineChunk text itself. The seed "detail"
  and "excerpt" strings are scan metadata, not timeline text - quoting them fails
  verification. Quote the timeline entry the seed points at instead, or use null.
- "item_type" describes the friction, not the seed. A seed's kind is not an item_type:
  a compaction seed is context_loss, a repeated_user_text seed is usually
  user_correction, and a stall seed is wasted_work when time was lost redoing or
  waiting on avoidable work, or backend_limit when the backend itself was slow.
- Never invent events the chunk does not show; label inference as "inferred".
- An empty items array is the right answer for an uneventful chunk.
- At most 20 items; prefer the highest-severity ones.
- "open_threads": at most 12 short notes (<= 200 characters each) on issues still
  unfolding at the chunk boundary. Copy forward the incoming open_threads that are
  still open, close the ones this chunk resolves, and add new ones.
"""

VERIFY_SYSTEM = """You are reviewing pain-point evidence mined from an agent-session timeline by a faster
model without reasoning. Each item shows its claim, its verbatim quote, its locator,
and its deterministic corroboration (a script-detected seed, or a note that it is
narrative-derived).
Flag an item when it is actually wrong: the quote or seed does not support the claim,
the skill attribution contradicts the locator's context, the item_type is wrong for
what the evidence shows, or the severity is inflated beyond what the evidence supports.
Do not flag an item for phrasing, for a severity you would nudge one step, or for being
small - papercuts are in scope. Do not flag a narrative-derived item merely because no
seed corroborates it; ambiguity and knowledge-reliance findings come from the narrative
by design and are judged on the quoted evidence alone. Do not flag an inferred item for
being inferred when it is labeled "inferred" and is plausible on the evidence shown."""

ESCALATE_SYSTEM = """A reviewer rejected one pain-point evidence item mined from an agent-session timeline.
You see the objection, the original item, and the full timeline chunk it came from.
Return exactly one JSON object and nothing else: either the corrected item, with the
same fields as the original (item_type, severity, attribution, text, direct_quotes,
locator, interpretation, confidence, seed_ids, change_type, recommendation_hint,
notes), or {"drop": true, "reason": "<why the item is unsupportable>"} when the
timeline does not support any version of it.
A direct_quotes value must appear character-for-character in the timeline chunk, and
never from inside an ELIDED marker. Address the reviewer's objection specifically."""

REDUCE_SYSTEM = """You compress pain-point evidence records into a memo for a report author who will not
see the originals. Preserve every [p######] evidence id attached to any fact you keep,
exactly as written; never invent ids. Keep counts and severities honest. Prefer keeping
the highest-severity and most repeated issues. Group related records. Stay under the
character budget stated in the request. Return only the memo text."""

AUTHOR_SYSTEM = """You write one section of a skill-tuning report about a recorded agent session, for a
reader - human or model - who never saw the session. The deeper purpose is helping a
small local model punch above its weight: turning observed friction into clearer
instructions, less ambiguity, smaller deterministic steps, and less reliance on model
knowledge and reasoning.

Rules:
- Every substantive claim cites evidence ids in square brackets, like [p000042]. Use
  only ids present in the provided evidence; never invent ids.
- For each issue: state what happened, why it hurts a small non-thinking local model,
  and the recommended change, naming its change_type and severity.
- Start each issue with a "### " heading. Never emit "## " headings - the report
  assembles those. The executive summary uses no headings at all.
- Stay under the character budget stated in the request. Plain Markdown, no
  placeholders, no code fences around the whole section.
- Recommendations must be concrete enough to act on: name the skill file, prompt
  clause, guard, or config knob to change when the evidence shows it.
Return only the section body text."""

METHOD_TEXT = (
    "This report was produced by the skill-tuner workflow: the session log was rendered "
    "into a deterministic timeline (oversized payloads elided), scanned for deterministic "
    "findings, mined chunk by chunk on the local chat service, reviewed by the thinking "
    "service, and authored by the thinking service from merged evidence. Bracketed ids of "
    "the form [p######] cite evidence items in the appendix; each resolves to a timeline entry "
    "(its L-number is the line in the source session log). Quotes are verbatim from the "
    "rendered timeline. Severity is one of blocker, major, minor, papercut; recommended "
    "changes name one of: instruction_clarification, decomposition, deterministic_guard, "
    "contract_tightening, backend_config, new_reference, new_tool."
)

# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


class UserError(RuntimeError):
    """A unit could not be processed; the run records it and continues."""


def progress(message):
    """Per-unit progress on stderr; stdout stays one JSON result."""
    print(message, file=sys.stderr, flush=True)


def structured(status, artifacts=None, warnings=None, errors=None, data=None):
    return {
        "status": status,
        "artifacts": artifacts or [],
        "warnings": warnings or [],
        "errors": errors or [],
        "data": data,
    }


def emit(status="ok", artifacts=None, warnings=None, errors=None, data=None):
    print(json.dumps(structured(status, artifacts, warnings, errors, data), ensure_ascii=False))


def fail(message, code="error", exit_code=1):
    print(f"Error: {message}", file=sys.stderr)
    emit("error", errors=[{"code": code, "message": str(message)}])
    raise SystemExit(exit_code)


def utc_now():
    return run_state.utc_now()


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def blank(value):
    return value is None or (isinstance(value, str) and value.strip() == "")


def estimate_tokens(text):
    return (len(text) + 3) // 4


def truncate(text, limit):
    value = str(text or "")
    return value if len(value) <= limit else value[: limit - 1] + "…"


def parse_timestamp(value):
    """ISO timestamp to an aware datetime; Python 3.9's fromisoformat cannot
    parse a trailing Z, so normalize it first."""
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def canonical_json(value):
    return run_state.canonical_json(value)


# --------------------------------------------------------------------------- #
# Session format v3 parsing. The normalized entry shape is the adapter seam:
# a future format lands as another parse_session_* returning the same shape,
# and everything downstream stays format-agnostic.
# --------------------------------------------------------------------------- #


def read_session_rows(path):
    """Rows with their real 1-based file line numbers.

    run_state.read_jsonl_recover_tail drops line numbers and may repair; the
    session log is a read-only input and its line numbers are the locator
    vocabulary, so this reads with the same tail-tolerant semantics but never
    modifies the file.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise UserError(f"session log is not valid UTF-8: {path}")
    lines = text.splitlines(keepends=True)
    rows = []
    warnings = []
    for number, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            rows.append((number, json.loads(stripped)))
        except json.JSONDecodeError as error:
            if number == len(lines) and not raw.endswith("\n"):
                warnings.append(f"ignored an incomplete final record at line {number}; the session may still have been writing")
                continue
            raise UserError(f"invalid JSONL at line {number}: {error}")
    return rows, warnings


def _text_blocks(content, line):
    blocks = []
    if isinstance(content, str):
        if content:
            blocks.append({"type": "text", "text": content})
        return blocks
    if not isinstance(content, list):
        raise UserError(f"line {line}: message content is neither a string nor a list")
    for part in content:
        if not isinstance(part, dict):
            raise UserError(f"line {line}: content block is not an object")
        text = part.get("text")
        blocks.append({"type": "text", "text": text if isinstance(text, str) else ""})
    return blocks


def _assistant_blocks(content, line):
    if not isinstance(content, list):
        raise UserError(f"line {line}: assistant content is not a list")
    blocks = []
    for part in content:
        kind = part.get("type") if isinstance(part, dict) else None
        if kind == "text":
            blocks.append({"type": "text", "text": part.get("text") or ""})
        elif kind == "thinking":
            blocks.append({"type": "thinking", "text": part.get("thinking") or ""})
        elif kind == "toolCall":
            blocks.append(
                {
                    "type": "tool_call",
                    "tool": part.get("name") or "unknown",
                    "tool_call_id": part.get("id") or "",
                    "text": canonical_json(part.get("arguments") if isinstance(part.get("arguments"), dict) else {}),
                }
            )
        else:
            raise UserError(
                f"line {line}: unknown assistant content block type {kind!r}; "
                f"skill-tuner understands {', '.join(sorted(KNOWN_ASSISTANT_BLOCKS))}"
            )
    return blocks


def _normalize_message(line, row):
    message = row.get("message")
    if not isinstance(message, dict):
        raise UserError(f"line {line}: message entry has no message object")
    role = message.get("role")
    if role not in KNOWN_MESSAGE_ROLES:
        raise UserError(f"line {line}: unknown message role {role!r}; skill-tuner v1 parses pi session format v3 only")
    if role == "user":
        return "user", _text_blocks(message.get("content"), line), {}
    if role == "assistant":
        usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
        meta = {
            "stopReason": message.get("stopReason"),
            "model": message.get("model"),
            "responseModel": message.get("responseModel"),
            "usage": {key: usage.get(key) for key in ("input", "output", "reasoning", "totalTokens") if key in usage},
        }
        return "assistant", _assistant_blocks(message.get("content"), line), meta
    texts = [block["text"] for block in _text_blocks(message.get("content"), line)]
    meta = {
        "tool": message.get("toolName") or "unknown",
        "toolCallId": message.get("toolCallId") or "",
        "isError": bool(message.get("isError")),
        "details": message.get("details"),
    }
    return "tool_result", [{"type": "tool_result_text", "text": "\n".join(texts)}], meta


def normalize_entry(line, row):
    entry_type = row.get("type")
    if entry_type not in KNOWN_ENTRY_TYPES:
        raise UserError(
            f"line {line}: unknown entry type {entry_type!r}; skill-tuner v1 parses pi session format v3 only"
        )
    entry = {
        "line": line,
        "entryId": row.get("id") or "",
        "parentId": row.get("parentId"),
        "kind": entry_type,
        "timestamp": row.get("timestamp"),
        "blocks": [],
        "meta": {},
    }
    if entry_type == "session":
        entry["meta"] = {"version": row.get("version"), "cwd": row.get("cwd")}
    elif entry_type == "model_change":
        entry["meta"] = {"provider": row.get("provider"), "modelId": row.get("modelId")}
    elif entry_type == "thinking_level_change":
        entry["meta"] = {"thinkingLevel": row.get("thinkingLevel")}
    elif entry_type == "custom_message":
        entry["kind"] = "custom"
        entry["meta"] = {"customType": row.get("customType"), "display": bool(row.get("display"))}
        content = row.get("content")
        entry["blocks"] = _text_blocks(content, line) if content else []
    elif entry_type == "compaction":
        entry["meta"] = {"tokensBefore": row.get("tokensBefore"), "firstKeptEntryId": row.get("firstKeptEntryId")}
        summary = row.get("summary")
        entry["blocks"] = [{"type": "text", "text": summary}] if isinstance(summary, str) and summary else []
    else:
        kind, blocks, meta = _normalize_message(line, row)
        entry["kind"] = kind
        entry["blocks"] = blocks
        entry["meta"] = meta
    return entry


def parse_session_v3(path):
    """Parse a pi session v3 log into (header, entries, warnings)."""
    rows, warnings = read_session_rows(path)
    if not rows:
        raise UserError(f"session log is empty: {path}")
    first_line, first_row = rows[0]
    if first_row.get("type") != "session":
        raise UserError(
            f"line {first_line}: the first record is not a session header; "
            "skill-tuner v1 parses pi session format v3 only"
        )
    version = first_row.get("version")
    if version != SUPPORTED_SESSION_VERSION:
        raise UserError(
            f"unsupported session format version {version!r}; skill-tuner v1 parses pi session format v3 only"
        )
    entries = [normalize_entry(line, row) for line, row in rows]
    header = {
        "sessionId": first_row.get("id") or "",
        "version": version,
        "cwd": first_row.get("cwd"),
        "startedAt": first_row.get("timestamp"),
    }
    return header, entries, warnings


def pair_tool_calls(entries):
    """Annotate tool results with their call line; return pairing findings.

    Pairing is by toolCall.id == toolResult.toolCallId - one assistant entry
    can fan out several parallel calls - never by parent links.
    """
    calls = {}
    for entry in entries:
        if entry["kind"] != "assistant":
            continue
        for block in entry["blocks"]:
            if block["type"] == "tool_call" and block["tool_call_id"]:
                calls[block["tool_call_id"]] = {"line": entry["line"], "tool": block["tool"], "answered": False}
    findings = []
    for entry in entries:
        if entry["kind"] != "tool_result":
            continue
        call = calls.get(entry["meta"].get("toolCallId") or "")
        if call is None:
            findings.append({"kind": "orphan_tool_result", "line": entry["line"], "tool": entry["meta"].get("tool")})
            continue
        call["answered"] = True
        entry["meta"]["callLine"] = call["line"]
    for identifier, call in sorted(calls.items(), key=lambda pair: pair[1]["line"]):
        if not call["answered"]:
            findings.append({"kind": "unanswered_tool_call", "line": call["line"], "tool": call["tool"]})
    return findings


# --------------------------------------------------------------------------- #
# Timeline rendering. A pure function of the normalized entries, written once
# at init and byte-reproducible: it is the source of truth quotes are verified
# against, so it must never depend on anything but the input.
# --------------------------------------------------------------------------- #


def elide(text, limits):
    threshold, head, tail = limits
    value = str(text or "")
    if len(value) <= threshold:
        return value, 0
    elided = len(value) - head - tail
    marker = ELIDE_MARKER.format(n=elided, total=len(value))
    return value[:head] + "\n" + marker + "\n" + value[len(value) - tail :], elided


def _scaled(limits, factor):
    return (max(1, limits[0] // factor), max(1, limits[1] // factor), max(1, limits[2] // factor))


def entry_header(entry):
    parts = [f"=== L{entry['line']} e:{entry['entryId']} {entry['kind']}"]
    meta = entry["meta"]
    kind = entry["kind"]
    if kind == "session":
        parts.append(f"version:{meta.get('version')} cwd:{meta.get('cwd')}")
    elif kind == "model_change":
        parts.append(f"{meta.get('provider')}/{meta.get('modelId')}")
    elif kind == "thinking_level_change":
        parts.append(f"thinking:{meta.get('thinkingLevel')}")
    elif kind == "custom":
        parts.append(str(meta.get("customType")))
    elif kind == "compaction":
        parts.append(f"tokensBefore:{meta.get('tokensBefore')} firstKept:{meta.get('firstKeptEntryId')}")
    elif kind == "assistant" and meta.get("stopReason"):
        parts.append(f"stop:{meta['stopReason']}")
    elif kind == "tool_result":
        call_id = (meta.get("toolCallId") or "")[:8]
        parts.append(f"tool:{meta.get('tool')} tc:{call_id} {'ERROR' if meta.get('isError') else 'ok'}")
    if entry.get("timestamp"):
        parts.append(f"t:{entry['timestamp']}")
    return " ".join(parts)


def render_entry(entry, factor=1):
    lines = [entry_header(entry)]
    elided_total = 0
    for block in entry["blocks"]:
        kind = block["type"]
        if kind == "thinking":
            body, elided = elide(block["text"], _scaled(ELIDE_THINKING, factor))
            lines.append("[thinking]")
            if body:
                lines.append(body)
        elif kind == "tool_call":
            body, elided = elide(block["text"], _scaled(ELIDE_TEXT, factor))
            lines.append(f"[toolCall {block['tool']} tc:{block['tool_call_id'][:8]}]")
            if body:
                lines.append(body)
        elif kind == "tool_result_text":
            body, elided = elide(block["text"], _scaled(ELIDE_TOOL_RESULT, factor))
            if body:
                lines.append(body)
        else:
            body, elided = elide(block["text"], _scaled(ELIDE_TEXT, factor))
            if body:
                lines.append(body)
        elided_total += elided
    return "\n".join(lines), elided_total


def render_timeline(entries, chunk_budget):
    """Render every entry, returning (text, index, warnings).

    Index rows map each entry to its character span in the final text, which is
    what locator correction and the chunker rely on. An entry that still
    exceeds the chunk budget after normal elision is re-rendered with
    progressively halved limits rather than splitting it mid-entry.
    """
    rendered = []
    index = []
    warnings = []
    position = 0
    for entry in entries:
        factor = 1
        text, elided = render_entry(entry, factor)
        while len(text) > chunk_budget and factor <= 32:
            factor *= 2
            text, elided = render_entry(entry, factor)
        if factor > 1:
            warnings.append(f"line {entry['line']}: entry exceeded the chunk budget; rendered with limits reduced {factor}x")
        rendered.append(text)
        index.append(
            {
                "line": entry["line"],
                "entryId": entry["entryId"],
                "kind": entry["kind"],
                "tool": entry["meta"].get("tool"),
                "charStart": position,
                "charEnd": position + len(text),
                "elidedChars": elided,
            }
        )
        position += len(text) + 2
    return "\n\n".join(rendered) + "\n", index, warnings


def chunk_spans(index, chunk_budget):
    """Group index rows into chunks on entry boundaries by rendered size."""
    chunks = []
    current = []
    for row in index:
        size = row["charEnd"] - row["charStart"]
        current_size = current[-1]["charEnd"] - current[0]["charStart"] if current else 0
        if current and current_size + size + 2 > chunk_budget:
            chunks.append(current)
            current = []
        current.append(row)
    if current:
        chunks.append(current)
    result = []
    for number, rows in enumerate(chunks, start=1):
        result.append(
            {
                "chunkId": f"c{number:04d}",
                "lineStart": rows[0]["line"],
                "lineEnd": rows[-1]["line"],
                "charStart": rows[0]["charStart"],
                "charEnd": rows[-1]["charEnd"],
                "chars": rows[-1]["charEnd"] - rows[0]["charStart"],
            }
        )
    return result


# --------------------------------------------------------------------------- #
# Deterministic pre-scan. Everything a script can detect is detected here and
# handed to the model as seeds; the model contextualizes and attributes rather
# than rediscovering. Seeds also corroborate evidence at verification time.
# --------------------------------------------------------------------------- #


def entry_epoch(entry):
    parsed = parse_timestamp(entry.get("timestamp"))
    return parsed.timestamp() if parsed else None


def entry_text(entry):
    return "\n".join(block["text"] for block in entry["blocks"] if block.get("text"))


def normalize_plain(text):
    return WHITESPACE_RE.sub(" ", str(text or "")).strip().casefold()


def parse_envelope(text):
    """A structured skill envelope inside a tool result, or None."""
    stripped = str(text or "").strip()
    if not stripped.startswith("{"):
        return None
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if isinstance(value, dict) and "status" in value:
        return value
    return None


def _failed_extraction_markers(value, found):
    if isinstance(value, dict):
        if value.get("extractionMethod") == "failed":
            found.append(f"extractionMethod failed (charCount {value.get('charCount')})")
        for child in value.values():
            _failed_extraction_markers(child, found)
    elif isinstance(value, list):
        for child in value:
            _failed_extraction_markers(child, found)


def _scan_per_entry(entries, seeds):
    for entry in entries:
        meta = entry["meta"]
        if entry["kind"] == "assistant" and meta.get("stopReason") == "length":
            seeds.append(
                {
                    "kind": "output_truncation",
                    "lines": [entry["line"]],
                    "tool": None,
                    "detail": "assistant response stopped at the output token limit (stopReason length)",
                    "excerpt": truncate(entry_text(entry), SEED_EXCERPT_CHARS),
                }
            )
        elif entry["kind"] == "compaction":
            seeds.append(
                {
                    "kind": "compaction",
                    "lines": [entry["line"]],
                    "tool": None,
                    "detail": (
                        f"context compacted at {meta.get('tokensBefore')} tokens; "
                        f"entries before {meta.get('firstKeptEntryId')} were summarized away"
                    ),
                    "excerpt": truncate(entry_text(entry), SEED_EXCERPT_CHARS),
                }
            )
        elif entry["kind"] == "tool_result":
            text = entry_text(entry)
            if meta.get("isError"):
                seeds.append(
                    {
                        "kind": "tool_error",
                        "lines": [entry["line"]],
                        "tool": meta.get("tool"),
                        "detail": f"{meta.get('tool')} returned isError true",
                        "excerpt": truncate(text, SEED_EXCERPT_CHARS),
                    }
                )
                continue
            markers = []
            envelope = parse_envelope(text)
            if envelope:
                problems = [str(value) for value in (envelope.get("warnings") or [])]
                problems.extend(str(value) for value in (envelope.get("errors") or []))
                if problems:
                    markers.append("; ".join(problems))
            _failed_extraction_markers(meta.get("details"), markers)
            if markers:
                seeds.append(
                    {
                        "kind": "silent_failure",
                        "lines": [entry["line"]],
                        "tool": meta.get("tool"),
                        "detail": truncate(f"isError false, but: {'; '.join(markers)}", SEED_EXCERPT_CHARS),
                        "excerpt": truncate(text, SEED_EXCERPT_CHARS),
                    }
                )


def _scan_gaps(entries, seeds):
    previous = None
    for entry in entries:
        epoch = entry_epoch(entry)
        if epoch is None:
            continue
        if previous is not None and epoch - previous[0] > GAP_SECONDS:
            gap = int(epoch - previous[0])
            kind = entry["kind"]
            if kind == "user":
                boundary = "user_idle"
            elif kind == "tool_result":
                boundary = "tool_stall"
            elif kind == "assistant":
                boundary = "assistant_stall"
            else:
                boundary = None
            if boundary:
                seeds.append(
                    {
                        "kind": boundary,
                        "lines": [previous[1], entry["line"]],
                        "tool": entry["meta"].get("tool"),
                        "detail": f"{gap}s gap before line {entry['line']} ({kind})",
                        "excerpt": truncate(entry_text(entry), SEED_EXCERPT_CHARS),
                        # A pause before a user turn is the human being away,
                        # not session friction; keep it as context only.
                        "informational": kind == "user",
                    }
                )
        previous = (epoch, entry["line"])


def _scan_repeats(entries, seeds):
    groups = {}
    for entry in entries:
        if entry["kind"] != "assistant":
            continue
        for block in entry["blocks"]:
            if block["type"] != "tool_call":
                continue
            key = (block["tool"], block["text"])
            groups.setdefault(key, {"lines": [], "callIds": []})
            groups[key]["lines"].append(entry["line"])
            groups[key]["callIds"].append(block["tool_call_id"])
    errored_calls = {
        entry["meta"].get("toolCallId")
        for entry in entries
        if entry["kind"] == "tool_result" and entry["meta"].get("isError")
    }
    for (tool, arguments), group in sorted(groups.items(), key=lambda pair: pair[1]["lines"][0]):
        count = len(group["lines"])
        with_error = any(call_id in errored_calls for call_id in group["callIds"])
        if count >= REPEAT_MIN_UNCONDITIONAL or (count >= REPEAT_MIN_WITH_ERROR and with_error):
            seeds.append(
                {
                    "kind": "retry_loop",
                    "lines": group["lines"],
                    "tool": tool,
                    "detail": f"identical {tool} call repeated {count}x" + (" with an error result" if with_error else ""),
                    "excerpt": truncate(arguments, SEED_EXCERPT_CHARS),
                }
            )


def _scan_repeated_user_text(entries, seeds):
    error_probes = []
    user_seen = []
    for entry in entries:
        if entry["kind"] == "tool_result" and entry["meta"].get("isError"):
            probe = normalize_plain(entry_text(entry))[:REPASTED_ERROR_PROBE_CHARS]
            if len(probe) >= REPEATED_TEXT_MIN_CHARS:
                error_probes.append((entry["line"], probe))
        if entry["kind"] != "user":
            continue
        text = normalize_plain(entry_text(entry))
        for earlier_line, earlier_text in user_seen:
            if len(earlier_text) >= REPEATED_TEXT_MIN_CHARS and earlier_text in text:
                seeds.append(
                    {
                        "kind": "repeated_user_text",
                        "lines": [earlier_line, entry["line"]],
                        "tool": None,
                        "detail": f"user message at line {entry['line']} substantially repeats line {earlier_line}",
                        "excerpt": truncate(entry_text(entry), SEED_EXCERPT_CHARS),
                    }
                )
                break
        for error_line, probe in error_probes:
            if error_line < entry["line"] and probe in text:
                seeds.append(
                    {
                        "kind": "repeated_user_text",
                        "lines": [error_line, entry["line"]],
                        "tool": None,
                        "detail": f"user message at line {entry['line']} re-pastes the error from line {error_line}",
                        "excerpt": truncate(entry_text(entry), SEED_EXCERPT_CHARS),
                    }
                )
                break
        if len(text) >= REPEATED_TEXT_MIN_CHARS:
            user_seen.append((entry["line"], text))


def _scan_skill_attribution(entries, seeds):
    references = {}
    for entry in entries:
        if entry["kind"] != "assistant":
            continue
        for block in entry["blocks"]:
            if block["type"] != "tool_call":
                continue
            names = set(SKILL_PATH_RE.findall(block["text"]))
            names.update(SKILL_SCRIPT_RE.findall(block["text"]))
            for name in names:
                references.setdefault(name, []).append(entry["line"])
    for name in sorted(references):
        lines = references[name]
        seeds.append(
            {
                "kind": "skill_attribution",
                "lines": lines[:20],
                "tool": None,
                "skill": name,
                "detail": f"{len(lines)} tool calls referenced skill {name}",
                "excerpt": None,
                "informational": True,
            }
        )


def scan_session(entries, pair_findings):
    """All deterministic seeds, in a stable order with stable ids."""
    seeds = []
    _scan_per_entry(entries, seeds)
    _scan_gaps(entries, seeds)
    _scan_repeats(entries, seeds)
    _scan_repeated_user_text(entries, seeds)
    for finding in pair_findings:
        seeds.append(
            {
                "kind": finding["kind"],
                "lines": [finding["line"]],
                "tool": finding.get("tool"),
                "detail": f"{finding['kind'].replace('_', ' ')} at line {finding['line']}",
                "excerpt": None,
            }
        )
    _scan_skill_attribution(entries, seeds)
    seeds.sort(key=lambda seed: (seed["lines"][0], seed["kind"]))
    for number, seed in enumerate(seeds, start=1):
        seed["id"] = f"s{number:03d}"
        seed.setdefault("informational", False)
        seed.setdefault("skill", None)
    return seeds


def skills_seen(seeds):
    return sorted({seed["skill"] for seed in seeds if seed.get("skill")})


def session_brief(header, entries, seeds):
    counts = Counter(entry["kind"] for entry in entries)
    tool_calls = sum(
        1 for entry in entries if entry["kind"] == "assistant" for block in entry["blocks"] if block["type"] == "tool_call"
    )
    provider = None
    model_id = None
    thinking = None
    for entry in entries:
        if entry["kind"] == "model_change":
            provider = entry["meta"].get("provider")
            model_id = entry["meta"].get("modelId")
        elif entry["kind"] == "thinking_level_change":
            thinking = entry["meta"].get("thinkingLevel")
    timestamps = [entry["timestamp"] for entry in entries if entry.get("timestamp")]
    seed_counts = Counter(seed["kind"] for seed in seeds if not seed["informational"])
    return {
        "sessionId": header["sessionId"],
        "cwd": header.get("cwd"),
        "provider": provider,
        "modelId": model_id,
        "thinkingLevel": thinking,
        "startedAt": timestamps[0] if timestamps else None,
        "endedAt": timestamps[-1] if timestamps else None,
        "entries": len(entries),
        "assistantTurns": counts.get("assistant", 0),
        "toolCalls": tool_calls,
        "toolResults": counts.get("tool_result", 0),
        "userTurns": counts.get("user", 0),
        "compactions": counts.get("compaction", 0),
        "skillsSeen": skills_seen(seeds),
        "seedCounts": dict(sorted(seed_counts.items())),
    }


# --------------------------------------------------------------------------- #
# Run directory plumbing
# --------------------------------------------------------------------------- #


def resolve_session_file(raw_path):
    path = Path(raw_path).expanduser().resolve()
    if not path.exists():
        fail(f"session log does not exist: {path}", code="missing_input")
    if path.is_dir():
        candidates = sorted(path.glob("*.jsonl"))
        if not candidates:
            fail(f"no .jsonl session log found in {path}", code="missing_input")
        if len(candidates) > 1:
            names = ", ".join(candidate.name for candidate in candidates[:5])
            fail(f"{path} holds {len(candidates)} .jsonl files ({names}…); pass the session file itself", code="ambiguous_input")
        return candidates[0]
    return path


def init_configuration(session_path, args):
    return {
        "workflow": RUN_STATE_WORKFLOW,
        "command": "init",
        "input": {"path": str(session_path)},
        "options": {
            "reportBudgetTokens": args.report_budget_tokens,
            "chunkChars": args.chunk_chars,
            "sessionVersion": SUPPORTED_SESSION_VERSION,
            "estimatedTokensMethod": "ceil(characters / 4)",
        },
    }


def require_run_directory(raw_path):
    path = Path(raw_path).expanduser().resolve()
    if not path.is_dir():
        fail(f"run directory does not exist: {path}", code="missing_run")
    if not (path / "run_config.json").is_file():
        fail(f"run_config.json is missing: {path}", code="missing_run")
    return path


def load_run(run_directory):
    try:
        run = json.loads((run_directory / "run_config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail(f"could not read run_config.json: {error}", code="corrupt_run")
    if run.get("schemaVersion") != RUN_SCHEMA_VERSION:
        fail(f"unsupported run schema version: {run.get('schemaVersion')}", code="corrupt_run")
    return run


def load_state(run_directory):
    try:
        return run_state.load_run_state(run_directory, RUN_STATE_WORKFLOW)
    except ValueError as error:
        fail(str(error), code="corrupt_run")


def load_json_artifact(run_directory, name):
    path = run_directory / name
    if not path.is_file():
        fail(f"{name} is missing: {path}; re-run init in a fresh output directory", code="corrupt_run")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail(f"could not read {name}: {error}", code="corrupt_run")


def load_timeline(run_directory):
    path = run_directory / "timeline.txt"
    if not path.is_file():
        fail(f"timeline.txt is missing: {path}", code="corrupt_run")
    return path.read_text(encoding="utf-8")


def input_drift_check(run):
    source = Path(run["input"]["path"])
    if not source.is_file():
        return f"session log is missing: {source}"
    if sha256_file(source) != run["input"]["sha256"]:
        return f"session log changed after init: {source}"
    return None


def require_stable_input(run):
    """A session log is an immutable artifact, so there is no refresh path:
    drift means the analysis no longer describes this file, and the answer is
    a new run directory, not reconciliation."""
    drift = input_drift_check(run)
    if drift:
        fail(f"{drift}; start a new run against the current file", code="input_drift")


def load_chunk_results(run_directory):
    path = run_directory / "chunk_results.jsonl"
    try:
        rows, _warnings = run_state.read_jsonl_recover_tail(path, repair=True)
    except ValueError as error:
        fail(str(error), code="corrupt_run")
    effective = {}
    for row in rows:
        chunk_id = row.get("chunkId")
        if chunk_id in effective and not row.get("supersedes"):
            fail(f"duplicate result for chunk {chunk_id}", code="corrupt_run")
        effective[chunk_id] = row
    return effective


def load_escalation_outcomes(run_directory):
    path = run_directory / "escalations.jsonl"
    try:
        rows, _warnings = run_state.read_jsonl_recover_tail(path, repair=True)
    except ValueError as error:
        fail(str(error), code="corrupt_run")
    return {row["id"]: row for row in rows if row.get("id")}


def next_evidence_number(run_directory, results):
    """The next unused p-number. Ids are never reused: a retried chunk's old
    ids linger in the verification journal, and a reused id would inherit a
    stale verdict, so the scan covers the journals as well as the live rows."""
    highest = 0
    for row in results.values():
        for item in row.get("items") or []:
            match = re.fullmatch(r"p(\d{6})", str(item.get("id") or ""))
            if match:
                highest = max(highest, int(match.group(1)))
    for name in ("verified.jsonl", "escalations.jsonl"):
        path = run_directory / name
        if not path.is_file():
            continue
        try:
            rows, _warnings = run_state.read_jsonl_recover_tail(path, repair=False)
        except ValueError:
            rows = []
        for row in rows:
            match = re.fullmatch(r"p(\d{6})", str(row.get("id") or ""))
            if match:
                highest = max(highest, int(match.group(1)))
    return highest + 1


def project_evidence(run_directory, run, results=None, write=True):
    """Rebuild evidence.jsonl from the journals.

    The projection overlays verification verdicts and escalation outcomes on
    the extracted items: ok becomes verified, a flag with a successful
    escalation becomes the corrected item, a flag without one becomes
    needs_review, and an explicit reviewer drop stays visible as dropped -
    review never silently removes anything. ``write=False`` computes the
    projection without touching disk, for read-only commands.
    """
    if results is None:
        results = load_chunk_results(run_directory)
    verdicts = forge_verify.load_verdicts(run_directory / "verified.jsonl")
    escalations = load_escalation_outcomes(run_directory)
    items = []
    for chunk in run["chunks"]:
        row = results.get(chunk["chunkId"])
        if not row:
            continue
        for item in row.get("items") or []:
            projected = {**item, "sessionId": run["input"]["sessionId"], "chunkId": chunk["chunkId"], "status": "extracted"}
            verdict = verdicts.get(item["id"])
            if verdict:
                if verdict["verdict"] == forge_verify.VERDICT_OK:
                    projected["status"] = "verified"
                else:
                    outcome = escalations.get(item["id"])
                    if outcome and outcome.get("dropped"):
                        projected["status"] = "dropped"
                        projected["reviewNote"] = truncate(
                            f"reviewer: {verdict.get('reason')}; escalation: {outcome.get('reason')}", 400
                        )
                    elif outcome and isinstance(outcome.get("item"), dict):
                        projected = {
                            **outcome["item"],
                            "id": item["id"],
                            "sessionId": run["input"]["sessionId"],
                            "chunkId": chunk["chunkId"],
                            "status": "escalated",
                            "reviewNote": truncate(f"reviewer: {verdict.get('reason')}", 400),
                        }
                    else:
                        projected["status"] = "needs_review"
                        projected["reviewNote"] = truncate(f"reviewer: {verdict.get('reason')}", 400)
            items.append(projected)
    if write:
        text = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items)
        run_state.atomic_write_text(run_directory / "evidence.jsonl", text)
    return items


def pending_chunks(run, results):
    return [chunk["chunkId"] for chunk in run["chunks"] if chunk["chunkId"] not in results]


def chunk_by_id(run, chunk_id):
    for chunk in run["chunks"]:
        if chunk["chunkId"] == chunk_id:
            return chunk
    return None


def chunk_text(run_directory, chunk_id):
    path = run_directory / "chunks" / f"{chunk_id}.txt"
    if not path.is_file():
        fail(f"chunk file is missing: {path}", code="corrupt_run")
    return path.read_text(encoding="utf-8")


def set_phase(run_directory, phase, next_action, event):
    def mutate(state):
        state["phase"] = phase
        state["nextAction"] = next_action
        return state

    return run_state.update_run_state(run_directory, mutate, event)


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #


def command_init(args):
    session_path = resolve_session_file(args.session)
    configuration = init_configuration(session_path, args)
    output = Path(args.output).expanduser().resolve()
    if output.exists() and (output / "run_state.json").is_file():
        try:
            state = run_state.load_run_state(output, RUN_STATE_WORKFLOW)
            run_state.assert_compatible_run(state, configuration)
        except ValueError as error:
            fail(str(error), code="incompatible_run")
        run = load_run(output)
        results = load_chunk_results(output)
        emit(
            data={
                "runDirectory": str(output),
                "resumed": True,
                "phase": state["phase"],
                "nextAction": state.get("nextAction"),
                "complete": state["status"] == "complete",
                "chunks": len(run["chunks"]),
                "recorded": len(results),
                "inputDrift": input_drift_check(run),
            }
        )
        return
    if output.exists() and any(output.iterdir()):
        fail(f"output directory is populated but has no run_state.json; refusing legacy or unrelated directory: {output}", code="legacy_output")

    try:
        header, entries, parse_warnings = parse_session_v3(session_path)
    except UserError as error:
        fail(str(error), code="unsupported_format")
    pair_findings = pair_tool_calls(entries)
    seeds = scan_session(entries, pair_findings)
    timeline, index, render_warnings = render_timeline(entries, args.chunk_chars)
    chunks = chunk_spans(index, args.chunk_chars)
    for chunk in chunks:
        chunk["seedIds"] = [
            seed["id"] for seed in seeds if chunk["lineStart"] <= seed["lines"][0] <= chunk["lineEnd"]
        ]
    brief = session_brief(header, entries, seeds)
    warnings = parse_warnings + render_warnings

    output.mkdir(parents=True, exist_ok=True)
    run = {
        "schemaVersion": RUN_SCHEMA_VERSION,
        "createdAt": utc_now(),
        "input": {
            "path": str(session_path),
            "sha256": sha256_file(session_path),
            "bytes": session_path.stat().st_size,
            "entries": len(entries),
            "sessionId": header["sessionId"],
        },
        "options": configuration["options"],
        "brief": brief,
        "skillsSeen": brief["skillsSeen"],
        "chunks": chunks,
    }
    run_state.atomic_write_text(output / "timeline.txt", timeline)
    run_state.atomic_write_json(output / "timeline_index.json", index)
    run_state.atomic_write_json(
        output / "scan.json",
        {"seeds": seeds, "skillsSeen": brief["skillsSeen"], "counts": brief["seedCounts"]},
    )
    (output / "chunks").mkdir(exist_ok=True)
    for chunk in chunks:
        run_state.atomic_write_text(
            output / "chunks" / f"{chunk['chunkId']}.txt", timeline[chunk["charStart"] : chunk["charEnd"]] + "\n"
        )
    run_state.atomic_write_json(output / "run_config.json", run)
    run_state.atomic_write_text(output / "chunk_results.jsonl", "")
    state = run_state.create_run_state(
        RUN_STATE_WORKFLOW,
        "init",
        configuration["input"],
        configuration["options"],
        items=[{"id": chunk["chunkId"], "status": "pending", "attempts": 0, "transient": False} for chunk in chunks],
        phase="extract",
        next_action="extract",
    )
    state["warnings"] = warnings
    run_state.initialize_run_state(output, state)
    emit(
        artifacts=[str(output / "timeline.txt"), str(output / "scan.json")],
        warnings=warnings,
        data={
            "runDirectory": str(output),
            "sessionId": header["sessionId"],
            "entries": len(entries),
            "chunks": len(chunks),
            "timelineChars": len(timeline),
            "seeds": len([seed for seed in seeds if not seed["informational"]]),
            "skillsSeen": brief["skillsSeen"],
            "nextAction": "extract",
        },
    )


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


def command_status(args):
    run_directory = require_run_directory(args.run_directory)
    state = load_state(run_directory)
    run = load_run(run_directory)
    results = load_chunk_results(run_directory)
    items = project_evidence(run_directory, run, results, write=False)
    chunk_counts = Counter(row.get("status") for row in results.values())
    evidence_counts = Counter(item["status"] for item in items)
    sections = {}
    sections_dir = run_directory / "sections"
    if sections_dir.is_dir():
        sections = {path.stem: "complete" for path in sorted(sections_dir.glob("*.md"))}
    emit(
        data={
            "runDirectory": str(run_directory),
            "status": state["status"],
            "phase": state["phase"],
            "nextAction": state.get("nextAction"),
            "chunks": len(run["chunks"]),
            "recorded": len(results),
            "chunkCounts": {status: chunk_counts[status] for status in sorted(chunk_counts)},
            "evidence": len(items),
            "evidenceCounts": {status: evidence_counts[status] for status in sorted(evidence_counts)},
            "sections": sections,
            "inputDrift": input_drift_check(run),
            "warnings": state.get("warnings", []),
        }
    )


# --------------------------------------------------------------------------- #
# Evidence validation and quote verification. Deterministic checks run before
# any model review: a quotation either is in the rendered timeline or it is
# fabricated, and that check is worth more than any prompt rule.
# --------------------------------------------------------------------------- #


def normalize_for_quote_match(text):
    return WHITESPACE_RE.sub(" ", str(text or "").translate(QUOTE_CHARACTERS)).strip().casefold()


def quote_fragments(quotes):
    """Split a quote into independently-checkable fragments.

    Sentences are checked separately - measured on the example session, the
    small model reorders adjacent sentences inside an otherwise verbatim
    quote, and per-sentence matching keeps byte-exactness without failing
    real text over its order.
    """
    fragments = [part for part in re.split(r'["“”]|\.\.\.|…|\n|;\s|\.\s+', str(quotes)) if part.strip()]
    return fragments or [str(quotes)]


def seed_metadata_texts(seeds):
    """Normalized seed detail/excerpt strings, for salvaging metadata quotes."""
    texts = []
    for seed in seeds:
        for key in ("detail", "excerpt"):
            value = seed.get(key)
            if value:
                texts.append(normalize_for_quote_match(value))
    return texts


def normalized_entry_slices(timeline, index_rows):
    return [(row, normalize_for_quote_match(timeline[row["charStart"] : row["charEnd"]])) for row in index_rows]


def check_item_quotes(item, chunk_slices, all_slices, seed_texts):
    """Verify one item's quotes; repair what is repairable deterministically.

    Returns (violations, corrected_line, salvage). Small models fumble line
    numbers far more often than they fabricate text, so a quote that exists in
    the timeline but not in the cited entry relocates the locator instead of
    burning the corrective retry. A quote copied from the scan seeds' own
    detail/excerpt metadata - a failure the live gate observed repeatedly - is
    salvaged by nulling the quote: the claim keeps its seed corroboration, and
    only genuinely invented text stays a violation.
    """
    quotes = item.get("direct_quotes")
    if not quotes:
        return [], None, False
    if ELIDE_MARKER_RE.search(str(quotes)):
        return ["direct_quotes includes an ELIDED marker; quote only from visible timeline text"], None, False
    cited_line = item["locator"]["line"]
    cited = next((pair for pair in chunk_slices if pair[0]["line"] == cited_line), None)
    if cited is None:
        cited = next((pair for pair in all_slices if pair[0]["line"] == cited_line), None)
    corrected_line = None
    salvage = False
    for fragment in quote_fragments(quotes):
        candidate = normalize_for_quote_match(fragment)
        if len(candidate) < QUOTE_MIN_MATCH_CHARACTERS:
            continue
        if cited is not None and candidate in cited[1]:
            continue
        home = next((row for row, slice_text in chunk_slices if candidate in slice_text), None)
        if home is None:
            home = next((row for row, slice_text in all_slices if candidate in slice_text), None)
        if home is not None:
            if corrected_line is None:
                corrected_line = home["line"]
            continue
        if any(candidate in text or text in candidate for text in seed_texts):
            salvage = True
            continue
        return [f"direct_quotes not found in the timeline: {fragment.strip()[:120]!r}"], None, False
    if corrected_line is not None and corrected_line == cited_line:
        corrected_line = None
    return [], corrected_line, salvage


def validate_chunk_response(value, skills, line_numbers, allowed_seed_ids):
    """Normalize one chunk's extraction response, returning (result, errors).

    Items are validated strictly - they become citable evidence. The carry
    fields (open_threads, chunk_summary) are coerced leniently: they are aids
    for the next chunk, not evidence, and a missing key there should not cost
    a retry.
    """
    if not isinstance(value, dict):
        return None, ['response must be a JSON object with "items", "open_threads", and "chunk_summary"']
    raw_items = value.get("items")
    if not isinstance(raw_items, list):
        return None, ['"items" must be a JSON array']
    if len(raw_items) > MAX_ITEMS_PER_CHUNK:
        return None, [f'"items" has {len(raw_items)} entries; the contract allows at most {MAX_ITEMS_PER_CHUNK}']
    items = []
    errors = []
    for index, item in enumerate(raw_items, start=1):
        if not isinstance(item, dict):
            errors.append(f"item {index} is not an object")
            continue
        item_type = item.get("item_type")
        alias_note = None
        if item_type in ITEM_TYPE_ALIASES:
            alias_note = f"item_type normalized from seed kind {item_type!r}"
            item_type = ITEM_TYPE_ALIASES[item_type]
        if item_type not in ITEM_TYPE_SET:
            errors.append(
                f"item {index} has invalid item_type {item.get('item_type')!r} - a seed's kind is not an "
                f"item_type; classify the friction it caused. Expected one of {', '.join(ITEM_TYPES)}"
            )
            continue
        severity = item.get("severity")
        if severity not in SEVERITY_WEIGHT:
            errors.append(f"item {index} has invalid severity {severity!r}; expected one of {', '.join(SEVERITIES)}")
            continue
        attribution = item.get("attribution")
        if not isinstance(attribution, dict):
            errors.append(f"item {index} requires an attribution object")
            continue
        skill = attribution.get("skill")
        if skill is not None and skill not in skills:
            errors.append(f"item {index} attributes skill {skill!r}, which this session never touched; use one of {', '.join(skills) or '(none)'} or null")
            continue
        layer = attribution.get("layer")
        if layer not in LAYERS:
            errors.append(f"item {index} has invalid layer {layer!r}; expected one of {', '.join(LAYERS)}")
            continue
        if blank(item.get("text")):
            errors.append(f"item {index} requires a nonblank text value")
            continue
        locator = item.get("locator")
        line = locator.get("line") if isinstance(locator, dict) else None
        if not isinstance(line, int) or line not in line_numbers:
            errors.append(f"item {index} locator.line {line!r} is not an entry line in the timeline")
            continue
        interpretation = item.get("interpretation")
        if interpretation not in INTERPRETATIONS:
            errors.append(f"item {index} has invalid interpretation {interpretation!r}; expected explicit, inferred, or unclear")
            continue
        confidence = item.get("confidence")
        if confidence not in CONFIDENCES:
            errors.append(f"item {index} has invalid confidence {confidence!r}; expected high, medium, or low")
            continue
        seed_ids = item.get("seed_ids")
        if seed_ids is None:
            seed_ids = []
        if not isinstance(seed_ids, list) or any(seed not in allowed_seed_ids for seed in seed_ids):
            errors.append(f"item {index} seed_ids must be a list of seed ids present in this chunk")
            continue
        change_type = item.get("change_type")
        if change_type not in CHANGE_TYPES:
            errors.append(f"item {index} has invalid change_type {change_type!r}; expected one of {', '.join(CHANGE_TYPES)}")
            continue
        invalid_optional = [
            optional
            for optional in ("direct_quotes", "recommendation_hint", "notes")
            if item.get(optional) is not None and not isinstance(item.get(optional), str)
        ]
        if invalid_optional:
            errors.append(f"item {index} field {invalid_optional[0]} must be a string or null")
            continue
        normalized = {
            "item_type": item_type,
            "severity": severity,
            "attribution": {"skill": skill, "layer": layer},
            "text": item["text"],
            "direct_quotes": item.get("direct_quotes"),
            "locator": {"line": line},
            "interpretation": interpretation,
            "confidence": confidence,
            "seed_ids": seed_ids,
            "change_type": change_type,
            "recommendation_hint": item.get("recommendation_hint"),
            "notes": item.get("notes"),
        }
        if alias_note:
            append_note(normalized, alias_note)
        items.append(normalized)
    if errors:
        return None, errors
    threads = value.get("open_threads")
    if not isinstance(threads, list):
        threads = []
    threads = [truncate(thread, OPEN_THREAD_CHARS) for thread in threads if isinstance(thread, str) and thread.strip()]
    summary = value.get("chunk_summary")
    result = {
        "items": items,
        "openThreads": threads[:OPEN_THREADS_MAX],
        "chunkSummary": truncate(summary if isinstance(summary, str) else "", CHUNK_SUMMARY_CHARS),
    }
    return result, []


def append_note(item, note):
    item["notes"] = f"{item['notes']}; {note}" if item.get("notes") else note


def verify_and_relocate_quotes(items, chunk_slices, all_slices, seed_texts=()):
    violations = []
    for index, item in enumerate(items, start=1):
        item_violations, corrected_line, salvage = check_item_quotes(item, chunk_slices, all_slices, seed_texts)
        if item_violations:
            violations.extend(f"item {index}: {violation}" for violation in item_violations)
            continue
        if salvage:
            item["direct_quotes"] = None
            append_note(item, "quote was scan metadata, not timeline text; removed")
        if corrected_line is not None:
            original = item["locator"]["line"]
            item["locator"] = {"line": corrected_line}
            append_note(item, f"locator corrected from L{original}")
    return violations


# --------------------------------------------------------------------------- #
# extract - serial over chunks on the chat service. Serial by design: each
# chunk's open_threads chain into the next, which is how an issue spanning
# chunk boundaries (a truncation loop, a long remediation) survives them.
# --------------------------------------------------------------------------- #


def model_call_json(run_directory, service, messages, **options):
    value, record = forge_llm.call_json_with_retry(service, messages, **options)
    run_state.append_jsonl_fsync(run_directory / "inference_journal.jsonl", record)
    return value


def extract_user_payload(run, seeds, chunk, chunk_body, open_threads, previous_summary):
    chunk_seed_ids = set(chunk.get("seedIds") or [])
    seeds_in_chunk = [
        {key: seed[key] for key in ("id", "kind", "lines", "tool", "detail", "excerpt")}
        for seed in seeds
        if seed["id"] in chunk_seed_ids and not seed["informational"]
    ]
    elsewhere = Counter(
        seed["kind"] for seed in seeds if seed["id"] not in chunk_seed_ids and not seed["informational"]
    )
    payload = {
        "chunkIndex": int(chunk["chunkId"][1:]),
        "chunkCount": len(run["chunks"]),
        "sessionBrief": run["brief"],
        "seeds": seeds_in_chunk,
        "seedCountsElsewhere": dict(sorted(elsewhere.items())),
        "openThreads": open_threads,
        "previousChunkSummary": previous_summary,
        "timelineChunk": chunk_body,
    }
    return json.dumps(payload, ensure_ascii=False)


def attempt_chunk(run_directory, service, messages, run, chunk, seeds, chunk_slices, all_slices, line_index, args):
    """One extraction attempt. Transient transport failures propagate so the
    command aborts with the chunk still pending, rather than laundering an
    unreachable endpoint into a needs_review verdict on every chunk."""
    try:
        value = model_call_json(
            run_directory,
            service,
            messages,
            max_tokens=EXTRACT_MAX_TOKENS,
            background=not args.foreground,
            cache_prompt=args.cache_prompt,
            timeout=args.request_timeout,
            task="skill-tuner-extract",
        )
    except forge_llm.ContextBudgetError as error:
        return None, [str(error)]
    except forge_llm.ChatError as error:
        if run_state.is_transient_failure(error):
            raise
        return None, [str(error)]
    chunk_seed_ids = set(chunk.get("seedIds") or [])
    chunk_seeds = [seed for seed in seeds if seed["id"] in chunk_seed_ids]
    result, errors = validate_chunk_response(value, set(run["skillsSeen"]), line_index, {seed["id"] for seed in chunk_seeds})
    if errors:
        return None, errors
    violations = verify_and_relocate_quotes(result["items"], chunk_slices, all_slices, seed_metadata_texts(chunk_seeds))
    if violations:
        return None, violations
    return result, []


def extract_chunk(run_directory, service, run, seeds, timeline, index, chunk, open_threads, previous_summary, args):
    body = chunk_text(run_directory, chunk["chunkId"])
    chunk_rows = [row for row in index if chunk["lineStart"] <= row["line"] <= chunk["lineEnd"]]
    chunk_slices = normalized_entry_slices(timeline, chunk_rows)
    all_slices = normalized_entry_slices(timeline, index)
    line_index = {row["line"] for row in index}
    messages = [
        {"role": "system", "content": EXTRACT_SYSTEM},
        {"role": "user", "content": extract_user_payload(run, seeds, chunk, body, open_threads, previous_summary)},
    ]
    result, errors = attempt_chunk(run_directory, service, messages, run, chunk, seeds, chunk_slices, all_slices, line_index, args)
    if errors:
        repair = [
            *messages,
            {"role": "user", "content": "That response was unusable: " + "; ".join(errors[:5]) + ". Return corrected JSON only."},
        ]
        result, errors = attempt_chunk(run_directory, service, repair, run, chunk, seeds, chunk_slices, all_slices, line_index, args)
        if errors:
            raise UserError("; ".join(errors[:5]))
    return result


def record_chunk(run_directory, run, chunk_id, status, result, note, carried_threads, supersedes=False):
    results = load_chunk_results(run_directory)
    items = result["items"] if result else []
    number = next_evidence_number(run_directory, results)
    for offset, item in enumerate(items):
        item["id"] = f"p{number + offset:06d}"
    row = {
        "chunkId": chunk_id,
        "status": status,
        "items": items,
        "openThreads": result["openThreads"] if result else carried_threads,
        "chunkSummary": result["chunkSummary"] if result else "",
        "note": note,
        "recordedAt": utc_now(),
    }
    if supersedes:
        row["supersedes"] = True
    run_state.append_jsonl_fsync(run_directory / "chunk_results.jsonl", row)
    results[chunk_id] = row
    project_evidence(run_directory, run, results)
    remaining = len(pending_chunks(run, results))

    def mutate(state):
        for item in state.get("items", []):
            if item.get("id") == chunk_id:
                item["status"] = status
                item["attempts"] = item.get("attempts", 0) + 1
        state["phase"] = "extract" if remaining else "verify"
        state["nextAction"] = "extract" if remaining else "verify"
        return state

    run_state.update_run_state(
        run_directory,
        mutate,
        {"type": "item_completed", "itemId": chunk_id, "phase": "extract", "status": status},
    )
    return remaining


def carried_context(run, results, chunk_id):
    """The open threads and summary from the last recorded chunk before this one."""
    threads = []
    summary = ""
    for chunk in run["chunks"]:
        if chunk["chunkId"] == chunk_id:
            break
        row = results.get(chunk["chunkId"])
        if row:
            threads = row.get("openThreads") or []
            summary = row.get("chunkSummary") or ""
    return threads, summary


def command_extract(args):
    run_directory = require_run_directory(args.run_directory)
    with run_state.run_lock(run_directory):
        load_state(run_directory)
        run = load_run(run_directory)
        require_stable_input(run)
        seeds = load_json_artifact(run_directory, "scan.json")["seeds"]
        timeline = load_timeline(run_directory)
        index = load_json_artifact(run_directory, "timeline_index.json")
        service = forge_llm.resolve_service("chat", base_url=args.base_url, model=args.model)
        if not service["enabled"]:
            fail("connectedServices.chat is disabled; configure the local chat endpoint before extracting", code="service_disabled")

        processed = 0
        needs_review = 0
        limit = args.limit if args.limit and args.limit > 0 else None
        while True:
            results = load_chunk_results(run_directory)
            pending = pending_chunks(run, results)
            if not pending or (limit is not None and processed >= limit):
                break
            chunk = chunk_by_id(run, pending[0])
            position = len(results) + 1
            total = len(run["chunks"])
            progress(f"[{position}/{total}] {chunk['chunkId']} L{chunk['lineStart']}-L{chunk['lineEnd']}")
            threads, summary = carried_context(run, results, chunk["chunkId"])
            try:
                result = extract_chunk(run_directory, service, run, seeds, timeline, index, chunk, threads, summary, args)
                remaining = record_chunk(run_directory, run, chunk["chunkId"], "success", result, None, threads)
                progress(f"[{position}/{total}] {chunk['chunkId']}: {len(result['items'])} items, {len(result['openThreads'])} open threads")
            except UserError as error:
                needs_review += 1
                remaining = record_chunk(run_directory, run, chunk["chunkId"], "needs_review", None, f"extraction failed: {error}", threads)
                progress(f"[{position}/{total}] {chunk['chunkId']}: needs review ({error})")
            except InterruptedError:
                run_state.append_run_event(run_directory, {"type": "extract_preempted", "itemId": chunk["chunkId"]})
                emit(
                    warnings=["extraction was preempted by interactive activity; run extract again to resume"],
                    data={"processed": processed, "remaining": len(pending), "nextAction": "extract"},
                )
                return
            except forge_llm.ChatError as error:
                fail(f"chat endpoint unreachable at chunk {chunk['chunkId']}: {error}", code="endpoint_unreachable")
            processed += 1

        results = load_chunk_results(run_directory)
        remaining = len(pending_chunks(run, results))
        emit(
            data={
                "processed": processed,
                "needsReview": needs_review,
                "remaining": remaining,
                "evidence": sum(len(row.get("items") or []) for row in results.values()),
                "nextAction": "extract" if remaining else "verify",
            }
        )


# --------------------------------------------------------------------------- #
# verify - one batched review pass on the thinking service, after all bulk
# extraction. Every payload carries its own evidence inline (quote, locator,
# deterministic corroboration): a verifier shown only paraphrases rubber-stamps.
# An unreachable verifier is recorded as skipped, never read as approval.
# --------------------------------------------------------------------------- #


def corroboration_for(item, seeds_by_id):
    details = [seeds_by_id[seed_id]["detail"] for seed_id in item.get("seed_ids") or [] if seed_id in seeds_by_id]
    if details:
        return truncate("; ".join(details), 300)
    return "no deterministic corroboration (narrative-derived)"


def verification_payload(item, seeds_by_id, index_by_line):
    row = index_by_line.get(item["locator"]["line"], {})
    return {
        "id": item["id"],
        "item_type": item["item_type"],
        "severity": item["severity"],
        "layer": item["attribution"]["layer"],
        "skill": item["attribution"]["skill"],
        "claim": truncate(item["text"], 400),
        "quote": truncate(item.get("direct_quotes") or "", 300) or None,
        "locator": {"line": item["locator"]["line"], "kind": row.get("kind"), "tool": row.get("tool")},
        "interpretation": item["interpretation"],
        "corroboration": corroboration_for(item, seeds_by_id),
    }


def load_report_meta(run_directory):
    path = run_directory / "report_meta.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def update_report_meta(run_directory, updates):
    meta = load_report_meta(run_directory)
    meta.update(updates)
    meta["updatedAt"] = utc_now()
    run_state.atomic_write_json(run_directory / "report_meta.json", meta)
    return meta


def compute_coverage(items, seeds):
    citable = [item for item in items if item["status"] in CITABLE_ITEM_STATUSES]
    types_present = sorted({item["item_type"] for item in citable})
    non_informational = [seed for seed in seeds if not seed["informational"]]
    explained = set()
    for item in citable:
        explained.update(item.get("seed_ids") or [])
    unexplained = [seed["id"] for seed in non_informational if seed["id"] not in explained]
    return {
        "itemTypesPresent": types_present,
        "itemTypesAbsent": [item_type for item_type in ITEM_TYPES if item_type not in types_present],
        "seedKindsPresent": sorted({seed["kind"] for seed in non_informational}),
        "seedsTotal": len(non_informational),
        "seedsExplained": len(non_informational) - len(unexplained),
        "seedsUnexplained": unexplained,
    }


def escalation_redo(run_directory, think, run, seeds, timeline, index, items_by_id, args):
    all_slices = normalized_entry_slices(timeline, index)
    line_index = {row["line"] for row in index}

    def redo(payload, reason):
        original = items_by_id[payload["id"]]
        chunk = chunk_by_id(run, original["chunkId"])
        body = chunk_text(run_directory, original["chunkId"])
        chunk_rows = [row for row in index if chunk["lineStart"] <= row["line"] <= chunk["lineEnd"]]
        chunk_slices = normalized_entry_slices(timeline, chunk_rows)
        item_fields = {key: value for key, value in original.items() if key not in ("id", "sessionId", "chunkId", "status", "reviewNote")}
        messages = [
            {"role": "system", "content": ESCALATE_SYSTEM},
            {
                "role": "user",
                "content": json.dumps(
                    {"objection": reason, "item": item_fields, "timelineChunk": body}, ensure_ascii=False
                ),
            },
        ]
        value = model_call_json(
            run_directory,
            think,
            messages,
            max_tokens=DEFAULT_MAX_TOKENS,
            background=not args.foreground,
            timeout=args.request_timeout,
            task="skill-tuner-escalate",
        )
        if isinstance(value, dict) and value.get("drop") is True:
            return {"drop": True, "reason": str(value.get("reason") or "reviewer objection upheld")}
        chunk_seed_ids = set(chunk.get("seedIds") or [])
        chunk_seeds = [seed for seed in seeds if seed["id"] in chunk_seed_ids]
        result, errors = validate_chunk_response(
            {"items": [value], "open_threads": [], "chunk_summary": ""},
            set(run["skillsSeen"]),
            line_index,
            {seed["id"] for seed in chunk_seeds},
        )
        if not errors:
            errors = verify_and_relocate_quotes(result["items"], chunk_slices, all_slices, seed_metadata_texts(chunk_seeds))
        if errors:
            raise UserError("; ".join(errors[:5]))
        return result["items"][0]

    return redo


def command_verify(args):
    run_directory = require_run_directory(args.run_directory)
    with run_state.run_lock(run_directory):
        load_state(run_directory)
        run = load_run(run_directory)
        require_stable_input(run)
        results = load_chunk_results(run_directory)
        pending = pending_chunks(run, results)
        if pending:
            fail(f"{len(pending)} chunks are still unextracted; run extract first", code="wrong_phase")
        seeds = load_json_artifact(run_directory, "scan.json")["seeds"]
        seeds_by_id = {seed["id"]: seed for seed in seeds}
        timeline = load_timeline(run_directory)
        index = load_json_artifact(run_directory, "timeline_index.json")
        index_by_line = {row["line"]: row for row in index}
        items = project_evidence(run_directory, run, results)
        reviewable = [item for item in items if item["status"] != "dropped"]

        verification = None
        if not reviewable:
            verification = {"skipped": "no evidence items were extracted"}
        else:
            think = forge_llm.resolve_think_or_chat(base_url=args.think_url, model=args.think_model)
            if not think["enabled"]:
                verification = {"skipped": "no thinking service is configured"}
            else:
                payloads = [verification_payload(item, seeds_by_id, index_by_line) for item in reviewable]
                progress(f"verifying {len(payloads)} evidence items on {think['url']}")
                try:
                    verdicts = forge_verify.verify_packets(
                        think,
                        VERIFY_SYSTEM,
                        payloads,
                        journal_path=run_directory / "verified.jsonl",
                        packet_size=args.verify_packet_size,
                        background=not args.foreground,
                        timeout=args.request_timeout,
                        progress=progress,
                    )
                except forge_verify.VerificationError as error:
                    verification = {"skipped": str(error)}
                except InterruptedError:
                    run_state.append_run_event(run_directory, {"type": "verify_preempted"})
                    emit(
                        warnings=["verification was preempted by interactive activity; run verify again to resume"],
                        data={"nextAction": "verify"},
                    )
                    return
                else:
                    items_by_id = {item["id"]: item for item in reviewable}
                    flagged = [
                        (payload, verdicts[payload["id"]]["reason"])
                        for payload in payloads
                        if verdicts.get(payload["id"], {}).get("verdict") == forge_verify.VERDICT_FLAG
                    ]
                    redo = escalation_redo(run_directory, think, run, seeds, timeline, index, items_by_id, args)
                    try:
                        escalations = forge_verify.escalate(
                            flagged, redo, journal_path=run_directory / "verified.jsonl", progress=progress
                        )
                    except InterruptedError:
                        run_state.append_run_event(run_directory, {"type": "verify_preempted"})
                        emit(
                            warnings=["escalation was preempted by interactive activity; run verify again to resume"],
                            data={"nextAction": "verify"},
                        )
                        return
                    for item_id, outcome in escalations.items():
                        if outcome.get("resumed") or not outcome.get("ok"):
                            continue
                        value = outcome["value"]
                        row = {"id": item_id, "at": utc_now()}
                        if isinstance(value, dict) and value.get("drop"):
                            row["dropped"] = True
                            row["reason"] = value.get("reason")
                        else:
                            row["item"] = value
                        run_state.append_jsonl_fsync(run_directory / "escalations.jsonl", row)
                    verification = forge_verify.summarize(verdicts, escalations)

        items = project_evidence(run_directory, run, results)
        coverage = compute_coverage(items, seeds)
        update_report_meta(run_directory, {"verification": verification, "coverage": coverage})
        set_phase(run_directory, "synthesize", "synthesize", {"type": "verification_recorded", "verification": verification})
        counts = Counter(item["status"] for item in items)
        emit(
            data={
                "verification": verification,
                "coverage": {key: coverage[key] for key in ("itemTypesPresent", "itemTypesAbsent", "seedsExplained", "seedsTotal")},
                "evidenceCounts": {status: counts[status] for status in sorted(counts)},
                "nextAction": "synthesize",
            }
        )


# --------------------------------------------------------------------------- #
# synthesize - deterministic merge and ranking first, then an authoring
# context bounded to fit one slot. Embedding clusters are advisory retrieval
# aids and never merge anything; the deterministic merge is the behavior.
# --------------------------------------------------------------------------- #


def _layer_for_members(members):
    counts = Counter(member["attribution"]["layer"] for member in members)
    best = max(counts.items(), key=lambda pair: (pair[1], -LAYERS.index(pair[0])))
    return best[0]


def merge_evidence(items):
    """Merge items sharing (item_type, skill) that describe the same event -
    same locator line or overlapping seeds - keeping the maximum severity."""
    eligible = [item for item in items if item["status"] in CITABLE_ITEM_STATUSES]
    subgroups = {}
    for item in eligible:
        subgroups.setdefault((item["item_type"], item["attribution"]["skill"] or ""), []).append(item)
    groups = []
    for (item_type, skill), members in sorted(subgroups.items()):
        parent = list(range(len(members)))

        def find(node):
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                same_line = members[i]["locator"]["line"] == members[j]["locator"]["line"]
                shared_seed = set(members[i].get("seed_ids") or []) & set(members[j].get("seed_ids") or [])
                if same_line or shared_seed:
                    root_i, root_j = find(i), find(j)
                    if root_i != root_j:
                        parent[max(root_i, root_j)] = min(root_i, root_j)
        components = {}
        for node in range(len(members)):
            components.setdefault(find(node), []).append(members[node])
        for component in components.values():
            severity = max((member["severity"] for member in component), key=lambda value: SEVERITY_WEIGHT[value])
            seed_ids = sorted({seed for member in component for seed in member.get("seed_ids") or []})
            groups.append(
                {
                    "itemType": item_type,
                    "skill": skill or None,
                    "layer": _layer_for_members(component),
                    "severity": severity,
                    "itemIds": sorted(member["id"] for member in component),
                    "lines": sorted({member["locator"]["line"] for member in component}),
                    "seedIds": seed_ids,
                    "changeTypes": sorted({member["change_type"] for member in component}),
                    "weight": SEVERITY_WEIGHT[severity] * len(component),
                }
            )
    groups.sort(key=lambda group: (-group["weight"], group["lines"][0], group["itemType"]))
    for number, group in enumerate(groups, start=1):
        group["groupId"] = f"g{number:04d}"
    return groups


def bucket_groups(groups):
    buckets = {"skills": {}, "backend": [], "crosscutting": []}
    for group in groups:
        if group["layer"] in ("backend", "harness"):
            buckets["backend"].append(group["groupId"])
        elif group["layer"] == "skill" and group["skill"]:
            buckets["skills"].setdefault(group["skill"], []).append(group["groupId"])
        else:
            buckets["crosscutting"].append(group["groupId"])
    return buckets


def advisory_clusters(groups, items_by_id, args):
    """Embedding-linked group ids, advisory only; degrades to nothing."""
    if getattr(args, "no_embeddings", False) or len(groups) < 2:
        return [], None
    try:
        texts = [truncate(items_by_id[group["itemIds"][0]]["text"], EMBED_TEXT_CHARS) for group in groups]
        result = forge_embeddings.embed_texts(texts, url=getattr(args, "embeddings_url", None))
        if not result["ok"]:
            return [], f"embedding advisory skipped: {result['reason']}"
        normalized = [forge_embeddings.normalize(vector) for vector in result["vectors"]]
        components = forge_embeddings.cluster_components(normalized, EMBED_GROUP_THRESHOLD)
        clusters = [
            sorted(groups[position]["groupId"] for position in component)
            for component in components
            if len(component) > 1
        ]
        return sorted(clusters), None
    except Exception as error:  # noqa: BLE001 - advisory path must never sink the run
        return [], f"embedding advisory skipped: {type(error).__name__}: {error}"


def group_digest(group, items_by_id, index_by_line):
    lines_label = ",".join(f"L{line}" for line in group["lines"][:8])
    header = (
        f"[{group['groupId']}] {group['itemType']} | skill:{group['skill'] or '-'} | layer:{group['layer']} "
        f"| severity:{group['severity']} | items:{len(group['itemIds'])} | {lines_label}"
    )
    lines = [header]
    for item_id in group["itemIds"]:
        item = items_by_id[item_id]
        row = index_by_line.get(item["locator"]["line"], {})
        piece = (
            f"  [{item_id}] L{item['locator']['line']} ({row.get('kind')}"
            + (f" {row.get('tool')}" if row.get("tool") else "")
            + f") {item['severity']} {item['change_type']}: {truncate(item['text'], 300)}"
        )
        quote = item.get("direct_quotes")
        if quote:
            piece += f' | quote: "{truncate(quote, 200)}"'
        if item.get("recommendation_hint"):
            piece += f" | hint: {truncate(item['recommendation_hint'], 200)}"
        lines.append(piece)
    return "\n".join(lines)


def packetize_blocks(blocks, target_tokens):
    packets = []
    current = []
    current_tokens = 0
    for block in blocks:
        tokens = estimate_tokens(block)
        if current and current_tokens + tokens > target_tokens:
            packets.append("\n".join(current))
            current = []
            current_tokens = 0
        current.append(block)
        current_tokens += tokens
    if current:
        packets.append("\n".join(current))
    return packets or ["No evidence groups were recorded for this run."]


def memo_evidence_ids(text):
    return set(EVIDENCE_ID_RE.findall(text or ""))


def reduce_memo_call(run_directory, think, packet, character_budget, known_ids, args):
    messages = [
        {"role": "system", "content": REDUCE_SYSTEM},
        {"role": "user", "content": f"CHARACTER BUDGET: {character_budget}\n\nEVIDENCE RECORDS:\n{packet}"},
    ]
    for attempt in range(2):
        content, record = forge_llm.call(
            think,
            messages,
            max_tokens=DEFAULT_MAX_TOKENS,
            background=not args.foreground,
            timeout=args.request_timeout,
            task="skill-tuner-reduce",
        )
        run_state.append_jsonl_fsync(run_directory / "inference_journal.jsonl", record)
        unknown = sorted(memo_evidence_ids(content) - known_ids)
        if not unknown:
            return content.strip()
        if attempt == 0:
            messages = [
                *messages,
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": "That memo cites ids that do not exist: "
                    + ", ".join(unknown[:10])
                    + ". Rewrite the memo using only ids present in the evidence records.",
                },
            ]
    raise UserError(f"reduction memo kept citing unknown evidence ids: {', '.join(unknown[:5])}")


def reduce_to_budget(run_directory, think, blocks, budget_tokens, known_ids, args):
    """Recursively reduce digest blocks through memo levels until they fit.

    Memo files are the resume state: the packetization is deterministic, so a
    re-run recomputes the same packets and skips the memos already on disk.
    Returns (blocks, dropped_ids, levels).
    """
    dropped = set()
    level = 0
    while estimate_tokens("\n\n".join(blocks)) > budget_tokens and level < 6:
        level += 1
        packets = packetize_blocks(blocks, budget_tokens)
        packet_directory = run_directory / "synthesis" / "packets" / f"level-{level}"
        memo_directory = run_directory / "synthesis" / "memos" / f"level-{level}"
        memo_budget = max(2000, (budget_tokens * 4) // max(1, len(packets)))
        memos = []
        for number, packet in enumerate(packets, start=1):
            packet_path = packet_directory / f"p{number:04d}.md"
            memo_path = memo_directory / f"p{number:04d}.md"
            run_state.atomic_write_text(packet_path, packet.rstrip() + "\n")
            if memo_path.is_file():
                memo = memo_path.read_text(encoding="utf-8").strip()
            else:
                progress(f"[reduce level {level}] packet {number}/{len(packets)}")
                memo = reduce_memo_call(run_directory, think, packet, memo_budget, known_ids, args)
                run_state.atomic_write_text(memo_path, memo + "\n")
            dropped.update(memo_evidence_ids(packet) - memo_evidence_ids(memo))
            memos.append(memo)
        blocks = memos
        run_state.append_run_event(run_directory, {"type": "synthesis_level_reduced", "level": level, "packets": len(packets)})
    return blocks, sorted(dropped - memo_evidence_ids("\n\n".join(blocks))), level


def build_authoring_context(run, meta, groups, digest_blocks, excluded):
    parts = [
        f"# Authoring Context - skill-tuner session {run['input']['sessionId']}",
        "",
        "## Session Brief",
        "```json",
        json.dumps(run["brief"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## Verification and Coverage",
        "```json",
        json.dumps({"verification": meta.get("verification"), "coverage": meta.get("coverage")}, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    if excluded["needsReview"] or excluded["dropped"]:
        parts.append("## Excluded From Synthesis")
        for item_id, reason in excluded["needsReview"]:
            parts.append(f"- [{item_id}] needs review: {reason}")
        for item_id, reason in excluded["dropped"]:
            parts.append(f"- [{item_id}] dropped by review: {reason}")
        parts.append("")
    parts.append(f"## Ranked Evidence Groups ({len(groups)})")
    parts.append("")
    parts.extend(digest_blocks)
    return "\n".join(parts).rstrip() + "\n"


def command_synthesize(args):
    run_directory = require_run_directory(args.run_directory)
    with run_state.run_lock(run_directory):
        load_state(run_directory)
        run = load_run(run_directory)
        require_stable_input(run)
        meta = load_report_meta(run_directory)
        if "verification" not in meta:
            fail("verification has not run; run verify first (it records skipped when no thinking service exists)", code="wrong_phase")
        results = load_chunk_results(run_directory)
        if pending_chunks(run, results):
            fail("chunks are still unextracted; run extract first", code="wrong_phase")
        index = load_json_artifact(run_directory, "timeline_index.json")
        index_by_line = {row["line"]: row for row in index}
        items = project_evidence(run_directory, run, results)
        items_by_id = {item["id"]: item for item in items}

        groups = merge_evidence(items)
        buckets = bucket_groups(groups)
        clusters, advisory_warning = advisory_clusters(groups, items_by_id, args)
        excluded = {
            "needsReview": [(item["id"], truncate(item.get("reviewNote") or item.get("notes") or "", 200)) for item in items if item["status"] == "needs_review"],
            "dropped": [(item["id"], truncate(item.get("reviewNote") or "", 200)) for item in items if item["status"] == "dropped"],
        }
        run_state.atomic_write_json(
            run_directory / "synthesis" / "groups.json",
            {
                "groups": groups,
                "ranking": [group["groupId"] for group in groups],
                "buckets": buckets,
                "advisoryClusters": clusters,
                "excluded": {
                    "needsReview": [item_id for item_id, _reason in excluded["needsReview"]],
                    "dropped": [item_id for item_id, _reason in excluded["dropped"]],
                },
            },
        )

        digest_blocks = [group_digest(group, items_by_id, index_by_line) for group in groups]
        budget_tokens = AUTHORING_TARGET_CONTEXT - AUTHORING_RESERVED_CONTEXT
        known_ids = set(items_by_id)
        warnings = [advisory_warning] if advisory_warning else []
        dropped_ids = []
        levels = 0
        if estimate_tokens("\n\n".join(digest_blocks)) > budget_tokens:
            think = forge_llm.resolve_think_or_chat(base_url=args.think_url, model=args.think_model)
            if not think["enabled"]:
                fail("the evidence digest exceeds the authoring budget and no thinking service is configured to reduce it", code="service_disabled")
            try:
                digest_blocks, dropped_ids, levels = reduce_to_budget(run_directory, think, digest_blocks, budget_tokens, known_ids, args)
            except InterruptedError:
                run_state.append_run_event(run_directory, {"type": "synthesize_preempted"})
                emit(
                    warnings=["reduction was preempted by interactive activity; run synthesize again to resume"],
                    data={"nextAction": "synthesize"},
                )
                return
            except (UserError, forge_llm.ChatError) as error:
                fail(f"reduction failed: {error}", code="reduction_failed")
            if dropped_ids:
                warnings.append(
                    f"reduction dropped {len(dropped_ids)} evidence ids from the authoring context: "
                    + ", ".join(dropped_ids[:10])
                )

        context = build_authoring_context(run, meta, groups, digest_blocks, excluded)
        run_state.atomic_write_text(run_directory / "authoring_context.md", context)
        update_report_meta(
            run_directory,
            {
                "groups": len(groups),
                "buckets": {"skills": sorted(buckets["skills"]), "backend": len(buckets["backend"]), "crosscutting": len(buckets["crosscutting"])},
                "advisoryClusters": clusters,
                "reductionLevels": levels,
                "reductionDroppedIds": dropped_ids,
                "authoringContextTokens": estimate_tokens(context),
            },
        )
        set_phase(run_directory, "report", "report", {"type": "synthesis_completed", "groups": len(groups)})
        emit(
            artifacts=[str(run_directory / "synthesis" / "groups.json"), str(run_directory / "authoring_context.md")],
            warnings=warnings,
            data={
                "groups": len(groups),
                "skills": sorted(buckets["skills"]),
                "backendGroups": len(buckets["backend"]),
                "crosscuttingGroups": len(buckets["crosscutting"]),
                "advisoryClusters": len(clusters),
                "reductionLevels": levels,
                "nextAction": "report",
            },
        )


# --------------------------------------------------------------------------- #
# report - deterministic sections first, model sections authored on think
# under explicit character budgets, appendix built from exactly the ids the
# body cites. Overruns shrink through re-authoring; nothing is ever truncated
# silently.
# --------------------------------------------------------------------------- #


def report_budget_chars(run):
    return int(run["options"]["reportBudgetTokens"]) * 4


def stats_table(run, results, items):
    counts = Counter(item["status"] for item in items)
    rows = [
        ("session entries", str(run["input"]["entries"])),
        ("session span", f"{run['brief'].get('startedAt')} - {run['brief'].get('endedAt')}"),
        ("model", f"{run['brief'].get('provider')}/{run['brief'].get('modelId')} (thinking {run['brief'].get('thinkingLevel')})"),
        ("chunks", str(len(run["chunks"]))),
        ("evidence items", str(len(items))),
        (
            "evidence by status",
            ", ".join(f"{status} {counts[status]}" for status in sorted(counts)) or "none",
        ),
        ("skills touched", ", ".join(run["skillsSeen"]) or "none"),
        ("report budget", f"{run['options']['reportBudgetTokens']} tokens ({report_budget_chars(run)} chars)"),
    ]
    lines = ["| metric | value |", "|---|---|"]
    lines.extend(f"| {name} | {value} |" for name, value in rows)
    return "\n".join(lines)


def verification_text(meta, items):
    verification = meta.get("verification")
    coverage = meta.get("coverage") or {}
    lines = []
    if not verification or verification.get("skipped"):
        reason = (verification or {}).get("skipped") or "verification has not run"
        lines.append(f"- **Not verified: {reason}.** Every item below is unreviewed extraction output.")
    else:
        lines.append(
            f"- Reviewed by the thinking model: {verification.get('verified', 0)} items; "
            f"agreed {verification.get('ok', 0)}, flagged {verification.get('flagged', 0)}, "
            f"re-done with reasoning {verification.get('escalated', 0)}, left for review {verification.get('needsReview', 0)}."
        )
    present = coverage.get("itemTypesPresent") or []
    absent = coverage.get("itemTypesAbsent") or []
    lines.append(f"- Categories observed: {', '.join(present) or 'none'}.")
    lines.append(
        f"- Categories with no findings: {', '.join(absent) or 'none'} - an empty category means "
        "the session showed nothing for it, or extraction missed it; treat absence as unexamined, not as a clean bill."
    )
    lines.append(
        f"- Deterministic seeds explained by evidence: {coverage.get('seedsExplained', 0)} of {coverage.get('seedsTotal', 0)}."
    )
    unexplained = coverage.get("seedsUnexplained") or []
    if unexplained:
        lines.append(f"- Seeds no evidence item explains: {', '.join(unexplained[:15])}.")
    for item in items:
        if item["status"] == "needs_review":
            lines.append(f"- Needs human review: [{item['id']}] {truncate(item.get('reviewNote') or item.get('notes') or 'extraction could not be validated', 200)}")
        elif item["status"] == "dropped":
            lines.append(f"- Dropped by review (kept for the record): [{item['id']}] {truncate(item.get('reviewNote') or '', 200)}")
    return "\n".join(lines)


def journal_services(run_directory):
    path = run_directory / "inference_journal.jsonl"
    if not path.is_file():
        return []
    try:
        rows, _warnings = run_state.read_jsonl_recover_tail(path, repair=True)
    except ValueError:
        return []
    seen = []
    for row in rows:
        label = f"{row.get('service')}: {row.get('endpoint')} ({row.get('model')})"
        if label not in seen:
            seen.append(label)
    return seen


def provenance_text(run, run_directory):
    lines = [
        f"- Input: `{run['input']['path']}` (sha256 `{run['input']['sha256']}`, {run['input']['bytes']} bytes)",
        f"- Run directory: `{run_directory}`",
    ]
    for label in journal_services(run_directory):
        lines.append(f"- Service used: {label}")
    lines.append("- Options: `" + canonical_json(run["options"]) + "`")
    lines.append(f"- Generated at: {utc_now()}")
    return "\n".join(lines)


def appendix_line(item, index_by_line):
    line = item["locator"]["line"]
    row = index_by_line.get(line, {})
    location = f"L{line} e:{row.get('entryId', '?')} {row.get('kind', '?')}"
    if row.get("tool"):
        location += f" {row['tool']}"
    label = item["attribution"]["skill"] or item["attribution"]["layer"]
    quote = item.get("direct_quotes")
    if quote:
        support = '"' + truncate(quote_fragments(quote)[0].strip(), APPENDIX_QUOTE_CHARS) + '"'
    else:
        support = truncate(item["text"], APPENDIX_QUOTE_CHARS) + " (paraphrase)"
    suffix = ""
    if item["status"] in ("needs_review", "dropped"):
        suffix = f" [{item['status']}]"
    return f"- [{item['id']}] {location} - {item['item_type']}, {label}, {item['severity']}{suffix} - {support}"


def plan_sections(groups, buckets):
    groups_by_id = {group["groupId"]: group for group in groups}
    sections = [{"sectionId": "executive-summary", "title": "Executive Summary", "kind": "exec", "groupIds": [group["groupId"] for group in groups[:EXEC_SUMMARY_TOP_GROUPS]]}]
    skill_mass = {
        name: sum(groups_by_id[group_id]["weight"] for group_id in group_ids)
        for name, group_ids in buckets["skills"].items()
    }
    for name in sorted(skill_mass, key=lambda value: (-skill_mass[value], value)):
        sections.append({"sectionId": f"skill-{name}", "title": f"Skill: {name}", "kind": "skill", "skill": name, "groupIds": buckets["skills"][name]})
    if buckets["backend"]:
        sections.append({"sectionId": "backend", "title": "Backend and Configuration", "kind": "backend", "groupIds": buckets["backend"]})
    if buckets["crosscutting"]:
        sections.append({"sectionId": "crosscutting", "title": "Crosscutting", "kind": "crosscutting", "groupIds": buckets["crosscutting"]})
    return sections


def allocate_budgets(sections, groups_by_id, remaining):
    """Character budget per section, never above what one call can emit.

    Sections share the remaining budget by severity-weighted evidence mass,
    then every budget is clamped to MAX_SECTION_CHARS. Coming in under the
    report budget is free; asking for a section the model cannot finish is not.
    """
    budgets = {}
    exec_budget = int(min(EXEC_SUMMARY_MAX_CHARS, max(EXEC_SUMMARY_MIN_CHARS, remaining * EXEC_SUMMARY_SHARE)))
    budgets["executive-summary"] = min(exec_budget, MAX_SECTION_CHARS)
    rest = max(0, remaining - budgets["executive-summary"])
    others = [section for section in sections if section["kind"] != "exec"]
    masses = {
        section["sectionId"]: max(1, sum(groups_by_id[group_id]["weight"] for group_id in section["groupIds"]))
        for section in others
    }
    total_mass = sum(masses.values()) or 1
    for section in others:
        budgets[section["sectionId"]] = max(SECTION_FLOOR_CHARS, int(rest * masses[section["sectionId"]] / total_mass))
    allocated = sum(budgets[section["sectionId"]] for section in others)
    if allocated > rest and allocated > 0:
        scale = rest / allocated
        for section in others:
            budgets[section["sectionId"]] = max(SECTION_FLOOR_CHARS, int(budgets[section["sectionId"]] * scale))
    for section in others:
        budgets[section["sectionId"]] = min(budgets[section["sectionId"]], MAX_SECTION_CHARS)
    return budgets


def section_payload(section, budget, run, meta, groups_by_id, items_by_id, index_by_line, clusters):
    group_data = []
    for group_id in section["groupIds"]:
        group = groups_by_id[group_id]
        group_data.append(
            {
                **{key: group[key] for key in ("groupId", "itemType", "skill", "layer", "severity", "changeTypes", "lines")},
                "items": [
                    {
                        "id": item_id,
                        "severity": items_by_id[item_id]["severity"],
                        "change_type": items_by_id[item_id]["change_type"],
                        "text": truncate(items_by_id[item_id]["text"], 400),
                        "quote": truncate(items_by_id[item_id].get("direct_quotes") or "", 200) or None,
                        "line": items_by_id[item_id]["locator"]["line"],
                        "hint": truncate(items_by_id[item_id].get("recommendation_hint") or "", 200) or None,
                    }
                    for item_id in group["itemIds"]
                ],
            }
        )
    related = [cluster for cluster in clusters if any(group_id in section["groupIds"] for group_id in cluster)]
    payload = {
        "section": section["title"],
        "kind": section["kind"],
        "charBudget": budget,
        "sessionId": run["input"]["sessionId"],
        "groups": group_data,
        "possiblyRelated": related,
    }
    if section["kind"] == "exec":
        payload["sessionBrief"] = run["brief"]
        payload["coverage"] = meta.get("coverage")
        payload["verification"] = meta.get("verification")
    return json.dumps(payload, ensure_ascii=False)


def section_gates(content, section, budget, valid_ids):
    problems = []
    text = content.strip()
    if not text:
        problems.append("the section is empty")
        return problems
    if len(text) > budget:
        problems.append(f"the section is {len(text)} characters, over its {budget}-character budget; rewrite tighter and keep the citations")
    cited = memo_evidence_ids(text)
    unknown = sorted(cited - valid_ids)
    if unknown:
        problems.append("these cited ids do not exist in the evidence: " + ", ".join(unknown[:10]))
    if not cited:
        problems.append("the section cites no evidence ids; every substantive claim needs a [p######] citation")
    if any(line.startswith("## ") for line in text.splitlines()):
        problems.append('the section must not contain "## " headings; use "### " for issues')
    if section["kind"] != "exec":
        blocks = re.split(r"^### ", text, flags=re.MULTILINE)
        if len(blocks) < 2:
            problems.append('each issue needs its own "### " heading')
        else:
            for block in blocks[1:]:
                if not memo_evidence_ids(block):
                    title = block.splitlines()[0] if block.splitlines() else "?"
                    problems.append(f"issue block {title!r} cites no evidence ids")
    if ELIDE_MARKER_RE.search(text) or "<!-- TODO" in text:
        problems.append("the section contains an elision marker or placeholder")
    return problems


def author_section(run_directory, think, section, payload, budget, valid_ids, args):
    messages = [{"role": "system", "content": AUTHOR_SYSTEM}, {"role": "user", "content": payload}]
    problems = []
    for _attempt in range(SECTION_ATTEMPTS_MAX):
        content, record = forge_llm.call(
            think,
            messages,
            max_tokens=AUTHOR_MAX_TOKENS,
            background=not args.foreground,
            timeout=args.request_timeout,
            task="skill-tuner-author",
        )
        run_state.append_jsonl_fsync(run_directory / "inference_journal.jsonl", record)
        problems = section_gates(content, section, budget, valid_ids)
        if not problems:
            return content.strip()
        messages = [
            *messages,
            {"role": "assistant", "content": content},
            {"role": "user", "content": "That section was unusable: " + "; ".join(problems[:5]) + ". Return the corrected section body only."},
        ]
    raise UserError("; ".join(problems[:5]))


def section_path(run_directory, order, section):
    return run_directory / "sections" / f"{order:02d}-{section['sectionId']}.md"


def assemble_report(run, run_directory, meta, results, items, sections, bodies, index_by_line):
    items_by_id = {item["id"]: item for item in items}
    parts = [
        f"# Skill Tuning Report - session {run['input']['sessionId']}",
        "## Session and Method",
        stats_table(run, results, items),
        METHOD_TEXT,
        "## Verification and Coverage",
        verification_text(meta, items),
    ]
    for section in sections:
        parts.append(f"## {section['title']}")
        parts.append(bodies[section["sectionId"]])
    if not any(section["kind"] == "backend" for section in sections):
        parts.append("## Backend and Configuration")
        parts.append("No issues were attributed to the backend layer.")
    if not any(section["kind"] == "crosscutting" for section in sections):
        parts.append("## Crosscutting")
        parts.append("No crosscutting issues were recorded.")
    body_so_far = "\n\n".join(parts)
    cited = memo_evidence_ids(body_so_far)
    appendix_items = [items_by_id[item_id] for item_id in sorted(cited) if item_id in items_by_id]
    parts.append("## Evidence Appendix")
    parts.append("\n".join(appendix_line(item, index_by_line) for item in appendix_items) or "No evidence was cited.")
    parts.append("## Provenance")
    parts.append(provenance_text(run, run_directory))
    return "\n\n".join(parts) + "\n", sorted(cited)


def command_report(args):
    run_directory = require_run_directory(args.run_directory)
    with run_state.run_lock(run_directory):
        load_state(run_directory)
        run = load_run(run_directory)
        require_stable_input(run)
        meta = load_report_meta(run_directory)
        groups_payload = load_json_artifact(run_directory, "synthesis/groups.json")
        if "verification" not in meta:
            fail("verification has not run; run verify first", code="wrong_phase")
        results = load_chunk_results(run_directory)
        items = project_evidence(run_directory, run, results)
        items_by_id = {item["id"]: item for item in items}
        index = load_json_artifact(run_directory, "timeline_index.json")
        index_by_line = {row["line"]: row for row in index}
        groups = groups_payload["groups"]
        groups_by_id = {group["groupId"]: group for group in groups}
        clusters = groups_payload.get("advisoryClusters") or []
        valid_ids = {item["id"] for item in items if item["status"] in CITABLE_ITEM_STATUSES}

        sections = plan_sections(groups, groups_payload["buckets"]) if groups else []
        budget = report_budget_chars(run)
        fixed_probe, _cited = assemble_report(
            run, run_directory, meta, results, items, [], {}, index_by_line
        )
        appendix_reserve = sum(len(appendix_line(item, index_by_line)) + 1 for item in items if item["status"] in CITABLE_ITEM_STATUSES)
        section_overhead = sum(len(section["title"]) + 8 for section in sections)
        remaining = budget - len(fixed_probe) - appendix_reserve - section_overhead
        if sections and remaining < SECTION_FLOOR_CHARS * len(sections):
            fail(
                f"report budget of {budget} chars leaves only {remaining} for {len(sections)} authored sections; "
                "raise --report-budget-tokens at init or reduce the evidence",
                code="budget_too_small",
            )
        budgets = allocate_budgets(sections, groups_by_id, remaining) if sections else {}

        think = None
        if sections:
            think = forge_llm.resolve_think_or_chat(base_url=args.think_url, model=args.think_model)
            if not think["enabled"]:
                fail("no thinking service is configured to author the report", code="service_disabled")

        bodies = {}
        for order, section in enumerate(sections, start=1):
            path = section_path(run_directory, order, section)
            if path.is_file():
                bodies[section["sectionId"]] = path.read_text(encoding="utf-8").strip()
                continue
            progress(f"[author {order}/{len(sections)}] {section['title']} ({budgets[section['sectionId']]} chars)")
            payload = section_payload(section, budgets[section["sectionId"]], run, meta, groups_by_id, items_by_id, index_by_line, clusters)
            try:
                body = author_section(run_directory, think, section, payload, budgets[section["sectionId"]], valid_ids, args)
            except InterruptedError:
                run_state.append_run_event(run_directory, {"type": "report_preempted", "sectionId": section["sectionId"]})
                emit(
                    warnings=["authoring was preempted by interactive activity; run report again to resume"],
                    data={"nextAction": "report", "completedSections": sorted(bodies)},
                )
                return
            except (UserError, forge_llm.ChatError) as error:
                fail(f"section {section['sectionId']} could not be authored: {error}", code="section_failed")
            run_state.atomic_write_text(path, body + "\n")
            run_state.append_run_event(run_directory, {"type": "section_authored", "sectionId": section["sectionId"]})
            bodies[section["sectionId"]] = body

        report, cited = assemble_report(run, run_directory, meta, results, items, sections, bodies, index_by_line)
        shrink_pass = 0
        while len(report) > budget and shrink_pass < ASSEMBLY_SHRINK_PASSES and sections:
            shrink_pass += 1
            overage = len(report) - budget
            order, fattest = max(
                enumerate(sections, start=1), key=lambda pair: len(bodies[pair[1]["sectionId"]])
            )
            current = len(bodies[fattest["sectionId"]])
            new_budget = max(SECTION_FLOOR_CHARS, current - overage - current // 10)
            progress(f"[shrink {shrink_pass}] report is {overage} chars over; re-authoring {fattest['sectionId']} at {new_budget} chars")
            payload = section_payload(fattest, new_budget, run, meta, groups_by_id, items_by_id, index_by_line, clusters)
            try:
                body = author_section(run_directory, think, fattest, payload, new_budget, valid_ids, args)
            except (UserError, forge_llm.ChatError) as error:
                fail(f"shrink pass failed on {fattest['sectionId']}: {error}", code="section_failed")
            except InterruptedError:
                run_state.append_run_event(run_directory, {"type": "report_preempted", "sectionId": fattest["sectionId"]})
                emit(warnings=["shrink pass was preempted; run report again to resume"], data={"nextAction": "report"})
                return
            run_state.atomic_write_text(section_path(run_directory, order, fattest), body + "\n")
            bodies[fattest["sectionId"]] = body
            report, cited = assemble_report(run, run_directory, meta, results, items, sections, bodies, index_by_line)

        run_state.atomic_write_text(run_directory / "report.md", report)
        update_report_meta(
            run_directory,
            {
                "reportChars": len(report),
                "reportTokens": estimate_tokens(report),
                "reportBudgetTokens": run["options"]["reportBudgetTokens"],
                "citedEvidenceIds": cited,
                "sectionBudgets": budgets,
            },
        )
        set_phase(run_directory, "validate", "validate", {"type": "report_written", "chars": len(report)})
        warnings = []
        if len(report) > budget:
            warnings.append(f"report is {len(report) - budget} chars over budget after {ASSEMBLY_SHRINK_PASSES} shrink passes; validate will fail")
        emit(
            artifacts=[str(run_directory / "report.md")],
            warnings=warnings,
            data={
                "reportChars": len(report),
                "reportTokens": estimate_tokens(report),
                "budgetChars": budget,
                "sections": [section["sectionId"] for section in sections],
                "citedEvidence": len(cited),
                "nextAction": "validate",
            },
        )


# --------------------------------------------------------------------------- #
# validate - deterministic quality gates. Errors block completion; the report
# is never trimmed or repaired here, only judged.
# --------------------------------------------------------------------------- #


def split_report_sections(report):
    sections = {}
    current = "_preamble"
    lines = []
    for line in report.splitlines():
        if line.startswith("## "):
            sections[current] = "\n".join(lines)
            current = line[3:].strip()
            lines = []
        else:
            lines.append(line)
    sections[current] = "\n".join(lines)
    return sections


def command_validate(args):
    run_directory = require_run_directory(args.run_directory)
    run = load_run(run_directory)
    results = load_chunk_results(run_directory)
    items = project_evidence(run_directory, run, results, write=False)
    items_by_id = {item["id"]: item for item in items}
    meta = load_report_meta(run_directory)
    errors = []
    warnings = []

    pending = pending_chunks(run, results)
    if pending:
        errors.append(f"{len(pending)} chunks were never extracted: {', '.join(pending[:5])}")
    drift = input_drift_check(run)
    if drift:
        errors.append(drift)

    report_path = run_directory / "report.md"
    if not report_path.is_file():
        errors.append("report.md is missing; run report")
        report = ""
    else:
        report = report_path.read_text(encoding="utf-8")
    if report:
        budget = report_budget_chars(run)
        if len(report) > budget:
            errors.append(f"report is {len(report)} chars, over the {budget}-char ({run['options']['reportBudgetTokens']}-token) budget")
        if "<!-- TODO" in report:
            errors.append("report contains an unresolved placeholder")
        if ELIDE_MARKER_RE.search(report):
            errors.append("report quotes elided timeline content")
        report_sections = split_report_sections(report)
        appendix = report_sections.get("Evidence Appendix", "")
        appendix_ids = set(re.findall(r"^- \[(p\d{6})\]", appendix, flags=re.MULTILINE))
        before_appendix = report.split("\n\n## Evidence Appendix", 1)[0]
        cited_ids = memo_evidence_ids(before_appendix)
        unresolvable = sorted(item_id for item_id in cited_ids if item_id not in items_by_id)
        if unresolvable:
            errors.append("cited evidence ids do not resolve: " + ", ".join(unresolvable[:10]))
        if appendix_ids != cited_ids:
            missing = sorted(cited_ids - appendix_ids)
            extra = sorted(appendix_ids - cited_ids)
            if missing:
                errors.append("appendix is missing cited ids: " + ", ".join(missing[:10]))
            if extra:
                errors.append("appendix lists uncited ids: " + ", ".join(extra[:10]))
        verification = meta.get("verification")
        if not verification or verification.get("skipped"):
            if "Not verified" not in report:
                errors.append("verification was skipped but the report does not say so")
        elif "Reviewed by the thinking model" not in report:
            errors.append("the report's verification section does not match the recorded verification summary")
        for item in items:
            if item["status"] in ("needs_review", "dropped") and item["id"] not in report:
                errors.append(f"item {item['id']} is {item['status']} but the report never mentions it")
        if run["input"]["sha256"] not in report:
            errors.append("provenance does not record the input sha256")
    sections_dir = run_directory / "sections"
    if sections_dir.is_dir():
        for path in sorted(sections_dir.glob("*.md")):
            body = path.read_text(encoding="utf-8")
            section_id = path.stem.split("-", 1)[1] if "-" in path.stem else path.stem
            cited = memo_evidence_ids(body)
            if not cited:
                errors.append(f"authored section {path.name} cites no evidence")
            bad_status = sorted(
                item_id for item_id in cited if item_id in items_by_id and items_by_id[item_id]["status"] not in CITABLE_ITEM_STATUSES
            )
            if bad_status:
                errors.append(f"authored section {path.name} cites non-citable items: " + ", ".join(bad_status[:5]))
            if section_id != "executive-summary":
                for block in re.split(r"^### ", body, flags=re.MULTILINE)[1:]:
                    if not memo_evidence_ids(block):
                        title = block.splitlines()[0] if block.splitlines() else "?"
                        errors.append(f"issue block {title!r} in {path.name} cites no evidence")

    valid = not errors
    if valid and not args.read_only:
        run_state.update_run_state(
            run_directory,
            lambda state: {**state, "status": "complete", "phase": "complete", "nextAction": None},
            {"type": "run_completed"},
        )
    emit(
        status="ok" if valid else "error",
        artifacts=[str(report_path)] if report else [],
        warnings=warnings,
        errors=[{"code": "validation_error", "message": message} for message in errors],
        data={
            "valid": valid,
            "reportChars": len(report),
            "reportTokens": estimate_tokens(report) if report else 0,
            "citedEvidence": len(meta.get("citedEvidenceIds") or []),
        },
    )
    if errors:
        raise SystemExit(1)


# --------------------------------------------------------------------------- #
# retry - requeue failed units. Chunk retries clear the generated downstream
# artifacts (they are projections of this run, regenerated by the later
# stages); the session log and the journals are never touched.
# --------------------------------------------------------------------------- #


def clear_downstream_artifacts(run_directory):
    removed = []
    sections_dir = run_directory / "sections"
    if sections_dir.is_dir():
        for path in sorted(sections_dir.glob("*.md")):
            path.unlink()
            removed.append(str(path))
    report_path = run_directory / "report.md"
    if report_path.is_file():
        report_path.unlink()
        removed.append(str(report_path))
    return removed


def command_retry(args):
    run_directory = require_run_directory(args.run_directory)
    with run_state.run_lock(run_directory):
        load_state(run_directory)
        run = load_run(run_directory)
        results = load_chunk_results(run_directory)
        if args.all_failed:
            targets = {chunk_id for chunk_id, row in results.items() if row.get("status") == "needs_review"}
        else:
            targets = {args.item} if args.item in results else set()
            if args.item and args.item not in results and not chunk_by_id(run, args.item):
                fail(f"unknown chunk id: {args.item}", code="unknown_item")
        if not targets:
            fail("no matching needs_review chunks to retry", code="unknown_item")
        kept = [row for chunk_id, row in results.items() if chunk_id not in targets]
        ordered = sorted(kept, key=lambda row: row["chunkId"])
        text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in ordered)
        run_state.atomic_write_text(run_directory / "chunk_results.jsonl", text)
        removed = clear_downstream_artifacts(run_directory)
        project_evidence(run_directory, run)

        def mutate(state):
            for item in state.get("items", []):
                if item.get("id") in targets:
                    item["status"] = "pending"
                    item["attempts"] = 0
            state["status"] = "running"
            state["phase"] = "extract"
            state["nextAction"] = "extract"
            return state

        run_state.update_run_state(run_directory, mutate, {"type": "items_retried", "itemIds": sorted(targets)})
        emit(
            warnings=[f"cleared generated artifact {path}" for path in removed],
            data={"retried": sorted(targets), "nextAction": "extract"},
        )


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


def command_doctor(args):
    chat = forge_llm.resolve_service("chat")
    think = forge_llm.resolve_think_or_chat()
    report = {
        "python": sys.version.split()[0],
        "chat": forge_llm.service_doctor(chat, expect_non_thinking=True),
        "think": forge_llm.service_doctor(think),
        "embeddings": forge_embeddings.embeddings_doctor(),
        "note": "chat mines chunks; think reviews, escalates, reduces, and authors; embeddings only group advisory clusters and degrade cleanly.",
    }
    emit(data=report)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def add_model_arguments(command, think=False):
    command.add_argument("--request-timeout", type=float, default=DEFAULT_REQUEST_TIMEOUT)
    command.add_argument("--foreground", action="store_true", help="Run without the background slot and preemption.")
    if think:
        command.add_argument("--think-url", help="thinking service (default: connectedServices.think)")
        command.add_argument("--think-model")


def parser():
    root = argparse.ArgumentParser(
        description="Mine a pi session log for pain points and author a bounded, evidence-cited skill-tuning report."
    )
    subparsers = root.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Probe the chat, think, and embeddings services.")
    doctor.add_argument("--json", action="store_true", help="Accepted for symmetry; output is always JSON.")
    doctor.set_defaults(handler=command_doctor)

    init = subparsers.add_parser("init", help="Parse, scan, and chunk one session log into a resumable run.")
    init.add_argument("session", help="A pi session .jsonl file, or a directory holding exactly one.")
    init.add_argument("--output", required=True)
    init.add_argument("--report-budget-tokens", type=int, default=DEFAULT_REPORT_BUDGET_TOKENS, help="Report cap in estimated tokens, ceil(characters / 4).")
    init.add_argument("--chunk-chars", type=int, default=DEFAULT_CHUNK_CHARS, help="Rendered characters per extraction chunk.")
    init.set_defaults(handler=command_init)

    status = subparsers.add_parser("status", help="Report durable run progress and input drift.")
    status.add_argument("run_directory")
    status.add_argument("--json", action="store_true", help="Accepted for symmetry; output is always JSON.")
    status.set_defaults(handler=command_status)

    extract = subparsers.add_parser("extract", help="Mine every pending chunk on the chat service, serially.")
    extract.add_argument("run_directory")
    extract.add_argument("--limit", type=int, help="stop after this many chunks")
    extract.add_argument("--base-url", help="chat service (default: connectedServices.chat)")
    extract.add_argument("--model")
    extract.add_argument("--no-cache-prompt", action="store_true")
    add_model_arguments(extract)
    extract.set_defaults(handler=command_extract)

    verify = subparsers.add_parser("verify", help="Review all evidence on the thinking service and escalate flags.")
    verify.add_argument("run_directory")
    verify.add_argument("--verify-packet-size", type=int, default=15)
    add_model_arguments(verify, think=True)
    verify.set_defaults(handler=command_verify)

    synthesize = subparsers.add_parser("synthesize", help="Merge, rank, and bound the evidence into an authoring context.")
    synthesize.add_argument("run_directory")
    synthesize.add_argument("--embeddings-url", help="Override the embeddings endpoint (default FORGE_EMBEDDINGS_URL or http://llms:8005/v1/embeddings).")
    synthesize.add_argument("--no-embeddings", action="store_true", help="Skip the advisory embedding clusters.")
    add_model_arguments(synthesize, think=True)
    synthesize.set_defaults(handler=command_synthesize)

    report = subparsers.add_parser("report", help="Author the report sections on the thinking service and assemble report.md.")
    report.add_argument("run_directory")
    add_model_arguments(report, think=True)
    report.set_defaults(handler=command_report)

    validate = subparsers.add_parser("validate", help="Deterministic quality gates over the finished report.")
    validate.add_argument("run_directory")
    validate.add_argument("--read-only", action="store_true", help="Validate without updating run state.")
    validate.add_argument("--json", action="store_true", help="Accepted for symmetry; output is always JSON.")
    validate.set_defaults(handler=command_validate)

    retry = subparsers.add_parser("retry", help="Requeue needs_review chunks and clear generated downstream artifacts.")
    retry.add_argument("run_directory")
    retry_group = retry.add_mutually_exclusive_group(required=True)
    retry_group.add_argument("--item")
    retry_group.add_argument("--all-failed", action="store_true")
    retry.set_defaults(handler=command_retry)
    return root


def main():
    args = parser().parse_args()
    if hasattr(args, "no_cache_prompt"):
        args.cache_prompt = not args.no_cache_prompt
    args.handler(args)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "skill-tuner.py"
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("skill_tuner", SCRIPT)
skill_tuner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(skill_tuner)

# A port nothing listens on, so an unconfigured test can never reach a real
# endpoint; PI_FORGE_AGENT_DIR points nowhere so settings resolution cannot
# pick up a developer install.
DEAD_URL = "http://127.0.0.1:9/v1/chat/completions"
ISOLATED_ENVIRONMENT = {
    "PI_FORGE_AGENT_DIR": "/nonexistent-agent-directory",
    "FORGE_BASE_CHAT_URL": DEAD_URL,
    "FORGE_THINK_URL": DEAD_URL,
    "FORGE_EMBEDDINGS_URL": "http://127.0.0.1:9/v1/embeddings",
}


class FakeChatServer:
    """Minimal OpenAI-compatible endpoint that records what it was sent."""

    def __init__(self, responses=None):
        self.requests = []
        self.responses = list(responses or [])
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                body = json.dumps({"object": "list", "data": [{"id": "chat"}]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
                server.requests.append(payload)
                content = server.responses.pop(0) if server.responses else '{"ok": true}'
                body = json.dumps(
                    {
                        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 11, "prompt_tokens_details": {"cached_tokens": 7}},
                        "timings": {"predicted_n": 2, "prompt_ms": 1.0, "predicted_ms": 2.0},
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1/chat/completions"

    def __enter__(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_args):
        self.httpd.shutdown()
        self.httpd.server_close()


def timestamp(seconds):
    minutes, second = divmod(seconds, 60)
    hour, minute = divmod(minutes, 60)
    return f"2026-08-02T{10 + hour:02d}:{minute:02d}:{second:02d}.000Z"


TRUNCATION_ERROR = (
    'Tool call "write" was not executed: the response hit the output token limit, '
    "so its arguments may be truncated. Re-issue the tool call with complete arguments."
)
TRACEBACK_ERROR = (
    "Traceback (most recent call last):\n"
    '  File "<string>", line 3, in <module>\n'
    "urllib.error.HTTPError: HTTP Error 403: Forbidden while contacting api.example.com"
)
ENVELOPE_WARNING = "some wiki templates are missing or stale; run template-install"
BIG_REFERENCE = "HEADSENTINEL start of reference. " + ("wiki format detail. " * 160) + "end of reference TAILSENTINEL"
USAGE = {"input": 100, "output": 40, "cacheRead": 0, "cacheWrite": 0, "reasoning": 0, "totalTokens": 140}


def assistant(identifier, parent, at, content, stop_reason="toolUse"):
    return {
        "type": "message",
        "id": identifier,
        "parentId": parent,
        "timestamp": at,
        "message": {
            "role": "assistant",
            "content": content,
            "usage": USAGE,
            "stopReason": stop_reason,
            "model": "code",
            "responseModel": "chat-dense",
        },
    }


def tool_result(identifier, parent, at, call_id, tool, text, is_error=False, details=None):
    message = {
        "role": "toolResult",
        "toolCallId": call_id,
        "toolName": tool,
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
        "timestamp": 0,
    }
    if details is not None:
        message["details"] = details
    return {"type": "message", "id": identifier, "parentId": parent, "timestamp": at, "message": message}


def user(identifier, parent, at, text):
    return {
        "type": "message",
        "id": identifier,
        "parentId": parent,
        "timestamp": at,
        "message": {"role": "user", "content": [{"type": "text", "text": text}], "timestamp": 0},
    }


def session_rows():
    return [
        {"type": "session", "version": 3, "id": "fixture-session-0001", "timestamp": timestamp(0), "cwd": "/tmp/vault"},
        {"type": "model_change", "id": "m1", "parentId": None, "timestamp": timestamp(0), "provider": "forge-local", "modelId": "code"},
        {"type": "thinking_level_change", "id": "t1", "parentId": "m1", "timestamp": timestamp(0), "thinkingLevel": "off"},
        user("u1", "t1", timestamp(5), "please expand the wiki notes with the vault-wiki skill"),
        {
            "type": "custom_message",
            "customType": "vault-context",
            "content": "[OBSIDIAN VAULT DETECTED] root=/tmp/vault",
            "display": False,
            "id": "cv1",
            "parentId": "u1",
            "timestamp": timestamp(5),
        },
        assistant(
            "a1",
            "cv1",
            timestamp(10),
            [
                {
                    "type": "thinking",
                    "thinking": "I should read the skill first. The instructions are ambiguous about which notes count as wiki notes.",
                    "thinkingSignature": "sig",
                },
                {"type": "toolCall", "id": "tcRead0001aaaaaaaaaaaaaaaaaaaaaa", "name": "read", "arguments": {"path": "skills/vault-wiki/SKILL.md"}},
                {"type": "toolCall", "id": "tcRead0002aaaaaaaaaaaaaaaaaaaaaa", "name": "read", "arguments": {"path": "skills/vault-wiki/references/format.md"}},
            ],
        ),
        tool_result("r1", "a1", timestamp(12), "tcRead0001aaaaaaaaaaaaaaaaaaaaaa", "read", "vault-wiki: expand empty wiki notes using the schema note."),
        tool_result("r2", "a1", timestamp(12), "tcRead0002aaaaaaaaaaaaaaaaaaaaaa", "read", BIG_REFERENCE),
        assistant(
            "a2",
            "r2",
            timestamp(20),
            [
                {"type": "thinking", "thinking": "Doctor first.", "thinkingSignature": "sig"},
                {
                    "type": "toolCall",
                    "id": "tcBash0001aaaaaaaaaaaaaaaaaaaaaa",
                    "name": "bash",
                    "arguments": {"command": "python3 skills/vault-wiki/scripts/vault-wiki.py doctor --vault /tmp/vault"},
                },
            ],
        ),
        tool_result(
            "r3",
            "a2",
            timestamp(25),
            "tcBash0001aaaaaaaaaaaaaaaaaaaaaa",
            "bash",
            json.dumps({"status": "ok", "artifacts": [], "warnings": [ENVELOPE_WARNING], "errors": [], "data": {"selected": 0}}),
        ),
        assistant(
            "a3",
            "r3",
            timestamp(30),
            [
                {"type": "text", "text": "Writing the note now."},
                {"type": "toolCall", "id": "tcWrite0001aaaaaaaaaaaaaaaaaaaaa", "name": "write", "arguments": {"path": "09 Wiki/Animals.md", "content": "..."}},
            ],
            stop_reason="length",
        ),
        tool_result("r4", "a3", timestamp(31), "tcWrite0001aaaaaaaaaaaaaaaaaaaaa", "write", TRUNCATION_ERROR, is_error=True),
        {
            "type": "compaction",
            "id": "cp1",
            "parentId": "r4",
            "timestamp": timestamp(35),
            "summary": "Earlier exploration summarized: the model read vault-wiki docs and attempted one write.",
            "firstKeptEntryId": "u1",
            "tokensBefore": 120000,
            "details": {},
            "usage": USAGE,
            "fromHook": False,
        },
        user("u2", "cp1", timestamp(60), f"It failed again with: {TRUNCATION_ERROR}"),
        assistant(
            "a4",
            "u2",
            timestamp(70),
            [{"type": "toolCall", "id": "tcBash0002aaaaaaaaaaaaaaaaaaaaaa", "name": "bash", "arguments": {"command": "curl -s https://api.example.com/geo"}}],
        ),
        tool_result("r5", "a4", timestamp(75), "tcBash0002aaaaaaaaaaaaaaaaaaaaaa", "bash", TRACEBACK_ERROR, is_error=True),
        assistant(
            "a5",
            "r5",
            timestamp(80),
            [{"type": "toolCall", "id": "tcBash0003aaaaaaaaaaaaaaaaaaaaaa", "name": "bash", "arguments": {"command": "curl -s https://api.example.com/geo"}}],
        ),
        tool_result("r6", "a5", timestamp(150), "tcBash0003aaaaaaaaaaaaaaaaaaaaaa", "bash", TRACEBACK_ERROR, is_error=True),
        user("u3", "r6", timestamp(3900), "thanks, stopping here."),
        assistant(
            "a6",
            "u3",
            timestamp(3905),
            [{"type": "toolCall", "id": "tcOrphan01aaaaaaaaaaaaaaaaaaaaaa", "name": "read", "arguments": {"path": "README.md"}}],
        ),
    ]


def write_session(path, rows=None):
    rows = rows if rows is not None else session_rows()
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    return path


def run_script(arguments, environment=None):
    merged = {**os.environ, **ISOLATED_ENVIRONMENT, **(environment or {})}
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        capture_output=True,
        text=True,
        env=merged,
        timeout=120,
    )


def stdout_json(completed):
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    return json.loads(lines[-1])


def seed_id_at(run_directory, line, kind):
    scan = json.loads((run_directory / "scan.json").read_text(encoding="utf-8"))
    for seed in scan["seeds"]:
        if seed["kind"] == kind and line in seed["lines"]:
            return seed["id"]
    raise AssertionError(f"no {kind} seed at line {line}")


def extraction_response(run_directory):
    """A valid single-chunk extraction citing real fixture quotes."""
    return json.dumps(
        {
            "items": [
                {
                    "item_type": "output_truncation",
                    "severity": "major",
                    "attribution": {"skill": "vault-wiki", "layer": "skill"},
                    "text": "The wiki note write was cut off by the output token limit and never executed.",
                    "direct_quotes": "the response hit the output token limit",
                    "locator": {"line": 12},
                    "interpretation": "explicit",
                    "confidence": "high",
                    "seed_ids": [seed_id_at(run_directory, 12, "tool_error")],
                    "change_type": "decomposition",
                    "recommendation_hint": "Write the note in smaller sections.",
                    "notes": None,
                },
                {
                    "item_type": "silent_failure",
                    "severity": "minor",
                    "attribution": {"skill": "vault-wiki", "layer": "skill"},
                    "text": "The doctor reported ok while templates were missing.",
                    "direct_quotes": "some wiki templates are missing or stale",
                    "locator": {"line": 10},
                    "interpretation": "explicit",
                    "confidence": "high",
                    "seed_ids": [seed_id_at(run_directory, 10, "silent_failure")],
                    "change_type": "deterministic_guard",
                    "recommendation_hint": None,
                    "notes": None,
                },
            ],
            "open_threads": ["truncation loop unresolved at chunk end"],
            "chunk_summary": "The model hit the output limit while writing a wiki note.",
        },
        ensure_ascii=False,
    )


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.session = write_session(Path(self.directory.name) / "session.jsonl")

    def tearDown(self):
        self.directory.cleanup()

    def test_parses_and_pairs_fanout_by_tool_call_id(self):
        header, entries, warnings = skill_tuner.parse_session_v3(self.session)
        self.assertEqual(header["sessionId"], "fixture-session-0001")
        self.assertEqual(len(entries), 20)
        self.assertEqual(warnings, [])
        findings = skill_tuner.pair_tool_calls(entries)
        by_line = {entry["line"]: entry for entry in entries}
        self.assertEqual(by_line[7]["meta"]["callLine"], 6)
        self.assertEqual(by_line[8]["meta"]["callLine"], 6)
        self.assertEqual(
            [(finding["kind"], finding["line"]) for finding in findings],
            [("unanswered_tool_call", 20)],
        )
        self.assertEqual(by_line[6]["blocks"][0]["type"], "thinking")
        self.assertIn("ambiguous", by_line[6]["blocks"][0]["text"])

    def test_rejects_other_versions_loudly(self):
        rows = session_rows()
        rows[0]["version"] = 2
        write_session(self.session, rows)
        with self.assertRaises(skill_tuner.UserError) as context:
            skill_tuner.parse_session_v3(self.session)
        self.assertIn("version", str(context.exception))

    def test_rejects_unknown_entry_types_loudly(self):
        rows = session_rows()
        rows.append({"type": "hologram", "id": "x1", "timestamp": timestamp(4000)})
        write_session(self.session, rows)
        with self.assertRaises(skill_tuner.UserError) as context:
            skill_tuner.parse_session_v3(self.session)
        self.assertIn("hologram", str(context.exception))
        self.assertIn("line 21", str(context.exception))

    def test_tolerates_torn_final_line(self):
        with self.session.open("a", encoding="utf-8") as handle:
            handle.write('{"type": "message", "id": "torn"')
        header, entries, warnings = skill_tuner.parse_session_v3(self.session)
        self.assertEqual(len(entries), 20)
        self.assertEqual(len(warnings), 1)
        self.assertIn("incomplete final record", warnings[0])


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        session = write_session(Path(self.directory.name) / "session.jsonl")
        _header, self.entries, _warnings = skill_tuner.parse_session_v3(session)
        skill_tuner.pair_tool_calls(self.entries)

    def tearDown(self):
        self.directory.cleanup()

    def test_elides_oversized_blocks_keeping_head_and_tail(self):
        timeline, index, warnings = skill_tuner.render_timeline(self.entries, 48000)
        self.assertIn("HEADSENTINEL", timeline)
        self.assertIn("TAILSENTINEL", timeline)
        self.assertTrue(skill_tuner.ELIDE_MARKER_RE.search(timeline))
        self.assertEqual(warnings, [])
        row = next(row for row in index if row["line"] == 8)
        self.assertGreater(row["elidedChars"], 0)

    def test_rendering_is_byte_reproducible(self):
        first, _index, _warnings = skill_tuner.render_timeline(self.entries, 48000)
        second, _index2, _warnings2 = skill_tuner.render_timeline(self.entries, 48000)
        self.assertEqual(first, second)

    def test_index_offsets_locate_entry_headers(self):
        timeline, index, _warnings = skill_tuner.render_timeline(self.entries, 48000)
        for row in index:
            self.assertTrue(
                timeline[row["charStart"] : row["charEnd"]].startswith(f"=== L{row['line']} "),
                f"index offset broken for line {row['line']}",
            )

    def test_chunks_respect_entry_boundaries(self):
        timeline, index, _warnings = skill_tuner.render_timeline(self.entries, 48000)
        chunks = skill_tuner.chunk_spans(index, 2000)
        self.assertGreater(len(chunks), 1)
        boundaries = {row["line"] for row in index}
        previous_end = 0
        for chunk in chunks:
            self.assertIn(chunk["lineStart"], boundaries)
            self.assertIn(chunk["lineEnd"], boundaries)
            self.assertGreaterEqual(chunk["lineStart"], previous_end + 1 if previous_end else 1)
            previous_end = chunk["lineEnd"]
            self.assertTrue(timeline[chunk["charStart"] :].startswith(f"=== L{chunk['lineStart']} "))
        self.assertEqual(chunks[-1]["lineEnd"], index[-1]["line"])


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        session = write_session(Path(self.directory.name) / "session.jsonl")
        _header, self.entries, _warnings = skill_tuner.parse_session_v3(session)
        findings = skill_tuner.pair_tool_calls(self.entries)
        self.seeds = skill_tuner.scan_session(self.entries, findings)

    def tearDown(self):
        self.directory.cleanup()

    def seed_kinds(self):
        return {seed["kind"] for seed in self.seeds if not seed["informational"]}

    def test_detects_all_three_error_channels(self):
        kinds = self.seed_kinds()
        self.assertIn("tool_error", kinds)
        self.assertIn("output_truncation", kinds)
        self.assertIn("silent_failure", kinds)
        silent = next(seed for seed in self.seeds if seed["kind"] == "silent_failure")
        self.assertEqual(silent["lines"], [10])
        self.assertIn("templates are missing", silent["detail"])

    def test_detects_compaction_and_repasted_error(self):
        kinds = self.seed_kinds()
        self.assertIn("compaction", kinds)
        repasted = [seed for seed in self.seeds if seed["kind"] == "repeated_user_text"]
        self.assertEqual(len(repasted), 1)
        self.assertEqual(repasted[0]["lines"], [12, 14])

    def test_classifies_gaps_by_boundary(self):
        stalls = [seed for seed in self.seeds if seed["kind"] == "tool_stall"]
        self.assertEqual(len(stalls), 1)
        self.assertEqual(stalls[0]["lines"], [17, 18])
        self.assertFalse(stalls[0]["informational"])
        idles = [seed for seed in self.seeds if seed["kind"] == "user_idle"]
        self.assertTrue(idles)
        self.assertTrue(all(seed["informational"] for seed in idles))

    def test_detects_retry_loop_with_error(self):
        loops = [seed for seed in self.seeds if seed["kind"] == "retry_loop"]
        self.assertEqual(len(loops), 1)
        self.assertEqual(loops[0]["lines"], [15, 17])
        self.assertIn("error", loops[0]["detail"])

    def test_detects_pairing_findings_and_attribution(self):
        kinds = {seed["kind"] for seed in self.seeds}
        self.assertIn("unanswered_tool_call", kinds)
        self.assertEqual(skill_tuner.skills_seen(self.seeds), ["vault-wiki"])


class QuoteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        session = write_session(Path(self.directory.name) / "session.jsonl")
        _header, entries, _warnings = skill_tuner.parse_session_v3(session)
        findings = skill_tuner.pair_tool_calls(entries)
        self.seeds = skill_tuner.scan_session(entries, findings)
        self.timeline, self.index, _render_warnings = skill_tuner.render_timeline(entries, 48000)
        self.slices = skill_tuner.normalized_entry_slices(self.timeline, self.index)

    def tearDown(self):
        self.directory.cleanup()

    def item(self, quotes, line):
        return {
            "direct_quotes": quotes,
            "locator": {"line": line},
            "notes": None,
        }

    def test_real_quote_passes(self):
        item = self.item("the response hit the output token limit", 12)
        violations = skill_tuner.verify_and_relocate_quotes([item], self.slices, self.slices)
        self.assertEqual(violations, [])
        self.assertEqual(item["locator"]["line"], 12)

    def test_fabricated_quote_is_a_violation(self):
        item = self.item("the vault caught fire and burned down", 12)
        violations = skill_tuner.verify_and_relocate_quotes([item], self.slices, self.slices)
        self.assertEqual(len(violations), 1)
        self.assertIn("not found", violations[0])

    def test_fumbled_locator_is_corrected_not_retried(self):
        item = self.item("the response hit the output token limit", 4)
        violations = skill_tuner.verify_and_relocate_quotes([item], self.slices, self.slices)
        self.assertEqual(violations, [])
        self.assertEqual(item["locator"]["line"], 12)
        self.assertIn("locator corrected from L4", item["notes"])

    def test_quoting_elided_content_is_rejected(self):
        marker = skill_tuner.ELIDE_MARKER.format(n=5, total=10)
        item = self.item(f"start {marker} end", 8)
        violations = skill_tuner.verify_and_relocate_quotes([item], self.slices, self.slices)
        self.assertEqual(len(violations), 1)
        self.assertIn("ELIDED", violations[0])

    def test_reordered_sentences_still_match_per_sentence(self):
        item = self.item("Re-issue the tool call with complete arguments. the response hit the output token limit", 12)
        violations = skill_tuner.verify_and_relocate_quotes([item], self.slices, self.slices)
        self.assertEqual(violations, [])

    def test_seed_kind_echo_normalizes_or_teaches(self):
        base = {
            "severity": "minor",
            "attribution": {"skill": None, "layer": "unknown"},
            "text": "The context was compacted mid-task.",
            "direct_quotes": None,
            "locator": {"line": 13},
            "interpretation": "explicit",
            "confidence": "high",
            "seed_ids": [],
            "change_type": "instruction_clarification",
            "recommendation_hint": None,
            "notes": None,
        }
        response = {
            "items": [{**base, "item_type": "compaction"}, {**base, "item_type": "assistant_stall"}],
            "open_threads": [],
            "chunk_summary": "",
        }
        result, errors = skill_tuner.validate_chunk_response(response, set(), {13}, set())
        self.assertEqual(len(errors), 1)
        self.assertIn("a seed's kind is not an item_type", errors[0])
        response["items"] = [{**base, "item_type": "compaction"}]
        result, errors = skill_tuner.validate_chunk_response(response, set(), {13}, set())
        self.assertEqual(errors, [])
        self.assertEqual(result["items"][0]["item_type"], "context_loss")
        self.assertIn("normalized from seed kind", result["items"][0]["notes"])

    def test_seed_metadata_quote_is_salvaged_to_null(self):
        seed_texts = skill_tuner.seed_metadata_texts(self.seeds)
        item = self.item("assistant response stopped at the output token limit (stopReason length)", 11)
        violations = skill_tuner.verify_and_relocate_quotes([item], self.slices, self.slices, seed_texts)
        self.assertEqual(violations, [])
        self.assertIsNone(item["direct_quotes"])
        self.assertIn("scan metadata", item["notes"])


class BudgetTests(unittest.TestCase):
    def test_house_estimator_matches_budget(self):
        self.assertEqual(skill_tuner.report_budget_chars({"options": {"reportBudgetTokens": 16384}}), 65536)
        self.assertEqual(skill_tuner.estimate_tokens("x" * 65536), 16384)
        self.assertEqual(skill_tuner.estimate_tokens("x" * 65537), 16385)

    def test_section_budgets_never_exceed_one_call(self):
        """A budget the model cannot emit in one call costs a wasted retry."""
        sections = [
            {"sectionId": "executive-summary", "kind": "exec", "groupIds": ["g0001"]},
            {"sectionId": "skill-vault-wiki", "kind": "skill", "groupIds": ["g0001"]},
            {"sectionId": "backend", "kind": "backend", "groupIds": ["g0002"]},
        ]
        groups_by_id = {"g0001": {"weight": 16}, "g0002": {"weight": 4}}
        budgets = skill_tuner.allocate_budgets(sections, groups_by_id, 60000)
        self.assertEqual(set(budgets), {section["sectionId"] for section in sections})
        for section_id, budget in budgets.items():
            self.assertLessEqual(
                budget,
                skill_tuner.MAX_SECTION_CHARS,
                f"{section_id} budget {budget} exceeds what one authoring call can emit",
            )
            self.assertGreaterEqual(budget, skill_tuner.SECTION_FLOOR_CHARS)
        self.assertLessEqual(
            skill_tuner.MAX_SECTION_CHARS,
            skill_tuner.AUTHOR_MAX_TOKENS * skill_tuner.SECTION_CHARACTERS_PER_OUTPUT_TOKEN,
        )

    def test_merge_joins_same_event_and_keeps_max_severity(self):
        items = [
            {
                "id": "p000001",
                "status": "verified",
                "item_type": "output_truncation",
                "severity": "minor",
                "attribution": {"skill": "vault-wiki", "layer": "skill"},
                "locator": {"line": 12},
                "seed_ids": ["s001"],
                "change_type": "decomposition",
            },
            {
                "id": "p000002",
                "status": "verified",
                "item_type": "output_truncation",
                "severity": "blocker",
                "attribution": {"skill": "vault-wiki", "layer": "skill"},
                "locator": {"line": 12},
                "seed_ids": [],
                "change_type": "backend_config",
            },
            {
                "id": "p000003",
                "status": "verified",
                "item_type": "silent_failure",
                "severity": "papercut",
                "attribution": {"skill": "vault-wiki", "layer": "skill"},
                "locator": {"line": 10},
                "seed_ids": [],
                "change_type": "deterministic_guard",
            },
            {
                "id": "p000004",
                "status": "needs_review",
                "item_type": "ambiguity",
                "severity": "major",
                "attribution": {"skill": None, "layer": "unknown"},
                "locator": {"line": 4},
                "seed_ids": [],
                "change_type": "instruction_clarification",
            },
        ]
        groups = skill_tuner.merge_evidence(items)
        self.assertEqual(len(groups), 2)
        merged = groups[0]
        self.assertEqual(merged["itemIds"], ["p000001", "p000002"])
        self.assertEqual(merged["severity"], "blocker")
        self.assertEqual(merged["weight"], 16)
        self.assertEqual(groups[1]["itemIds"], ["p000003"])


class GroundingTests(unittest.TestCase):
    """A verified diagnosis with an invented fix is worse than no fix."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.session = write_session(Path(self.directory.name) / "session.jsonl")
        self.corpus = skill_tuner.session_corpus({"input": {"path": str(self.session)}})

    def tearDown(self):
        self.directory.cleanup()

    def check(self, token):
        return skill_tuner.ungrounded_referents(f"Recommended change: use `{token}` here.", self.corpus)

    def test_paths_the_session_used_are_grounded(self):
        for token in ("skills/vault-wiki/SKILL.md", "vault-wiki.py", "09 Wiki/Animals.md", "--vault"):
            blocking, advisory = self.check(token)
            self.assertEqual((blocking, advisory), ([], []), f"{token} should be grounded")

    def test_invented_paths_and_flags_are_blocking(self):
        for token in ("$VAULT/schema/wiki/index.md", "local_coordinates.json", "--auto-split"):
            blocking, _advisory = self.check(token)
            self.assertEqual(blocking, [token], f"{token} should be flagged as ungrounded")

    def test_a_lone_short_segment_cannot_ground_a_fabricated_tree(self):
        # "wiki" occurs inside "09 Wiki"; accepting it would ground the whole path.
        blocking, _advisory = self.check("$VAULT/wiki/<subdomain>/<note_name>.md")
        self.assertEqual(len(blocking), 1)

    def test_invented_identifiers_are_advisory_not_blocking(self):
        blocking, advisory = self.check("max_context_wait")
        self.assertEqual(blocking, [])
        self.assertEqual(advisory, ["max_context_wait"])

    def test_the_reports_own_vocabulary_is_never_flagged(self):
        """Noise here would bury the invented identifiers the check exists for."""
        body = (
            "Recommended change: `deterministic_guard`, severity: `major` [p000001]. "
            "Layer `harness`, interpretation `inferred`, type `silent_failure`, "
            "but tune `max_tool_call_latency` too."
        )
        blocking, advisory = skill_tuner.ungrounded_referents(body, self.corpus)
        self.assertEqual(blocking, [])
        self.assertEqual(advisory, ["max_tool_call_latency"])

    def test_a_ground_root_can_vouch_for_a_path_the_session_never_used(self):
        root = Path(self.directory.name) / "repo"
        (root / "forge" / "lib").mkdir(parents=True)
        (root / "forge" / "lib" / "forge_verify.py").write_text("", encoding="utf-8")
        token = "forge/lib/forge_verify.py"
        self.assertEqual(self.check(token)[0], [token])
        blocking, _advisory = skill_tuner.ungrounded_referents(
            f"use `{token}`", self.corpus, ground_roots=[root]
        )
        self.assertEqual(blocking, [])

    def test_grounding_caution_names_the_section_and_the_token(self):
        sections = [{"sectionId": "skill-vault-wiki", "title": "Skill: vault-wiki", "kind": "skill"}]
        bodies = {"skill-vault-wiki": "Write notes to `$VAULT/wiki/notes/entry.md` [p000001]."}
        findings = skill_tuner.grounding_report(sections, bodies, self.corpus, ())
        self.assertIn("skill-vault-wiki", findings)
        text = skill_tuner.grounding_text(sections, findings)
        self.assertIn("Skill: vault-wiki", text)
        self.assertIn("$VAULT/wiki/notes/entry.md", text)
        self.assertIn("Recommendations were not", text)


class PipelineTests(unittest.TestCase):
    """End-to-end CLI runs against the stub server; never a real endpoint."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.session = write_session(self.root / "session.jsonl")
        self.run_directory = self.root / "run"

    def tearDown(self):
        self.directory.cleanup()

    def init(self, extra=None):
        completed = run_script(["init", str(self.session), "--output", str(self.run_directory), *(extra or [])])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return stdout_json(completed)

    def test_init_status_resume_and_option_refusal(self):
        result = self.init()
        self.assertEqual(result["data"]["sessionId"], "fixture-session-0001")
        self.assertEqual(result["data"]["skillsSeen"], ["vault-wiki"])
        self.assertGreaterEqual(result["data"]["seeds"], 8)
        status = stdout_json(run_script(["status", str(self.run_directory)]))
        self.assertEqual(status["data"]["phase"], "extract")
        resumed = stdout_json(run_script(["init", str(self.session), "--output", str(self.run_directory)]))
        self.assertTrue(resumed["data"]["resumed"])
        refused = run_script(["init", str(self.session), "--output", str(self.run_directory), "--chunk-chars", "9000"])
        self.assertEqual(refused.returncode, 1)
        self.assertIn("do not match", refused.stderr)

    def test_extract_records_items_and_verifies_quotes(self):
        self.init()
        with FakeChatServer([extraction_response(self.run_directory)]) as server:
            completed = run_script(
                ["extract", str(self.run_directory), "--foreground"],
                environment={"FORGE_BASE_CHAT_URL": server.url},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = stdout_json(completed)
        self.assertEqual(result["data"]["processed"], 1)
        self.assertEqual(result["data"]["evidence"], 2)
        self.assertEqual(result["data"]["nextAction"], "verify")
        evidence = [
            json.loads(line)
            for line in (self.run_directory / "evidence.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual([item["id"] for item in evidence], ["p000001", "p000002"])
        self.assertTrue(all(item["status"] == "extracted" for item in evidence))
        self.assertTrue(all(item["sessionId"] == "fixture-session-0001" for item in evidence))

    def test_extract_fabricated_quote_costs_one_retry_then_needs_review(self):
        self.init()
        fabricated = json.dumps(
            {
                "items": [
                    {
                        "item_type": "tool_error",
                        "severity": "major",
                        "attribution": {"skill": "vault-wiki", "layer": "skill"},
                        "text": "Something broke.",
                        "direct_quotes": "the vault caught fire and burned down entirely",
                        "locator": {"line": 12},
                        "interpretation": "explicit",
                        "confidence": "high",
                        "seed_ids": [],
                        "change_type": "deterministic_guard",
                        "recommendation_hint": None,
                        "notes": None,
                    }
                ],
                "open_threads": [],
                "chunk_summary": "bad",
            }
        )
        with FakeChatServer([fabricated, fabricated]) as server:
            completed = run_script(
                ["extract", str(self.run_directory), "--foreground"],
                environment={"FORGE_BASE_CHAT_URL": server.url},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = stdout_json(completed)
            self.assertEqual(len(server.requests), 2)
            repair_prompt = server.requests[1]["messages"][-1]["content"]
        self.assertIn("not found in the timeline", repair_prompt)
        self.assertEqual(result["data"]["needsReview"], 1)
        rows = [
            json.loads(line)
            for line in (self.run_directory / "chunk_results.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(rows[0]["status"], "needs_review")
        self.assertEqual(rows[0]["items"], [])

    def test_extract_open_threads_chain_across_chunks_and_resume_skips_recorded(self):
        self.init(["--chunk-chars", "2000"])
        chunks = json.loads((self.run_directory / "run_config.json").read_text(encoding="utf-8"))["chunks"]
        self.assertGreater(len(chunks), 2)
        empty = json.dumps({"items": [], "open_threads": ["thread-alpha still open"], "chunk_summary": "carried"})
        responses = [empty for _chunk in chunks]
        with FakeChatServer(responses) as server:
            first = run_script(
                ["extract", str(self.run_directory), "--limit", "1", "--foreground"],
                environment={"FORGE_BASE_CHAT_URL": server.url},
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            rest = run_script(
                ["extract", str(self.run_directory), "--foreground"],
                environment={"FORGE_BASE_CHAT_URL": server.url},
            )
            self.assertEqual(rest.returncode, 0, rest.stderr)
            self.assertEqual(len(server.requests), len(chunks))
            second_payload = json.loads(server.requests[1]["messages"][-1]["content"])
        self.assertEqual(second_payload["openThreads"], ["thread-alpha still open"])
        self.assertEqual(second_payload["previousChunkSummary"], "carried")

    def test_extract_refuses_changed_input(self):
        self.init()
        with self.session.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(user("late", "a6", timestamp(4000), "one more thing")) + "\n")
        completed = run_script(["extract", str(self.run_directory), "--foreground"])
        self.assertEqual(completed.returncode, 1)
        self.assertIn("changed after init", completed.stderr)

    def extract_with_stub(self):
        with FakeChatServer([extraction_response(self.run_directory)]) as server:
            completed = run_script(
                ["extract", str(self.run_directory), "--foreground"],
                environment={"FORGE_BASE_CHAT_URL": server.url},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_verify_flags_escalate_with_objection_and_project_statuses(self):
        self.init()
        self.extract_with_stub()
        verdicts = json.dumps(
            {
                "verdicts": [
                    {"id": "p000001", "verdict": "ok"},
                    {"id": "p000002", "verdict": "flag", "reason": "severity inflated beyond the evidence"},
                ]
            }
        )
        corrected = json.dumps(
            {
                "item_type": "silent_failure",
                "severity": "papercut",
                "attribution": {"skill": "vault-wiki", "layer": "skill"},
                "text": "The doctor reported ok while templates were missing; low cost this session.",
                "direct_quotes": "some wiki templates are missing or stale",
                "locator": {"line": 10},
                "interpretation": "explicit",
                "confidence": "high",
                "seed_ids": [],
                "change_type": "deterministic_guard",
                "recommendation_hint": None,
                "notes": None,
            }
        )
        with FakeChatServer([verdicts, corrected]) as server:
            completed = run_script(
                ["verify", str(self.run_directory), "--foreground"],
                environment={"FORGE_THINK_URL": server.url},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = stdout_json(completed)
            escalation_prompt = server.requests[1]["messages"][-1]["content"]
        self.assertIn("severity inflated beyond the evidence", escalation_prompt)
        self.assertEqual(result["data"]["verification"]["flagged"], 1)
        self.assertEqual(result["data"]["verification"]["escalated"], 1)
        statuses = {
            json.loads(line)["id"]: json.loads(line)["status"]
            for line in (self.run_directory / "evidence.jsonl").read_text(encoding="utf-8").splitlines()
        }
        self.assertEqual(statuses, {"p000001": "verified", "p000002": "escalated"})

    def test_unreachable_verifier_is_skipped_never_approval(self):
        self.init()
        self.extract_with_stub()
        completed = run_script(["verify", str(self.run_directory), "--foreground"])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = stdout_json(completed)
        self.assertIn("skipped", result["data"]["verification"])
        statuses = {
            json.loads(line)["status"]
            for line in (self.run_directory / "evidence.jsonl").read_text(encoding="utf-8").splitlines()
        }
        self.assertEqual(statuses, {"extracted"})

    def full_pipeline_to_report(self):
        self.init()
        self.extract_with_stub()
        verdicts = json.dumps(
            {"verdicts": [{"id": "p000001", "verdict": "ok"}, {"id": "p000002", "verdict": "ok"}]}
        )
        with FakeChatServer([verdicts]) as server:
            completed = run_script(
                ["verify", str(self.run_directory), "--foreground"],
                environment={"FORGE_THINK_URL": server.url},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
        completed = run_script(["synthesize", str(self.run_directory), "--no-embeddings", "--foreground"])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        exec_body = "The session lost a write to the output token ceiling [p000001] and a template drift went unflagged [p000002]."
        skill_body = (
            "### Output-limit truncation while writing wiki notes\n"
            "The write arrived truncated and was never executed [p000001]. A small model cannot recover "
            "arguments it never emitted; recommended change (change_type: decomposition, severity: major): "
            "emit the note in fixed sections.\n\n"
            "### Doctor reports ok while templates are missing\n"
            "The envelope carried warnings under an ok status [p000002]. Recommended change "
            "(change_type: deterministic_guard, severity: minor): fail the doctor when templates are stale."
        )
        with FakeChatServer([exec_body, skill_body]) as server:
            completed = run_script(
                ["report", str(self.run_directory), "--foreground"],
                environment={"FORGE_THINK_URL": server.url},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.author_requests = list(server.requests)
        return stdout_json(completed)

    def test_report_assembles_within_budget_and_validate_completes(self):
        result = self.full_pipeline_to_report()
        self.assertLessEqual(result["data"]["reportChars"], 65536)
        report = (self.run_directory / "report.md").read_text(encoding="utf-8")
        self.assertIn("# Skill Tuning Report - session fixture-session-0001", report)
        self.assertIn("## Skill: vault-wiki", report)
        self.assertIn("Reviewed by the thinking model", report)
        self.assertIn("[p000001]", report)
        appendix = report.split("## Evidence Appendix", 1)[1]
        self.assertIn("[p000001]", appendix)
        self.assertIn("[p000002]", appendix)
        self.assertIn("L12", appendix)
        completed = run_script(["validate", str(self.run_directory)])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = stdout_json(run_script(["status", str(self.run_directory)]))
        self.assertEqual(status["data"]["phase"], "complete")

    def test_report_section_over_budget_gets_shrink_retry(self):
        self.init()
        self.extract_with_stub()
        verdicts = json.dumps(
            {"verdicts": [{"id": "p000001", "verdict": "ok"}, {"id": "p000002", "verdict": "ok"}]}
        )
        with FakeChatServer([verdicts]) as server:
            run_script(["verify", str(self.run_directory), "--foreground"], environment={"FORGE_THINK_URL": server.url})
        run_script(["synthesize", str(self.run_directory), "--no-embeddings", "--foreground"])
        fat_exec = "Padding [p000001]. " + ("filler sentence to overflow the executive budget. " * 200)
        slim_exec = "The truncation loop dominated the session [p000001][p000002]."
        skill_body = "### Issue\nEvidence [p000001] and [p000002]; recommended change (change_type: decomposition, severity: major)."
        with FakeChatServer([fat_exec, slim_exec, skill_body]) as server:
            completed = run_script(
                ["report", str(self.run_directory), "--foreground"],
                environment={"FORGE_THINK_URL": server.url},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            objection = server.requests[1]["messages"][-1]["content"]
        self.assertIn("over its", objection)

    def test_validate_catches_unresolvable_citation(self):
        self.full_pipeline_to_report()
        report_path = self.run_directory / "report.md"
        report = report_path.read_text(encoding="utf-8")
        report_path.write_text(report.replace("[p000001]", "[p000001] and [p999999]", 1), encoding="utf-8")
        completed = run_script(["validate", str(self.run_directory)])
        self.assertEqual(completed.returncode, 1)
        result = stdout_json(completed)
        messages = " ".join(error["message"] for error in result["errors"])
        self.assertIn("p999999", messages)

    def test_retry_requeues_chunk_and_clears_generated_artifacts(self):
        self.init()
        fabricated = json.dumps({"items": [], "open_threads": [], "chunk_summary": ""})
        with FakeChatServer([fabricated]) as server:
            run_script(
                ["extract", str(self.run_directory), "--foreground"],
                environment={"FORGE_BASE_CHAT_URL": server.url},
            )
        rows = [
            json.loads(line)
            for line in (self.run_directory / "chunk_results.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(rows[0]["status"], "success")
        completed = run_script(["retry", str(self.run_directory), "--all-failed"])
        self.assertEqual(completed.returncode, 1)
        self.assertIn("no matching needs_review", completed.stderr)


if __name__ == "__main__":
    unittest.main()

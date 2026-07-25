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

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "vault-transcripts.py"
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("vault_transcripts", SCRIPT)
vault_transcripts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vault_transcripts)
vt = vault_transcripts


SCHEMA = """---
type: system
status: active
domain: meta
subdomain: schemas
capture_type: manual
---

# Vault Schema

## Core invariants

- Only properties listed under **Approved properties** may appear in frontmatter.

## Approved properties

| Property | Required | Shape | Definition |
| --- | --- | --- | --- |
| `type` | yes | controlled scalar | Kind. |
| `status` | yes | controlled scalar | Lifecycle. |
| `domain` | yes | controlled scalar | Broad area. |
| `subdomain` | no | controlled scalar | Nested area. |
| `source_kind` | conditional | controlled scalar | Source kind. |
| `capture_type` | no | controlled scalar | Capture type. |

### Property constraints

- `source_kind` is required when `type: source` and forbidden for other types.

## Canonical frontmatter

```yaml
---
type: note
---
```

## Note types

- `note` — General note.
- `source` — External source.
- `journal` — Journal note.
- `meeting` — Synchronous exchange.

## Status values

- `raw` — Unprocessed.
- `active` — Active.

## Domains

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `personal` | `1` | `Personal` | Personal material. |
| `meta` | `99` | `Meta` | System notes. |

### Domain decision rules

- Choose the primary purpose.

## Subdomains

### personal

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `journal` | `1` | `Journal` | Dated records. |

### meta

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `schemas` | `2` | `Schemas` | Schema notes. |

## Project registry

| Approved value | Domain | Subdomain | Number | Definition |
| --- | --- | --- | --- | --- |
| `"[[Pi Forge]]"` | `personal` | `journal` | `1` | Local agent harness. |

### Project assignment rules

- Assign only when direct.

## Source kinds

- `transcript` — Verbatim speech.

## Capture types

- `manual` — Typed.
- `voice` — Voice memo.
- `meeting` — From a meeting.

## Folder routing

### Derived names

```text
domain-folder(domain):
  <pad2(domain.number)> <domain.label>
```

### Derived destination paths

```text
domain only:
  domain-folder(domain)/
```

## Inbox processing contract

1. Read this schema.

### Content preservation

- Preserve body.

## Legacy normalization map

| Legacy input | Canonical output |
| --- | --- |
| `type: daily` | `type: journal` |
"""


def block(text, seconds, speaker=None):
    stamp = f"{seconds // 60:02d}:{seconds % 60:02d}" if seconds < 3600 else f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"
    head = f"**{speaker}**\n" if speaker else ""
    return f"{head}*{stamp}*\n{text}\n"


def transcript(blocks, preamble="", trailing=""):
    return preamble + "\n".join(blocks) + ("\n" + trailing if trailing else "")


# Long enough to clear the tiny-note threshold, so the default fixture exercises
# the full treatment. TINY_BLOCKS covers the short path on purpose.
SOLO_BLOCKS = [
    block("Okay so I need to remember to order the replacement gasket for the espresso machine.", 0),
    block("The old one is cracked around the rim and it leaks whenever I pull a double shot.", 6),
    block("Also the grinder needs descaling, probably this weekend if I have time.", 14),
    block("And I should measure the counter before buying anything else for that corner.", 22),
    block("The other thing is the shelving unit in the pantry, which never got anchored properly.", 30),
    block("I keep meaning to buy the right brackets but I always forget the stud spacing.", 38),
    block("Probably worth photographing the wall before the hardware store trip this time.", 46),
    block("Then there is the question of whether we replace the kettle or just descale it too.", 54),
    block("Gillian thinks the kettle is fine, and honestly she is probably right about that.", 62),
    block("Last thing, I should check whether the warranty on the machine covers the gasket.", 70),
    block("If it does then none of this costs anything except the trip and the afternoon.", 78),
]
TINY_BLOCKS = [block("Remember to order the espresso machine gasket before the weekend.", 0)]
DIALOGUE_BLOCKS = [
    block("How did the deployment window go last night?", 0, "Speaker 1"),
    block("It went fine, we finished the migration around midnight.", 5, "Speaker 2"),
    block("Nobody had to roll anything back, which surprised me honestly.", 11, "Speaker 2"),
    block("Good. Did the reporting dashboards come back up cleanly?", 18, "Speaker 1"),
    block("They did, although the nightly aggregation ran twice and duplicated some counters.", 24, "Speaker 2"),
]


class StubChatHandler(BaseHTTPRequestHandler):
    """Answers each pipeline stage plausibly, keyed off its system prompt.

    Scripted responses take priority, so a test can inject a bad response for
    one stage and let the rest behave.
    """

    responses = []
    requests = []
    scripted = {}

    def stage_of(self, payload):
        system = payload["messages"][0]["content"]
        if '{"verdicts"' in system or "verdicts" in system.split("\n\n")[-1]:
            return "verify"
        if system.startswith("You read one voice recording"):
            return "classify"
        if system.startswith("You are a meticulous transcript editor"):
            return "clean"
        if system.startswith("You write the one-paragraph summary"):
            return "summarize"
        return "unknown"

    def default_for(self, stage, payload):
        if len(payload["messages"]) < 2:
            return "ready"  # the doctor probe, which sends one bare user message
        user = json.loads(payload["messages"][1]["content"])
        if stage == "classify":
            speakers = {
                label: {"who": "unknown", "kind": "unknown", "confidence": "low"} for label in user.get("labels", [])
            }
            return {
                "recording_type": "conversation" if speakers else "memo",
                "title": "Espresso Machine Repairs" if not speakers else "Deployment Window Review",
                "speakers": speakers,
                "effective_speakers": max(1, len(speakers)),
                "spoken_date": None,
                "evidence": None,
                "needs_review": False,
                "review_reason": None,
            }
        if stage == "clean":
            # Echoing the chunk is the most faithful cleanup possible, so the
            # deterministic gates pass and a test can measure everything else.
            return {"cleaned": user["chunk"], "chunk_summary": "A short stretch of the recording."}
        if stage == "summarize":
            return {"summary": "The speaker works through a short list of practical repairs and next steps."}
        if stage == "verify":
            return {"verdicts": [{"id": item["id"], "verdict": "ok"} for item in user["items"]]}
        return {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        self.__class__.requests.append(payload)
        stage = self.stage_of(payload)
        queue = self.__class__.scripted.get(stage)
        if queue:
            response = queue.pop(0)
        elif self.__class__.responses:
            response = self.__class__.responses.pop(0)
        else:
            response = self.default_for(stage, payload)
        content = response if isinstance(response, str) else json.dumps(response)
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


class SecondStubChatHandler(StubChatHandler):
    """A separate endpoint, so a test can watch the thinking service on its own."""

    responses = []
    requests = []
    scripted = {}


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        return


class StubServer:
    def __init__(self, responses=None, handler_cls=StubChatHandler, scripted=None):
        self.responses = list(responses or [])
        self.handler_cls = handler_cls
        self.scripted = {key: list(value) for key, value in (scripted or {}).items()}

    def __enter__(self):
        self.handler_cls.responses = list(self.responses)
        self.handler_cls.requests = []
        self.handler_cls.scripted = self.scripted
        self.server = QuietServer(("127.0.0.1", 0), self.handler_cls)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.url = f"http://{host}:{port}/v1/chat/completions"
        return self

    def __exit__(self, *exc):
        self.handler_cls.scripted = {}
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    @property
    def requests(self):
        return self.handler_cls.requests

    def reset(self):
        """Forget the calls so far. A resumed run has to reach the same endpoint
        as the run it resumes, so tests about resuming keep one server."""
        self.handler_cls.requests = []

    def stage_requests(self, stage):
        counted = []
        for payload in self.handler_cls.requests:
            system = payload["messages"][0]["content"]
            if stage == "verify" and "verdicts" in system:
                counted.append(payload)
            elif stage == "classify" and system.startswith("You read one voice recording"):
                counted.append(payload)
            elif stage == "clean" and system.startswith("You are a meticulous transcript editor"):
                counted.append(payload)
            elif stage == "summarize" and system.startswith("You write the one-paragraph summary"):
                counted.append(payload)
        return counted


def run_script(*args, environment=None):
    # Point the agent directory at nothing so endpoint resolution cannot pick up
    # the settings of whoever is running the tests.
    base = environment if environment is not None else os.environ
    env = {**base, "PYTHONDONTWRITEBYTECODE": "1"}
    env.setdefault("PI_FORGE_AGENT_DIR", "/nonexistent-agent-directory")
    arguments = list(args)
    if arguments and arguments[0] == "process" and not {"--no-verify", "--think-url"} & set(arguments):
        arguments.append("--no-verify")
    return subprocess.run([sys.executable, str(SCRIPT), *arguments], capture_output=True, text=True, env=env)


class ParsingTests(unittest.TestCase):
    def test_filename_patterns(self):
        self.assertEqual(
            vt.parse_filename("20260724 131748-9788991C.md"),
            {"date": "2026-07-24", "time_hhmm": "1317", "recording_id": "9788991C"},
        )
        self.assertEqual(
            vt.parse_filename("20260724 110913-A2F4CD8A 2026-07-24 11_11_29.md"),
            {"date": "2026-07-24", "time_hhmm": "1109", "recording_id": "A2F4CD8A"},
        )
        self.assertEqual(
            vt.parse_filename("20260616 092230.md"),
            {"date": "2026-06-16", "time_hhmm": "0922", "recording_id": None},
        )
        for undated in ("New Recording 41.md", "VPP Insiders #1: Intro to VPPs.md", "IMG_1836.md"):
            self.assertIsNone(vt.parse_filename(undated)["date"], undated)

    def test_impossible_date_is_not_a_date(self):
        self.assertIsNone(vt.parse_filename("20261340 110456-0517C158.md")["date"])

    def test_filename_title_hint(self):
        self.assertEqual(vt.filename_title_hint("VPP Insiders #1: Intro to VPPs.md"), "VPP Insiders #1: Intro to VPPs")
        self.assertEqual(vt.filename_title_hint("Buddhist Philosophy 2 Lesson 8.mov.md"), "Buddhist Philosophy 2 Lesson 8")
        self.assertEqual(vt.filename_title_hint("Lesson 19 Combined 8.20.19.m4v (1).md"), "Lesson 19 Combined 8.20.19")
        for generic in ("New Recording.md", "New Recording 41.md", "IMG_1836.md", "20260724 131748-9788991C.md"):
            self.assertIsNone(vt.filename_title_hint(generic), generic)

    def test_round_trip_is_byte_exact(self):
        bodies = [
            transcript(SOLO_BLOCKS),
            transcript(DIALOGUE_BLOCKS),
            transcript(SOLO_BLOCKS, preamble="Notes to self\n\n1. buy gasket\n2. call shop\n\n"),
            transcript(DIALOGUE_BLOCKS, trailing="\nstray closing line without a timestamp\n"),
            transcript([block("A single line.", 3)]),
        ]
        for body in bodies:
            parsed = vt.parse_transcript(body)
            self.assertEqual(vt.serialize_parsed(parsed), body)

    def test_preamble_is_kept_out_of_the_transcript(self):
        body = transcript(SOLO_BLOCKS, preamble="Notes to self\n\n1. buy gasket\n\n")
        parsed = vt.parse_transcript(body)
        self.assertEqual(parsed["preamble"], "Notes to self\n\n1. buy gasket\n\n")
        self.assertEqual(len(parsed["blocks"]), len(SOLO_BLOCKS))
        self.assertNotIn("gasket for the espresso", parsed["preamble"])
        self.assertTrue(vt.transcript_stats(parsed)["has_preamble"])

    def test_trailing_text_is_not_swallowed_by_the_last_block(self):
        parsed = vt.parse_transcript(transcript(SOLO_BLOCKS, trailing="\nafterthought line\n"))
        self.assertEqual(len(parsed["blocks"]), len(SOLO_BLOCKS))
        self.assertIn("afterthought", parsed["trailing"])
        self.assertNotIn("afterthought", parsed["blocks"][-1]["text"])

    def test_hour_timestamps_are_read_by_colon_count(self):
        parsed = vt.parse_transcript(transcript([block("Late in the lecture.", 4020)]))
        self.assertEqual(parsed["blocks"][0]["seconds"], 4020)
        self.assertEqual(vt.transcript_stats(parsed)["timestamp_style"], "HH:MM:SS")
        minutes = vt.parse_transcript(transcript([block("Early on.", 67)]))
        self.assertEqual(minutes["blocks"][0]["seconds"], 67)

    def test_real_names_are_accepted_as_speaker_labels(self):
        body = transcript([block("Over here.", 0, "Gillian"), block("Coming.", 4, "Ellie")])
        parsed = vt.parse_transcript(body)
        self.assertEqual([entry["speaker"] for entry in parsed["blocks"]], ["Gillian", "Ellie"])
        self.assertEqual(vt.ordered_labels(parsed["blocks"]), ["Gillian", "Ellie"])

    def test_stats(self):
        stats = vt.transcript_stats(vt.parse_transcript(transcript(DIALOGUE_BLOCKS)))
        self.assertEqual(stats["blocks"], 5)
        self.assertEqual(stats["duration_seconds"], 24)
        self.assertEqual(stats["speaker_labels"], {"Speaker 1": 2, "Speaker 2": 3})

    def test_detection(self):
        cases = {
            transcript(SOLO_BLOCKS): (True, None),
            "": (False, "no timestamped transcript blocks"),
            "Just some prose I typed myself.\n": (False, "no timestamped transcript blocks"),
            "---\ntype: note\n---\n\n" + transcript(SOLO_BLOCKS): (False, "already has frontmatter"),
            "---\ntype: note\n\nunclosed": (False, "frontmatter is malformed"),
        }
        for text, expected in cases.items():
            split = vt.split_frontmatter(text.encode("utf-8"))
            self.assertEqual(vt.is_transcript(split, vt.parse_transcript(split["body"])), expected)

    def test_chunking_never_splits_a_block(self):
        blocks = vt.parse_transcript(transcript(SOLO_BLOCKS * 40))["blocks"]
        chunks = vt.chunk_blocks(blocks, budget=900)
        self.assertGreater(len(chunks), 3)
        self.assertEqual([entry for chunk in chunks for entry in chunk], blocks)
        for chunk in chunks:
            self.assertLessEqual(sum(len(entry["text"]) for entry in chunk), 900 + max(len(entry["text"]) for entry in chunk))

    def test_collapse_merges_repeated_labels(self):
        blocks = vt.parse_transcript(transcript(DIALOGUE_BLOCKS))["blocks"]
        turns = vt.collapse_turns(blocks, {"Speaker 1": "Speaker 1", "Speaker 2": "Speaker 2"})
        self.assertEqual([turn["speaker"] for turn in turns], ["Speaker 1", "Speaker 2", "Speaker 1", "Speaker 2"])
        self.assertIn("roll anything back", turns[1]["text"])

    def test_collapse_drops_labels_for_a_single_speaker(self):
        blocks = vt.parse_transcript(transcript(DIALOGUE_BLOCKS))["blocks"]
        mapping, drop = vt.derive_speaker_map(["Speaker 1", "Speaker 2"], {}, 1, "names", None)
        self.assertTrue(drop)
        turns = vt.collapse_turns(blocks, mapping)
        self.assertEqual(len(turns), 1)
        self.assertNotIn("**", vt.render_turns(turns))


class NamingTests(unittest.TestCase):
    def test_every_pattern(self):
        cases = {
            "date-type-topic": "2026-07-24 - Therapy - Facing Family Dynamics.md",
            "date-topic": "2026-07-24 Facing Family Dynamics.md",
            "date-time-topic": "2026-07-24 1317 Facing Family Dynamics.md",
        }
        for pattern, expected in cases.items():
            self.assertEqual(
                vt.format_filename(pattern, "2026-07-24", "1317", "therapy", "Facing Family Dynamics"), expected
            )

    def test_undated_names_keep_the_type_but_drop_the_prefix(self):
        self.assertEqual(
            vt.format_filename("date-type-topic", None, None, "lecture", "Madhyamaka Lesson Eight"),
            "Lecture - Madhyamaka Lesson Eight.md",
        )
        self.assertEqual(vt.format_filename("date-topic", None, None, "memo", "Gasket Order"), "Gasket Order.md")

    def test_collision_ladder_prefers_the_time_over_a_counter(self):
        with tempfile.TemporaryDirectory() as raw:
            vault = Path(raw)
            (vault / "00 Inbox").mkdir()
            args = type("Args", (), {"filename_pattern": "date-type-topic"})()
            taken = set()
            first = vt.assign_unique_name(vault, "00 Inbox", args, "2026-07-24", "1317", "memo", "Gasket Order", taken, "x.md")
            second = vt.assign_unique_name(vault, "00 Inbox", args, "2026-07-24", "0900", "memo", "Gasket Order", taken, "y.md")
            third = vt.assign_unique_name(vault, "00 Inbox", args, "2026-07-24", "0900", "memo", "Gasket Order", taken, "z.md")
            self.assertEqual(Path(first).name, "2026-07-24 - Memo - Gasket Order.md")
            self.assertEqual(Path(second).name, "2026-07-24 0900 - Memo - Gasket Order.md")
            self.assertEqual(Path(third).name, "2026-07-24 0900 - Memo - Gasket Order (2).md")

    def test_a_name_may_stay_the_same_as_its_source(self):
        with tempfile.TemporaryDirectory() as raw:
            vault = Path(raw)
            (vault / "00 Inbox").mkdir()
            (vault / "00 Inbox" / "2026-07-24 - Memo - Gasket Order.md").write_text("x", encoding="utf-8")
            args = type("Args", (), {"filename_pattern": "date-type-topic"})()
            chosen = vt.assign_unique_name(
                vault,
                "00 Inbox",
                args,
                "2026-07-24",
                "1317",
                "memo",
                "Gasket Order",
                set(),
                "00 Inbox/2026-07-24 - Memo - Gasket Order.md",
            )
            self.assertEqual(chosen, "00 Inbox/2026-07-24 - Memo - Gasket Order.md")

    def test_unsafe_titles(self):
        self.assertEqual(vt.validate_title("VPP Insiders #1: Intro to VPPs"), "VPP Insiders 1 Intro to VPPs")
        self.assertEqual(vt.validate_title("  Spaced   Out  "), "Spaced Out")
        for bad in ("", "   ", "Voice Note", "transcript", "NUL", "###"):
            with self.assertRaises(vt.UserError, msg=bad):
                vt.validate_title(bad)

    def test_long_titles_are_trimmed_at_a_word_boundary(self):
        title = vt.validate_title("Thinking Through The Entire Kitchen Renovation Sequence And Its Many Consequences")
        self.assertLessEqual(len(title), vt.MAX_TITLE_CHARS)
        self.assertFalse(title.endswith(" "))
        self.assertTrue(title.startswith("Thinking Through The Entire Kitchen"))


class SpeakerPolicyTests(unittest.TestCase):
    def setUp(self):
        self.speakers = {
            "Speaker 1": {"who": "Ellie", "kind": "name", "confidence": "high"},
            "Speaker 2": {"who": "Therapist", "kind": "role", "confidence": "high"},
            "Speaker 3": {"who": "Gillian", "kind": "name", "confidence": "low"},
        }
        self.labels = ["Speaker 1", "Speaker 2", "Speaker 3"]

    def test_names_policy_uses_confident_names_only(self):
        mapping, drop = vt.derive_speaker_map(self.labels, self.speakers, 3, "names", None)
        self.assertFalse(drop)
        self.assertEqual(mapping, {"Speaker 1": "Ellie", "Speaker 2": "Therapist", "Speaker 3": "Speaker 3"})

    def test_roles_policy_keeps_roles_and_the_owner(self):
        mapping, _drop = vt.derive_speaker_map(self.labels, self.speakers, 3, "roles", "Ellie")
        self.assertEqual(mapping["Speaker 1"], "Ellie")
        self.assertEqual(mapping["Speaker 2"], "Therapist")
        mapping, _drop = vt.derive_speaker_map(self.labels, self.speakers, 3, "roles", None)
        self.assertEqual(mapping["Speaker 1"], "Speaker 1")

    def test_generic_policy_renumbers_only(self):
        mapping, _drop = vt.derive_speaker_map(self.labels, self.speakers, 3, "generic", "Ellie")
        self.assertEqual(mapping, {"Speaker 1": "Speaker 1", "Speaker 2": "Speaker 2", "Speaker 3": "Speaker 3"})

    def test_source_supplied_names_survive_every_policy(self):
        for policy in vt.SPEAKER_POLICIES:
            mapping, _drop = vt.derive_speaker_map(["Gillian", "Ellie"], {}, 2, policy, None)
            self.assertEqual(mapping, {"Gillian": "Gillian", "Ellie": "Ellie"}, policy)


class CheckTests(unittest.TestCase):
    def test_added_words_catches_invention_and_allows_structure(self):
        source = "the gasket is cracked around the rim"
        self.assertEqual(vt.added_words(source, "The gasket is cracked around the rim.", []), [])
        self.assertEqual(vt.added_words(source, "## Espresso\n\nThe gasket is cracked.", []), [])
        self.assertIn("warranty", vt.added_words(source, "The gasket is cracked under warranty.", []))
        self.assertEqual(vt.added_words(source, "**Ellie:** the gasket is cracked", ["Ellie"]), [])

    def test_added_words_sees_past_the_grammar_cleanup_adds(self):
        # Every one of these was a real false positive on the live inbox.
        source = (
            "we use the app expand the grid adjust each member share exceed the budget "
            "rpg net max qda identify the source"
        )
        cleaned = (
            "It uses the application and expands the grid, which adjusts each member's share "
            "so they must not exceed the budget. They identified RPGnet and MaxQDA as sources."
        )
        self.assertEqual(vt.added_words(source, cleaned, []), [])

    def test_added_words_still_catches_a_fabricated_sentence(self):
        source = "okay so I need to order the replacement gasket for the espresso machine"
        invented = vt.added_words(source, "The speaker described several unrelated household chores.", [])
        self.assertGreater(len(invented), vt.MAX_INVENTED_WORDS)

    def test_rare_word_retention_notices_a_dropped_passage(self):
        distinctive = (
            "gasket espresso grinder descaling counter measurement bracket pantry shelving "
            "deployment migration dashboards aggregation duplicated counters warranty"
        )
        source = distinctive + " padding " * vt.RARE_WORD_MIN_SOURCE_WORDS
        full, missing = vt.rare_word_retention(source, source)
        self.assertEqual(full, 1.0)
        self.assertEqual(missing, [])
        kept = "gasket espresso grinder descaling counter measurement bracket pantry shelving"
        partial, missing = vt.rare_word_retention(source, kept)
        self.assertLess(partial, vt.RARE_WORD_RETENTION)
        self.assertIn("aggregation", missing)

    def test_short_sources_are_exempt_from_retention(self):
        retention, _missing = vt.rare_word_retention("gasket cracked", "")
        self.assertEqual(retention, 1.0)

    def test_ordinary_words_are_not_distinctive_content(self):
        # Every one of these was flagged as lost content on the live inbox.
        ordinary = "adding going looking means number people purpose thats"
        source = " ".join([ordinary] + ["filler"] * 500)
        self.assertEqual(vt.rare_words(source) & set(ordinary.split()), set())
        self.assertIn("aggregation", vt.rare_words(source + " aggregation"))

    def test_containment_locates_a_kept_utterance_and_misses_a_dropped_one(self):
        cleaned = vt.content_words("The nightly aggregation ran twice and duplicated some counters overnight.")
        kept, _at = vt.best_containment("the nightly aggregation ran twice and duplicated some counters", cleaned)
        self.assertGreater(kept, 0.9)
        dropped, _at = vt.best_containment("we also discussed the quarterly hiring freeze at length", cleaned)
        self.assertLess(dropped, vt.FIDELITY_MIN_CONTAINMENT)

    def test_fidelity_sampling_is_stable_for_a_path(self):
        blocks = vt.parse_transcript(transcript(SOLO_BLOCKS * 6))["blocks"]
        first = [entry["text"] for entry in vt.fidelity_samples("00 Inbox/a.md", blocks)]
        again = [entry["text"] for entry in vt.fidelity_samples("00 Inbox/a.md", blocks)]
        other = [entry["text"] for entry in vt.fidelity_samples("00 Inbox/b.md", blocks)]
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)

    def test_chunk_gate(self):
        source = "the gasket is cracked around the rim and it leaks"
        self.assertEqual(vt.check_chunk("The gasket is cracked around the rim and it leaks.", source, {}, False, False), [])
        self.assertIn("level-one heading", vt.check_chunk("# Title\n\nthe gasket is cracked", source, {}, False, False)[0])
        self.assertIn("timestamp", vt.check_chunk("*00:04*\nthe gasket is cracked", source, {}, False, False)[0])
        self.assertIn("not in the chunk", vt.check_chunk("completely different invented sentences appear here instead", source, {}, False, False)[0])
        for labelled in ("**Speaker 1**\nthe gasket is cracked", "**Speaker 1:** the gasket is cracked"):
            self.assertIn("single-speaker", vt.check_chunk(labelled, source, {"Speaker 1": None}, True, False)[0], labelled)
        self.assertIn("headings", vt.check_chunk("## Heading\n\nthe gasket is cracked", source, {}, False, True)[0])


class NoteBuildingTests(unittest.TestCase):
    def setUp(self):
        self.schema = vt.compiled_schema = None
        import vault_schema

        self.schema = vault_schema.parse_schema_note(SCHEMA)
        self.args = type(
            "Args",
            (),
            {"summary_style": "callout", "tiny_words": 120, "filename_pattern": "date-type-topic", "speaker_policy": "names"},
        )()

    def test_frontmatter_is_only_approved_keys(self):
        metadata = vt.frontmatter_metadata(self.schema, "therapy")
        self.assertEqual(metadata, {"type": "meeting", "status": "raw", "capture_type": "meeting"})
        self.assertEqual(vt.frontmatter_metadata(self.schema, "memo")["capture_type"], "voice")
        self.assertEqual(vt.frontmatter_metadata(self.schema, "journal")["type"], "journal")
        for recording_type in vt.RECORDING_TYPES:
            self.assertEqual(set(vt.frontmatter_metadata(self.schema, recording_type)) - set(self.schema["properties"]), set())

    def test_missing_vocabulary_fails_closed(self):
        import vault_schema

        trimmed = vault_schema.parse_schema_note(SCHEMA.replace("- `voice` — Voice memo.\n", ""))
        with self.assertRaisesRegex(vt.UserError, "capture type"):
            vt.frontmatter_metadata(trimmed, "memo")

    def test_note_layout_and_verbatim_raw_section(self):
        body = transcript(SOLO_BLOCKS, preamble="Notes to self\n\n1. buy gasket\n\n")
        parsed = vt.parse_transcript(body)
        cleaned = "I need to order the replacement gasket for the espresso machine."
        note, head = vt.build_note(
            self.schema, vt.frontmatter_metadata(self.schema, "memo"), "A short summary paragraph.", "callout", parsed["preamble"], cleaned, body
        )
        self.assertTrue(note.startswith("---\ntype: note\nstatus: raw\ncapture_type: voice\n---\n"))
        self.assertIn("> [!summary]\n> A short summary paragraph.", head)
        self.assertIn("1. buy gasket", head)
        self.assertEqual([line for line in note.splitlines() if line.startswith("# ")], ["# Transcript"])
        self.assertTrue(note.endswith(body))
        self.assertEqual(note.split("\n# Transcript\n\n", 1)[1], body)

    def test_summary_styles(self):
        for style, marker in (("paragraph", "Summary text."), ("heading", "## Summary"), ("callout", "> [!summary]")):
            _note, head = vt.build_note(
                self.schema, vt.frontmatter_metadata(self.schema, "memo"), "Summary text.", style, "", "Cleaned.", "raw\n"
            )
            self.assertIn(marker, head)

    def test_omitted_summary_leaves_no_empty_block(self):
        _note, head = vt.build_note(
            self.schema, vt.frontmatter_metadata(self.schema, "memo"), None, "callout", "", "Cleaned text.", "raw\n"
        )
        self.assertNotIn("[!summary]", head)
        self.assertNotIn("\n\n\n", head)

    def check(self, body, cleaned, summary="A summary of the recording."):
        parsed = vt.parse_transcript(body)
        item = {
            "path": "00 Inbox/x.md",
            "raw_body": body,
            "stats": vt.transcript_stats(parsed),
        }
        note, head = vt.build_note(
            self.schema, vt.frontmatter_metadata(self.schema, "memo"), summary, "callout", parsed["preamble"], cleaned, body
        )
        return vt.check_note(item, cleaned, summary, note, head, parsed, self.args)

    def test_faithful_cleanup_passes_every_check(self):
        body = transcript(SOLO_BLOCKS)
        cleaned = " ".join(entry["text"] for entry in vt.parse_transcript(body)["blocks"])
        problems, measurements = self.check(body, cleaned)
        self.assertEqual(problems, [])
        self.assertAlmostEqual(measurements["cleaned_ratio"], 1.0, places=2)

    def test_a_gutted_cleanup_is_caught(self):
        body = transcript(SOLO_BLOCKS * 4)
        problems, _measurements = self.check(body, "The speaker mentioned the espresso machine.")
        self.assertTrue(any("outside" in problem for problem in problems))

    def test_a_dropped_passage_is_caught(self):
        blocks = vt.parse_transcript(transcript(SOLO_BLOCKS))["blocks"]
        body = transcript(SOLO_BLOCKS)
        cleaned = " ".join(entry["text"] for entry in blocks[:2]) + " " + " ".join(entry["text"] for entry in blocks[:2])
        problems, _measurements = self.check(body, cleaned)
        self.assertTrue(problems)

    def test_a_multi_paragraph_summary_is_caught(self):
        body = transcript(SOLO_BLOCKS)
        cleaned = " ".join(entry["text"] for entry in vt.parse_transcript(body)["blocks"])
        problems, _measurements = self.check(body, cleaned, summary="One paragraph.\n\nAnd another.")
        self.assertIn("summary is more than one paragraph", problems)

    def test_a_lost_preamble_is_caught(self):
        body = transcript(SOLO_BLOCKS, preamble="Notes to self\n\n1. buy gasket\n\n")
        parsed = vt.parse_transcript(body)
        cleaned = " ".join(entry["text"] for entry in parsed["blocks"])
        item = {"path": "00 Inbox/x.md", "raw_body": body, "stats": vt.transcript_stats(parsed)}
        note, head = vt.build_note(
            self.schema, vt.frontmatter_metadata(self.schema, "memo"), "A summary.", "callout", "", cleaned, body
        )
        problems, _measurements = vt.check_note(item, cleaned, "A summary.", note, head, parsed, self.args)
        self.assertIn("handwritten preamble did not survive into the generated section", problems)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.vault = self.root / "vault"
        (self.vault / "99 Meta" / "99.02 Schemas").mkdir(parents=True)
        (self.vault / "00 Inbox").mkdir()
        (self.vault / "99 Meta" / "99.02 Schemas" / "0.00 Vault Schema.md").write_text(SCHEMA, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text):
        path = self.vault / "00 Inbox" / name
        path.write_text(text, encoding="utf-8")
        return path

    def process(self, url, *extra):
        return run_script("process", "--vault", str(self.vault), "--base-url", url, "--model", "chat", *extra)

    def result_of(self, completed):
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return json.loads(completed.stdout)

    def inbox(self):
        return sorted(path.name for path in (self.vault / "00 Inbox").glob("*.md"))

    def test_dry_run_changes_nothing_and_plans_everything(self):
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        before = self.inbox()
        with StubServer() as server:
            result = self.result_of(self.process(server.url))
        self.assertEqual(self.inbox(), before)
        self.assertTrue(result["data"]["dry_run"])
        self.assertEqual(result["data"]["counts"]["processed"], 1)
        run_dir = Path(result["data"]["run_directory"])
        plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
        record = plan["records"][0]
        self.assertEqual(record["action"], "process")
        self.assertEqual(record["destination"], "00 Inbox/2026-07-24 - Memo - Espresso Machine Repairs.md")
        report = (run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("2026-07-24 - Memo - Espresso Machine Repairs.md", report)
        self.assertIn("Dry run: `true`", report)

    def test_apply_renames_rewrites_and_keeps_the_original_verbatim(self):
        body = transcript(SOLO_BLOCKS)
        self.write("20260724 131748-9788991C.md", body)
        with StubServer() as server:
            result = self.result_of(self.process(server.url, "--apply"))
        self.assertEqual(self.inbox(), ["2026-07-24 - Memo - Espresso Machine Repairs.md"])
        note = (self.vault / "00 Inbox" / "2026-07-24 - Memo - Espresso Machine Repairs.md").read_text(encoding="utf-8")
        self.assertTrue(note.startswith("---\ntype: note\nstatus: raw\ncapture_type: voice\n---\n"))
        self.assertIn("> [!summary]", note)
        self.assertEqual(note.split("\n# Transcript\n\n", 1)[1], body)
        self.assertEqual([line for line in note.splitlines() if line.startswith("# ")], ["# Transcript"])
        run_dir = Path(result["data"]["run_directory"])
        backup = run_dir / "backup" / "00 Inbox" / "20260724 131748-9788991C.md"
        self.assertEqual(backup.read_text(encoding="utf-8"), body)
        renames = [json.loads(line) for line in (run_dir / "renames.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(renames[0]["old"], "00 Inbox/20260724 131748-9788991C.md")
        self.assertEqual(renames[0]["new"], "00 Inbox/2026-07-24 - Memo - Espresso Machine Repairs.md")

    def test_processed_notes_are_skipped_on_a_second_pass(self):
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        with StubServer() as server:
            self.result_of(self.process(server.url, "--apply"))
        with StubServer() as server:
            result = self.result_of(self.process(server.url))
            self.assertEqual(server.stage_requests("classify"), [])
        self.assertEqual(result["data"]["counts"]["transcripts"], 0)
        self.assertEqual(result["data"]["counts"]["skipped_non_transcript"], 1)

    def test_exact_duplicates_are_quarantined_recoverably(self):
        body = transcript(SOLO_BLOCKS)
        self.write("20260612 093818-7FE5D769.md", body)
        self.write("20260612 093818-7FE5D769 (1).md", body)
        with StubServer() as server:
            result = self.result_of(self.process(server.url, "--apply"))
            self.assertEqual(len(server.stage_requests("classify")), 1)
        self.assertEqual(result["data"]["counts"]["duplicates_exact"], 1)
        self.assertEqual(self.inbox(), ["2026-06-12 - Memo - Espresso Machine Repairs.md"])
        quarantined = self.vault / ".vault-transcripts" / "duplicates" / "20260612 093818-7FE5D769 (1).md"
        self.assertEqual(quarantined.read_text(encoding="utf-8"), body)

    def test_same_recording_with_different_content_is_left_alone(self):
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        self.write(
            "20260724 131748-9788991C 2026-07-24 13_20_45.md",
            transcript(TINY_BLOCKS, preamble="Notes to self\n\n1. buy gasket\n\n"),
        )
        before = self.inbox()
        with StubServer() as server:
            result = self.result_of(self.process(server.url, "--apply"))
            self.assertEqual(server.stage_requests("classify"), [])
        self.assertEqual(self.inbox(), before)
        self.assertEqual(result["data"]["counts"]["duplicate_review"], 2)
        report = (run_dir_of(result) / "report.md").read_text(encoding="utf-8")
        self.assertIn("Same Recording, Different Content", report)
        self.assertIn("handwritten notes above the transcript", report)

    def test_undated_recordings_are_processed_and_flagged(self):
        self.write("New Recording 41.md", transcript(SOLO_BLOCKS))
        with StubServer() as server:
            result = self.result_of(self.process(server.url, "--apply"))
        self.assertEqual(self.inbox(), ["Memo - Espresso Machine Repairs.md"])
        self.assertEqual(result["data"]["counts"]["undated"], 1)
        report = (run_dir_of(result) / "report.md").read_text(encoding="utf-8")
        self.assertIn("no recording date in the filename", report)

    def test_tiny_memos_get_no_summary(self):
        self.write("20260724 131748-9788991C.md", transcript(TINY_BLOCKS))
        with StubServer() as server:
            result = self.result_of(self.process(server.url, "--apply"))
            self.assertEqual(server.stage_requests("summarize"), [])
            self.assertTrue(json.loads(server.stage_requests("clean")[0]["messages"][1]["content"])["tiny"])
        self.assertEqual(result["data"]["counts"]["tiny"], 1)
        note = (self.vault / "00 Inbox" / "2026-07-24 - Memo - Espresso Machine Repairs.md").read_text(encoding="utf-8")
        self.assertNotIn("[!summary]", note)
        self.assertIn("# Transcript", note)

    def test_unfaithful_cleanup_is_held_back(self):
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        scripted = {
            "clean": [
                {"cleaned": "The speaker described several unrelated household chores.", "chunk_summary": "x"},
                {"cleaned": "The speaker described several unrelated household chores.", "chunk_summary": "x"},
            ]
        }
        with StubServer(scripted=scripted) as server:
            result = self.result_of(self.process(server.url, "--apply"))
        self.assertEqual(self.inbox(), ["20260724 131748-9788991C.md"])
        self.assertEqual(result["data"]["counts"]["processed"], 0)
        self.assertEqual(result["data"]["counts"]["review_required"], 1)
        queue = [
            json.loads(line)
            for line in (run_dir_of(result) / "review-queue.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(queue[0]["source"], "00 Inbox/20260724 131748-9788991C.md")

    def test_a_banal_title_is_rejected_then_repaired(self):
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        first = {
            "recording_type": "memo",
            "title": "Voice Note",
            "speakers": {},
            "effective_speakers": 1,
            "spoken_date": None,
            "evidence": None,
            "needs_review": False,
            "review_reason": None,
        }
        second = {**first, "title": "Espresso Repairs"}
        with StubServer(scripted={"classify": [first, second]}) as server:
            self.result_of(self.process(server.url, "--apply"))
            self.assertEqual(len(server.stage_requests("classify")), 2)
        self.assertEqual(self.inbox(), ["2026-07-24 - Memo - Espresso Repairs.md"])

    def test_two_recordings_with_the_same_title_both_survive(self):
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        self.write("20260724 154302-1B2C3D4E.md", transcript(SOLO_BLOCKS + [block("A different closing thought here.", 86)]))
        with StubServer() as server:
            result = self.result_of(self.process(server.url, "--apply"))
        self.assertEqual(result["data"]["counts"]["processed"], 2)
        self.assertEqual(
            self.inbox(),
            [
                "2026-07-24 - Memo - Espresso Machine Repairs.md",
                "2026-07-24 1543 - Memo - Espresso Machine Repairs.md",
            ],
        )
        for name in self.inbox():
            self.assertIn("# Transcript", (self.vault / "00 Inbox" / name).read_text(encoding="utf-8"))

    def test_an_overlong_summary_is_asked_for_again(self):
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        overlong = " ".join(["word"] * (vt.SUMMARY_MAX_WORDS + 5))
        scripted = {"summarize": [{"summary": overlong}, {"summary": "A tight paragraph about espresso repairs."}]}
        with StubServer(scripted=scripted) as server:
            self.result_of(self.process(server.url, "--apply"))
            self.assertEqual(len(server.stage_requests("summarize")), 2)
            repair = server.stage_requests("summarize")[1]["messages"][-1]["content"]
            self.assertIn("over the", repair)
        note = (self.vault / "00 Inbox" / "2026-07-24 - Memo - Espresso Machine Repairs.md").read_text(encoding="utf-8")
        self.assertIn("> [!summary]\n> A tight paragraph about espresso repairs.", note)

    def test_a_summary_that_stays_unusable_holds_the_note(self):
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        bad = {"summary": "This recording covers several things."}
        with StubServer(scripted={"summarize": [bad, bad]}) as server:
            result = self.result_of(self.process(server.url, "--apply"))
        self.assertEqual(self.inbox(), ["20260724 131748-9788991C.md"])
        self.assertEqual(result["data"]["counts"]["review_required"], 1)

    def test_summary_gate(self):
        self.assertEqual(vt.check_summary("A concrete paragraph about the espresso machine."), [])
        self.assertIn("more than one paragraph", vt.check_summary("One paragraph.\n\nAnd another.")[0])
        self.assertIn("over the", vt.check_summary(" ".join(["word"] * (vt.SUMMARY_MAX_WORDS + 1)))[0])
        self.assertIn("opens with", vt.check_summary("This recording is about a gasket.")[0])
        self.assertTrue(vt.check_summary(""))

    def test_an_invented_spoken_date_is_ignored(self):
        self.write("New Recording.md", transcript(SOLO_BLOCKS))
        scripted = {
            "classify": [
                {
                    "recording_type": "memo",
                    "title": "Espresso Repairs",
                    "speakers": {},
                    "effective_speakers": 1,
                    "spoken_date": "2019-01-01",
                    "evidence": "Today is the first of January twenty nineteen.",
                    "needs_review": False,
                    "review_reason": None,
                }
            ]
        }
        with StubServer(scripted=scripted) as server:
            result = self.result_of(self.process(server.url, "--apply"))
        self.assertEqual(self.inbox(), ["Memo - Espresso Repairs.md"])
        report = (run_dir_of(result) / "report.md").read_text(encoding="utf-8")
        self.assertIn("ignored spoken date 2019-01-01", report)

    def test_verification_flag_is_redone_on_the_thinking_service(self):
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        better = {
            "recording_type": "memo",
            "title": "Espresso Machine Maintenance",
            "speakers": {},
            "effective_speakers": 1,
            "spoken_date": None,
            "evidence": None,
            "needs_review": False,
            "review_reason": None,
        }
        with StubServer() as chat, StubServer(handler_cls=SecondStubChatHandler) as think:
            SecondStubChatHandler.scripted = {
                "verify": [{"verdicts": [{"id": "00 Inbox/20260724 131748-9788991C.md", "verdict": "flag", "reason": "title is vague"}]}],
                "classify": [better],
                "summarize": [{"summary": "The speaker lists espresso machine maintenance tasks and next steps."}],
            }
            result = self.result_of(self.process(chat.url, "--apply", "--think-url", think.url, "--think-model", "code"))
        self.assertEqual(self.inbox(), ["2026-07-24 - Memo - Espresso Machine Maintenance.md"])
        self.assertEqual(result["data"]["verification"]["escalated"], 1)
        report = (run_dir_of(result) / "report.md").read_text(encoding="utf-8")
        self.assertIn("re-done with reasoning", report)
        self.assertIn("title is vague", report)

    def test_a_failed_escalation_leaves_the_note_untouched(self):
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        with StubServer() as chat, StubServer(handler_cls=SecondStubChatHandler) as think:
            SecondStubChatHandler.scripted = {
                "verify": [{"verdicts": [{"id": "00 Inbox/20260724 131748-9788991C.md", "verdict": "flag", "reason": "wrong type"}]}],
                "classify": ["this is not json at all"],
            }
            result = self.result_of(self.process(chat.url, "--apply", "--think-url", think.url, "--think-model", "code"))
        self.assertEqual(self.inbox(), ["20260724 131748-9788991C.md"])
        self.assertEqual(result["data"]["verification"]["needsReview"], 1)
        self.assertIn("needs your review", (run_dir_of(result) / "report.md").read_text(encoding="utf-8"))

    def test_an_unreachable_verifier_never_reads_as_approval(self):
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        with StubServer() as chat:
            result = self.result_of(
                self.process(chat.url, "--think-url", "http://127.0.0.1:1/v1/chat/completions", "--think-model", "code")
            )
        self.assertIn("skipped", result["data"]["verification"])
        self.assertIn("**Not verified**", (run_dir_of(result) / "report.md").read_text(encoding="utf-8"))

    def test_no_verify_says_so_in_the_report(self):
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        with StubServer() as chat:
            result = self.result_of(self.process(chat.url, "--no-verify"))
        self.assertIn("Nothing here was reviewed", (run_dir_of(result) / "report.md").read_text(encoding="utf-8"))

    def test_resume_reuses_the_journals(self):
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        with StubServer() as server:
            first = self.result_of(self.process(server.url))
            self.assertGreater(len(server.requests), 0)
            server.reset()
            self.result_of(self.process(server.url, "--apply", "--run", str(run_dir_of(first))))
            self.assertEqual(server.requests, [])
        self.assertEqual(self.inbox(), ["2026-07-24 - Memo - Espresso Machine Repairs.md"])

    def test_resume_refuses_changed_options(self):
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        with StubServer() as server:
            first = self.result_of(self.process(server.url))
            completed = self.process(server.url, "--run", str(run_dir_of(first)), "--speaker-policy", "generic")
        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stdout)
        self.assertIn("--speaker-policy differs", payload["errors"][0]["message"])

    def test_reapplying_a_finished_run_is_a_no_op(self):
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        with StubServer() as server:
            first = self.result_of(self.process(server.url, "--apply"))
            server.reset()
            second = self.result_of(self.process(server.url, "--apply", "--run", str(run_dir_of(first))))
            self.assertEqual(server.requests, [])
        self.assertEqual(self.inbox(), ["2026-07-24 - Memo - Espresso Machine Repairs.md"])
        self.assertEqual(second["data"]["counts"]["applied"], 1)
        self.assertEqual(second["data"]["counts"]["review_required"], 0)

    def test_a_note_edited_after_planning_is_refused_at_apply(self):
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        with StubServer() as server:
            first = self.result_of(self.process(server.url))
            self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS + [block("One more thought here.", 90)]))
            result = self.result_of(self.process(server.url, "--apply", "--run", str(run_dir_of(first))))
        self.assertEqual(self.inbox(), ["20260724 131748-9788991C.md"])
        self.assertTrue(any("changed" in warning for warning in result["warnings"]), result["warnings"])

    def test_non_transcripts_and_other_files_are_left_alone(self):
        self.write("A typed note.md", "# A Note\n\nJust prose.\n")
        self.write("20260616 092230.md", "")
        (self.vault / "00 Inbox" / "FilesPage.html").write_text("<html></html>", encoding="utf-8")
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        with StubServer() as server:
            result = self.result_of(self.process(server.url, "--apply"))
        self.assertEqual(result["data"]["counts"]["skipped_non_transcript"], 2)
        self.assertIn("A typed note.md", self.inbox())
        self.assertIn("20260616 092230.md", self.inbox())
        self.assertTrue((self.vault / "00 Inbox" / "FilesPage.html").exists())

    def test_an_undecodable_note_is_skipped_not_fatal(self):
        (self.vault / "00 Inbox" / "broken.md").write_bytes(b"\xff\xfe not utf-8 at all \x00")
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        with StubServer() as server:
            result = self.result_of(self.process(server.url, "--apply"))
        self.assertEqual(result["data"]["counts"]["processed"], 1)
        self.assertIn("broken.md", self.inbox())
        report = (run_dir_of(result) / "report.md").read_text(encoding="utf-8")
        self.assertIn("2026-07-24 - Memo - Espresso Machine Repairs.md", report)

    def test_dialogue_uses_the_speaker_map(self):
        self.write("20260724 131748-9788991C.md", transcript(DIALOGUE_BLOCKS))
        scripted = {
            "classify": [
                {
                    "recording_type": "conversation",
                    "title": "Deployment Window Review",
                    "speakers": {
                        "Speaker 1": {"who": "Ellie", "kind": "name", "confidence": "high"},
                        "Speaker 2": {"who": "Gillian", "kind": "name", "confidence": "high"},
                    },
                    "effective_speakers": 2,
                    "spoken_date": None,
                    "evidence": None,
                    "needs_review": False,
                    "review_reason": None,
                }
            ]
        }
        with StubServer(scripted=scripted) as server:
            self.result_of(self.process(server.url, "--apply"))
            payload = json.loads(server.stage_requests("clean")[0]["messages"][1]["content"])
        self.assertEqual(payload["speakers"], {"Speaker 1": "Ellie", "Speaker 2": "Gillian"})
        self.assertIn("**Ellie**", payload["chunk"])
        self.assertEqual(self.inbox(), ["2026-07-24 - Conversation - Deployment Window Review.md"])

    def test_long_transcripts_are_chunked_with_context(self):
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS * 60))
        with StubServer() as server:
            self.result_of(self.process(server.url))
            payloads = [json.loads(entry["messages"][1]["content"]) for entry in server.stage_requests("clean")]
        self.assertGreater(len(payloads), 1)
        self.assertEqual(payloads[0]["chunkIndex"], 1)
        self.assertEqual(payloads[1]["chunkCount"], len(payloads))
        self.assertIn("previousTail", payloads[1])
        summarize = json.loads(server.stage_requests("summarize")[0]["messages"][1]["content"])
        self.assertIn("sectionSummaries", summarize)

    def test_status_and_doctor(self):
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        with StubServer() as server:
            first = self.result_of(self.process(server.url))
            status = self.result_of(run_script("status", "--run", str(run_dir_of(first))))
            self.assertEqual(status["data"]["phase"], "planned")
            self.assertEqual(status["data"]["transcripts"], 1)
            self.assertEqual(status["data"]["classified"], 1)
            self.assertEqual(status["data"]["cleaned_chunks"], 1)
            doctor = self.result_of(
                run_script(
                    "doctor",
                    "--vault",
                    str(self.vault),
                    "--base-url",
                    server.url,
                    "--model",
                    "chat",
                    "--think-url",
                    server.url,
                    "--think-model",
                    "code",
                )
            )
        checks = doctor["data"]["checks"]
        self.assertTrue(checks["vault"]["ok"])
        self.assertTrue(checks["schema"]["ok"])
        self.assertTrue(checks["chat"]["ok"])
        self.assertEqual(checks["inbox"]["transcripts"], 1)

    def test_missing_inbox_is_a_clean_error(self):
        completed = run_script("process", "--vault", str(self.root), "--base-url", "http://127.0.0.1:1/v1", "--model", "chat")
        self.assertEqual(completed.returncode, 2)
        self.assertIn("inbox", json.loads(completed.stdout)["errors"][0]["message"])


def run_dir_of(result):
    return Path(result["data"]["run_directory"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

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
| `parent` | no | quoted wikilink | Parent hub. |
| `source_kind` | conditional | controlled scalar | Source kind. |
| `capture_type` | no | controlled scalar | Capture type. |
| `processed_by` | no | list | Automated workflows that transformed this note. |

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
- `complete` — Finished.

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

# The same vault, opted in to filing sources by kind, so a recording gets a note
# of its own in `10 Sources/10.03 Transcript`. Derived from SCHEMA so the two
# cannot drift apart on anything but that one section.
SOURCES_SCHEMA = SCHEMA.replace(
    "## Source kinds\n\n- `transcript` — Verbatim speech.\n",
    """## Sources root

| Number | Label | Definition |
| --- | --- | --- |
| `10` | `Sources` | External source notes, filed by source kind. |

## Source kinds

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `transcript` | `3` | `Transcript` | Verbatim speech. |
""",
)

# A vault whose schema cannot describe a recording's own note.
NO_SOURCE_SCHEMA = SCHEMA.replace("- `source` — External source.\n", "")


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


def classify_response(recording_type, title="Espresso Machine Repairs"):
    return {
        "recording_type": recording_type,
        "title": title,
        "speakers": {},
        "effective_speakers": 1,
        "spoken_date": None,
        "evidence": None,
        "needs_review": False,
        "review_reason": None,
    }


def run_script(*args, environment=None):
    # Point the agent directory at nothing so endpoint resolution cannot pick up
    # the settings of whoever is running the tests.
    base = environment if environment is not None else os.environ
    env = {**base, "PYTHONDONTWRITEBYTECODE": "1"}
    env.setdefault("PI_FORGE_AGENT_DIR", "/nonexistent-agent-directory")
    arguments = list(args)
    if arguments and arguments[0] in {"process", "reprocess"} and not {"--no-verify", "--think-url"} & set(arguments):
        arguments.append("--no-verify")
    return subprocess.run([sys.executable, str(SCRIPT), *arguments], capture_output=True, text=True, env=env)


class TranscriptMarkerTests(unittest.TestCase):
    """A note that already carries `# Transcript` must not gain a second one."""

    BODY = transcript(SOLO_BLOCKS)

    def test_a_body_without_the_marker_is_returned_unchanged(self):
        self.assertEqual(vt.transcript_source(self.BODY), self.BODY)

    def test_a_head_stripped_note_yields_the_recording_alone(self):
        self.assertEqual(vt.transcript_source(f"# Transcript\n\n{self.BODY}"), self.BODY)

    def test_a_fully_processed_note_yields_the_recording_alone(self):
        processed = f"---\ntype: note\n---\n\n> [!summary]\n> A summary.\n\nCleaned.\n\n# Transcript\n\n{self.BODY}"
        body = vt.split_frontmatter(processed.encode("utf-8"))["body"]
        self.assertEqual(vt.transcript_source(body), self.BODY)

    def test_the_marker_is_matched_only_as_a_whole_line(self):
        spoken = f"I wrote # Transcript at the top of it.\n\n{self.BODY}"
        self.assertEqual(vt.transcript_source(spoken), spoken)
        deeper = f"## Transcript\n\n{self.BODY}"
        self.assertEqual(vt.transcript_source(deeper), deeper)

    def test_the_leftover_marker_no_longer_reaches_the_preamble(self):
        parsed = vt.parse_transcript(vt.transcript_source(f"# Transcript\n\n{self.BODY}"))
        self.assertEqual(parsed["preamble"], "")

    def test_a_handwritten_preamble_still_survives_untouched(self):
        preamble = "1. call the shop\n2. measure the counter\n\n"
        parsed = vt.parse_transcript(vt.transcript_source(f"# Transcript\n\n{preamble}{self.BODY}"))
        self.assertEqual(parsed["preamble"], preamble)

    def test_a_link_under_the_marker_is_followed_to_the_recording(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            (vault / "00 Inbox").mkdir()
            (vault / "00 Inbox" / "A Talk - Transcript.md").write_text(
                f"---\ntype: source\nsource_kind: transcript\n---\n\n{self.BODY}", encoding="utf-8"
            )
            body = f"> [!summary]\n> A summary.\n\nCleaned.\n\n# Transcript\n\n[[A Talk - Transcript]]\n"
            self.assertEqual(vt.transcript_source(body, vault), self.BODY)

    def test_a_link_to_nothing_reads_as_no_recording(self):
        # Better an empty read, which skips the note, than processing the link
        # text as if somebody had said it.
        with tempfile.TemporaryDirectory() as directory:
            body = "Cleaned.\n\n# Transcript\n\n[[Gone Missing]]\n"
            self.assertEqual(vt.transcript_source(body, Path(directory)), "")

    def test_a_recording_that_merely_mentions_a_link_is_still_a_recording(self):
        section = f"[[Some Note]] came up in conversation.\n\n{self.BODY}"
        self.assertEqual(vt.transcript_source(f"# Transcript\n\n{section}", None), section)


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

    def test_a_name_spelled_properly_is_not_an_invented_word(self):
        # Live failure on the Buddhism lectures: the tokenizer cut at the first
        # accented letter, so "Śāntideva" reached the check as "ntidevas" and
        # "sūtras" as "tras" — fragments with no root in the source, which read
        # as fabrication when the cleanup had simply spelled the name right.
        source = "we read aryadeva and the sutras of santideva and the sastras"
        cleaned = "We read Āryadeva and the sūtras of Śāntideva, and the śāstras."
        self.assertEqual(vt.added_words(source, cleaned, []), [])

    def test_folding_leaves_ordinary_words_alone(self):
        self.assertEqual(vt.content_words("The gasket needs replacing"), ["the", "gasket", "needs", "replacing"])
        self.assertEqual(vt.fold_diacritics("café Gödel naïve"), "cafe Godel naive")

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
        self.assertEqual(
            metadata,
            {"type": "meeting", "status": "raw", "capture_type": "meeting", "processed_by": ["vault-transcripts"]},
        )
        self.assertEqual(vt.frontmatter_metadata(self.schema, "memo")["capture_type"], "voice")
        self.assertEqual(vt.frontmatter_metadata(self.schema, "journal")["type"], "journal")
        for recording_type in vt.RECORDING_TYPES:
            self.assertEqual(set(vt.frontmatter_metadata(self.schema, recording_type)) - set(self.schema["properties"]), set())

    def test_capture_type_records_the_channel_and_processed_by_records_the_pipeline(self):
        # A cleaned recording is model-transformed but still arrived as voice.
        # Losing either fact would make a transcript unfindable as a recording
        # or indistinguishable from something typed by hand.
        for recording_type in vt.RECORDING_TYPES:
            metadata = vt.frontmatter_metadata(self.schema, recording_type)
            self.assertIn(metadata["capture_type"], {"voice", "meeting"})
            self.assertEqual(metadata["processed_by"], ["vault-transcripts"])

    def test_vault_without_the_property_gets_no_processed_by(self):
        import vault_schema

        older = vault_schema.parse_schema_note(
            SCHEMA.replace("| `processed_by` | no | list | Automated workflows that transformed this note. |\n", "")
        )
        self.assertNotIn("processed_by", vt.frontmatter_metadata(older, "memo"))

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
        self.assertTrue(note.startswith('---\ntype: note\nstatus: raw\ncapture_type: voice\nprocessed_by:\n  - "vault-transcripts"\n---\n'))
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

    def test_journal_reflection_sits_above_the_cleaned_text(self):
        # Generated material together at the top; the speaker's own words below.
        reflection = "> [!reflection]- Observations\n> - The owner described a difficult week."
        note, head = vt.build_note(
            self.schema,
            vt.frontmatter_metadata(self.schema, "journal"),
            "A short summary.",
            "callout",
            "",
            "Cleaned authorial text.",
            "raw journal\n",
            reflection=reflection,
        )
        self.assertLess(note.index("[!summary]"), note.index("[!reflection]- Observations"))
        self.assertLess(note.index("[!reflection]- Observations"), note.index("Cleaned authorial text."))
        self.assertLess(note.index("Cleaned authorial text."), note.index("# Transcript"))
        self.assertIn(reflection, head)

    def test_memo_reflection_sits_above_the_cleaned_text(self):
        reflection = "> [!reflection]- Context\n> - Continues the espresso repair thread."
        note, _head = vt.build_note(
            self.schema,
            vt.frontmatter_metadata(self.schema, "memo"),
            "A short summary.",
            "callout",
            "",
            "Cleaned memo text.",
            "raw memo\n",
            reflection=reflection,
        )
        self.assertLess(note.index("[!reflection]- Context"), note.index("Cleaned memo text."))
        self.assertLess(note.index("Cleaned memo text."), note.index("# Transcript"))

    def test_the_preamble_stays_next_to_the_cleaned_text(self):
        # The preamble is the owner's writing, not apparatus: it belongs with
        # their words, below everything the pipeline made.
        note, _head = vt.build_note(
            self.schema,
            vt.frontmatter_metadata(self.schema, "memo"),
            "A short summary.",
            "callout",
            "1. call the shop\n",
            "Cleaned memo text.",
            "raw memo\n",
            reflection="> [!reflection]- Context\n> - Continues the thread.",
        )
        self.assertLess(note.index("[!reflection]- Context"), note.index("1. call the shop"))
        self.assertLess(note.index("1. call the shop"), note.index("Cleaned memo text."))

    def test_the_head_ends_with_exactly_one_marker(self):
        head = vt.assemble_head("A summary.", "callout", "", "Cleaned text.", None)
        self.assertTrue(head.endswith(vt.TRANSCRIPT_MARKER))
        self.assertEqual(head.count("# Transcript"), 1)

    def test_stripping_the_marker_does_not_cut_at_a_spoken_one(self):
        # Reprocessing removes the head's marker and reattaches the note's own
        # transcript section. Searching for the words would cut the note the
        # first time the speaker happened to say them; stripping the known
        # suffix cannot.
        spoken = "I told them to write # Transcript at the top, and then we moved on."
        head = vt.assemble_head("A summary.", "callout", "", spoken, None)
        without_marker = head[: -len(vt.TRANSCRIPT_MARKER)]
        self.assertIn("and then we moved on.", without_marker)
        self.assertIn("# Transcript at the top", without_marker)

    def test_the_summary_callout_stays_open(self):
        # Every other generated section folds; the summary is the one worth
        # reading without a click.
        _note, head = vt.build_note(
            self.schema,
            vt.frontmatter_metadata(self.schema, "memo"),
            "A short summary.",
            "callout",
            "",
            "Cleaned memo text.",
            "raw memo\n",
        )
        self.assertIn("> [!summary]\n> A short summary.", head)
        self.assertNotIn("[!summary]-", head)

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

    def test_the_ratio_floor_admits_a_condensed_cleanup(self):
        # The register compresses. A cleanup at roughly half the spoken length is
        # the register working, not a cleanup that summarized.
        body = transcript(SOLO_BLOCKS * 4)
        words = " ".join(entry["text"] for entry in vt.parse_transcript(body)["blocks"]).split()
        condensed = " ".join(words[: int(len(words) * 0.45)])
        problems, measurements = self.check(body, condensed)
        self.assertGreater(measurements["cleaned_ratio"], vt.CLEANED_RATIO_MIN)
        self.assertFalse([problem for problem in problems if "outside" in problem], problems)

    def test_discourse_fillers_are_not_distinctive_content(self):
        # Deleting these is the register's job, so losing them must not read as
        # losing the substance.
        rare = vt.rare_words(
            "I basically think the gasket essentially needs replacing, literally, "
            "and definitely the portafilter too, probably."
        )
        for filler in ("basically", "essentially", "literally", "definitely", "probably"):
            self.assertNotIn(filler, rare)
        self.assertIn("portafilter", rare)

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


class VoiceBoundaryTests(unittest.TestCase):
    def test_only_single_speaker_memo_and_journal_get_owner_mode(self):
        owner = {"material_role": "owner-authored", "recording_type": "journal", "effective_speakers": 1}
        conversation = {
            "material_role": "personal-exchange",
            "recording_type": "conversation",
            "effective_speakers": 2,
        }
        source = {"material_role": "external-source", "recording_type": "lecture", "effective_speakers": 1}
        self.assertEqual(vt.voice_context_for(owner), "owner")
        self.assertEqual(vt.voice_context_for(conversation), "none")
        self.assertEqual(vt.voice_context_for(source), "source")

    def test_inconsistent_owner_classification_is_held_for_review(self):
        body = transcript(DIALOGUE_BLOCKS)
        parsed = vt.parse_transcript(body)
        item = {"path": "00 Inbox/x.md"}
        value = {
            "recording_type": "conversation",
            "material_role": "owner-authored",
            "title": "Deployment Window Review",
            "speakers": {},
            "effective_speakers": 2,
            "spoken_date": None,
            "needs_review": False,
        }
        record, warnings = vt.validate_classification(value, item, parsed)
        self.assertTrue(record["needs_review"])
        self.assertIn("inconsistent", record["review_reason"])
        self.assertTrue(any("inconsistent" in warning for warning in warnings))

    def test_external_source_payload_requests_structured_full_content(self):
        voice = vt.vault_voice.parse_voice_note(
            "## Global voice\n\n### Source-derived\n\n- Describe source claims analytically.\n"
        )
        record = {
            "recording_type": "lecture",
            "material_role": "external-source",
            "effective_speakers": 1,
        }
        chunk = [{"speaker": None, "seconds": 0, "timestamp": "00:00", "text": "The mechanism has two parts."}]
        payload, _source = vt.cleanup_payload(record, chunk, 1, 1, [], "", {}, True, False, voice)
        self.assertTrue(payload["structuredFullContent"])
        self.assertEqual(payload["materialRole"], "external-source")


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

    def assertProcessed(self, *names):
        """The inbox holds exactly these notes and a recording note for each.

        Processing writes a pair: the note made from the recording, and the
        recording itself under its own name. Stating it once here keeps the
        tests about what they are each testing, and still fails loudly if a
        recording goes missing or an extra one appears.
        """
        expected = sorted([*names, *(f"{name[:-3]} - Transcript.md" for name in names)])
        self.assertEqual(self.inbox(), expected)

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
        self.assertEqual(
            self.inbox(),
            [
                "2026-07-24 - Memo - Espresso Machine Repairs - Transcript.md",
                "2026-07-24 - Memo - Espresso Machine Repairs.md",
            ],
        )
        note = (self.vault / "00 Inbox" / "2026-07-24 - Memo - Espresso Machine Repairs.md").read_text(encoding="utf-8")
        self.assertTrue(note.startswith('---\ntype: note\nstatus: raw\ncapture_type: voice\nprocessed_by:\n  - "vault-transcripts"\n---\n'))
        self.assertIn("> [!summary]", note)
        # The note points at the recording; the recording is the note beside it,
        # byte for byte under its own frontmatter.
        self.assertEqual(
            note.split("\n# Transcript\n\n", 1)[1],
            "[[2026-07-24 - Memo - Espresso Machine Repairs - Transcript]]\n",
        )
        self.assertEqual([line for line in note.splitlines() if line.startswith("# ")], ["# Transcript"])
        raw = (
            self.vault / "00 Inbox" / "2026-07-24 - Memo - Espresso Machine Repairs - Transcript.md"
        ).read_text(encoding="utf-8")
        self.assertTrue(raw.endswith(body))
        self.assertTrue(
            raw.startswith(
                "---\ntype: source\nstatus: complete\n"
                'parent: "[[2026-07-24 - Memo - Espresso Machine Repairs]]"\n'
                "source_kind: transcript\ncapture_type: voice\n---\n"
            ),
            raw[:400],
        )
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
        # Both halves of the pair carry frontmatter now, so both are skipped.
        self.assertEqual(result["data"]["counts"]["skipped_non_transcript"], 2)

    def test_exact_duplicates_are_quarantined_recoverably(self):
        body = transcript(SOLO_BLOCKS)
        self.write("20260612 093818-7FE5D769.md", body)
        self.write("20260612 093818-7FE5D769 (1).md", body)
        with StubServer() as server:
            result = self.result_of(self.process(server.url, "--apply"))
            self.assertEqual(len(server.stage_requests("classify")), 1)
        self.assertEqual(result["data"]["counts"]["duplicates_exact"], 1)
        self.assertProcessed("2026-06-12 - Memo - Espresso Machine Repairs.md")
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
        self.assertProcessed("Memo - Espresso Machine Repairs.md")
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
        self.assertProcessed("2026-07-24 - Memo - Espresso Repairs.md")

    def test_two_recordings_with_the_same_title_both_survive(self):
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        self.write("20260724 154302-1B2C3D4E.md", transcript(SOLO_BLOCKS + [block("A different closing thought here.", 86)]))
        with StubServer() as server:
            result = self.result_of(self.process(server.url, "--apply"))
        self.assertEqual(result["data"]["counts"]["processed"], 2)
        self.assertProcessed(
            "2026-07-24 - Memo - Espresso Machine Repairs.md",
            "2026-07-24 1543 - Memo - Espresso Machine Repairs.md",
        )
        # Each note points at its own recording, not at the other one's.
        for name in ("2026-07-24 - Memo - Espresso Machine Repairs", "2026-07-24 1543 - Memo - Espresso Machine Repairs"):
            note = (self.vault / "00 Inbox" / f"{name}.md").read_text(encoding="utf-8")
            self.assertTrue(note.endswith(f"# Transcript\n\n[[{name} - Transcript]]\n"), note[-200:])

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
        self.assertProcessed("Memo - Espresso Repairs.md")
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
        self.assertProcessed("2026-07-24 - Memo - Espresso Machine Maintenance.md")
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
        self.assertProcessed("2026-07-24 - Memo - Espresso Machine Repairs.md")

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
        self.assertProcessed("2026-07-24 - Memo - Espresso Machine Repairs.md")
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
        self.assertProcessed("2026-07-24 - Conversation - Deployment Window Review.md")

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


LEXICON = {
    "terms": [
        {
            "correct": "Bodhicitta",
            "variants": ["Buddhic chitta"],
            "category": "term",
            "case_sensitive": False,
            "whole_word": True,
            "note": "",
        }
    ],
    "speakers": [
        {
            "name": "Marge Anderson",
            "link": "[[Marge Anderson]]",
            "appears": "always",
            "aliases": ["Marge"],
            "cue": "The other voice in the Slipstream sync.",
            "role": "Executive Vice President",
        },
        {
            "name": "Alexi Miller",
            "link": "[[Alexi Miller]]",
            "appears": "sometimes",
            "aliases": ["Alexi"],
            "cue": "",
            "role": "Director of Building Innovation",
        },
    ],
}


class LexiconTests(unittest.TestCase):
    """The lexicon's contact points with the pipeline's own invariants."""

    def blocks(self, *texts):
        return [{"speaker": None, "seconds": index, "text": text, "raw": f"*0:0{index}*\n{text}\n\n"}
                for index, text in enumerate(texts, start=1)]

    def test_corrections_rewrite_the_prompt_text_and_never_the_raw_export(self):
        blocks = self.blocks("we talked about Buddhic chitta for an hour")
        corrected, rows = vt.correct_blocks(blocks, LEXICON["terms"])
        self.assertIn("Bodhicitta", corrected[0]["text"])
        self.assertEqual(corrected[0]["raw"], blocks[0]["raw"])
        self.assertEqual(rows, [{"correct": "Bodhicitta", "variant": "Buddhic chitta", "count": 1}])

    def test_a_corrected_term_does_not_read_as_fabrication(self):
        # The gate compares against the corrected source, so a tier-one
        # correction is simply present on both sides.
        source = "we talked about Bodhicitta for an hour"
        self.assertEqual(vt.check_chunk("We talked about Bodhicitta for an hour.", source, {}, False, False), [])

    def test_a_model_correction_is_fabrication_unless_it_was_offered(self):
        source = "we talked about Buddhistta for an hour"
        cleaned = "We talked about Bodhicitta for an hour."
        self.assertIn("bodhicitta", vt.added_words(source, cleaned, []))
        self.assertEqual(vt.added_words(source, cleaned, ["Bodhicitta"]), [])

    def test_the_gate_takes_the_offered_terms_from_the_payload(self):
        source = "we covered Buddhistta and Lhojjong and Tongkapa and Shantee Deva and Adisha today"
        cleaned = "We covered Bodhicitta and Lojong and Tsongkhapa and Śāntideva and Atiśa today."
        terms = ["Bodhicitta", "Lojong", "Tsongkhapa", "Śāntideva", "Atiśa"]
        self.assertIn("not in the chunk", vt.check_chunk(cleaned, source, {}, False, False)[0])
        glossary = [{"term": term, "heardAs": "x"} for term in terms]
        self.assertEqual(vt.check_chunk(cleaned, source, {}, False, False, glossary), [])

    def test_an_applied_offer_becomes_a_proposal(self):
        glossary = [{"term": "Bodhicitta", "heardAs": "Bodhi citta"}]
        source = "we talked about Bodhi citta"
        self.assertEqual(
            vt.accepted_corrections(source, "We talked about Bodhicitta.", glossary),
            [{"correct": "Bodhicitta", "variant": "Bodhi citta"}],
        )
        # Declined offers are not proposed.
        self.assertEqual(vt.accepted_corrections(source, "We talked about Bodhi citta.", glossary), [])

    def test_the_roster_reaches_classification_from_anywhere_in_the_recording(self):
        blocks = self.blocks(*(["nothing much to see here"] * 400), "and then Alexi sent the numbers over")
        item = {"path": "00 Inbox/a.md", "filename_hint": None, "sha256": "x",
                "stats": {"blocks": len(blocks), "words": 2000, "duration_seconds": 60}}
        payload = vt.classify_payload(item, {"preamble": "", "blocks": blocks, "trailing": ""}, LEXICON)
        offered = {entry["name"] for entry in payload["knownSpeakers"]}
        # Alexi is named far past the head excerpt the model is shown.
        self.assertNotIn("Alexi", payload["head"])
        self.assertEqual(offered, {"Marge Anderson", "Alexi Miller"})

    def test_a_roster_name_outside_the_offered_list_is_dropped(self):
        record, warnings = self.classified(
            {"who": "Someone Invented", "kind": "name", "confidence": "high", "source": "roster",
             "evidence": "you are presenting at the USGBC session"}
        )
        self.assertEqual(record["speakers"]["Speaker 1"]["who"], "unknown")
        self.assertTrue(any("not in the offered roster" in warning for warning in warnings))

    def classified(self, entry, roster_names=("Marge Anderson",)):
        parsed = {
            "preamble": "",
            "blocks": [
                {"speaker": "Speaker 1", "seconds": 1, "text": "I need the numbers before Friday", "raw": ""},
                {"speaker": "Speaker 2", "seconds": 2, "text": "you are presenting at the USGBC session", "raw": ""},
            ],
            "trailing": "",
        }
        value = {
            "recording_type": "meeting",
            "material_role": "personal-exchange",
            "title": "A Real Meeting Title",
            "effective_speakers": 2,
            "speakers": {"Speaker 1": entry},
        }
        return vt.validate_classification(value, {"path": "00 Inbox/a.md", "stats": {}}, parsed, list(roster_names))

    def test_an_offered_roster_name_survives_and_is_spelled_the_roster_way(self):
        record, _warnings = self.classified(
            {"who": "marge anderson", "kind": "name", "confidence": "medium", "source": "roster",
             "evidence": "you are presenting at the USGBC session"}
        )
        self.assertEqual(record["speakers"]["Speaker 1"]["who"], "Marge Anderson")

    def test_quoting_the_cue_back_is_not_evidence(self):
        # The observed failure: the model repeats the roster cue verbatim and
        # attaches a real person to whichever label came first.
        record, warnings = self.classified(
            {"who": "Marge Anderson", "kind": "name", "confidence": "medium", "source": "roster",
             "evidence": "The other voice in my recurring Slipstream one-to-one."}
        )
        self.assertEqual(record["speakers"]["Speaker 1"]["who"], "unknown")
        self.assertTrue(any("not from the transcript" in warning for warning in warnings))

    def test_a_roster_name_with_no_evidence_at_all_is_dropped(self):
        record, warnings = self.classified(
            {"who": "Marge Anderson", "kind": "name", "confidence": "medium", "source": "roster"}
        )
        self.assertEqual(record["speakers"]["Speaker 1"]["who"], "unknown")
        self.assertTrue(any("not from the transcript" in warning for warning in warnings))

    def test_a_roster_identification_is_usable_at_medium_confidence(self):
        labels = ["Speaker 1", "Speaker 2"]
        speakers = {
            "Speaker 1": {"who": "Marge Anderson", "kind": "name", "confidence": "medium", "source": "roster"},
            "Speaker 2": {"who": "Someone", "kind": "name", "confidence": "medium", "source": "transcript"},
        }
        mapping, drop = vt.derive_speaker_map(labels, speakers, 2, "names", None, LEXICON)
        self.assertFalse(drop)
        # The roster carries the identification; a medium transcript guess does not.
        self.assertEqual(mapping, {"Speaker 1": "Marge Anderson", "Speaker 2": "Speaker 2"})

    def test_an_alias_is_written_as_the_name_the_vault_files(self):
        speakers = {"Speaker 1": {"who": "Marge", "kind": "name", "confidence": "high", "source": "transcript"}}
        mapping, _drop = vt.derive_speaker_map(["Speaker 1", "Speaker 2"], speakers, 2, "names", None, LEXICON)
        self.assertEqual(mapping["Speaker 1"], "Marge Anderson")

    def test_a_corrected_term_is_not_counted_as_a_dropped_source_word(self):
        # The rare-word check measures against the source the cleanup read, not
        # the raw export, or every correction reads as a vanished distinctive word.
        class Args:
            compiled_lexicon = LEXICON

        parsed = {"preamble": "", "blocks": self.blocks("we talked about Buddhic chitta"), "trailing": ""}
        corrected = vt.corrected_source_text(parsed, Args())
        self.assertIn("Bodhicitta", corrected)
        self.assertNotIn("Buddhic chitta", corrected)
        # A correction the model made on offer counts too, though it is not
        # recorded in the dictionary yet.
        parsed = {"preamble": "", "blocks": self.blocks("he explained Bodhi citta"), "trailing": ""}
        proposals = [{"correct": "Bodhicitta", "variant": "Bodhi citta"}]
        self.assertIn("Bodhicitta", vt.corrected_source_text(parsed, Args(), proposals))

    def test_the_report_names_corrections_proposals_and_roster_speakers(self):
        records = [
            {
                "source": "00 Inbox/a.md",
                "corrections": [{"correct": "Bodhicitta", "variant": "Buddhic chitta", "count": 3}],
                "proposals": [{"correct": "Bodhicitta", "variant": "Bodhi citta"}],
                "roster_speakers": ["Marge Anderson"],
            }
        ]
        report = "\n".join(vt.lexicon_report(records))
        self.assertIn("`Buddhic chitta` → `Bodhicitta` ×3", report)
        self.assertIn("Marge Anderson", report)
        self.assertIn("`Bodhi citta` → `Bodhicitta`", report)
        self.assertEqual(vt.lexicon_report([{"source": "x"}]), [])


def run_dir_of(result):
    return Path(result["data"]["run_directory"])


PROFILE_CARD = {
    "order": 0,
    "name": "People in My Life",
    "link": "[[People in My Life]]",
    "tier": "when-relevant",
    "scope": "owner-authored",
    "routes": frozenset({"personal"}),
    "triggers": ["Gillian"],
    "note": "",
    "facts": ["Gillian Eorwyn is my spouse."],
}
ALWAYS_CARD = {
    **PROFILE_CARD,
    "order": 1,
    "name": "Core Identity",
    "link": "[[Core Identity]]",
    "tier": "always",
    "scope": "universal",
    "routes": frozenset(),
    "triggers": [],
    "facts": ["Sociologist of knowledge."],
}
PROFILE = {"cards": [PROFILE_CARD, ALWAYS_CARD]}


class PersonalContextSiteTests(unittest.TestCase):
    def site(self, recording_type, material_role, speakers=1):
        return vault_transcripts.profile_site_for(
            {"recording_type": recording_type, "material_role": material_role, "effective_speakers": speakers}
        )

    def test_therapy_is_owner_context_where_voice_is_none(self):
        """The reason profile_site_for exists: voice asks whose style, this asks
        whose life, and for therapy those answers differ."""
        record = {"recording_type": "therapy", "material_role": "personal-exchange", "effective_speakers": 2}
        self.assertEqual(vault_transcripts.voice_context_for(record), vault_transcripts.vault_voice.CONTEXT_NONE)
        self.assertEqual(
            vault_transcripts.profile_site_for(record)["context_mode"],
            vault_transcripts.vault_voice.CONTEXT_OWNER,
        )

    def test_therapy_and_journal_are_the_only_types_asserting_a_route(self):
        self.assertTrue(self.site("therapy", "personal-exchange", 2)["routes"])
        self.assertTrue(self.site("journal", "owner-authored")["routes"])
        for recording_type in ("meeting", "conversation", "lecture", "memo", "other"):
            self.assertEqual(self.site(recording_type, "personal-exchange", 2)["routes"], frozenset())

    def test_a_work_meeting_gets_no_route_gated_card(self):
        site = self.site("meeting", "personal-exchange", 3)
        selected = vault_transcripts.vault_profile.select_cards(PROFILE, "Gillian came up", site)
        self.assertEqual([card["name"] for card in selected], ["Core Identity"])

    def test_a_therapy_session_does_get_the_route_gated_card(self):
        site = self.site("therapy", "personal-exchange", 2)
        selected = vault_transcripts.vault_profile.select_cards(PROFILE, "Gillian came up", site)
        self.assertIn("People in My Life", [card["name"] for card in selected])

    def test_an_unknown_role_selects_nothing(self):
        site = self.site("journal", "unknown")
        self.assertEqual(vault_transcripts.vault_profile.select_cards(PROFILE, "Gillian", site), [])


class CleanupFidelityTests(unittest.TestCase):
    """Regression lock. cleanup runs behind check_chunk, which rejects a chunk
    containing words the source did not. A card naming Gillian would invite the
    model to write that name and the gate would then throw the chunk away, so
    no profile-derived key may ever appear in this payload."""

    def test_cleanup_payload_carries_no_personal_context(self):
        record = {
            "recording_type": "journal",
            "material_role": "owner-authored",
            "effective_speakers": 1,
            "title": "A journal entry",
        }
        chunk = [{"speaker": None, "seconds": 0, "timestamp": "00:00", "text": "I talked with my wife today."}]
        payload, _source = vault_transcripts.cleanup_payload(record, chunk, 1, 1, [], "", {}, True, False)
        serialized = json.dumps(payload)
        self.assertNotIn("personalContext", payload)
        self.assertNotIn("Gillian", serialized)
        self.assertNotIn("Sociologist", serialized)

    def test_the_fidelity_source_excludes_the_generated_callouts(self):
        # The reviewer compares an utterance against the cleanup. A summary that
        # paraphrases the same utterance must not be able to answer for a
        # cleanup that dropped it.
        head = (
            "> [!summary]\n> The speaker discussed the espresso gasket at length.\n\n"
            "> [!reflection]- Context\n> - Continues the repair thread.\n\n"
            "The gasket needs replacing.\n"
        )
        self.assertEqual(vt.strip_callout_lines(head).strip(), "The gasket needs replacing.")

    def test_stripping_callouts_leaves_ordinary_prose_alone(self):
        text = "A paragraph.\n\n## A heading\n\n- a bullet\n"
        self.assertEqual(vt.strip_callout_lines(text), text.rstrip("\n"))


class SummarySystemTests(unittest.TestCase):
    def test_the_always_tier_reaches_the_summary_system_prompt(self):
        site = vault_transcripts.vault_profile.profile_site(
            vault_transcripts.vault_voice.CONTEXT_OWNER, routes=["personal/journal"]
        )
        system = vault_transcripts.summary_system(None, vault_transcripts.vault_voice.CONTEXT_OWNER, PROFILE, site)
        self.assertIn("Sociologist of knowledge.", system)
        self.assertNotIn("Gillian", system)

    def test_without_a_site_the_summary_system_prompt_is_unchanged(self):
        system = vault_transcripts.summary_system(None, vault_transcripts.vault_voice.CONTEXT_NONE)
        self.assertEqual(system, vault_transcripts.SUMMARY_SYSTEM)


class ReflectionTests(unittest.TestCase):
    CANDIDATES = {"[[Espresso machine]]"}
    URLS = {"https://ok.example/seal"}

    def render(self, value, recording_type="memo"):
        return vt.validate_reflection(value, recording_type, self.CANDIDATES, self.URLS)

    def test_only_memo_and_journal_get_a_reflection(self):
        self.assertEqual(sorted(vt.REFLECTION_SECTIONS), ["journal", "memo"])
        self.assertEqual(sorted(vt.REFLECTION_SYSTEMS), ["journal", "memo"])

    def test_memo_sections_render_in_order_and_omit_the_empty_ones(self):
        markdown, dropped = self.render(
            {
                "context": ["Continues the repair thread."],
                "open_questions": [],
                "next_steps": ["Order the gasket."],
                "connections": ["[[Espresso machine]] has the model number."],
            }
        )
        self.assertEqual(dropped, [])
        self.assertEqual(
            [line for line in markdown.splitlines() if line.startswith("> [!")],
            [
                "> [!reflection]- Context",
                "> [!reflection]- Next steps",
                "> [!connections]- Connections",
            ],
        )
        self.assertNotIn("Open questions", markdown)

    def test_sections_render_as_collapsed_callouts(self):
        markdown, _dropped = self.render(
            {"context": ["Continues the repair thread."], "connections": ["[[Espresso machine]] has it."]}
        )
        self.assertIn("> [!reflection]- Context\n> - Continues the repair thread.", markdown)
        self.assertIn("> [!connections]- Connections\n> - [[Espresso machine]] has it.", markdown)
        # Never a heading: as one it was indistinguishable from the cleanup's own.
        self.assertNotIn("##", markdown)

    def test_a_journal_keeps_its_own_section_set(self):
        markdown, _dropped = self.render(
            {"observations": ["The owner described a hard week."], "context": ["ignored"]}, "journal"
        )
        self.assertIn("[!reflection]- Observations", markdown)
        self.assertNotIn("Context", markdown)

    def test_a_cited_outside_connection_survives(self):
        markdown, dropped = self.render(
            {"connections": ["Outside vault: the seal is food-grade (https://ok.example/seal)."]}
        )
        self.assertEqual(dropped, [])
        self.assertIn("Outside vault: the seal is food-grade", markdown)

    def test_an_outside_connection_citing_an_unread_url_is_dropped(self):
        markdown, dropped = self.render({"connections": ["Outside vault: a claim (https://nope.example/x)."]})
        self.assertEqual(markdown, "")
        self.assertEqual(len(dropped), 1)
        self.assertIn("https://nope.example/x", dropped[0])

    def test_an_uncited_claim_is_dropped_however_it_is_labelled(self):
        for item in (
            "Gaskets usually last five years.",
            "Outside knowledge: gaskets usually last five years.",
            "Outside vault: gaskets usually last five years.",
        ):
            markdown, dropped = self.render({"connections": [item]})
            self.assertEqual(markdown, "", item)
            self.assertEqual(len(dropped), 1, item)

    def test_a_wikilink_outside_the_candidate_set_is_dropped(self):
        markdown, dropped = self.render({"connections": ["[[Invented Note]] looks related."]})
        self.assertEqual(markdown, "")
        self.assertIn("[[Invented Note]]", dropped[0])

    def test_one_bad_connection_does_not_cost_the_rest_of_the_reflection(self):
        markdown, dropped = self.render(
            {
                "context": ["Continues the repair thread."],
                "connections": ["Remembered fact.", "[[Espresso machine]] has the model number."],
            }
        )
        self.assertIn("[!reflection]- Context", markdown)
        self.assertIn("[[Espresso machine]] has the model number.", markdown)
        self.assertNotIn("Remembered fact.", markdown)
        self.assertEqual(len(dropped), 1)

    def test_a_malformed_response_is_still_fatal(self):
        with self.assertRaises(vt.UserError):
            self.render(["not", "an", "object"])
        with self.assertRaises(vt.UserError):
            self.render({"context": ["fine"], "connections": [{"not": "a string"}]})


class OutsideSourceTests(unittest.TestCase):
    NOTE = (
        "---\ntype: note\n---\n\n"
        "## Findings\n\n"
        "- Group seals are food grade.\n"
        '  - "Rated to 120 C for food contact" — https://ok.example/seal\n\n'
        "## Sources\n\n"
        "- https://bare.example/nothing\n"
    )

    def harvest(self, material):
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            (vault / "Espresso machine.md").write_text(self.NOTE, encoding="utf-8")
            candidates = [{"path": "Espresso machine.md", "wikilink": "[[Espresso machine]]"}]
            return vt.outside_sources(material, vault, candidates)

    def test_a_link_in_the_recording_is_harvested_with_its_line(self):
        found = self.harvest("I found the part page at https://parts.example/gasket-42 and it looks right.")
        self.assertEqual(found[0]["url"], "https://parts.example/gasket-42")
        self.assertEqual(found[0]["source"], "this recording")
        self.assertIn("looks right", found[0]["excerpt"])

    def test_a_cited_quote_in_a_candidate_note_is_harvested_and_attributed(self):
        entry = next(row for row in self.harvest("no links here") if row["url"] == "https://ok.example/seal")
        self.assertEqual(entry["source"], "[[Espresso machine]]")
        self.assertIn("food contact", entry["excerpt"])

    def test_a_bare_url_carrying_no_claim_is_not_a_source(self):
        urls = {row["url"] for row in self.harvest("https://also-bare.example/x")}
        self.assertNotIn("https://bare.example/nothing", urls)
        self.assertNotIn("https://also-bare.example/x", urls)

    def test_a_repeated_url_is_harvested_once(self):
        material = "See https://ok.example/seal for the rating, which the note also cites."
        found = [row for row in self.harvest(material) if row["url"] == "https://ok.example/seal"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["source"], "this recording")

    def test_harvesting_is_capped(self):
        material = "\n".join(
            f"A claim worth citing here about part {index} at https://example.com/{index}" for index in range(40)
        )
        self.assertEqual(len(self.harvest(material)), vt.vault_reflection.OUTSIDE_SOURCE_LIMIT)

    def test_an_unreadable_candidate_is_skipped_rather_than_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            candidates = [{"path": "missing.md", "wikilink": "[[missing]]"}]
            self.assertEqual(vt.outside_sources("no links", vault, candidates), [])


class SplitTests(unittest.TestCase):
    """Moving the recording out of notes processed before the split existed.

    Deterministic by construction: the recording is the bytes after the marker,
    so every test here is really one claim -- the two halves put back together
    are the note that was there.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name).resolve() / "vault"
        (self.vault / "99 Meta" / "99.02 Schemas").mkdir(parents=True)
        (self.vault / "00 Inbox").mkdir()
        (self.vault / "99 Meta" / "99.02 Schemas" / "0.00 Vault Schema.md").write_text(
            SOURCES_SCHEMA, encoding="utf-8"
        )
        self.body = transcript(SOLO_BLOCKS)

    def tearDown(self):
        self.tmp.cleanup()

    def combined(self, relative, frontmatter, body=None):
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        text = f"---\n{frontmatter}---\n\n> [!summary]\n> A summary.\n\nCleaned prose.\n\n# Transcript\n\n{body or self.body}"
        path.write_text(text, encoding="utf-8")
        return path

    def split(self, *extra):
        completed = run_script("split", "--vault", str(self.vault), *extra)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return json.loads(completed.stdout)

    def notes(self):
        return sorted(
            path.relative_to(self.vault).as_posix()
            for path in self.vault.rglob("*.md")
            if not any(part.startswith(".") for part in path.relative_to(self.vault).parts)
        )

    def test_a_dry_run_changes_nothing(self):
        note = self.combined(
            "01 Personal/1.01 Journal/A Talk.md",
            "type: note\nstatus: active\ndomain: personal\nsubdomain: journal\n",
        )
        before = note.read_text(encoding="utf-8")
        result = self.split()
        self.assertEqual(result["data"]["counts"]["notes_to_split"], 1)
        self.assertEqual(result["data"]["counts"]["applied"], 0)
        self.assertEqual(note.read_text(encoding="utf-8"), before)
        self.assertEqual(len(self.notes()), 2)  # the note and the schema

    def test_the_two_halves_reconstruct_the_original(self):
        note = self.combined(
            "01 Personal/1.01 Journal/A Talk.md",
            "type: note\nstatus: active\ndomain: personal\nsubdomain: journal\n",
        )
        original = note.read_text(encoding="utf-8")
        self.split("--apply")
        raw = self.vault / "10 Sources/10.03 Transcript/Personal/Journal/A Talk - Transcript.md"
        self.assertTrue(raw.is_file(), self.notes())
        head = note.read_text(encoding="utf-8").split("---\n", 2)[2].rsplit("[[", 1)[0]
        recording = raw.read_text(encoding="utf-8").split("---\n", 2)[2].lstrip("\n")
        self.assertEqual(head + recording, original.split("---\n", 2)[2])
        self.assertEqual(recording, self.body)

    def test_the_note_points_at_the_recording_and_back(self):
        note = self.combined(
            "01 Personal/1.01 Journal/A Talk.md",
            "type: note\nstatus: active\ndomain: personal\nsubdomain: journal\n",
        )
        self.split("--apply")
        raw = self.vault / "10 Sources/10.03 Transcript/Personal/Journal/A Talk - Transcript.md"
        self.assertTrue(note.read_text(encoding="utf-8").endswith("# Transcript\n\n[[A Talk - Transcript]]\n"))
        self.assertIn('parent: "[[A Talk]]"', raw.read_text(encoding="utf-8"))
        self.assertIn("source_kind: transcript", raw.read_text(encoding="utf-8"))
        self.assertIn("status: complete", raw.read_text(encoding="utf-8"))

    def test_a_source_note_becomes_a_note_about_its_recording(self):
        note = self.combined(
            "01 Personal/1.01 Journal/A Lecture.md",
            "type: source\nstatus: active\ndomain: personal\nsubdomain: journal\nsource_kind: video\n",
        )
        result = self.split("--apply")
        self.assertEqual(result["data"]["counts"]["converted_from_source"], 1)
        text = note.read_text(encoding="utf-8")
        self.assertIn("type: note\n", text)
        # The schema forbids source_kind anywhere but a source.
        self.assertNotIn("source_kind", text)
        raw = self.vault / "10 Sources/10.03 Transcript/Personal/Journal/A Lecture - Transcript.md"
        self.assertIn("source_kind: transcript", raw.read_text(encoding="utf-8"))

    def test_a_note_the_domain_does_not_cover_is_held_not_guessed(self):
        self.combined("01 Personal/1.01 Journal/Stray.md", "type: note\nstatus: active\ndomain: gardening\n")
        result = self.split("--apply")
        self.assertEqual(result["data"]["counts"]["notes_to_split"], 0)
        self.assertEqual(result["data"]["counts"]["skipped"], 1)
        self.assertTrue(any("not in the schema" in warning for warning in result["warnings"]), result["warnings"])

    def test_an_unknown_subdomain_files_at_the_domain_with_a_warning(self):
        self.combined("01 Personal/Loose.md", "type: note\nstatus: active\ndomain: personal\nsubdomain: gardening\n")
        result = self.split("--apply")
        self.assertTrue((self.vault / "10 Sources/10.03 Transcript/Personal/Loose - Transcript.md").is_file(), self.notes())
        self.assertTrue(any("subdomain" in warning for warning in result["warnings"]), result["warnings"])

    def test_splitting_twice_is_a_no_op(self):
        self.combined(
            "01 Personal/1.01 Journal/A Talk.md",
            "type: note\nstatus: active\ndomain: personal\nsubdomain: journal\n",
        )
        self.split("--apply")
        after = self.notes()
        result = self.split("--apply")
        self.assertEqual(result["data"]["counts"]["notes_to_split"], 0)
        self.assertEqual(self.notes(), after)

    def test_two_notes_of_the_same_name_get_separate_recordings(self):
        self.combined(
            "01 Personal/1.01 Journal/A Talk.md",
            "type: note\nstatus: active\ndomain: personal\nsubdomain: journal\n",
        )
        self.combined(
            "99 Meta/99.02 Schemas/A Talk.md",
            "type: note\nstatus: active\ndomain: personal\nsubdomain: journal\n",
            body=transcript(TINY_BLOCKS),
        )
        self.split("--apply")
        recordings = sorted(
            path.name for path in (self.vault / "10 Sources/10.03 Transcript/Personal/Journal").glob("*.md")
        )
        self.assertEqual(recordings, ["A Talk - Transcript (2).md", "A Talk - Transcript.md"])

    def test_a_note_with_no_marker_is_left_alone(self):
        path = self.vault / "01 Personal" / "Plain.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\ntype: note\nstatus: active\ndomain: personal\n---\n\nJust prose.\n", encoding="utf-8")
        result = self.split("--apply")
        self.assertEqual(result["data"]["counts"]["notes_to_split"], 0)
        self.assertEqual(result["data"]["counts"]["skipped"], 0)

    def test_a_raw_export_without_frontmatter_is_left_for_the_pipeline(self):
        path = self.vault / "00 Inbox" / "20260724 131748-9788991C.md"
        path.write_text(self.body, encoding="utf-8")
        result = self.split("--apply")
        self.assertEqual(result["data"]["counts"]["notes_to_split"], 0)
        self.assertEqual(path.read_text(encoding="utf-8"), self.body)

    def test_the_original_is_backed_up_before_it_is_rewritten(self):
        note = self.combined(
            "01 Personal/1.01 Journal/A Talk.md",
            "type: note\nstatus: active\ndomain: personal\nsubdomain: journal\n",
        )
        original = note.read_text(encoding="utf-8")
        result = self.split("--apply")
        run_dir = Path(result["data"]["run_directory"])
        backup = run_dir / "backup" / "01 Personal/1.01 Journal/A Talk.md"
        self.assertEqual(backup.read_text(encoding="utf-8"), original)
        log = [json.loads(line) for line in (run_dir / "apply-log.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(log[0]["op"], "split")
        self.assertEqual(log[0]["status"], "ok")

    def test_a_vault_without_the_vocabulary_is_refused(self):
        (self.vault / "99 Meta" / "99.02 Schemas" / "0.00 Vault Schema.md").write_text(
            NO_SOURCE_SCHEMA, encoding="utf-8"
        )
        completed = run_script("split", "--vault", str(self.vault))
        self.assertEqual(completed.returncode, 2)
        self.assertIn("source", json.loads(completed.stdout)["errors"][0]["message"])


class ReconcileTests(unittest.TestCase):
    """Re-exports of recordings the vault already has.

    Matching is on the recording's text, never the filename: the export names
    drift, and two files a byte apart are still the same recording.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name).resolve() / "vault"
        (self.vault / "99 Meta" / "99.02 Schemas").mkdir(parents=True)
        (self.vault / "00 Inbox").mkdir()
        (self.vault / "99 Meta" / "99.02 Schemas" / "0.00 Vault Schema.md").write_text(
            SOURCES_SCHEMA, encoding="utf-8"
        )
        self.body = transcript(SOLO_BLOCKS)

    def tearDown(self):
        self.tmp.cleanup()

    def filed(self, recording=None):
        path = self.vault / "01 Personal" / "1.01 Journal" / "2026-07-24 - Memo - Espresso Machine Repairs.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\ntype: note\nstatus: active\ndomain: personal\n---\n\n> [!summary]\n> A summary.\n\n"
            f"Cleaned.\n\n# Transcript\n\n{recording or self.body}",
            encoding="utf-8",
        )
        return path

    def export(self, name="20260724 131748-9788991C.md", body=None):
        path = self.vault / "00 Inbox" / name
        path.write_text(body or self.body, encoding="utf-8")
        return path

    def reconcile(self, *extra):
        completed = run_script("reconcile", "--vault", str(self.vault), *extra)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return json.loads(completed.stdout)

    def test_a_re_export_of_a_filed_recording_is_quarantined(self):
        self.filed()
        export = self.export()
        result = self.reconcile("--apply")
        self.assertEqual(result["data"]["counts"]["matched"], 1)
        self.assertEqual(result["data"]["counts"]["quarantined"], 1)
        self.assertFalse(export.exists())
        quarantined = self.vault / ".vault-transcripts" / "duplicates" / export.name
        self.assertEqual(quarantined.read_text(encoding="utf-8"), self.body)

    def test_a_recording_differing_only_in_whitespace_still_matches(self):
        # The re-exports in the real inbox differ from their filed twins by a
        # byte or two of trailing whitespace.
        self.filed()
        self.export(body=self.body + "\n\n")
        result = self.reconcile("--apply")
        self.assertEqual(result["data"]["counts"]["matched"], 1)

    def test_a_recording_the_vault_does_not_have_is_left_alone(self):
        self.filed()
        export = self.export(name="20260801 090000-AAAA1111.md", body=transcript(TINY_BLOCKS))
        result = self.reconcile("--apply")
        self.assertEqual(result["data"]["counts"]["matched"], 0)
        self.assertEqual(result["data"]["counts"]["unmatched"], 1)
        self.assertTrue(export.is_file())

    def test_it_matches_through_a_split_notes_link(self):
        recording = self.vault / "10 Sources" / "10.03 Transcript" / "A Talk - Transcript.md"
        recording.parent.mkdir(parents=True, exist_ok=True)
        recording.write_text(f"---\ntype: source\nsource_kind: transcript\n---\n\n{self.body}", encoding="utf-8")
        self.filed(recording="[[A Talk - Transcript]]\n")
        self.export()
        result = self.reconcile("--apply")
        self.assertEqual(result["data"]["counts"]["matched"], 1)

    def test_a_dry_run_moves_nothing(self):
        self.filed()
        export = self.export()
        result = self.reconcile()
        self.assertEqual(result["data"]["counts"]["matched"], 1)
        self.assertEqual(result["data"]["counts"]["quarantined"], 0)
        self.assertTrue(export.is_file())

    def test_a_processed_inbox_note_is_not_a_candidate(self):
        self.filed()
        processed = self.vault / "00 Inbox" / "2026-07-24 - Memo - Something.md"
        processed.write_text(f"---\ntype: note\n---\n\nCleaned.\n\n# Transcript\n\n{self.body}", encoding="utf-8")
        result = self.reconcile("--apply")
        self.assertEqual(result["data"]["counts"]["matched"], 0)
        self.assertTrue(processed.is_file())


class ReprocessTests(unittest.TestCase):
    """Regenerating the head of a note the pipeline already wrote.

    The recording did not change; what was made of it did. So the invariants are
    about what must survive untouched: the name every wikilink points at, the
    frontmatter the organizer wrote, and the recording section itself.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name).resolve() / "vault"
        (self.vault / "99 Meta" / "99.02 Schemas").mkdir(parents=True)
        (self.vault / "00 Inbox").mkdir()
        (self.vault / "99 Meta" / "99.02 Schemas" / "0.00 Vault Schema.md").write_text(
            SOURCES_SCHEMA, encoding="utf-8"
        )
        self.body = transcript(SOLO_BLOCKS)

    def tearDown(self):
        self.tmp.cleanup()

    # Frontmatter the organizer would have written: far more than this skill
    # knows how to produce, and all of it has to survive.
    FILED = (
        "type: note\nstatus: active\ndomain: personal\nsubdomain: journal\n"
        'parent: "[[A Hub]]"\ncapture_type: voice\n'
    )

    def filed(self, name="2026-07-24 - Memo - Espresso Machine Repairs.md", frontmatter=None, tail=None):
        path = self.vault / "01 Personal" / "1.01 Journal" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\n{frontmatter or self.FILED}---\n\n> [!summary]\n> An older summary.\n\n"
            f"Older cleaned prose.\n\n# Transcript\n\n{tail if tail is not None else self.body}",
            encoding="utf-8",
        )
        return path

    def reprocess(self, url, *extra):
        completed = run_script("reprocess", "--vault", str(self.vault), "--base-url", url, "--model", "chat", *extra)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return json.loads(completed.stdout)

    def test_the_head_is_regenerated_and_the_recording_is_untouched(self):
        note = self.filed()
        with StubServer() as server:
            result = self.reprocess(server.url, "--apply")
        self.assertEqual(result["data"]["counts"]["reprocessed"], 1)
        text = note.read_text(encoding="utf-8")
        self.assertNotIn("Older cleaned prose.", text)
        self.assertNotIn("An older summary.", text)
        self.assertTrue(text.endswith(f"# Transcript\n\n{self.body}"), text[-120:])

    def test_frontmatter_survives_byte_for_byte(self):
        # The organizer's classification lives here. Rebuilding it from this
        # skill's three keys would throw domain, subdomain, and parent away.
        note = self.filed()
        with StubServer() as server:
            self.reprocess(server.url, "--apply")
        self.assertTrue(note.read_text(encoding="utf-8").startswith(f"---\n{self.FILED}---\n\n"))

    def test_the_note_is_never_renamed(self):
        note = self.filed()
        with StubServer() as server:
            self.reprocess(server.url, "--apply")
        self.assertTrue(note.is_file())
        self.assertEqual(
            [path.name for path in (self.vault / "01 Personal" / "1.01 Journal").glob("*.md")],
            ["2026-07-24 - Memo - Espresso Machine Repairs.md"],
        )

    def test_a_therapy_note_is_excluded_by_its_name(self):
        self.filed(name="2026-07-24 - Therapy - Facing Family Dynamics.md")
        with StubServer() as server:
            result = self.reprocess(server.url, "--apply")
            self.assertEqual(server.requests, [])
        self.assertEqual(result["data"]["counts"]["reprocessed"], 0)
        self.assertEqual(result["data"]["counts"]["skipped"], 1)
        self.assertTrue(any("therapy" in warning for warning in result["warnings"]), result["warnings"])

    def test_a_classified_therapy_disagreement_holds_the_note(self):
        # The filename wins for exclusion, but a therapy reading is a stop.
        self.filed()
        scripted = {"classify": [classify_response("therapy")]}
        with StubServer(scripted=scripted) as server:
            result = self.reprocess(server.url, "--apply")
        self.assertEqual(result["data"]["counts"]["reprocessed"], 0)
        self.assertTrue(any("held" in warning for warning in result["warnings"]), result["warnings"])

    def test_a_type_disagreement_keeps_the_name_and_warns(self):
        self.filed()
        scripted = {"classify": [classify_response("lecture")]}
        with StubServer(scripted=scripted) as server:
            result = self.reprocess(server.url, "--apply")
        self.assertEqual(result["data"]["counts"]["reprocessed"], 1)
        self.assertTrue(
            any("kept the name's reading" in warning for warning in result["warnings"]), result["warnings"]
        )

    def test_a_split_note_is_read_through_its_link(self):
        recording = self.vault / "10 Sources" / "10.03 Transcript" / "A Talk - Transcript.md"
        recording.parent.mkdir(parents=True, exist_ok=True)
        recording.write_text(f"---\ntype: source\nsource_kind: transcript\n---\n\n{self.body}", encoding="utf-8")
        note = self.filed(name="A Talk.md", tail="[[A Talk - Transcript]]\n")
        with StubServer() as server:
            result = self.reprocess(server.url, "--apply")
        self.assertEqual(result["data"]["counts"]["reprocessed"], 1)
        text = note.read_text(encoding="utf-8")
        self.assertTrue(text.endswith("# Transcript\n\n[[A Talk - Transcript]]\n"), text[-80:])
        # The recording's own note is not touched by reprocessing.
        self.assertTrue(recording.read_text(encoding="utf-8").endswith(self.body))

    def test_the_inbox_is_left_to_process(self):
        (self.vault / "00 Inbox" / "20260724 131748-9788991C.md").write_text(self.body, encoding="utf-8")
        processed = self.vault / "00 Inbox" / "2026-07-24 - Memo - Something.md"
        processed.write_text(f"---\ntype: note\n---\n\nCleaned.\n\n# Transcript\n\n{self.body}", encoding="utf-8")
        with StubServer() as server:
            result = self.reprocess(server.url, "--apply")
            self.assertEqual(server.requests, [])
        self.assertEqual(result["data"]["counts"]["selected"], 0)

    def test_a_dry_run_changes_nothing(self):
        note = self.filed()
        before = note.read_text(encoding="utf-8")
        with StubServer() as server:
            result = self.reprocess(server.url)
        self.assertEqual(result["data"]["counts"]["reprocessed"], 1)
        self.assertEqual(result["data"]["counts"]["applied"], 0)
        self.assertEqual(note.read_text(encoding="utf-8"), before)

    def test_the_original_is_backed_up_and_the_report_shows_both_summaries(self):
        note = self.filed()
        before = note.read_text(encoding="utf-8")
        with StubServer() as server:
            result = self.reprocess(server.url, "--apply")
        run_dir = Path(result["data"]["run_directory"])
        backup = run_dir / "backup" / "01 Personal/1.01 Journal/2026-07-24 - Memo - Espresso Machine Repairs.md"
        self.assertEqual(backup.read_text(encoding="utf-8"), before)
        report = (run_dir / "reprocess-report.md").read_text(encoding="utf-8")
        self.assertIn("Summaries, Before And After", report)
        self.assertIn("An older summary.", report)

    def test_rerunning_is_last_run_wins(self):
        note = self.filed()
        with StubServer() as server:
            self.reprocess(server.url, "--apply")
        first = note.read_text(encoding="utf-8")
        with StubServer() as server:
            result = self.reprocess(server.url, "--apply")
        self.assertEqual(result["data"]["counts"]["reprocessed"], 1)
        self.assertEqual(note.read_text(encoding="utf-8"), first)

    def test_a_note_without_a_marker_is_not_selected(self):
        path = self.vault / "01 Personal" / "Plain.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\ntype: note\nstatus: active\ndomain: personal\n---\n\nJust prose.\n", encoding="utf-8")
        with StubServer() as server:
            result = self.reprocess(server.url, "--apply")
            self.assertEqual(server.requests, [])
        self.assertEqual(result["data"]["counts"]["selected"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

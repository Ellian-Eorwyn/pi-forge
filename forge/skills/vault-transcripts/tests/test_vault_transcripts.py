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
from types import SimpleNamespace

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "vault-transcripts.py"
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("vault_transcripts", SCRIPT)
vault_transcripts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vault_transcripts)
vt = vault_transcripts

_shim_spec = importlib.util.spec_from_file_location(
    "obsidian_shim", Path(__file__).resolve().parents[3] / "lib" / "tests" / "obsidian_shim.py"
)
_obsidian_shim = importlib.util.module_from_spec(_shim_spec)
_shim_spec.loader.exec_module(_obsidian_shim)
ShimEnvironment = _obsidian_shim.ShimEnvironment


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
SOLO_TEXTS = [
    "Okay so I need to remember to order the replacement gasket for the espresso machine.",
    "The old one is cracked around the rim and it leaks whenever I pull a double shot.",
    "Also the grinder needs descaling, probably this weekend if I have time.",
    "And I should measure the counter before buying anything else for that corner.",
    "The other thing is the shelving unit in the pantry, which never got anchored properly.",
    "I keep meaning to buy the right brackets but I always forget the stud spacing.",
    "Probably worth photographing the wall before the hardware store trip this time.",
    "Then there is the question of whether we replace the kettle or just descale it too.",
    "Gillian thinks the kettle is fine, and honestly she is probably right about that.",
    "Last thing, I should check whether the warranty on the machine covers the gasket.",
    "If it does then none of this costs anything except the trip and the afternoon.",
]
SOLO_SECONDS = [0, 6, 14, 22, 30, 38, 46, 54, 62, 70, 78]
SOLO_BLOCKS = [block(text, seconds) for text, seconds in zip(SOLO_TEXTS, SOLO_SECONDS)]
TINY_BLOCKS = [block("Remember to order the espresso machine gasket before the weekend.", 0)]
DIALOGUE_BLOCKS = [
    block("How did the deployment window go last night?", 0, "Speaker 1"),
    block("It went fine, we finished the migration around midnight.", 5, "Speaker 2"),
    block("Nobody had to roll anything back, which surprised me honestly.", 11, "Speaker 2"),
    block("Good. Did the reporting dashboards come back up cleanly?", 18, "Speaker 1"),
    block("They did, although the nightly aggregation ran twice and duplicated some counters.", 24, "Speaker 2"),
]
# One person the transcriber split in two: the same memo, with the trailing
# sign-off landing under a new label. Measured across the corpus this lands at a
# 3.9%-14.3% minority share, which is exactly where real conversations sit too --
# the shape is not what distinguishes it, so nothing may key off the ratio.
SPLIT_LABEL_BLOCKS = [
    block("Okay so I need to remember to order the replacement gasket for the espresso machine.", 0, "Speaker 1"),
    block("The old one is cracked around the rim and it leaks whenever I pull a double shot.", 6, "Speaker 1"),
    block("Also the grinder needs descaling, probably this weekend if I have time.", 14, "Speaker 1"),
    block("And I should measure the counter before buying anything else for that corner.", 22, "Speaker 1"),
    block("The other thing is the shelving unit in the pantry, which never got anchored properly.", 30, "Speaker 1"),
    block("I keep meaning to buy the right brackets but I always forget the stud spacing.", 38, "Speaker 1"),
    block("Probably worth photographing the wall before the hardware store trip this time.", 46, "Speaker 1"),
    block("Then there is the question of whether we replace the kettle or just descale it too.", 54, "Speaker 1"),
    block("Gillian thinks the kettle is fine, and honestly she is probably right about that.", 62, "Speaker 1"),
    block("Last thing, I should check whether the warranty on the machine covers the gasket.", 70, "Speaker 1"),
    block("If it does then none of this costs anything except the trip and the afternoon.", 78, "Speaker 1"),
    block("Um yeah. That's pretty much it.", 86, "Speaker 2"),
]

# A roster whose `always` cue promises a second voice on every personal
# recording. The template offers this cue shape as a good one, so the pipeline
# has to stay right in spite of it rather than ask for it to be rewritten.
ROSTER_NOTE = """---
type: system
status: active
domain: meta
subdomain: schemas
capture_type: manual
---

# Speakers and Terms

## Speakers

| Person | Appears | Aliases | Cue |
| --- | --- | --- | --- |
| `[[Gillian Eorwyn]]` | `always` | `Gillian` | my spouse; the second voice in home recordings and personal voice notes |
"""


def classified(recording_type, effective, speakers=None, title="Espresso Machine Repairs"):
    """A classify response in the shape validate_classification accepts."""
    return {
        "recording_type": recording_type,
        "material_role": "owner-authored",
        "title": title,
        "speakers": speakers or {},
        "effective_speakers": effective,
        "spoken_date": None,
        "evidence": None,
        "needs_review": False,
        "review_reason": None,
    }


# What the roster talks the model into: the sign-off becomes the spouse.
SPLIT_BY_ROSTER = classified(
    "memo",
    2,
    {
        "Speaker 1": {"who": "unknown", "kind": "unknown", "confidence": "low", "source": "transcript"},
        "Speaker 2": {
            "who": "Gillian Eorwyn",
            "kind": "name",
            "confidence": "medium",
            "source": "roster",
            "evidence": "Um yeah. That's pretty much it.",
        },
    },
)


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
        # The fidelity meaning-judge and the note-level review both end in the
        # verdict contract; split them by their opening so a test can script the
        # provisional fidelity verdict without touching the note-level one.
        if system.startswith("You are checking whether a cleaned-up transcript"):
            return "verify-fidelity"
        if '{"verdicts"' in system or "verdicts" in system.split("\n\n")[-1]:
            return "verify"
        if system.startswith("You process one voice recording"):
            return "write"
        if system.startswith("You review one processed voice-recording note"):
            return "review"
        if system.startswith("You read one voice recording"):
            return "classify"
        # The repair prompt is a transcript editor too, so it must be told apart
        # from ordinary cleanup before the generic editor match.
        if system.startswith("You are a meticulous transcript editor fixing"):
            return "repair"
        if system.startswith("You are a meticulous transcript editor"):
            return "clean"
        if system.startswith("You write the one-paragraph summary"):
            return "summarize"
        # Both reflection prompts open this way, and nothing else does.
        if system.startswith("You add a"):
            return "reflect"
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
        if stage == "repair":
            # Default repair changes nothing; a test scripts the actual restore.
            return {"cleaned": user.get("currentCleaned", "")}
        if stage == "summarize":
            return {"summary": "The speaker works through a short list of practical repairs and next steps."}
        if stage in ("verify", "verify-fidelity"):
            return {"verdicts": [{"id": item["id"], "verdict": "ok"} for item in user["items"]]}
        if stage == "review":
            return {"verdict": "ok"}
        if stage == "write":
            return self.compose_write(payload)
        return {}

    def compose_write(self, payload):
        """A writer response assembled from legacy per-stage scripts.

        The one-pass writer answers classification, cleaned body, and summary in
        one response; composing it from the old "classify"/"clean"/"summarize"
        script queues lets a test keep steering exactly the knob it always did.
        A scripted non-dict part (a malformed-response test) becomes the whole
        response, so the writer's corrective re-ask sees what the stage's did."""
        if len(payload["messages"]) < 2:
            return "ready"  # the doctor probe
        scripted = self.__class__.scripted
        user = json.loads(payload["messages"][1]["content"])
        merged = dict(self.default_for("classify", payload))
        part = scripted.get("classify")
        if part:
            value = part.pop(0)
            if not isinstance(value, dict):
                return value
            merged.update(value)
        part = scripted.get("clean")
        if part:
            value = part.pop(0)
            if not isinstance(value, dict):
                return value
            merged["cleaned"] = value.get("cleaned")
        else:
            merged["cleaned"] = user.get("transcript", "")
        part = scripted.get("summarize")
        if part:
            value = part.pop(0)
            if not isinstance(value, dict):
                return value
            merged["summary"] = value.get("summary")
        elif user.get("tiny"):
            merged["summary"] = None
        else:
            merged["summary"] = self.default_for("summarize", payload).get("summary")
        return merged

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        self.__class__.requests.append(payload)
        stage = self.stage_of(payload)
        queue = self.__class__.scripted.get(stage)
        if queue:
            response = queue.pop(0)
        elif stage == "write" and any(self.__class__.scripted.get(name) for name in ("classify", "clean", "summarize")):
            response = self.compose_write(payload)
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
            if stage == "verify-fidelity" and system.startswith("You are checking whether a cleaned-up transcript"):
                counted.append(payload)
            elif stage == "verify" and "verdicts" in system and not system.startswith(
                "You are checking whether a cleaned-up transcript"
            ):
                counted.append(payload)
            elif stage == "write" and system.startswith("You process one voice recording"):
                counted.append(payload)
            elif stage == "classify" and system.startswith("You read one voice recording"):
                counted.append(payload)
            elif stage == "clean" and system.startswith("You are a meticulous transcript editor"):
                counted.append(payload)
            elif stage == "summarize" and system.startswith("You write the one-paragraph summary"):
                counted.append(payload)
            elif stage == "reflect" and system.startswith("You add a"):
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
            {"date": "2026-07-24", "time_hhmm": "1317", "time_hhmmss": "13:17:48", "recording_id": "9788991C"},
        )
        self.assertEqual(
            vt.parse_filename("20260724 110913-A2F4CD8A 2026-07-24 11_11_29.md"),
            {"date": "2026-07-24", "time_hhmm": "1109", "time_hhmmss": "11:09:13", "recording_id": "A2F4CD8A"},
        )
        self.assertEqual(
            vt.parse_filename("20260616 092230.md"),
            {"date": "2026-06-16", "time_hhmm": "0922", "time_hhmmss": "09:22:30", "recording_id": None},
        )
        # The dashed-date, dotted-time export shape (the common one on this inbox):
        # its recording date must be read, not fall back to today.
        self.assertEqual(
            vt.parse_filename("2025-08-08 13.07.21 Ellian.md"),
            {"date": "2025-08-08", "time_hhmm": "1307", "time_hhmmss": "13:07:21", "recording_id": None},
        )
        # A processed date-type-topic note is not an export stamp: the ` - ` after
        # the date is not a time, so it must not be read as a recording date.
        self.assertIsNone(vt.parse_filename("2026-08-11 - Meeting - Waste Heat Recovery.md")["date"])
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

    def test_impossible_mmss_rate_is_reinterpreted_as_hhmm(self):
        # A 57-minute recording an app wrote as *00:57* reads as 57 seconds under
        # MM:SS — 6000 words against it is impossible speech, so it is coarsened to
        # minutes and the corrupt-input gate no longer holds it.
        body = transcript([block("Opening remarks.", 0), block(" ".join(["word"] * 6000), 57)])
        parsed = vt.parse_transcript(body)
        self.assertEqual(parsed["blocks"][-1]["seconds"], 57 * 60)
        stats = vt.transcript_stats(parsed)
        self.assertEqual(stats["timestamp_style"], "HH:MM")
        self.assertEqual(stats["duration_seconds"], 57 * 60)
        self.assertIsNone(vt.unusable_input_reason(stats))

    def test_reinterpretation_preserves_byte_exact_raw(self):
        body = transcript([block("Opening remarks.", 0), block(" ".join(["word"] * 6000), 57)])
        self.assertEqual(vt.serialize_parsed(vt.parse_transcript(body)), body)

    def test_both_readings_impossible_is_not_reinterpreted_and_still_held(self):
        body = transcript([block("start", 0), block(" ".join(["word"] * 30000), 63)])
        parsed = vt.parse_transcript(body)
        self.assertEqual(parsed["blocks"][-1]["seconds"], 63)
        stats = vt.transcript_stats(parsed)
        self.assertEqual(stats["timestamp_style"], "MM:SS")
        self.assertIsNotNone(vt.unusable_input_reason(stats))

    def test_fast_short_memo_below_floor_is_not_reinterpreted(self):
        # 80 words at *00:10* is over the MM:SS ceiling, but the minutes reading
        # (0.13 wps) is below the floor, so it is left alone rather than rescaled
        # into a plausible-looking lie.
        body = transcript([block("hi", 0), block(" ".join(["word"] * 80), 10)])
        parsed = vt.parse_transcript(body)
        self.assertEqual(parsed["blocks"][-1]["seconds"], 10)
        self.assertEqual(vt.transcript_stats(parsed)["timestamp_style"], "MM:SS")

    def test_three_part_timestamps_are_never_reinterpreted(self):
        # Even with an impossible two-part rate in the same file, any 3-part stamp
        # blocks the minutes rescale — a blanket *60 would corrupt a real HH:MM:SS.
        body = "*00:05*\n" + " ".join(["word"] * 6000) + "\n\n*0:10:30*\nlate\n\n"
        parsed = vt.parse_transcript(body)
        self.assertEqual([entry["seconds"] for entry in parsed["blocks"]], [5, 630])
        self.assertNotEqual(vt.transcript_stats(parsed)["timestamp_style"], "HH:MM")

    def test_unordered_timestamps_are_not_reinterpreted(self):
        body = transcript([block(" ".join(["word"] * 6000), 57), block("earlier", 5)])
        parsed = vt.parse_transcript(body)
        self.assertEqual([entry["seconds"] for entry in parsed["blocks"]], [57, 5])

    def _unlabeled_body(self, turns=8):
        speakers = ["Ellian", "Sopagna"]
        utterance = " ".join(["word"] * 30)
        return "".join(f"{speakers[index % 2]}\n{utterance}\n\n" for index in range(turns))

    def test_untimestamped_speaker_transcript_is_detected(self):
        split = {"malformed": False, "had_frontmatter": False}
        self.assertTrue(vt.looks_like_untimestamped_transcript(split, self._unlabeled_body()))

    def test_untimestamped_transcript_is_skipped_by_default(self):
        parsed = vt.parse_transcript(self._unlabeled_body())  # no allow_unlabeled
        self.assertEqual(parsed["blocks"], [])
        transcript_flag, reason = vt.is_transcript({"malformed": False, "had_frontmatter": False}, parsed)
        self.assertFalse(transcript_flag)
        self.assertEqual(reason, "no timestamped transcript blocks")

    def test_unlabeled_parse_is_byte_exact_and_has_no_seconds(self):
        body = self._unlabeled_body()
        parsed = vt.parse_transcript(body, allow_unlabeled=True)
        self.assertEqual(len(parsed["blocks"]), 8)
        self.assertEqual([block["speaker"] for block in parsed["blocks"][:2]], ["Ellian", "Sopagna"])
        self.assertTrue(all(block["seconds"] is None for block in parsed["blocks"]))
        self.assertEqual(vt.serialize_parsed(parsed), body)

    def test_unlabeled_stats_have_no_duration_and_pass_the_rate_gate(self):
        stats = vt.transcript_stats(vt.parse_transcript(self._unlabeled_body(), allow_unlabeled=True))
        self.assertEqual(stats["timestamp_style"], "unlabeled")
        self.assertEqual(stats["duration_seconds"], 0)
        self.assertIsNone(vt.unusable_input_reason(stats))

    def test_prose_note_is_not_detected_as_a_transcript(self):
        split = {"malformed": False, "had_frontmatter": False}
        prose = "# Heading\n\n" + " ".join(["The meeting went well and we made real progress today."] * 40)
        self.assertFalse(vt.looks_like_untimestamped_transcript(split, prose))
        # A short speaker exchange is under the word floor and also left alone.
        self.assertFalse(vt.looks_like_untimestamped_transcript(split, "Ann\nHi.\n\nBob\nHello.\n"))

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

    def test_a_meeting_with_a_project_drops_the_generic_label(self):
        # The project codename is the better label, and the title already leads
        # with it, so the "Meeting" word would only be redundant.
        self.assertEqual(
            vt.format_filename(
                "date-type-topic", "2026-08-20", "1430", "meeting", "FORGE - Q3 Timeline", project="FORGE"
            ),
            "2026-08-20 - FORGE - Q3 Timeline.md",
        )
        # No project identified: the note keeps the shape it has always had.
        self.assertEqual(
            vt.format_filename("date-type-topic", "2026-08-20", "1430", "meeting", "Deployment Window Review"),
            "2026-08-20 - Meeting - Deployment Window Review.md",
        )
        # The project label-drop is a meeting-only affordance.
        self.assertEqual(
            vt.format_filename("date-type-topic", "2026-08-20", None, "memo", "Gasket Order", project="FORGE"),
            "2026-08-20 - Memo - Gasket Order.md",
        )

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


def meeting_value(title="FORGE - Q3 Timeline", project="FORGE", **extra):
    """A meeting classify response that validation accepts without holding it."""
    return {
        "recording_type": "meeting",
        "material_role": "personal-exchange",
        "title": title,
        "project": project,
        "speakers": {},
        "effective_speakers": 2,
        "spoken_date": None,
        "evidence": None,
        "needs_review": False,
        "review_reason": None,
        **extra,
    }


class MeetingBriefTests(unittest.TestCase):
    def test_topics_are_the_headings_minus_the_fixed_sections(self):
        cleaned = (
            "## Staging Cutover\n\nprose\n\n## QA Sign-off\n\nprose\n\n"
            "## Decisions\n\n- ship\n\n## Action Items\n\n- Marcus: rollback plan"
        )
        self.assertEqual(vt.meeting_topics(cleaned), ["Staging Cutover", "QA Sign-off"])

    def test_attendees_drop_the_unnamed_voices(self):
        speaker_map = {"Speaker 1": "Priya Shah", "Speaker 2": "Speaker 2", "Speaker 3": "Marcus Feld"}
        self.assertEqual(vt.meeting_attendees(speaker_map), ["Priya Shah", "Marcus Feld"])
        self.assertEqual(vt.meeting_attendees({"A": None}), [])

    def test_brief_is_block_first_and_omits_the_lines_it_cannot_fill(self):
        cleaned = "## Staging Cutover\n\nprose\n\n## Decisions\n\n- ship"
        block = vt.meeting_brief({"project": "FORGE"}, "2026-08-20", "1430", cleaned, {"Speaker 1": "Priya Shah"})
        self.assertEqual(
            block,
            "> [!info] Meeting\n"
            "> **Project:** FORGE\n"
            "> **Date:** 2026-08-20\n"
            "> **Time:** 14:30\n"
            "> **Attendees:** Priya Shah\n"
            "> **Topics:** Staging Cutover",
        )

    def test_an_empty_brief_is_no_brief(self):
        # No project, no date, no time, no named attendees, no topic headings.
        self.assertIsNone(vt.meeting_brief({}, None, None, "just prose with no headings", {"A": None}))

    def test_the_brief_leads_the_assembled_head_above_the_summary(self):
        block = "> [!info] Meeting\n> **Project:** FORGE"
        head = vt.assemble_head("A summary.", "callout", "", "## Topic\n\nbody", brief=block)
        self.assertTrue(head.startswith(block))
        self.assertLess(head.index("[!info]"), head.index("[!summary]"))


class ClassificationProjectTests(unittest.TestCase):
    def _validate(self, value):
        parsed = vt.parse_transcript(transcript(DIALOGUE_BLOCKS))
        return vt.validate_classification(value, {"path": "00 Inbox/x.md"}, parsed)[0]

    def test_a_meeting_keeps_its_project(self):
        self.assertEqual(self._validate(meeting_value())["project"], "FORGE")

    def test_a_non_meeting_drops_the_project(self):
        record = self._validate(classified("memo", 1, title="Gasket Order") | {"project": "FORGE"})
        self.assertIsNone(record["project"])

    def test_a_reviewed_meeting_carries_no_project(self):
        value = meeting_value(needs_review=True, review_reason="too garbled to title")
        self.assertIsNone(self._validate(value)["project"])

    def test_a_nonsense_project_is_dropped_not_raised(self):
        self.assertIsNone(self._validate(meeting_value(project=42))["project"])
        self.assertIsNone(self._validate(meeting_value(project="   "))["project"])


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

    def test_a_transcript_is_dated_by_its_recording_not_by_today(self):
        import datetime
        import vault_schema

        with_date = vault_schema.parse_schema_note(
            SCHEMA.replace(
                "| `capture_type` | no | controlled scalar | Capture type. |",
                "| `capture_type` | no | controlled scalar | Capture type. |\n"
                "| `date` | no | scalar, human-owned | What the note is about. |",
            )
        )
        # A recording processed a week late is still about the day it was made.
        self.assertEqual(vt.frontmatter_metadata(with_date, "journal", "2026-07-30")["date"], "2026-07-30")
        self.assertEqual(vt.raw_metadata(with_date, "journal", "Some Note", "2026-07-30")["date"], "2026-07-30")
        # Only an undated recording falls back to today.
        today = datetime.date.today().isoformat()
        self.assertEqual(vt.frontmatter_metadata(with_date, "journal")["date"], today)

    def test_a_schema_without_date_gets_no_date(self):
        for recording_type in vt.RECORDING_TYPES:
            self.assertNotIn("date", vt.frontmatter_metadata(self.schema, recording_type))
            self.assertNotIn("date", vt.raw_metadata(self.schema, recording_type, "Some Note"))

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
        # `check_note` now splits its problems into structural vs fidelity; these
        # tests predate that and care only about "did any check fail", so the
        # helper recombines them into the flat list they were written against.
        structural, fidelity, measurements = vt.check_note(item, cleaned, summary, note, head, parsed, self.args)
        return structural + fidelity, measurements

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

    def test_a_meeting_summary_skips_the_verbatim_gate(self):
        # A minutes-style cleanup at a third the length with distinctive words
        # dropped: held as verbatim cleanup, but correct as meeting minutes. Only
        # the verbatim checks (ratio, retention, utterance-locator) are dropped;
        # the structural checks still run.
        body = transcript(SOLO_BLOCKS * 4)
        minutes = "## Repairs\n\nThe team discussed the espresso machine and agreed to order a gasket."
        held, _ = self.check(body, minutes)
        self.assertTrue(any("outside" in problem or "survived" in problem for problem in held), held)
        parsed = vt.parse_transcript(body)
        item = {"path": "00 Inbox/x.md", "raw_body": body, "stats": vt.transcript_stats(parsed)}
        note, head = vt.build_note(
            self.schema, vt.frontmatter_metadata(self.schema, "meeting"),
            "A summary.", "callout", parsed["preamble"], minutes, body,
        )
        structural, fidelity, _ = vt.check_note(item, minutes, "A summary.", note, head, parsed, self.args, summarized=True)
        self.assertEqual(structural + fidelity, [])

    def test_a_meeting_summary_still_fails_a_structural_check(self):
        # Summarize-mode drops the verbatim checks, not the structural ones: a
        # broken transcript-preservation is still caught.
        body = transcript(SOLO_BLOCKS)
        parsed = vt.parse_transcript(body)
        item = {"path": "00 Inbox/x.md", "raw_body": body, "stats": vt.transcript_stats(parsed)}
        note, head = vt.build_note(
            self.schema, vt.frontmatter_metadata(self.schema, "meeting"),
            "A summary.", "callout", parsed["preamble"], "Some minutes.", body,
        )
        tampered = note[:-5] + "xxxxx"  # corrupt the preserved transcript tail
        structural, fidelity, _ = vt.check_note(item, "Some minutes.", "A summary.", tampered, head, parsed, self.args, summarized=True)
        # A broken transcript is a structural fault, not a fidelity proxy: it holds
        # outright and is never deferred to the meaning-judge.
        self.assertTrue(any("byte-identical" in problem for problem in structural), structural)
        self.assertEqual(fidelity, [])

    def test_a_lost_preamble_is_caught(self):
        body = transcript(SOLO_BLOCKS, preamble="Notes to self\n\n1. buy gasket\n\n")
        parsed = vt.parse_transcript(body)
        cleaned = " ".join(entry["text"] for entry in parsed["blocks"])
        item = {"path": "00 Inbox/x.md", "raw_body": body, "stats": vt.transcript_stats(parsed)}
        note, head = vt.build_note(
            self.schema, vt.frontmatter_metadata(self.schema, "memo"), "A summary.", "callout", "", cleaned, body
        )
        structural, fidelity, _measurements = vt.check_note(item, cleaned, "A summary.", note, head, parsed, self.args)
        # A lost preamble is structural — the meaning-judge has no say over it.
        self.assertIn("handwritten preamble did not survive into the generated section", structural)
        self.assertNotIn(
            "handwritten preamble did not survive into the generated section", fidelity
        )

    def test_check_note_splits_structural_from_fidelity(self):
        # The split is what lets `assemble_items` hold on structure but defer the
        # fidelity floors to the meaning-judge. A note that trips both at once —
        # a stray heading and a gutted length — proves each lands in its own bucket
        # so the structural fault holds the note whatever the judge later says.
        body = transcript(SOLO_BLOCKS * 4)
        parsed = vt.parse_transcript(body)
        item = {"path": "00 Inbox/x.md", "raw_body": body, "stats": vt.transcript_stats(parsed)}
        gutted = " ".join(entry["text"] for entry in parsed["blocks"][:2])
        note, head = vt.build_note(
            self.schema, vt.frontmatter_metadata(self.schema, "memo"), "A summary.", "callout", "", gutted, body
        )
        structural, fidelity, _ = vt.check_note(
            item, gutted, "A summary.", note, "# Stray Heading\n\n" + head, parsed, self.args
        )
        self.assertTrue(any("level-one heading" in problem for problem in structural), structural)
        self.assertFalse(any("level-one heading" in problem for problem in fidelity), fidelity)
        self.assertTrue(any("outside" in problem for problem in fidelity), fidelity)
        self.assertFalse(any("outside" in problem for problem in structural), structural)


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

    def test_a_memo_with_two_speakers_is_held_like_any_other_inconsistency(self):
        # The corpus shape: the type and the role are right and agree with each
        # other, and only the count dissents. It is still held, and the roster-free
        # second look is what has to rescue it -- never a softening of this check.
        parsed = vt.parse_transcript(transcript(SPLIT_LABEL_BLOCKS))
        record, _warnings = vt.validate_classification(SPLIT_BY_ROSTER, {"path": "00 Inbox/x.md"}, parsed)
        self.assertTrue(record["needs_review"])
        self.assertIn("inconsistent", record["review_reason"])

    def test_only_an_owner_authored_memo_or_journal_gets_a_second_look(self):
        cases = [
            (("memo", "owner-authored", 2), True, "the shape the roster leaves behind"),
            (("journal", "owner-authored", 3), True, "same, with more labels"),
            (("memo", "owner-authored", 1), False, "already consistent, nothing to re-ask"),
            (("conversation", "personal-exchange", 2), False, "two voices are the point"),
            (("lecture", "external-source", 2), False, "not owner-authored"),
            (("conversation", "owner-authored", 2), False, "inconsistent on the type, not the count"),
        ]
        for (recording_type, material_role, effective), expected, why in cases:
            classification = {
                "recording_type": recording_type,
                "material_role": material_role,
                "effective_speakers": effective,
            }
            self.assertEqual(vt.roster_may_have_split_one_voice(classification), expected, why)

    def test_the_payload_carries_the_roster_only_when_one_is_offered(self):
        # The second look withholds the roster by passing no lexicon, so the
        # difference between those two payloads is the whole mechanism.
        parsed = vt.parse_transcript(transcript(SPLIT_LABEL_BLOCKS))
        item = {"path": "00 Inbox/x.md", "filename_hint": None,
                "stats": {"blocks": 12, "words": 160, "duration_seconds": 92}}
        roster = {"terms": [], "speakers": [
            {"name": "Gillian Eorwyn", "link": "[[Gillian Eorwyn]]", "appears": "always",
             "aliases": ["Gillian"], "cue": "the second voice in personal voice notes", "role": ""}
        ]}
        self.assertIn("knownSpeakers", vt.classify_payload(item, parsed, roster))
        self.assertNotIn("knownSpeakers", vt.classify_payload(item, parsed))

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

    def roster(self):
        (self.vault / "99 Meta" / "99.02 Schemas" / "0.02 Speakers and Terms.md").write_text(
            ROSTER_NOTE, encoding="utf-8"
        )

    def process(self, url, *extra):
        return run_script("process", "--vault", str(self.vault), "--base-url", url, "--model", "chat", *extra)

    def result_of(self, completed):
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return json.loads(completed.stdout)

    def inbox(self):
        # The inbox-review control note is a tool artifact, not a recording note,
        # so it is excluded here the same way the scan excludes it.
        return sorted(
            path.name
            for path in (self.vault / "00 Inbox").glob("*.md")
            if path.name != vt.vault_review.REVIEW_NOTE_NAME
        )

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
        self.assertEqual(renames[0]["linkRewrite"], "rename")

    def test_a_rename_takes_inbound_links_with_it_when_obsidian_can(self):
        """The rename this skill performs is the one that actually breaks links.

        A transcript arrives named for when it was recorded and leaves named for
        what it says, so the basename changes — and a changed basename is exactly
        what Obsidian's basename resolution cannot paper over. Without the CLI
        the old name is simply left behind in whatever pointed at it.
        """
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        pointer = self.vault / "00 Inbox" / "Pointer.md"
        pointer.write_text(
            "---\ntype: note\n---\n\nRecorded in [[20260724 131748-9788991C]].\n", encoding="utf-8"
        )
        env = ShimEnvironment(vault_path=self.vault, vault_name="vault")
        self.addCleanup(env.cleanup)

        with StubServer() as server:
            result = self.result_of(self.process(server.url, "--apply"))

        self.assertIn(
            "[[2026-07-24 - Memo - Espresso Machine Repairs]]",
            pointer.read_text(encoding="utf-8"),
            "the note pointing at the transcript followed the rename",
        )
        run_dir = Path(result["data"]["run_directory"])
        renames = [json.loads(line) for line in (run_dir / "renames.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(renames[0]["linkRewrite"], "obsidian-cli")
        self.assertEqual(renames[0]["linksRewrittenIn"], ["00 Inbox/Pointer.md"])
        backup = run_dir / "backup" / "00 Inbox" / "Pointer.md"
        self.assertIn(
            "[[20260724 131748-9788991C]]", backup.read_text(encoding="utf-8"), "backed up before the rewrite"
        )

    def test_without_obsidian_the_old_name_is_left_behind(self):
        # The behaviour this skill has always had. Stated as a test so that a
        # change to it is a decision rather than an accident.
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        pointer = self.vault / "00 Inbox" / "Pointer.md"
        pointer.write_text(
            "---\ntype: note\n---\n\nRecorded in [[20260724 131748-9788991C]].\n", encoding="utf-8"
        )
        with StubServer() as server:
            self.result_of(self.process(server.url, "--apply"))
        self.assertIn("[[20260724 131748-9788991C]]", pointer.read_text(encoding="utf-8"))

    def test_link_rewrite_require_fails_when_links_would_not_follow(self):
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        env = ShimEnvironment(vault_path=self.vault, vault_name="vault", link_updates="unset")
        self.addCleanup(env.cleanup)
        with StubServer() as server:
            completed = self.process(server.url, "--apply", "--link-rewrite", "require")
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("link-safe moves are unavailable", json.loads(completed.stdout)["errors"][0]["message"])

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
            self.assertEqual(len(server.stage_requests("write")), 1)
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
            self.assertTrue(json.loads(server.stage_requests("write")[0]["messages"][1]["content"])["tiny"])
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

    def test_a_voice_the_roster_split_in_two_is_re_read_without_it(self):
        # Measured on the corpus: an `always` roster entry promising "the second
        # voice in personal voice notes" turned every one of these into a held
        # note. The same recordings answered one speaker when no roster was
        # offered, which is what the second call reproduces.
        self.roster()
        self.write("20260724 131748-9788991C.md", transcript(SPLIT_LABEL_BLOCKS))
        with StubServer(scripted={"classify": [SPLIT_BY_ROSTER, classified("memo", 1)]}) as server:
            result = self.result_of(self.process(server.url, "--apply"))
            written = server.stage_requests("write")
            asked = server.stage_requests("classify")
        self.assertEqual(result["data"]["counts"]["review_required"], 0)
        self.assertEqual(result["data"]["counts"]["processed"], 1)
        self.assertProcessed("2026-07-24 - Memo - Espresso Machine Repairs.md")
        # The roster is present in the writer call and withheld in the re-read:
        # that difference is the entire mechanism.
        self.assertEqual(len(written), 1)
        self.assertEqual(len(asked), 1)
        self.assertIn("knownSpeakers", json.loads(written[0]["messages"][1]["content"]))
        self.assertNotIn("knownSpeakers", json.loads(asked[0]["messages"][1]["content"]))

    def test_a_second_voice_found_without_the_roster_keeps_the_note_held(self):
        # Nothing prompted this one, so the disagreement is real and the safety
        # net stands. The reason string is what the review queue is grouped by.
        self.roster()
        self.write("20260724 131748-9788991C.md", transcript(SPLIT_LABEL_BLOCKS))
        with StubServer(scripted={"classify": [SPLIT_BY_ROSTER, classified("memo", 2)]}) as server:
            result = self.result_of(self.process(server.url, "--apply"))
            asked = server.stage_requests("classify")
        self.assertEqual(self.inbox(), ["20260724 131748-9788991C.md"])
        self.assertEqual(result["data"]["counts"]["review_required"], 1)
        self.assertEqual(len(asked), 1)
        queue = [
            json.loads(line)
            for line in (run_dir_of(result) / "review-queue.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            queue[0]["reason"],
            "owner-authored classification is inconsistent with a single-speaker memo or journal",
        )

    def test_with_no_roster_offered_there_is_no_second_look(self):
        # Asking the identical question again at temperature 0 buys the same
        # answer, so the note is held on the first one.
        self.write("20260724 131748-9788991C.md", transcript(SPLIT_LABEL_BLOCKS))
        with StubServer(scripted={"classify": [classified("memo", 2)]}) as server:
            result = self.result_of(self.process(server.url, "--no-lexicon", "--apply"))
            self.assertEqual(len(server.stage_requests("write")), 1)
            self.assertEqual(server.stage_requests("classify"), [])
        self.assertEqual(result["data"]["counts"]["review_required"], 1)
        self.assertEqual(self.inbox(), ["20260724 131748-9788991C.md"])

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
            self.assertEqual(len(server.stage_requests("write")), 2)
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
            self.assertEqual(len(server.stage_requests("write")), 2)
            repair = server.stage_requests("write")[1]["messages"][-1]["content"]
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
            payload = json.loads(server.stage_requests("write")[0]["messages"][1]["content"])
        # The writer names speakers in its own response; the payload carries the
        # raw labels, and the derived map is reconciled into the body afterwards.
        self.assertEqual(payload["labels"], ["Speaker 1", "Speaker 2"])
        note = (self.vault / "00 Inbox" / "2026-07-24 - Conversation - Deployment Window Review.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("**Ellie**", note)
        self.assertIn("**Gillian**", note)
        self.assertNotIn("**Speaker 1**", note.split("# Transcript")[0])

    def test_long_transcripts_are_chunked_with_context(self):
        # Enough repeats to clear WRITE_BUDGET_CHARS, so the writer takes its
        # oversize path: per-chunk calls with continuity, classification from the
        # first chunk, summary from the last.
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS * 130))
        with StubServer() as server:
            self.result_of(self.process(server.url))
            payloads = [json.loads(entry["messages"][1]["content"]) for entry in server.stage_requests("write")]
        self.assertGreater(len(payloads), 1)
        self.assertEqual(payloads[0]["chunkIndex"], 1)
        self.assertEqual(payloads[1]["chunkCount"], len(payloads))
        self.assertIn("previousTail", payloads[1])

    def test_status_and_doctor(self):
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        with StubServer() as server:
            first = self.result_of(self.process(server.url))
            status = self.result_of(run_script("status", "--run", str(run_dir_of(first))))
            self.assertEqual(status["data"]["phase"], "planned")
            self.assertEqual(status["data"]["transcripts"], 1)
            self.assertEqual(status["data"]["written"], 1)
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

    def reflect(self, server, record=None):
        args = SimpleNamespace(
            cache_prompt=True, request_timeout=60, api_key=None, compiled_voice=None, compiled_profile=None
        )
        service = {
            "name": "chat",
            "enabled": True,
            "url": server.url,
            "model": "chat",
            "scheduling": vt.forge_llm.DEFAULT_SERVICES["chat"]["scheduling"],
        }
        record = record or {
            "recording_type": "memo",
            "title": "Espresso Machine Repairs",
            "material_role": "owner-authored",
        }
        return vt.reflect_note(args, service, record, "Cleaned memo text.", [])

    def test_a_malformed_section_is_asked_for_again(self):
        # A section arriving as a bare string held the whole note, costing it its
        # summary as well as its reflection. One more ask, as every other
        # validated call in this pipeline gets.
        bad = {"context": "Continues the repair thread.", "connections": []}
        good = {"context": ["Continues the repair thread."], "connections": []}
        with StubServer(scripted={"reflect": [bad, good]}) as server:
            markdown, dropped = self.reflect(server)
            asked = server.stage_requests("reflect")
        self.assertEqual(len(asked), 2)
        self.assertIn("must be an array of strings", asked[1]["messages"][-1]["content"])
        self.assertIn("Return corrected JSON only", asked[1]["messages"][-1]["content"])
        self.assertIn("> [!reflection]- Context\n> - Continues the repair thread.", markdown)
        self.assertEqual(dropped, [])

    def test_a_reflection_that_stays_malformed_is_still_fatal(self):
        bad = {"context": "Continues the repair thread.", "connections": []}
        with StubServer(scripted={"reflect": [bad, bad]}) as server:
            with self.assertRaises(vt.UserError):
                self.reflect(server)
            self.assertEqual(len(server.stage_requests("reflect")), 2)


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

    def test_a_voice_the_roster_split_in_two_is_reprocessed_not_held(self):
        # The path the corpus was measured on: filed notes, a roster in the
        # vault, and 19% of the owner-authored ones held for a speaker count of
        # two. The name already says Memo and the classifier already agrees --
        # only the count dissented, and the second look settles it.
        (self.vault / "99 Meta" / "99.02 Schemas" / "0.02 Speakers and Terms.md").write_text(
            ROSTER_NOTE, encoding="utf-8"
        )
        self.filed(tail=transcript(SPLIT_LABEL_BLOCKS))
        with StubServer(scripted={"classify": [SPLIT_BY_ROSTER, classified("memo", 1)]}) as server:
            result = self.reprocess(server.url, "--apply")
            asked = server.stage_requests("classify")
        self.assertEqual(result["data"]["counts"]["reprocessed"], 1)
        self.assertEqual(len(asked), 1, "the writer classifies; only the roster re-read is a classify call")

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


class DailyLogTests(unittest.TestCase):
    """Fan-in: several short recordings from one day becoming one note.

    Verified against a real hand-built log — `2026-08-03 — Thoughts & To-Dos` —
    whose six recordings, section markers, and ordering are what these assert.
    """

    def members(self, *stamps):
        items = []
        for stamp in stamps:
            date, time_hhmmss = stamp.split(" ")
            compact = time_hhmmss.replace(":", "")
            name = f"{date.replace('-', '')} {compact}-DEADBEEF.md"
            items.append(
                {
                    "path": name,
                    "is_transcript": True,
                    "date": date,
                    "time_hhmm": compact[:4],
                    "time_hhmmss": time_hhmmss,
                    "recording_id": "DEADBEEF",
                }
            )
        return items

    def records(self, items, recording_type="memo", role="owner-authored"):
        return {item["path"]: {"recording_type": recording_type, "material_role": role} for item in items}

    def test_the_seconds_a_filename_carries_are_kept(self):
        """Parsed and discarded until fan-in needed them: two memos begun in the
        same minute have to stay in the order they were spoken."""
        parsed = vt.parse_filename("20260803 103810-23F0FD21 2026-08-03 10_38_43.md")
        self.assertEqual(parsed["date"], "2026-08-03")
        self.assertEqual(parsed["time_hhmm"], "1038")
        self.assertEqual(parsed["time_hhmmss"], "10:38:10")

    def test_a_filename_with_no_stamp_carries_no_time(self):
        self.assertIsNone(vt.parse_filename("Some Recording.md")["time_hhmmss"])

    def test_a_days_memos_group_in_the_order_spoken(self):
        items = self.members("2026-08-03 10:38:10", "2026-08-03 09:36:50", "2026-08-03 10:41:36")
        groups = vt.group_daily(items, self.records(items))
        self.assertEqual(len(groups), 1)
        self.assertEqual(
            [item["time_hhmmss"] for item in groups[0]["members"]], ["09:36:50", "10:38:10", "10:41:36"]
        )

    def test_a_lone_recording_is_not_a_group(self):
        items = self.members("2026-08-03 09:36:50")
        self.assertEqual(vt.group_daily(items, self.records(items)), [])

    def test_different_days_never_merge(self):
        items = self.members("2026-08-03 09:36:50", "2026-08-04 09:36:50")
        self.assertEqual(vt.group_daily(items, self.records(items)), [])

    def test_a_meeting_is_a_document_not_a_page_of_a_day(self):
        for recording_type in ("conversation", "meeting", "therapy", "lecture"):
            items = self.members("2026-08-03 09:36:50", "2026-08-03 10:38:10")
            groups = vt.group_daily(items, self.records(items, recording_type=recording_type))
            self.assertEqual(groups, [], f"{recording_type} should never group")

    def test_a_recording_with_someone_else_in_it_never_groups(self):
        items = self.members("2026-08-03 09:36:50", "2026-08-03 10:38:10")
        self.assertEqual(vt.group_daily(items, self.records(items, role="personal-exchange")), [])

    def test_a_recording_with_no_filename_date_never_groups(self):
        items = self.members("2026-08-03 09:36:50", "2026-08-03 10:38:10")
        items[1]["date"] = None
        self.assertEqual(vt.group_daily(items, self.records(items)), [])

    def test_offsets_are_rebased_onto_one_monotone_clock(self):
        """Each recording's `*MM:SS*` restarts at zero, so concatenating them raw
        produces a day that starts over once per recording."""
        items = self.members("2026-08-03 09:36:50", "2026-08-03 10:38:10")
        parsed = {
            items[0]["path"]: vt.parse_transcript(transcript([block("First thing.", 0), block("Second thing.", 9)])),
            items[1]["path"]: vt.parse_transcript(transcript([block("Later thing.", 0)])),
        }
        merged = vt.merge_transcripts(items, parsed)
        self.assertEqual([entry["clock"] for entry in merged], ["09:36:50", "09:36:59", "10:38:10"])
        self.assertEqual([entry["unit_id"] for entry in merged], ["s-0001", "s-0001", "s-0002"])
        self.assertTrue(all(merged[i]["seconds"] <= merged[i + 1]["seconds"] for i in range(len(merged) - 1)))

    def test_the_merged_transcript_names_each_recording(self):
        items = self.members("2026-08-03 09:36:50", "2026-08-03 10:38:10")
        parsed = {
            items[0]["path"]: vt.parse_transcript(transcript([block("First thing.", 0)])),
            items[1]["path"]: vt.parse_transcript(transcript([block("Later thing.", 0)])),
        }
        rendered = vt.render_merged_transcript(vt.merge_transcripts(items, parsed))
        self.assertIn("--- recording s-0001 ---", rendered)
        self.assertIn("--- recording s-0002 ---", rendered)
        self.assertLess(rendered.index("First thing."), rendered.index("--- recording s-0002 ---"))

    def test_section_markers_are_computed_from_filenames(self):
        """The exact markers the hand-built log carries. A time is a fact about a
        file; asking a model for one invites an invented one."""
        items = self.members(
            "2026-08-03 09:36:50", "2026-08-03 10:34:16", "2026-08-03 10:38:10",
            "2026-08-03 10:38:44", "2026-08-03 10:39:35", "2026-08-03 10:41:36",
        )
        self.assertEqual(vt.daily_marker(items, ["s-0001"]), "(~09:36)")
        self.assertEqual(vt.daily_marker(items, ["s-0002"]), "(~10:34)")
        self.assertEqual(vt.daily_marker(items, ["s-0003", "s-0004"]), "(~10:38)")
        self.assertEqual(vt.daily_marker(items, ["s-0005", "s-0006"]), "(~10:39–10:41)")

    def test_a_marker_spanning_one_minute_does_not_read_as_a_range(self):
        items = self.members("2026-08-03 10:38:10", "2026-08-03 10:38:44")
        self.assertEqual(vt.daily_marker(items, ["s-0001", "s-0002"]), "(~10:38)")

    def test_the_heading_names_the_day_in_words(self):
        """The filename carries the ISO date and Obsidian shows it above the note,
        so repeating it inside would say the same thing twice in two formats."""
        self.assertEqual(vt.daily_title("2026-08-03", "Thoughts & To-Dos"), "August 3 — Thoughts & To-Dos")


class DailyNoteBuildingTests(unittest.TestCase):
    """The assembled log, and the gates that decide whether it may be written."""

    FORMAT = (Path(__file__).resolve().parents[3] / "lib" / "vault-format" / "note-format.md").read_text(
        encoding="utf-8"
    )

    def setUp(self):
        import vault_compose
        import vault_format
        import vault_schema

        self.vc = vault_compose
        self.fmt = vault_format.parse_format(self.FORMAT)
        self.schema = vault_schema.parse_schema_note(SCHEMA)
        self.group = {
            "date": "2026-08-03",
            "recording_type": "memo",
            "members": [
                {"path": "a.md", "time_hhmm": "0936", "time_hhmmss": "09:36:50"},
                {"path": "b.md", "time_hhmm": "1038", "time_hhmmss": "10:38:10"},
            ],
        }
        self.sources = self.vc.source_set(
            [
                self.vc.source_unit(self.vc.KIND_TRANSCRIPT, "a", "A qualitative coding tool for interviews."),
                self.vc.source_unit(self.vc.KIND_TRANSCRIPT, "b", "Order yogurt and other groceries this week."),
            ]
        )
        self.raw_names = {"a.md": "a - Transcript.md", "b.md": "b - Transcript.md"}

    def composition(self, **overrides):
        base = {
            "title": "Thoughts & To-Dos",
            "summary": "A coding tool idea and the groceries.",
            "sections": [
                {"heading": "Coding tool", "sourceIds": ["s-0001"], "lines": ["A qualitative coding tool for interviews."]},
                {"heading": "Errands", "sourceIds": ["s-0002"], "lines": ["Order yogurt and other groceries this week."]},
            ],
        }
        base.update(overrides)
        return base

    def build(self, composition=None):
        return vt.build_daily_note(
            self.fmt, self.schema, self.group, composition or self.composition(), self.sources,
            self.raw_names, "/tmp/run-x",
        )

    def test_the_log_carries_the_channel_and_the_machines_hand_separately(self):
        """`capture_type` says how the material arrived; the provenance block says
        who wrote the note. One property cannot answer both."""
        text = self.build()
        self.assertIn("capture_type: voice", text)
        self.assertIn("> [!provenance]- How this note was made", text)

    def test_the_lead_is_prose_rather_than_a_callout(self):
        """The note format's `journal` row asks for the owner's language first."""
        text = self.build()
        self.assertNotIn("[!summary]", text)
        self.assertLess(text.index("A coding tool idea"), text.index("## Coding tool"))

    def test_every_recording_is_listed_with_its_time(self):
        text = self.build()
        self.assertIn("## Source Recordings", text)
        self.assertIn("- [[a - Transcript]] (~09:36)", text)
        self.assertIn("- [[b - Transcript]] (~10:38)", text)

    def test_recordings_are_linked_by_basename(self):
        """Filing moves a recording into the sources tree; a full-path link does
        not survive that and a basename one does."""
        self.assertNotIn("[[10 Sources/", self.build())

    def test_the_assembled_log_satisfies_the_grammar(self):
        body = self.build().split("---\n", 2)[2]
        self.assertEqual(self.vc.check_grammar(self.fmt, body), [])

    def test_a_section_citing_no_recording_is_held(self):
        composition = self.composition()
        composition["sections"][0]["sourceIds"] = []
        review = vt.check_daily_note(self.fmt, self.sources, self.build(composition), composition, self.group["members"])
        self.assertTrue(any("cites no recording" in line for line in review))

    def test_a_section_citing_an_unknown_recording_is_held(self):
        composition = self.composition()
        composition["sections"][0]["sourceIds"] = ["s-9999"]
        review = vt.check_daily_note(self.fmt, self.sources, self.build(composition), composition, self.group["members"])
        self.assertTrue(any("unknown recordings" in line for line in review))

    def test_a_dropped_recording_is_held(self):
        """The failure fan-in produces: a day's log built from one of two
        recordings, with nothing about it looking wrong."""
        composition = self.composition(
            summary="A coding tool idea.",
            sections=[{"heading": "Coding tool", "sourceIds": ["s-0001"], "lines": ["A qualitative coding tool."]}],
        )
        review = vt.check_daily_note(self.fmt, self.sources, self.build(composition), composition, self.group["members"])
        self.assertTrue(any("s-0002" in line and "did not reach the note" in line for line in review))

    def test_a_recording_summarized_hard_is_not_reported_as_dropped(self):
        """The floor is deliberately low. A recording that contributed one clause
        to a day has genuinely been used, and holding the log for that would make
        the check unusable on exactly the short fragments it exists to serve."""
        composition = self.composition(
            summary="A coding tool idea and the groceries.",
            sections=[{"heading": "Coding tool", "sourceIds": ["s-0001"], "lines": ["A qualitative coding tool."]}],
        )
        review = vt.check_daily_note(self.fmt, self.sources, self.build(composition), composition, self.group["members"])
        self.assertFalse(any("did not reach the note" in line for line in review))

    def test_an_invented_name_is_held(self):
        composition = self.composition()
        composition["sections"][0]["lines"] = ["A coding tool that Priya suggested for interviews."]
        review = vt.check_daily_note(self.fmt, self.sources, self.build(composition), composition, self.group["members"])
        self.assertTrue(any("name not in any recording: Priya" in line for line in review))

    def test_a_recording_id_the_day_does_not_have_is_dropped_not_refused(self):
        """A six-recording day drew `s-0007` through `s-0011` on a real run -- the
        model counting sections rather than reading the dividers. The ids feed the
        time markers and nothing else, so an invented one costs nothing to
        discard, and refusing the whole day for it loses six real recordings."""
        response = {
            "title": "Thoughts",
            "summary": "A day.",
            "sections": [{"heading": "Coding tool", "sourceIds": ["s-0001", "s-0007"], "lines": ["A tool."]}],
        }
        composition = vt.validate_daily_composition(response, self.sources)
        self.assertEqual(composition["sections"][0]["sourceIds"], ["s-0001"])
        self.assertEqual(composition["dropped_source_ids"], ["Coding tool: s-0007"])

    def test_a_section_left_citing_nothing_real_is_still_held(self):
        """Dropping the noise must not swallow the signal."""
        response = {
            "title": "Thoughts",
            "summary": "A day.",
            "sections": [{"heading": "Coding tool", "sourceIds": ["s-0099"], "lines": ["A tool."]}],
        }
        composition = vt.validate_daily_composition(response, self.sources)
        self.assertEqual(composition["sections"][0]["sourceIds"], [])
        review = vt.check_daily_note(
            self.fmt, self.sources, self.build(composition), composition, self.group["members"]
        )
        self.assertTrue(any("cites no recording" in line for line in review))

    def test_the_model_may_not_write_the_source_recordings_section(self):
        response = {
            "title": "Thoughts",
            "summary": "A day.",
            "sections": [{"heading": "Source Recordings", "sourceIds": ["s-0001"], "lines": ["- [[a]]"]}],
        }
        with self.assertRaises(vt.UserError):
            vt.validate_daily_composition(response, self.sources)

    def test_grounding_reads_the_whole_day_not_one_section(self):
        """A day is merged and cleaned as one document before the model sees any
        section boundary, so its citations attribute an already-unified text.
        Narrowing by them reported the day's own vocabulary as invented."""
        composition = self.composition()
        composition["sections"][0]["lines"] = ["Ordering yogurt came up while thinking about the coding tool."]
        review = vt.check_daily_note(
            self.fmt, self.sources, self.build(composition), composition, self.group["members"]
        )
        self.assertFalse(any("not in any recording" in line for line in review))

    def test_recordings_stay_unfiled_for_the_organizer(self):
        """Routing them means choosing a domain, and the only way to choose one
        here is to hardcode it."""
        taken = set()
        destination = vt.daily_raw_destination(self.group, self.group["members"][0], "A Memo", taken)
        self.assertTrue(destination.startswith("00 Inbox/"))
        self.assertTrue(destination.endswith(" - Transcript.md"))

    def test_a_faithful_log_passes_every_gate(self):
        composition = self.composition()
        self.assertEqual(
            vt.check_daily_note(self.fmt, self.sources, self.build(composition), composition, self.group["members"]),
            [],
        )


class CleanupConcurrencyTests(PipelineTests):
    """Files are independent; chunks inside a file are not."""

    # Identical bodies would be collapsed as duplicates before cleanup ever ran,
    # so each file has to say something of its own.
    NAMES = ("20260724 131748-9788991C.md", "20260725 131748-9788991D.md", "20260726 131748-9788991E.md")

    def write_distinct(self, count):
        for offset, name in enumerate(self.NAMES[:count]):
            blocks = [
                block(f"{text} Note {offset} on that.", seconds)
                for text, seconds in zip(SOLO_TEXTS, SOLO_SECONDS)
            ]
            self.write(name, transcript(blocks))

    def test_jobs_cleans_several_files_and_keeps_each_one_whole(self):
        self.write_distinct(3)
        with StubServer() as server:
            result = self.result_of(self.process(server.url, "--apply", "--jobs", "2"))
        self.assertEqual(result["data"]["counts"]["processed"], 3)
        self.assertEqual(result["data"]["counts"]["review_required"], 0)

    def test_jobs_matches_serial_output(self):
        self.write_distinct(3)
        with StubServer() as server:
            serial = self.result_of(self.process(server.url, "--apply"))
        serial_notes = {path.name: path.read_text(encoding="utf-8") for path in (self.vault / "00 Inbox").glob("*.md")}

        self.tearDown()
        self.setUp()
        self.write_distinct(3)
        with StubServer() as server:
            parallel = self.result_of(self.process(server.url, "--apply", "--jobs", "2"))
        parallel_notes = {path.name: path.read_text(encoding="utf-8") for path in (self.vault / "00 Inbox").glob("*.md")}

        self.assertEqual(serial["data"]["counts"], parallel["data"]["counts"])
        self.assertEqual(serial_notes, parallel_notes)

    def test_jobs_never_exceeds_the_files_it_has(self):
        self.write_distinct(1)
        with StubServer() as server:
            result = self.result_of(self.process(server.url, "--apply", "--jobs", "8"))
        self.assertEqual(result["data"]["counts"]["processed"], 1)


class CleanupRetryFailedTests(PipelineTests):
    """A recorded failure is inherited, so it needs a deliberate way out."""

    def journal_rows(self, run_dir):
        return [json.loads(line) for line in (run_dir / "written.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_a_failed_note_is_inherited_on_a_plain_resume(self):
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        # Empty cleaned text is a contract failure that survives the corrective
        # re-ask, so the note is recorded as failed and stays failed.
        empty = {"cleaned": "", "chunk_summary": "x"}
        with StubServer(scripted={"clean": [empty, empty]}) as server:
            first = self.result_of(self.process(server.url))
            run_dir = run_dir_of(first)
            self.assertTrue(any(row.get("status") == "failed" for row in self.journal_rows(run_dir)))
            server.reset()
            self.result_of(
                run_script(
                    "process", "--vault", str(self.vault), "--base-url", server.url, "--model", "chat",
                    "--run", str(run_dir), "--no-verify",
                )
            )
            # The resume inherited the failed note rather than asking again.
            self.assertEqual(server.stage_requests("write"), [])

    def test_retry_failed_asks_again_and_can_succeed(self):
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        empty = {"cleaned": "", "chunk_summary": "x"}
        with StubServer(scripted={"clean": [empty, empty]}) as server:
            first = self.result_of(self.process(server.url))
            run_dir = run_dir_of(first)
            self.assertTrue(any(row.get("status") == "failed" for row in self.journal_rows(run_dir)))
            server.reset()
            # The scripted queue is spent, so the re-attempt gets the faithful
            # default echo and clears.
            retried = self.result_of(
                run_script(
                    "process", "--vault", str(self.vault), "--base-url", server.url, "--model", "chat",
                    "--run", str(run_dir), "--retry-failed", "--apply", "--no-verify",
                )
            )
            self.assertTrue(server.stage_requests("write"))
        self.assertEqual(retried["data"]["counts"]["processed"], 1)
        rows = self.journal_rows(run_dir)
        # The failed row stays on the record; a later ok row supersedes it.
        self.assertTrue(any(row.get("status") == "failed" for row in rows))
        self.assertEqual(rows[-1]["status"], "ok")


class HoldReviewHandler(SecondStubChatHandler):
    """A thinking reviewer that holds every note as a source defect."""

    responses = []
    requests = []
    scripted = {}
    HOLD_REASON = "the export mislabels the therapist's lines as the client's throughout"

    def default_for(self, stage, payload):
        if stage == "review":
            return {"verdict": "hold", "reason_code": "source_defect", "reason": self.HOLD_REASON}
        return super().default_for(stage, payload)


class ReviewFixTests(unittest.TestCase):
    """The whole-note review that fixes in place — the replacement for the
    batched verify, the utterance spot-check, the meaning-judge, and the
    repair/escalation loops. Its own harness (rather than a `PipelineTests`
    subclass) keeps the 34 inherited pipeline tests from running again."""

    NAME = "20260724 131748-9788991C.md"
    DESTINATION = "2026-07-24 - Memo - Espresso Machine Repairs.md"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name).resolve() / "vault"
        (self.vault / "99 Meta" / "99.02 Schemas").mkdir(parents=True)
        (self.vault / "00 Inbox").mkdir()
        (self.vault / "99 Meta" / "99.02 Schemas" / "0.00 Vault Schema.md").write_text(SCHEMA, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, body):
        (self.vault / "00 Inbox" / name).write_text(body, encoding="utf-8")

    def inbox(self):
        return sorted(
            path.name
            for path in (self.vault / "00 Inbox").glob("*.md")
            if path.name != vt.vault_review.REVIEW_NOTE_NAME
        )

    def result_of(self, completed):
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return json.loads(completed.stdout)

    def think_process(self, chat_url, think_url, *extra):
        return run_script(
            "process", "--vault", str(self.vault), "--base-url", chat_url, "--model", "chat",
            "--think-url", think_url, "--think-model", "code", *extra,
        )

    def gutted_cleanup(self):
        source = transcript(SOLO_BLOCKS * 4)
        blocks = vt.parse_transcript(source)["blocks"]
        kept = " ".join(entry["text"] for entry in blocks[:2])
        return source, {"cleaned": kept, "chunk_summary": "The opening of the memo."}

    def review_rows(self, run_dir):
        path = run_dir / "review.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def record_for(self, result):
        plan = json.loads((run_dir_of(result) / "plan.json").read_text(encoding="utf-8"))
        return next(record for record in plan["records"] if record["source"].endswith(self.NAME))

    def test_a_sound_note_is_reviewed_ok_and_filed(self):
        self.write(self.NAME, transcript(SOLO_BLOCKS))
        with StubServer() as chat, StubServer(handler_cls=SecondStubChatHandler) as think:
            result = self.result_of(self.think_process(chat.url, think.url, "--apply"))
        self.assertIn(self.DESTINATION, self.inbox())
        verification = result["data"]["verification"]
        self.assertEqual(verification["reviewed"], 1)
        self.assertEqual(verification["ok"], 1)
        self.assertEqual(verification["held"], 0)
        record = self.record_for(result)
        self.assertEqual(record["verified"], "reviewed-ok")
        rows = self.review_rows(run_dir_of(result))
        self.assertEqual([row["verdict"] for row in rows], ["ok"])

    def test_the_reviewer_fixes_a_gutted_note_in_place(self):
        source, cleanup = self.gutted_cleanup()
        self.write(self.NAME, source)
        blocks = vt.parse_transcript(source)["blocks"]
        restored = " ".join(entry["text"] for entry in blocks)
        fixed = {"verdict": "fixed", "body": restored, "reason": "restored the dropped errands and warranty question"}
        with StubServer(scripted={"clean": [cleanup]}) as chat, \
                StubServer(handler_cls=SecondStubChatHandler, scripted={"review": [fixed]}) as think:
            result = self.result_of(self.think_process(chat.url, think.url, "--apply"))
        self.assertIn(self.DESTINATION, self.inbox())
        note_text = (self.vault / "00 Inbox" / self.DESTINATION).read_text(encoding="utf-8")
        self.assertIn("stud spacing", note_text)  # from the restored tail
        verification = result["data"]["verification"]
        self.assertEqual(verification["fixed"], 1)
        record = self.record_for(result)
        self.assertEqual(record["verified"], "reviewed-fixed")
        self.assertNotIn("fidelity_provisional", record)
        rows = self.review_rows(run_dir_of(result))
        self.assertEqual(rows[-1]["verdict"], "fixed")
        self.assertTrue(rows[-1]["fixed_hash"])

    def test_a_fix_that_breaks_structure_gets_one_deep_retry(self):
        source, cleanup = self.gutted_cleanup()
        self.write(self.NAME, source)
        blocks = vt.parse_transcript(source)["blocks"]
        restored = " ".join(entry["text"] for entry in blocks)
        reviews = [
            {"verdict": "fixed", "body": f"# A Level-One Heading\n\n{restored}", "reason": "restored"},
            {"verdict": "fixed", "body": restored, "reason": "restored, structure respected"},
        ]
        with StubServer(scripted={"clean": [cleanup]}) as chat, \
                StubServer(handler_cls=SecondStubChatHandler, scripted={"review": reviews}) as think:
            result = self.result_of(self.think_process(chat.url, think.url, "--apply"))
        self.assertIn(self.DESTINATION, self.inbox())
        self.assertEqual(result["data"]["verification"]["fixed"], 1)
        review_requests = [
            payload for payload in SecondStubChatHandler.requests
            if payload["messages"][0]["content"].startswith("You review one processed")
        ]
        self.assertEqual(len(review_requests), 2)
        self.assertEqual(review_requests[-1].get("reasoning_effort"), vt.ESCALATION_EFFORT)

    def test_a_source_defect_hold_carries_its_code(self):
        self.write(self.NAME, transcript(SOLO_BLOCKS))
        with StubServer() as chat, StubServer(handler_cls=HoldReviewHandler) as think:
            result = self.result_of(self.think_process(chat.url, think.url, "--apply"))
        self.assertEqual(self.inbox(), [self.NAME])
        record = self.record_for(result)
        self.assertTrue(record["needs_review"])
        self.assertEqual(record["reason_code"], "source_defect")
        self.assertIn(HoldReviewHandler.HOLD_REASON, record["review_reason"])
        run_dir = run_dir_of(result)
        queued = [json.loads(line) for line in (run_dir / "review-queue.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(queued[0]["reason_code"], "source_defect")
        report = (run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("source_defect", report)

    def test_an_unreachable_reviewer_rearms_the_floors(self):
        source, cleanup = self.gutted_cleanup()
        self.write(self.NAME, source)
        with StubServer(scripted={"clean": [cleanup]}) as chat:
            result = self.result_of(
                self.think_process(chat.url, "http://127.0.0.1:9/v1/chat/completions", "--apply")
            )
        self.assertEqual(self.inbox(), [self.NAME])
        record = self.record_for(result)
        self.assertTrue(record["needs_review"])
        report = (run_dir_of(result) / "report.md").read_text(encoding="utf-8")
        self.assertIn("Not verified", report)

    def test_a_settled_verdict_is_not_re_reviewed_on_resume(self):
        self.write(self.NAME, transcript(SOLO_BLOCKS))
        with StubServer() as chat, StubServer(handler_cls=SecondStubChatHandler) as think:
            first = self.result_of(self.think_process(chat.url, think.url))
            reviews = len([
                payload for payload in SecondStubChatHandler.requests
                if payload["messages"][0]["content"].startswith("You review one processed")
            ])
            self.assertEqual(reviews, 1)
            self.result_of(
                run_script(
                    "process", "--vault", str(self.vault), "--base-url", chat.url, "--model", "chat",
                    "--think-url", think.url, "--think-model", "code", "--run", str(run_dir_of(first)),
                )
            )
            reviews_after = len([
                payload for payload in SecondStubChatHandler.requests
                if payload["messages"][0]["content"].startswith("You review one processed")
            ])
        self.assertEqual(reviews_after, 1, "the settled verdict was journaled; nothing is reviewed twice")


class ReviewNoteRenderParseTests(unittest.TestCase):
    """The reusable render/parse contract in vault_review."""

    def test_ticks_round_trip_through_hand_edits(self):
        vr = vt.vault_review
        to_process = [
            vr.ReviewItem(name="A Note", summary="About things.", facts="memo · 2m"),
            vr.ReviewItem(name="Another Note", summary="More things.", facts="memo · 1m"),
        ]
        note = vr.render_review_note(
            generated_at="2026-08-12 10:00 UTC",
            run_directory=".vault-transcripts/runs/R1",
            to_process=to_process,
            decisions=[vr.ReviewItem(name="dup", source="dup.md", reason="duplicate")],
            apply_uri=vr.apply_uri("Loom", "cmd-1"),
            apply_command="python3 x.py process --apply --from-review",
        )
        # Passed notes are ticked by default.
        self.assertIn("- [x] [[A Note]]", note)
        self.assertIn("- [x] [[Another Note]]", note)
        self.assertIn("obsidian://shell-commands?vault=Loom&execute=cmd-1", note)

        parsed = vr.parse_review_note(note)
        self.assertEqual(parsed.run_directory, ".vault-transcripts/runs/R1")
        self.assertEqual(parsed.approved, {"A Note", "Another Note"})
        # The reviewer unticks one; a sub-bullet and reordering do not confuse the
        # parser.
        edited = note.replace("- [x] [[Another Note]]", "- [ ] [[Another Note]]\n    - skip this one")
        self.assertEqual(vr.parse_review_note(edited).approved, {"A Note"})

    def test_no_command_id_drops_the_link_but_keeps_the_command(self):
        vr = vt.vault_review
        note = vr.render_review_note(
            generated_at="t",
            run_directory="r",
            to_process=[],
            decisions=[],
            apply_uri=vr.apply_uri("Loom", None),
            apply_command="python3 x.py --from-review",
        )
        self.assertNotIn("obsidian://", note)
        self.assertIn("python3 x.py --from-review", note)

    def test_autonomous_note_is_a_receipt_plus_the_held_set(self):
        vr = vt.vault_review
        note = vr.render_review_note(
            generated_at="2026-08-17 09:00 UTC",
            run_directory=".vault-transcripts/runs/R2",
            applied=[vr.ReviewItem(name="A Filed Note", summary="Filed.", facts="memo · 2m")],
            decisions=[vr.ReviewItem(name="held", source="held.md", reason="cleaned transcript may not be faithful")],
            apply_uri=vr.apply_uri("Loom", "cmd-1"),
            apply_command="python3 x.py process --autonomous",
        )
        # A receipt, not a tick list: the filed note has no checkbox.
        self.assertIn("## Filed automatically", note)
        self.assertIn("[[A Filed Note]]", note)
        self.assertNotIn("- [x]", note)
        self.assertNotIn("- [ ]", note)
        # The held item is surfaced with its reason; there is no Apply section.
        self.assertIn("still needs you", note)
        self.assertIn("cleaned transcript may not be faithful", note)
        self.assertNotIn("## Apply", note)
        # The run reference still round-trips for a re-run.
        self.assertEqual(vr.parse_review_note(note).run_directory, ".vault-transcripts/runs/R2")

    def test_autonomous_note_with_nothing_held_still_reports_what_it_filed(self):
        vr = vt.vault_review
        note = vr.render_review_note(
            generated_at="t",
            run_directory="r",
            applied=[vr.ReviewItem(name="Filed", facts="memo")],
            decisions=[],
            apply_uri=None,
            apply_command="python3 x.py process --autonomous",
        )
        self.assertIn("1 note filed automatically", note)
        self.assertIn("Nothing needs you", note)
        self.assertIn("[[Filed]]", note)


class CleanOneChunkRetryTests(unittest.TestCase):
    """clean_one_chunk retries an invented-words failure once, then commits."""

    def run_chunk(self, first_invented=True, summarized=False):
        seen = []

        def fake_once(args, service, messages, source, speaker_map, drop_labels, tiny, task, glossary=()):
            seen.append(task)
            if first_invented and len(seen) == 1:
                return "The barista mentioned unrelated hypothetical scenarios.", "x", \
                    ["these words are not in the chunk: barista, mentioned"], ["barista", "mentioned", "scenarios"]
            return "The machine leaks.", "x", [], []

        original = vt.clean_chunk_once
        vt.clean_chunk_once = fake_once
        try:
            args = SimpleNamespace(cache_prompt=True, request_timeout=60, routing={})
            cleaned, _summary = vt.clean_one_chunk(
                args, {"name": "chat", "model": "chat", "url": "http://127.0.0.1:1/v1/chat/completions"},
                {"chunk": "x"}, "x", {}, True, False, summarized=summarized,
            )
            return cleaned, seen
        finally:
            vt.clean_chunk_once = original

    def test_an_invented_failure_is_retried_once_and_the_clean_retry_wins(self):
        cleaned, seen = self.run_chunk()
        self.assertEqual(len(seen), 2)  # one corrective retry
        self.assertEqual(cleaned, "The machine leaks.")

    def test_summarized_chunk_ignores_invented_words(self):
        # Minutes paraphrase by design, so the invented-words check does not
        # apply: no retry, the best-effort minutes pass straight through.
        seen = []

        def fake_once(args, service, messages, source, speaker_map, drop_labels, tiny, task, glossary=()):
            seen.append(task)
            return "The team agreed to order a gasket.", "x", \
                ["these words are not in the chunk: team, agreed"], ["team", "agreed"]

        original = vt.clean_chunk_once
        vt.clean_chunk_once = fake_once
        try:
            args = SimpleNamespace(cache_prompt=True, request_timeout=60, routing={})
            cleaned, _summary = vt.clean_one_chunk(
                args, {"name": "chat", "model": "chat", "url": "http://127.0.0.1:1/v1/chat/completions"},
                {"chunk": "x"}, "x", {}, False, False, summarized=True,
            )
        finally:
            vt.clean_chunk_once = original
        self.assertEqual(len(seen), 1)  # no retry
        self.assertIn("gasket", cleaned)


class InboxReviewTests(PipelineTests):
    """Staging, the control note, and the from-review apply end to end."""

    NAME = "2026-07-24 - Memo - Espresso Machine Repairs"

    def review_path(self):
        return self.vault / "00 Inbox" / vt.vault_review.REVIEW_NOTE_NAME

    def pending_dir(self):
        return self.vault / "00 Inbox" / vt.vault_review.PENDING_DIRNAME

    def set_tick(self, name, checked):
        path = self.review_path()
        text = path.read_text(encoding="utf-8")
        old = f"- [{'x' if not checked else ' '}] [[{name}]]"
        new = f"- [{'x' if checked else ' '}] [[{name}]]"
        path.write_text(text.replace(old, new), encoding="utf-8")

    def test_dry_run_writes_the_control_note_and_stages_proposals(self):
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        before = self.inbox()
        with StubServer() as server:
            self.result_of(self.process(server.url, "--no-verify"))
        # The recordings themselves are untouched by a dry run.
        self.assertEqual(self.inbox(), before)
        review = self.review_path().read_text(encoding="utf-8")
        self.assertIn(f"- [x] [[{self.NAME}]]", review)  # passed, ticked by default
        self.assertTrue((self.pending_dir() / f"{self.NAME}.md").is_file())
        self.assertIn(self.NAME, vt.vault_review.parse_review_note(review).approved)

    def test_from_review_applies_a_ticked_note_and_resets_the_surface(self):
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        with StubServer() as server:
            self.result_of(self.process(server.url, "--no-verify"))
            self.result_of(self.process(server.url, "--from-review", "--no-verify"))
        self.assertProcessed(f"{self.NAME}.md")
        self.assertIn("Nothing is waiting", self.review_path().read_text(encoding="utf-8"))
        self.assertFalse(any(self.pending_dir().glob("*.md")) if self.pending_dir().exists() else False)

    def test_from_review_skips_an_unticked_note(self):
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        with StubServer() as server:
            self.result_of(self.process(server.url, "--no-verify"))
            self.set_tick(self.NAME, checked=False)
            self.result_of(self.process(server.url, "--from-review", "--no-verify"))
        # Nothing applied: the original recording is still there, no processed note.
        self.assertIn("20260724 131748-9788991C.md", self.inbox())
        self.assertNotIn(f"{self.NAME}.md", self.inbox())

    def test_a_meeting_is_summarized_and_staged_not_held(self):
        # A multi-speaker meeting whose cleanup compresses to short minutes: this
        # would trip the verbatim gate (ratio/retention), but summarize-mode skips
        # it, so the note is staged for approval rather than held.
        self.write("20260724 131748-9788991C.md", transcript(DIALOGUE_BLOCKS * 3))
        meeting = {
            "recording_type": "meeting",
            "material_role": "personal-exchange",
            "title": "Deployment Window Review",
            "speakers": {
                "Speaker 1": {"who": "unknown", "kind": "unknown", "confidence": "low", "source": "transcript"},
                "Speaker 2": {"who": "unknown", "kind": "unknown", "confidence": "low", "source": "transcript"},
            },
            "effective_speakers": 2,
            "spoken_date": None,
            "evidence": None,
            "needs_review": False,
            "review_reason": None,
        }
        minutes = {
            "cleaned": "## Deployment\n\nThe migration finished around midnight with no rollback; the dashboards recovered, though the nightly aggregation ran twice.",
            "chunk_summary": "x",
        }
        with StubServer(scripted={"classify": [meeting], "clean": [minutes, minutes, minutes]}) as server:
            result = self.result_of(self.process(server.url, "--no-verify"))
        self.assertGreaterEqual(result["data"]["counts"]["processed"], 1)
        self.assertEqual(result["data"]["counts"]["review_required"], 0)
        name = "2026-07-24 - Meeting - Deployment Window Review"
        self.assertTrue((self.pending_dir() / f"{name}.md").is_file())

    MEETING_CLASSIFY = {
        "recording_type": "meeting",
        "material_role": "personal-exchange",
        "title": "Deployment Window Review",
        "speakers": {
            "Speaker 1": {"who": "unknown", "kind": "unknown", "confidence": "low", "source": "transcript"},
            "Speaker 2": {"who": "unknown", "kind": "unknown", "confidence": "low", "source": "transcript"},
        },
        "effective_speakers": 2,
        "spoken_date": None,
        "evidence": None,
        "needs_review": False,
        "review_reason": None,
    }

    def test_from_review_applies_a_meeting(self):
        # Regression: a meeting is minutes, so the apply-time gate recompute must
        # NOT re-hold it for "invented words" — paraphrase is the point. Before the
        # fix this reconciled the meeting back to review and applied nothing.
        self.write("20260724 131748-9788991C.md", transcript(DIALOGUE_BLOCKS * 3))
        minutes = {"cleaned": "## Deployment\n\nThe migration finished with no rollback; the dashboards recovered.", "chunk_summary": "x"}
        with StubServer(scripted={"classify": [self.MEETING_CLASSIFY], "clean": [minutes, minutes, minutes]}) as server:
            self.result_of(self.process(server.url, "--no-verify"))
            result = self.result_of(self.process(server.url, "--from-review", "--no-verify"))
        self.assertEqual(result["data"]["counts"]["applied"], 1)
        self.assertProcessed("2026-07-24 - Meeting - Deployment Window Review.md")

    def test_from_review_with_nothing_ticked_keeps_the_review(self):
        # Regression: if nothing is applied (all unticked), the review note and
        # staging must be preserved, not silently reset — otherwise a failed apply
        # looks "done" while nothing went in.
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        with StubServer() as server:
            self.result_of(self.process(server.url, "--no-verify"))
            self.set_tick(self.NAME, checked=False)
            result = self.result_of(self.process(server.url, "--from-review", "--no-verify"))
        self.assertEqual(result["data"]["counts"]["applied"], 0)
        self.assertNotIn("Nothing is waiting", self.review_path().read_text(encoding="utf-8"))
        self.assertTrue((self.pending_dir() / f"{self.NAME}.md").is_file())

    def test_scan_excludes_the_review_surface(self):
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        with StubServer() as server:
            self.result_of(self.process(server.url, "--no-verify"))
        # A later run must not treat the control note or the staged proposals as
        # inbox input, or it would file the note and grow a twin of every proposal.
        paths = [item["path"] for item in vt.scan_inbox(self.vault)]
        self.assertNotIn(f"00 Inbox/{vt.vault_review.REVIEW_NOTE_NAME}", paths)
        self.assertFalse(any(vt.vault_review.PENDING_DIRNAME in path for path in paths))

    def test_an_edit_that_fabricates_is_applied_with_a_warning(self):
        # Word overlap is advisory at apply time: a person reviewed and ticked
        # this note, so a distinctive addition is logged, never re-held. The
        # transcript tail stays the hard gate (see the identity tests).
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        with StubServer() as server:  # faithful default cleanup, stages normally
            self.result_of(self.process(server.url, "--no-verify"))
            staged = self.pending_dir() / f"{self.NAME}.md"
            fabricated = " ".join(f"zz{chr(97 + i // 20)}{chr(97 + i % 20)}" for i in range(60))
            staged.write_text(
                staged.read_text(encoding="utf-8").replace("# Transcript", fabricated + "\n\n# Transcript", 1),
                encoding="utf-8",
            )
            self.set_tick(self.NAME, checked=True)
            result = self.result_of(self.process(server.url, "--from-review", "--no-verify"))
        self.assertIn(f"{self.NAME}.md", self.inbox())
        self.assertNotIn("20260724 131748-9788991C.md", self.inbox())
        plan = json.loads((run_dir_of(result) / "plan.json").read_text(encoding="utf-8"))
        record = next(row for row in plan["records"] if row["source"].endswith("9788991C.md"))
        self.assertTrue(
            any("reviewer approval" in warning for warning in record.get("warnings", [])),
            record.get("warnings"),
        )

    def test_from_review_recording_note_keeps_a_basename_parent(self):
        # The link-safe rename fires while the processed note is still ambiguous
        # with its staged review copy, so the CLI path-qualifies the recording
        # note's parent link to [[00 Inbox/_Pending Review/X]] -- a path that goes
        # stale the moment finish_review clears the staging. The post-apply
        # normalization pass puts it back to the bare basename the pipeline wrote,
        # so the filed recording note survives and the organizer has nothing to
        # object to. SHIM_PATHQUALIFY makes the shim reproduce that qualification.
        self.write("20260724 131748-9788991C.md", transcript(SOLO_BLOCKS))
        env = ShimEnvironment(vault_path=self.vault, vault_name="vault")
        self.addCleanup(env.cleanup)
        env.set_env(SHIM_PATHQUALIFY="00 Inbox/_Pending Review")
        with StubServer() as server:
            self.result_of(self.process(server.url, "--no-verify"))
            result = self.result_of(self.process(server.url, "--from-review", "--no-verify"))
        raw = (self.vault / "00 Inbox" / f"{self.NAME} - Transcript.md").read_text(encoding="utf-8")
        self.assertIn(f'parent: "[[{self.NAME}]]"', raw)
        self.assertNotIn("_Pending Review", raw)
        # The recording itself is preserved byte for byte below its frontmatter.
        self.assertTrue(raw.endswith(transcript(SOLO_BLOCKS)))
        # The pass backed the note up before rewriting and journaled the change,
        # so the CLI really did qualify the link and this is not a vacuous pass.
        run_dir = Path(result["data"]["run_directory"])
        journal = [
            json.loads(line)
            for line in (run_dir / "parent-normalize.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(journal), 1)
        self.assertEqual(journal[0]["to"], self.NAME)
        self.assertIn("_Pending Review", journal[0]["from"])
        backup = run_dir / "backup" / "00 Inbox" / f"{self.NAME} - Transcript.md"
        self.assertIn("_Pending Review", backup.read_text(encoding="utf-8"))


class ApplyCommandDiscoveryTests(unittest.TestCase):
    """The review note finds the shell-commands apply command by its text."""

    def write_data(self, root, payload):
        path = Path(root).joinpath(*vt.SHELLCOMMANDS_DATA)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_finds_the_from_review_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_data(tmp, {"shell_commands": [
                {"id": "other", "platform_specific_commands": {"default": "echo hi"}},
                {"id": "apply-1", "platform_specific_commands": {
                    "default": "python3 x/vault-transcripts.py process --vault v --apply --from-review"}},
            ]})
            self.assertEqual(vt.discover_apply_command_id(Path(tmp)), "apply-1")

    def test_handles_the_dict_form(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_data(tmp, {"shell_commands": {
                "z9": {"id": "z9", "shell_command": "run vault-transcripts ... --from-review"}}})
            self.assertEqual(vt.discover_apply_command_id(Path(tmp)), "z9")

    def test_no_plugin_config_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(vt.discover_apply_command_id(Path(tmp)))

    def test_env_var_overrides_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_data(tmp, {"shell_commands": [
                {"id": "disk", "platform_specific_commands": {"default": "vault-transcripts --from-review"}}]})
            os.environ[vt.APPLY_COMMAND_ID_ENV] = "envid"
            try:
                self.assertEqual(vt.resolve_apply_command_id(Path(tmp)), "envid")
            finally:
                del os.environ[vt.APPLY_COMMAND_ID_ENV]


class SoloLabelStripTests(unittest.TestCase):
    def test_standalone_labels_are_stripped(self):
        text = "**Speaker 1**\nI meditated on death.\n**Speaker 1**\nAnd impermanence."
        self.assertEqual(vt.strip_solo_labels(text), "I meditated on death.\nAnd impermanence.")

    def test_label_with_colon_is_stripped(self):
        self.assertEqual(vt.strip_solo_labels("**Ellie:**\nfoo"), "foo")

    def test_inline_label_and_emphasis_are_kept(self):
        self.assertEqual(vt.strip_solo_labels("**Note:** the plan."), "**Note:** the plan.")
        self.assertEqual(vt.strip_solo_labels("This is **very** important."), "This is **very** important.")

    def test_check_chunk_not_held_for_labels_after_strip(self):
        # A solo recording whose cleanup carried standalone labels: after the strip
        # the deterministic gate no longer flags a speaker label, so it is not held.
        raw = "**Speaker 1**\nI meditated on death and impermanence today."
        cleaned = vt.strip_solo_labels(raw)
        problems = vt.check_chunk(cleaned, raw, {}, True, False)
        self.assertFalse(any("speaker label" in problem for problem in problems), problems)


class UnusableInputTests(unittest.TestCase):
    # These exercise the gate on already-final stats. In the real pipeline a file
    # whose only problem is an impossible MM:SS rate is reinterpreted as HH:MM in
    # parse_transcript before it reaches here (see the coarsening tests above); the
    # gate still fires when even the minutes reading is impossible.
    def test_impossible_speaking_rate_is_flagged(self):
        reason = vt.unusable_input_reason({"words": 6000, "duration_seconds": 57, "timestamps_ordered": True})
        self.assertIsNotNone(reason)
        self.assertIn("words per second", reason)

    def test_normal_and_fast_speech_pass(self):
        self.assertIsNone(vt.unusable_input_reason({"words": 900, "duration_seconds": 360, "timestamps_ordered": True}))
        self.assertIsNone(vt.unusable_input_reason({"words": 300, "duration_seconds": 60, "timestamps_ordered": True}))

    def test_out_of_order_timestamps_are_not_judged(self):
        # A repeated/scrambled timeline (e.g. a test fixture, or app quirk) has no
        # trustworthy duration, so the rate is not judged rather than misfiring.
        self.assertIsNone(vt.unusable_input_reason({"words": 6000, "duration_seconds": 40, "timestamps_ordered": False}))

    def test_no_rate_to_judge_passes(self):
        self.assertIsNone(vt.unusable_input_reason({"words": 500, "duration_seconds": 0, "timestamps_ordered": True}))
        self.assertIsNone(vt.unusable_input_reason(None))

    def test_short_memo_is_not_flagged(self):
        # Brevity alone is not corruption; only an impossible rate is.
        self.assertIsNone(vt.unusable_input_reason({"words": 6, "duration_seconds": 3, "timestamps_ordered": True}))


class ParentBasenameTests(unittest.TestCase):
    """A recording note's `parent` is kept a bare basename wikilink, whatever the
    link-safe move leaves behind."""

    QUALIFIED = (
        "---\n"
        "type: source\n"
        "status: complete\n"
        'parent: "[[00 Inbox/_Pending Review/2026-07-24 - Memo - Espresso Machine Repairs]]"\n'
        "source_kind: transcript\n"
        "capture_type: voice\n"
        "---\n"
        "\n"
        "The recording, verbatim.\n"
    )

    def test_a_directory_qualified_parent_is_reduced_to_the_basename(self):
        result = vt.parent_basename_bytes(self.QUALIFIED.encode("utf-8"))
        self.assertIsNotNone(result)
        rewritten, old_target, basename = result
        self.assertEqual(basename, "2026-07-24 - Memo - Espresso Machine Repairs")
        self.assertEqual(old_target, "00 Inbox/_Pending Review/2026-07-24 - Memo - Espresso Machine Repairs")
        text = rewritten.decode("utf-8")
        self.assertIn('parent: "[[2026-07-24 - Memo - Espresso Machine Repairs]]"\n', text)
        self.assertNotIn("_Pending Review", text)
        # Only the parent line changed; putting the old target back reproduces the
        # input byte for byte, so nothing else in the note was touched.
        self.assertEqual(
            text.replace(
                '"[[2026-07-24 - Memo - Espresso Machine Repairs]]"',
                '"[[00 Inbox/_Pending Review/2026-07-24 - Memo - Espresso Machine Repairs]]"',
            ),
            self.QUALIFIED,
        )

    def test_a_basename_parent_is_left_untouched(self):
        note = self.QUALIFIED.replace("00 Inbox/_Pending Review/", "")
        self.assertIsNone(vt.parent_basename_bytes(note.encode("utf-8")))

    def test_a_note_without_a_parent_is_left_untouched(self):
        note = "---\ntype: source\nstatus: complete\n---\n\nThe recording.\n"
        self.assertIsNone(vt.parent_basename_bytes(note.encode("utf-8")))

    def test_a_note_without_frontmatter_is_left_untouched(self):
        self.assertIsNone(vt.parent_basename_bytes(b"No frontmatter here.\n"))

    def test_a_qualified_target_with_a_md_suffix_reduces_to_the_basename(self):
        # link_basename keeps only the final path component and drops a `.md`
        # suffix, so either shape a rewriter might leave still lands on the basename.
        note = self.QUALIFIED.replace(
            "_Pending Review/2026-07-24 - Memo - Espresso Machine Repairs]]",
            "_Pending Review/2026-07-24 - Memo - Espresso Machine Repairs.md]]",
        )
        rewritten, _old, basename = vt.parent_basename_bytes(note.encode("utf-8"))
        self.assertEqual(basename, "2026-07-24 - Memo - Espresso Machine Repairs")
        self.assertIn('parent: "[[2026-07-24 - Memo - Espresso Machine Repairs]]"', rewritten.decode("utf-8"))


class ScanAdmissionTests(unittest.TestCase):
    """What the inbox scan admits, holds, and distrusts — decided once, at scan."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name).resolve() / "vault"
        (self.vault / "00 Inbox").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write_inbox(self, name, body):
        (self.vault / "00 Inbox" / name).write_text(body, encoding="utf-8")

    def scan_one(self):
        items = vt.scan_inbox(self.vault)
        self.assertEqual(len(items), 1)
        return items[0]

    def test_an_untimestamped_labelled_export_is_admitted_automatically(self):
        speakers = ["Ellian", "Sopagna"]
        utterance = " ".join(["word"] * 30)
        self.write_inbox(
            "Export.md", "".join(f"{speakers[index % 2]}\n{utterance}\n\n" for index in range(8))
        )
        item = self.scan_one()
        self.assertTrue(item["is_transcript"])
        self.assertTrue(item["unlabeled"])
        self.assertTrue(item["no_timestamps"])
        self.assertIsNone(item["skip_reason"])
        self.assertEqual(item["stats"]["timestamp_style"], "unlabeled")

    def test_a_broken_clock_export_keeps_its_words_and_distrusts_the_clock(self):
        # Three-part stamps block the HH:MM rescale, and 6000 words over 57
        # seconds is no speaking rate a human reaches — the clock is corrupt, not
        # the words. The note processes with the clock ignored instead of being
        # held for re-recording.
        body = "*0:00:05*\nhello there\n\n*0:00:57*\n" + " ".join(["word"] * 6000) + "\n"
        self.write_inbox("Broken Clock.md", body)
        item = self.scan_one()
        self.assertTrue(item["is_transcript"])
        self.assertTrue(item["clock_broken"])
        self.assertIn("words per second", item["clock_broken"])
        self.assertEqual(item["stats"]["duration_seconds"], 0)
        self.assertFalse(item.get("unlabeled"))
        record = vt.base_record(item)
        self.assertTrue(any("timestamps ignored" in warning for warning in record["warnings"]))

    def test_prose_is_still_left_alone(self):
        self.write_inbox(
            "Essay.md",
            "# Heading\n\n" + " ".join(["The meeting went well and we made real progress today."] * 40),
        )
        item = self.scan_one()
        self.assertFalse(item["is_transcript"])
        self.assertEqual(item["skip_reason"], "no timestamped transcript blocks")

    def test_the_record_carries_the_parse_mode(self):
        # parse_for_item must read the same transcript from an item and from the
        # plan record derived from it — that identity is what retired the
        # allow_unlabeled threading bug class.
        speakers = ["Ellian", "Sopagna"]
        utterance = " ".join(["word"] * 30)
        body = "".join(f"{speakers[index % 2]}\n{utterance}\n\n" for index in range(8))
        self.write_inbox("Export.md", body)
        item = self.scan_one()
        record = vt.base_record(item)
        self.assertTrue(record["unlabeled"])
        for owner in (item, record):
            parsed = vt.parse_for_item(owner, body)
            self.assertEqual(len(parsed["blocks"]), 8)


class UnlabeledPayloadRegressionTests(unittest.TestCase):
    """The verify/judge/floor/repair payload builders must parse an unlabeled
    export the way cleanup did.

    With the default parse a speaker-labelled, timestamp-less export reads as
    zero blocks, so the meaning-judge reviews against an empty raw ("no source
    material"), the note verifier renders an empty transcript, the utterance
    floor has nothing to locate, and every repair word reads as invented — the
    unresolvable holds of the 2026-08-19 inbox session.
    """

    SOURCE = "00 Inbox/Unlabeled Export.md"

    def setUp(self):
        import vault_schema

        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name).resolve() / "vault"
        (self.vault / "00 Inbox").mkdir(parents=True)
        self.run_dir = Path(self.tmp.name).resolve() / "run"
        (self.run_dir / "assembled").mkdir(parents=True)
        self.schema = vault_schema.parse_schema_note(SCHEMA)
        self.args = type(
            "Args",
            (),
            {
                "summary_style": "callout",
                "tiny_words": 120,
                "filename_pattern": "date-type-topic",
                "speaker_policy": "names",
                "compiled_lexicon": None,
            },
        )()
        # A speaker-labelled export with no timestamps: the --unlabeled shape.
        speakers = ["Ellian", "Sopagna"]
        self.body = "".join(
            f"{speakers[index % 2]}\n{text}\n\n" for index, text in enumerate(SOLO_TEXTS)
        )
        (self.vault / self.SOURCE).write_text(self.body, encoding="utf-8")
        self.summary = "Espresso machine errands and pantry repairs."
        self.cleaned = " ".join(SOLO_TEXTS)
        note_text = f"> [!summary]\n> {self.summary}\n\n{self.cleaned}\n\n# Transcript\n\n{self.body}"
        (self.run_dir / "assembled" / "artifact.md").write_text(note_text, encoding="utf-8")
        self.record = {
            "source": self.SOURCE,
            # The parse mode travels on the record (parse_for_item); without it
            # every payload below would read the export as zero blocks.
            "unlabeled": True,
            "destination": "00 Inbox/2026-07-24 - Memo - Espresso Machine Repairs.md",
            "recording_type": "memo",
            "title": "Espresso Machine Repairs",
            "summary": self.summary,
            "artifact": "artifact.md",
            "speaker_map": {"Ellian": None, "Sopagna": None},
            "proposals": [],
        }
        self.item = {
            "path": self.SOURCE,
            "raw_body": self.body,
            "stats": {
                "duration_seconds": 0,
                "words": len(self.body.split()),
                "speaker_labels": {"Ellian": 6, "Sopagna": 5},
            },
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_reviewer_sees_the_raw_transcript(self):
        payload = vt.review_payload(self.vault, self.record, self.run_dir, self.args)
        self.assertIsNotNone(payload)
        self.assertIn("replacement gasket", payload["rawTranscript"])
        self.assertNotIn("rawTruncated", payload)

    def test_a_verbatim_repair_is_not_read_as_invented(self):
        revised = " ".join(SOLO_TEXTS[:8])
        problems = vt.rebuild_note_with_cleaned(
            self.vault, self.schema, self.record, self.item, revised, self.args, self.run_dir
        )
        self.assertEqual(problems, [])
        self.assertTrue(self.record.get("final_hash"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

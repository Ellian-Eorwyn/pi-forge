#!/usr/bin/env python3
"""Tests for the vault-capture skill."""

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

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "vault-capture.py"
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "lib"))
spec = importlib.util.spec_from_file_location("vault_capture", SCRIPT)
vault_capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vault_capture)
vc = vault_capture


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
| `related` | no | list of quoted wikilinks | Related links. |
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
- `task` — Something to do.
- `draft` — Something being written.

## Status values

- `raw` — Unprocessed.
- `active` — Active.

## Domains

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `personal` | `1` | `Personal` | Personal material. |
| `meta` | `99` | `Meta` | System notes. |

## Subdomains

### personal

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `home` | `1` | `Home` | Home matters. |

### meta

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `schemas` | `2` | `Schemas` | Schema notes. |

## Project registry

| Approved value | Domain | Subdomain | Number | Definition |
| --- | --- | --- | --- | --- |
| `"[[Pi Forge]]"` | `personal` | `home` | `1` | Local agent harness. |

## Source kinds

- `book` — Book.

## Capture types

- `manual` — Typed.
- `generated` — Made by a script, agent, or model.
- `voice` — Voice memo.

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

BRAINDUMP = """Okay so the espresso machine gasket is cracked around the rim and it leaks
whenever I pull a double shot, I need to order a replacement before the weekend.
The grinder probably needs descaling too, same trip.

Separately I keep meaning to anchor the pantry shelving unit properly, I never
bought the right brackets because I always forget the stud spacing. Worth
photographing the wall before the hardware store trip this time.

Gillian thinks the kettle is fine and honestly she is probably right, so I will
leave that alone for now. I should check whether the warranty covers the gasket,
because if it does then none of this costs anything except the afternoon.
"""

TRANSCRIPT_EXPORT = """**Speaker 1**
*00:00*
Okay so the espresso machine gasket is cracked and it leaks constantly.

**Speaker 2**
*00:08*
Did you check whether the warranty covers that part at all?

**Speaker 1**
*00:14*
Not yet, I keep meaning to look it up before ordering a replacement.
"""


def split_response(notes=None, **overrides):
    response = {
        "notes": notes
        or [
            {
                "kind": "task",
                "title": "Espresso Machine Gasket Replacement",
                "gist": "The gasket is cracked and leaking, and a replacement needs ordering.",
                "covers": ["gasket is cracked around the rim", "order a replacement"],
            }
        ],
        "needs_review": False,
        "review_reason": None,
    }
    response.update(overrides)
    return response


TWO_NOTES = [
    {
        "kind": "task",
        "title": "Espresso Machine Gasket Replacement",
        "gist": "The gasket is cracked and leaking, and a replacement needs ordering.",
        "covers": ["gasket is cracked around the rim"],
    },
    {
        "kind": "idea",
        "title": "Pantry Shelving Anchoring",
        "gist": "The shelving needs anchoring, blocked on knowing the stud spacing.",
        "covers": ["anchor the pantry shelving unit"],
    },
]


class StubChatHandler(BaseHTTPRequestHandler):
    """Answers each stage plausibly, keyed off its system prompt.

    Scripted responses take priority, so a test can inject one bad response for
    a single stage and let everything else behave.
    """

    responses = []
    requests = []
    scripted = {}

    def stage_of(self, payload):
        system = payload["messages"][0]["content"]
        if "verdicts" in system:
            return "verify"
        if system.startswith("You read one person's braindump"):
            return "split"
        if system.startswith("You write one note"):
            return "draft"
        return "unknown"

    def default_for(self, stage, payload):
        if len(payload["messages"]) < 2:
            return "ready"  # the doctor probe, which sends one bare user message
        user = json.loads(payload["messages"][1]["content"])
        if stage == "split":
            return split_response()
        if stage == "draft":
            note = user["thisNote"]
            # Echoing the covered phrases keeps the deterministic gates happy so
            # a test can measure everything else.
            covered = " ".join(note.get("covers") or []) or note["gist"]
            return {"title": note["workingTitle"], "body": f"{note['gist']}\n\nIn the braindump: {covered}."}
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
        self.handler_cls.requests = []

    def stage_requests(self, stage):
        counted = []
        for payload in self.handler_cls.requests:
            system = payload["messages"][0]["content"]
            if stage == "verify" and "verdicts" in system:
                counted.append(payload)
            elif stage == "split" and system.startswith("You read one person's braindump"):
                counted.append(payload)
            elif stage == "draft" and system.startswith("You write one note"):
                counted.append(payload)
        return counted


def run_script(*args, environment=None, stdin=None):
    # Point the agent directory at nothing so endpoint resolution cannot pick up
    # the settings of whoever is running the tests.
    base = environment if environment is not None else os.environ
    env = {**base, "PYTHONDONTWRITEBYTECODE": "1"}
    env.setdefault("PI_FORGE_AGENT_DIR", "/nonexistent-agent-directory")
    arguments = list(args)
    if arguments and arguments[0] == "capture" and not {"--no-verify", "--think-url"} & set(arguments):
        arguments.append("--no-verify")
    # Style examples shell out to vault-connections, which reaches the
    # embeddings endpoint. Tests that want that path stub it directly.
    if arguments and arguments[0] in {"capture", "doctor"} and "--exemplars" not in arguments:
        arguments.append("--no-exemplars")
    arguments = [argument for argument in arguments if argument != "--exemplars"]
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments], capture_output=True, text=True, env=env, input=stdin
    )


class UnitTests(unittest.TestCase):
    def setUp(self):
        import vault_schema

        self.schema = vault_schema.parse_schema_note(SCHEMA)

    def test_every_captured_note_is_marked_generated(self):
        for kind in vc.CAPTURE_KINDS:
            metadata = vc.frontmatter_metadata(self.schema, kind)
            self.assertEqual(metadata["capture_type"], "generated")
            self.assertEqual(metadata["status"], "raw")
            self.assertIn(metadata["type"], self.schema["types"])

    def test_a_vault_that_cannot_say_generated_is_refused(self):
        import vault_schema

        without = vault_schema.parse_schema_note(SCHEMA.replace("- `generated` — Made by a script, agent, or model.\n", ""))
        with self.assertRaisesRegex(vc.UserError, "generated"):
            vc.frontmatter_metadata(without, "idea")

    def test_unknown_kind_falls_back_to_note(self):
        import vault_schema

        without_task = vault_schema.parse_schema_note(SCHEMA.replace("- `task` — Something to do.\n", ""))
        self.assertEqual(vc.frontmatter_metadata(without_task, "task")["type"], "note")

    def test_transcript_exports_are_recognized(self):
        self.assertTrue(vc.looks_like_transcript_export(TRANSCRIPT_EXPORT))
        self.assertFalse(vc.looks_like_transcript_export(BRAINDUMP))

    def test_a_name_from_the_braindump_is_not_invention(self):
        found = vc.invented_specifics(BRAINDUMP, "The gasket leaks, and Gillian says the kettle is fine.")
        self.assertEqual(found["names"], [])
        self.assertEqual(found["uncertain_names"], [])

    def test_an_invented_name_is_caught_mid_sentence(self):
        found = vc.invented_specifics(BRAINDUMP, "The gasket leaks, and Marcus recommended a supplier.")
        self.assertEqual(found["names"], ["Marcus"])

    def test_an_invented_name_opening_a_sentence_is_flagged_for_the_reviewer(self):
        # Position is all that separates this from the case above, and it is not
        # enough to prove a name, so it goes to the reviewer instead of holding.
        found = vc.invented_specifics(BRAINDUMP, "The gasket leaks. Marcus recommended a supplier.")
        self.assertEqual(found["names"], [])
        self.assertEqual(found["uncertain_names"], ["Marcus"])

    def test_a_word_derived_from_the_braindump_is_not_invention(self):
        found = vc.invented_specifics(BRAINDUMP, "The gasket leaks. Ordering a replacement this week.")
        self.assertEqual(found["names"], [])
        self.assertEqual(found["uncertain_names"], [])

    def test_invented_links_are_caught(self):
        found = vc.invented_specifics(BRAINDUMP, "See https://example.com/gaskets for parts.")
        self.assertEqual(found["links"], ["https://example.com/gaskets"])

    def test_spelled_out_numbers_cover_their_digits(self):
        self.assertEqual(vc.invented_specifics("I need three brackets.", "Buy 3 brackets.")["numbers"], [])
        self.assertEqual(vc.invented_specifics("I need brackets.", "Buy 7 brackets.")["numbers"], ["7"])

    def test_split_validation_rejects_a_bad_response(self):
        with self.assertRaisesRegex(vc.UserError, "no notes"):
            vc.validate_split({"notes": []}, 8)
        with self.assertRaisesRegex(vc.UserError, "unknown kind"):
            vc.validate_split(split_response(notes=[{**TWO_NOTES[0], "kind": "poem"}]), 8)
        with self.assertRaisesRegex(vc.UserError, "over --max-notes"):
            vc.validate_split(split_response(notes=TWO_NOTES), 1)
        with self.assertRaisesRegex(vc.UserError, "two notes titled"):
            vc.validate_split(split_response(notes=[TWO_NOTES[0], dict(TWO_NOTES[0])]), 8)

    def test_split_titles_are_made_filename_safe(self):
        notes, _review, _reason = vc.validate_split(
            split_response(notes=[{**TWO_NOTES[0], "title": "Gasket [draft] | v2"}]), 8
        )
        self.assertEqual(notes[0]["title"], "Gasket (draft) - v2")

    def test_draft_checks_catch_structural_mistakes(self):
        item = {"text": BRAINDUMP}
        problems, _notices = vc.check_draft({"item": item, "body": "# A Title\n\nBody."})
        self.assertTrue(any("level-one heading" in problem for problem in problems))
        problems, _notices = vc.check_draft({"item": item, "body": "---\ntype: note\n---\n\nBody."})
        self.assertTrue(any("frontmatter" in problem for problem in problems))
        problems, _notices = vc.check_draft({"item": item, "body": "Body.\n\n# Braindump\n\nstuff"})
        self.assertTrue(problems)


class ReflectionTests(unittest.TestCase):
    """Which kinds get reflection sections, and what a connection may cite."""

    CANDIDATE = {"path": "Espresso machine.md", "wikilink": "[[Espresso machine]]"}
    SOURCE = {
        "url": "https://ok.example/seal",
        "source": "[[Espresso machine]]",
        "excerpt": "rated for food contact https://ok.example/seal",
    }

    def entry(self, body, kind="task", candidates=True, sources=True):
        return {
            "item": {"text": "need to order a new gasket for the espresso machine before the weekend"},
            "body": body,
            "note": {"kind": kind},
            "connection_candidates": [self.CANDIDATE] if candidates else [],
            "outside_sources": [self.SOURCE] if sources else [],
        }

    def connections(self, *items, kind="task", **kwargs):
        body = "Order a new gasket.\n\n## Connections\n\n" + "\n".join(f"- {item}" for item in items) + "\n"
        return vc.check_reflection(self.entry(body, kind, **kwargs))

    def test_every_kind_but_draft_gets_a_reflection(self):
        self.assertEqual(sorted(vc.KIND_TO_REFLECTION), sorted(vc.CAPTURE_KINDS))
        self.assertEqual(vc.KIND_TO_REFLECTION["draft"], ())
        for kind in ("idea", "task", "question", "reference", "plan"):
            self.assertEqual(vc.KIND_TO_REFLECTION[kind], vc.WORKING_SECTIONS, kind)
        self.assertEqual(vc.KIND_TO_REFLECTION["journal"], vc.JOURNAL_SECTIONS)

    def test_the_section_list_and_its_guidance_travel_in_the_payload(self):
        payload = vc.draft_payload({"text": "dump"}, {"kind": "task", "title": "T", "gist": "g", "covers": []})
        self.assertEqual(payload["thisNote"]["reflectionSections"], list(vc.WORKING_SECTIONS))
        self.assertEqual(sorted(payload["thisNote"]["sectionGuidance"]), sorted(vc.WORKING_SECTIONS))

    def test_a_draft_is_given_no_sections_and_held_for_writing_one(self):
        payload = vc.draft_payload({"text": "dump"}, {"kind": "draft", "title": "T", "gist": "g", "covers": []})
        self.assertNotIn("reflectionSections", payload["thisNote"])
        problems = self.connections("[[Espresso machine]] has the model number.", kind="draft")
        self.assertTrue(any("does not get" in problem for problem in problems))

    def test_a_task_is_held_for_writing_a_journal_section(self):
        entry = self.entry("Body.\n\n## Interpretations\n\n- Reading into it.\n")
        self.assertTrue(any("Interpretations" in problem for problem in vc.check_reflection(entry)))

    def test_a_candidate_wikilink_and_a_cited_outside_line_both_pass(self):
        self.assertEqual(
            self.connections(
                "[[Espresso machine]] has the model number.",
                "Outside vault: the seal is food-grade (https://ok.example/seal).",
            ),
            [],
        )

    def test_an_uncited_connection_holds_the_note(self):
        problems = self.connections("Gaskets usually last about five years.")
        self.assertTrue(any("not labelled 'Outside vault:'" in problem for problem in problems))

    def test_an_outside_connection_citing_an_unread_url_holds_the_note(self):
        problems = self.connections("Outside vault: a claim (https://nope.example/x).")
        self.assertTrue(any("https://nope.example/x" in problem for problem in problems))

    def test_a_wikilink_outside_the_candidate_set_holds_the_note(self):
        problems = self.connections("[[Invented Note]] looks related.", candidates=False)
        self.assertTrue(any("not a candidate note" in problem for problem in problems))

    def test_a_cited_url_is_not_treated_as_an_invented_link(self):
        body = "Order a gasket.\n\n## Connections\n\n- Outside vault: food-grade (https://ok.example/seal).\n"
        source = self.entry(body)["item"]["text"]
        self.assertEqual(vc.invented_specifics(source, body, {self.SOURCE["url"]})["links"], [])
        # Without the allowlist the same URL is invention, which is what makes the
        # widening load-bearing rather than decorative.
        self.assertEqual(vc.invented_specifics(source, body)["links"], [self.SOURCE["url"]])
        self.assertEqual(vc.check_draft(self.entry(body))[0], [])

    def test_a_reflection_check_without_a_kind_still_checks_connections(self):
        entry = self.entry("Body.\n\n## Connections\n\n- Remembered fact.\n")
        del entry["note"]
        self.assertTrue(vc.check_reflection(entry))


class OutsideSourceTests(unittest.TestCase):
    """What counts as outside material this run actually read."""

    NOTE = (
        "---\ntype: note\n---\n\n"
        "## Findings\n\n"
        "- Group seals are food grade.\n"
        '  - "Rated to 120 C for food contact" — https://ok.example/seal\n\n'
        "## Sources\n\n"
        "- https://bare.example/nothing\n"
    )

    def harvest(self, braindump):
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            (vault / "Espresso machine.md").write_text(self.NOTE, encoding="utf-8")
            candidates = [{"path": "Espresso machine.md", "wikilink": "[[Espresso machine]]"}]
            return vc.collect_outside_sources(vault, braindump, candidates)

    def test_a_link_in_the_braindump_is_harvested_with_its_line(self):
        found = self.harvest("found the part page at https://parts.example/gasket-42 and it looks right")
        self.assertEqual(found[0]["url"], "https://parts.example/gasket-42")
        self.assertEqual(found[0]["source"], "this braindump")

    def test_a_cited_quote_in_a_candidate_note_is_harvested_and_attributed(self):
        entry = next(row for row in self.harvest("no links") if row["url"] == "https://ok.example/seal")
        self.assertEqual(entry["source"], "[[Espresso machine]]")
        self.assertIn("food contact", entry["excerpt"])

    def test_a_bare_url_carrying_no_claim_is_not_a_source(self):
        urls = {row["url"] for row in self.harvest("https://also-bare.example/x")}
        self.assertNotIn("https://bare.example/nothing", urls)
        self.assertNotIn("https://also-bare.example/x", urls)


class ExemplarTests(unittest.TestCase):
    """Which of the user's notes a draft is allowed to imitate."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name).resolve()
        (self.vault / "00 Inbox").mkdir(parents=True)
        self.original_search = vc.search_vault

    def tearDown(self):
        vc.search_vault = self.original_search
        self.tmp.cleanup()

    def write(self, relative, frontmatter, body):
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")
        return relative

    def stub_search(self, *paths):
        vc.search_vault = lambda vault, query, **kwargs: (list(paths), None)

    def prose(self, word="gasket"):
        return " ".join([f"A considered sentence about the {word} and what it means."] * 8)

    def test_generated_notes_are_never_imitated(self):
        mine = self.write("01 Personal/Mine.md", "type: note\nstatus: active", self.prose())
        theirs = self.write("01 Personal/Generated.md", "type: note\nstatus: raw\ncapture_type: generated", self.prose())
        self.stub_search(theirs, mine)
        exemplars, warning = vc.collect_exemplars(self.vault, "gasket")
        self.assertIsNone(warning)
        self.assertEqual([row["note"] for row in exemplars], ["Mine"])

    def test_pipeline_processed_notes_are_not_imitated_either(self):
        processed = self.write(
            "01 Personal/Cleaned.md", 'type: note\nstatus: raw\nprocessed_by:\n  - "vault-transcripts"', self.prose()
        )
        self.stub_search(processed)
        exemplars, _warning = vc.collect_exemplars(self.vault, "gasket")
        self.assertEqual(exemplars, [])

    def test_inbox_notes_are_not_exemplars(self):
        unfiled = self.write("00 Inbox/Unfiled.md", "type: note\nstatus: raw", self.prose())
        self.stub_search(unfiled)
        self.assertEqual(vc.collect_exemplars(self.vault, "gasket")[0], [])

    def test_the_matching_type_is_preferred(self):
        note = self.write("01 Personal/A Note.md", "type: note\nstatus: active", self.prose("note"))
        task = self.write("01 Personal/A Task.md", "type: task\nstatus: active", self.prose("task"))
        self.stub_search(note, task)
        exemplars, _warning = vc.collect_exemplars(self.vault, "gasket", wanted=1, note_type="task")
        self.assertEqual([row["note"] for row in exemplars], ["A Task"])

    def test_a_preserved_source_section_is_never_shown(self):
        note = self.write(
            "01 Personal/Recorded.md",
            "type: note\nstatus: active",
            f"{self.prose()}\n\n# Transcript\n\num so anyway the thing about the gasket is\n",
        )
        self.stub_search(note)
        exemplars, _warning = vc.collect_exemplars(self.vault, "gasket")
        self.assertNotIn("um so anyway", exemplars[0]["excerpt"])

    def test_a_failed_search_degrades_to_no_exemplars(self):
        vc.search_vault = lambda vault, query, **kwargs: ([], "vault search failed")
        exemplars, warning = vc.collect_exemplars(self.vault, "gasket")
        self.assertEqual(exemplars, [])
        self.assertEqual(warning, "vault search failed")

    def test_the_total_excerpt_budget_is_respected(self):
        paths = [
            self.write(f"01 Personal/Note {index}.md", "type: note\nstatus: active", "x" * 1400)
            for index in range(6)
        ]
        self.stub_search(*paths)
        exemplars, _warning = vc.collect_exemplars(self.vault, "gasket", wanted=6)
        self.assertLessEqual(sum(len(row["excerpt"]) for row in exemplars), vc.EXEMPLAR_TOTAL_CHARS)


class CaptureFixture(unittest.TestCase):
    """A disposable vault with a schema note, and helpers to run against it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.vault = self.root / "vault"
        (self.vault / "99 Meta" / "99.02 Schemas").mkdir(parents=True)
        (self.vault / "00 Inbox").mkdir()
        (self.vault / "99 Meta" / "99.02 Schemas" / "0.00 Vault Schema.md").write_text(SCHEMA, encoding="utf-8")
        self.inputs = self.root / "dumps"
        self.inputs.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write_dump(self, name, text=BRAINDUMP):
        path = self.inputs / name
        path.write_text(text, encoding="utf-8")
        return path

    def capture(self, url, *extra, inputs=None, stdin=None):
        arguments = ["capture"]
        arguments.extend(str(path) for path in (inputs if inputs is not None else [self.write_dump("dump.md")]))
        arguments.extend(["--vault", str(self.vault), "--base-url", url, *extra])
        return run_script(*arguments, stdin=stdin)

    def result_of(self, completed):
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        return json.loads(completed.stdout)

    def notes_in_inbox(self):
        return sorted(path.name for path in (self.vault / "00 Inbox").glob("*.md"))

    def read_note(self, name):
        return (self.vault / "00 Inbox" / name).read_text(encoding="utf-8")


class PipelineTests(CaptureFixture):
    def test_one_braindump_becomes_one_written_note(self):
        with StubServer() as server:
            result = self.result_of(self.capture(server.url))
        self.assertEqual(self.notes_in_inbox(), ["Espresso Machine Gasket Replacement.md"])
        note = self.read_note("Espresso Machine Gasket Replacement.md")
        self.assertTrue(note.startswith("---\ntype: task\nstatus: raw\ncapture_type: generated\n---\n"))
        self.assertIn("# Braindump", note)
        self.assertTrue(note.endswith(BRAINDUMP.rstrip("\n") + "\n"))
        self.assertEqual([line for line in note.splitlines() if line.startswith("# ")], ["# Braindump"])
        self.assertEqual(result["data"]["counts"]["created"], 1)

    def test_the_braindump_is_preserved_byte_for_byte(self):
        odd = "Ideas — with an em dash, a \"quote\", and trailing spaces   \n\nand a second paragraph.\n"
        self.write_dump("odd.md", odd)
        with StubServer() as server:
            self.result_of(self.capture(server.url, inputs=[self.inputs / "odd.md"]))
        note = self.read_note("Espresso Machine Gasket Replacement.md")
        self.assertEqual(note.split("# Braindump\n\n", 1)[1], odd)

    def test_a_split_dump_writes_siblings_that_link_to_the_primary(self):
        with StubServer(scripted={"split": [split_response(notes=TWO_NOTES)]}) as server:
            self.result_of(self.capture(server.url))
        self.assertEqual(
            self.notes_in_inbox(), ["Espresso Machine Gasket Replacement.md", "Pantry Shelving Anchoring.md"]
        )
        bodies = {name: self.read_note(name) for name in self.notes_in_inbox()}
        with_dump = [name for name, text in bodies.items() if "# Braindump" in text]
        self.assertEqual(len(with_dump), 1, "exactly one note carries the original")
        sibling = next(name for name in bodies if name not in with_dump)
        self.assertIn(f'related:\n  - "[[{Path(with_dump[0]).stem}]]"', bodies[sibling])

    def test_an_invented_name_holds_the_note_back(self):
        drafted = {
            "title": "Espresso Machine Gasket Replacement",
            "body": "The gasket leaks, and Marcus recommended a supplier.",
        }
        with StubServer(scripted={"draft": [drafted]}) as server:
            result = self.result_of(self.capture(server.url))
        self.assertEqual(self.notes_in_inbox(), [])
        self.assertEqual(result["data"]["counts"]["review"], 1)
        self.assertIn("Marcus", result["data"]["notes"][0]["held_reason"])

    def test_an_edited_note_is_never_rewritten_by_a_rerun(self):
        with StubServer() as server:
            result = self.result_of(self.capture(server.url))
            run_dir = result["data"]["run_directory"]
            note = self.vault / "00 Inbox" / "Espresso Machine Gasket Replacement.md"
            note.write_text("I rewrote this note myself.\n", encoding="utf-8")
            again = self.result_of(
                run_script(
                    "capture", "--vault", str(self.vault), "--base-url", server.url, "--run", run_dir, "--no-verify"
                )
            )
        self.assertEqual(note.read_text(encoding="utf-8"), "I rewrote this note myself.\n")
        self.assertEqual(self.notes_in_inbox(), ["Espresso Machine Gasket Replacement.md"])
        self.assertIn("has since changed", again["data"]["notes"][0]["held_reason"])

    def test_a_transcript_export_is_sent_to_the_other_skill(self):
        path = self.write_dump("recording.md", TRANSCRIPT_EXPORT)
        with StubServer() as server:
            result = self.result_of(self.capture(server.url, inputs=[path]))
        self.assertEqual(self.notes_in_inbox(), [])
        self.assertIn("vault-transcripts", " ".join(result["warnings"]))
        self.assertEqual(len(server.stage_requests("split")), 0, "a held input costs no model call")

    def test_force_synthesizes_from_a_transcript_export(self):
        path = self.write_dump("recording.md", TRANSCRIPT_EXPORT)
        with StubServer() as server:
            self.result_of(self.capture(server.url, "--force", inputs=[path]))
        self.assertEqual(self.notes_in_inbox(), ["Espresso Machine Gasket Replacement.md"])

    def test_dry_run_writes_nothing(self):
        with StubServer() as server:
            result = self.result_of(self.capture(server.url, "--dry-run"))
        self.assertEqual(self.notes_in_inbox(), [])
        self.assertEqual(result["data"]["counts"]["created"], 0)
        self.assertEqual(result["data"]["counts"]["ready"], 1)
        self.assertTrue(Path(result["artifacts"][0]).is_file())

    def test_stdin_is_captured(self):
        with StubServer() as server:
            result = self.result_of(
                run_script(
                    "capture", "--stdin", "--vault", str(self.vault), "--base-url", server.url, stdin=BRAINDUMP
                )
            )
        self.assertEqual(self.notes_in_inbox(), ["Espresso Machine Gasket Replacement.md"])
        self.assertEqual(result["data"]["notes"][0]["status"], "created")

    def test_a_name_collision_gets_a_suffix_and_never_overwrites(self):
        existing = self.vault / "00 Inbox" / "Espresso Machine Gasket Replacement.md"
        existing.write_text("A note that was already here.\n", encoding="utf-8")
        with StubServer() as server:
            self.result_of(self.capture(server.url))
        self.assertEqual(
            self.notes_in_inbox(),
            ["Espresso Machine Gasket Replacement (2).md", "Espresso Machine Gasket Replacement.md"],
        )
        self.assertEqual(existing.read_text(encoding="utf-8"), "A note that was already here.\n")

    def test_a_tiny_dump_is_held_rather_than_captured(self):
        path = self.write_dump("tiny.md", "buy gasket\n")
        with StubServer() as server:
            result = self.result_of(self.capture(server.url, inputs=[path]))
        self.assertEqual(self.notes_in_inbox(), [])
        self.assertIn("under", " ".join(result["warnings"]))

    def test_the_bulk_service_is_asked_for_a_non_thinking_completion(self):
        with StubServer() as server:
            self.result_of(self.capture(server.url))
        for payload in server.stage_requests("draft"):
            self.assertIs(payload.get("cache_prompt"), True)
            self.assertEqual(payload["messages"][0]["role"], "system")
            self.assertEqual(payload["temperature"], 0)

    def test_the_draft_system_prompt_is_byte_stable_across_a_run(self):
        with StubServer(scripted={"split": [split_response(notes=TWO_NOTES)]}) as server:
            self.result_of(self.capture(server.url))
        systems = {payload["messages"][0]["content"] for payload in server.stage_requests("draft")}
        self.assertEqual(len(systems), 1, "per-note variation belongs in the user message")

    def test_resume_reuses_the_journals(self):
        with StubServer() as server:
            result = self.result_of(self.capture(server.url, "--dry-run"))
            run_dir = result["data"]["run_directory"]
            server.reset()
            resumed = self.result_of(
                run_script(
                    "capture", "--vault", str(self.vault), "--base-url", server.url, "--run", run_dir, "--no-verify"
                )
            )
        self.assertEqual(len(server.stage_requests("split")), 0, "a resumed run re-splits nothing")
        self.assertEqual(len(server.stage_requests("draft")), 0, "a resumed run re-drafts nothing")
        self.assertEqual(resumed["data"]["counts"]["created"], 1)
        self.assertEqual(self.notes_in_inbox(), ["Espresso Machine Gasket Replacement.md"])

    def test_reapplying_a_finished_run_creates_no_duplicate(self):
        with StubServer() as server:
            result = self.result_of(self.capture(server.url))
            run_dir = result["data"]["run_directory"]
            again = self.result_of(
                run_script(
                    "capture", "--vault", str(self.vault), "--base-url", server.url, "--run", run_dir, "--no-verify"
                )
            )
        self.assertEqual(self.notes_in_inbox(), ["Espresso Machine Gasket Replacement.md"])
        self.assertEqual(again["data"]["counts"]["created"], 0)

    def test_resume_refuses_changed_options(self):
        with StubServer() as server:
            result = self.result_of(self.capture(server.url, "--dry-run"))
            run_dir = result["data"]["run_directory"]
            completed = run_script(
                "capture", "--vault", str(self.vault), "--base-url", server.url,
                "--run", run_dir, "--filename-pattern", "date-topic", "--no-verify",
            )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("--filename-pattern", completed.stdout)

    def test_no_verify_says_so_in_the_report(self):
        with StubServer() as server:
            result = self.result_of(self.capture(server.url))
        report = (Path(result["data"]["run_directory"]) / "report.md").read_text(encoding="utf-8")
        self.assertIn("Verification did not run", report)
        self.assertIn("nothing was reviewed", " ".join(result["warnings"]))

    def test_status_and_doctor(self):
        with StubServer() as server:
            result = self.result_of(self.capture(server.url))
            status = self.result_of(run_script("status", "--run", result["data"]["run_directory"]))
            self.assertEqual(status["data"]["notes_created"], 1)
            self.assertEqual(status["data"]["phase"], "complete")
            checked = run_script("doctor", "--vault", str(self.vault), "--base-url", server.url)
        payload = json.loads(checked.stdout)
        self.assertTrue(payload["data"]["checks"]["schema"]["ok"])
        self.assertTrue(payload["data"]["checks"]["vault"]["ok"])


VOICE_NOTE = """# Voice and Style

## Global voice

- Keep contractions. "I don't know" is how I talk.

## Per-type style

| Type | Style |
| --- | --- |
| `task` | Bullets for the things to do, one sentence of context above them. |

## Never do

- Never call me "the user" in a note.
"""


class VoiceTests(CaptureFixture):
    def write_voice(self, text=VOICE_NOTE):
        path = self.vault / "99 Meta" / "99.02 Schemas" / "0.01 Voice and Style.md"
        path.write_text(text, encoding="utf-8")
        return path

    def test_the_voice_note_reaches_the_draft_system_prompt(self):
        self.write_voice()
        with StubServer() as server:
            self.result_of(self.capture(server.url))
        system = server.stage_requests("draft")[0]["messages"][0]["content"]
        self.assertIn("Never call me", system)
        self.assertIn("Keep contractions", system)

    def test_the_per_type_row_travels_in_the_user_message(self):
        # It varies per note, and the system prompt has to stay byte-stable.
        self.write_voice()
        with StubServer() as server:
            self.result_of(self.capture(server.url))
        request = server.stage_requests("draft")[0]
        self.assertNotIn("Bullets for the things to do", request["messages"][0]["content"])
        payload = json.loads(request["messages"][1]["content"])
        self.assertEqual(
            payload["thisNote"]["styleForThisKind"], "Bullets for the things to do, one sentence of context above them."
        )

    def test_a_vault_without_a_voice_note_still_captures(self):
        with StubServer() as server:
            self.result_of(self.capture(server.url))
        self.assertEqual(self.notes_in_inbox(), ["Espresso Machine Gasket Replacement.md"])

    def test_no_voice_disables_an_existing_policy(self):
        self.write_voice()
        with StubServer() as server:
            self.result_of(self.capture(server.url, "--no-voice"))
        system = server.stage_requests("draft")[0]["messages"][0]["content"]
        self.assertNotIn("Never call me", system)

    def test_an_unreadable_voice_note_stops_the_run(self):
        self.write_voice("# Voice and Style\n\nJust some prose, no sections.\n")
        with StubServer() as server:
            completed = self.capture(server.url)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("none of its sections", completed.stdout)

    def test_a_changed_voice_note_refuses_a_resume(self):
        self.write_voice()
        with StubServer() as server:
            result = self.result_of(self.capture(server.url, "--dry-run"))
            self.write_voice(VOICE_NOTE.replace("Keep contractions.", "Write formally."))
            completed = run_script(
                "capture", "--vault", str(self.vault), "--base-url", server.url,
                "--run", result["data"]["run_directory"],
            )
        self.assertEqual(completed.returncode, 2)

    def test_the_report_names_the_voice_note_in_use(self):
        path = self.write_voice()
        with StubServer() as server:
            result = self.result_of(self.capture(server.url))
        report = (Path(result["data"]["run_directory"]) / "report.md").read_text(encoding="utf-8")
        self.assertIn(str(path), report)

    def test_doctor_reports_a_missing_voice_note_as_healthy(self):
        with StubServer() as server:
            payload = json.loads(run_script("doctor", "--vault", str(self.vault), "--base-url", server.url).stdout)
        self.assertTrue(payload["data"]["checks"]["voice"]["ok"])
        self.assertFalse(payload["data"]["checks"]["voice"]["configured"])


class PreferencesTests(CaptureFixture):
    def write_voice(self, text=VOICE_NOTE):
        path = self.vault / "99 Meta" / "99.02 Schemas" / "0.01 Voice and Style.md"
        path.write_text(text, encoding="utf-8")
        return path

    def propose(self, url, feedback, edits=None, **extra):
        response = {"edits": edits if edits is not None else [], "needs_review": False, "review_reason": None}
        with StubServer(responses=[response]) as server:
            server.url = url or server.url
            return self.result_of(
                run_script("preferences", "--vault", str(self.vault), "--feedback", feedback, "--base-url", server.url)
            )

    def test_feedback_becomes_a_proposal_and_changes_nothing_yet(self):
        path = self.write_voice()
        before = path.read_text(encoding="utf-8")
        edit = {
            "section": "global",
            "operation": "add",
            "text": "Keep sentences short.",
            "reason": "they said the notes ramble",
        }
        result = self.propose(None, "the notes ramble", [edit])
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertEqual(result["data"]["edits"][0]["id"], "p-001")
        report = (Path(result["data"]["run_directory"]) / "report.md").read_text(encoding="utf-8")
        self.assertIn("Keep sentences short.", report)

    def test_an_accepted_edit_is_applied_and_backed_up(self):
        path = self.write_voice()
        edit = {"section": "never", "operation": "add", "text": "Never end a note with a summary.", "reason": "asked"}
        proposed = self.propose(None, "stop adding summaries", [edit])
        applied = self.result_of(
            run_script(
                "preferences", "--vault", str(self.vault),
                "--run", proposed["data"]["run_directory"], "--accept", "p-001",
            )
        )
        self.assertIn("Never end a note with a summary.", path.read_text(encoding="utf-8"))
        self.assertTrue(Path(applied["data"]["backup"]).is_file())
        self.assertIn("Never call me", path.read_text(encoding="utf-8"), "existing rules survive")
        import vault_voice

        self.assertIn("Never end a note with a summary.", vault_voice.parse_voice_note(path.read_text())["never"])

    def test_a_rejected_edit_leaves_the_note_alone(self):
        path = self.write_voice()
        before = path.read_text(encoding="utf-8")
        edit = {"section": "global", "operation": "add", "text": "Write formally.", "reason": "guessed"}
        proposed = self.propose(None, "something", [edit])
        result = self.result_of(
            run_script(
                "preferences", "--vault", str(self.vault),
                "--run", proposed["data"]["run_directory"], "--reject", "p-001",
            )
        )
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertEqual(result["data"]["accepted"], [])

    def test_an_edited_note_refuses_a_stale_proposal(self):
        path = self.write_voice()
        edit = {"section": "global", "operation": "add", "text": "Keep sentences short.", "reason": "asked"}
        proposed = self.propose(None, "the notes ramble", [edit])
        path.write_text(VOICE_NOTE + "\n- And another rule I added by hand.\n", encoding="utf-8")
        completed = run_script(
            "preferences", "--vault", str(self.vault),
            "--run", proposed["data"]["run_directory"], "--accept", "p-001",
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("changed since these edits were proposed", completed.stdout)

    def test_an_unknown_proposal_id_is_refused(self):
        self.write_voice()
        proposed = self.propose(None, "something", [])
        completed = run_script(
            "preferences", "--vault", str(self.vault),
            "--run", proposed["data"]["run_directory"], "--accept", "p-999",
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("unknown proposal ids", completed.stdout)

    def test_an_amend_must_quote_an_existing_rule(self):
        self.write_voice()
        edit = {
            "section": "never",
            "operation": "amend",
            "text": "Never call me the reader.",
            "replaces": "A rule that was never in the note.",
            "reason": "hallucinated",
        }
        with StubServer(responses=[{"edits": [edit], "needs_review": False, "review_reason": None}]) as server:
            completed = run_script(
                "preferences", "--vault", str(self.vault), "--feedback", "x", "--base-url", server.url
            )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("not in the never section", completed.stdout)

    def test_a_vault_without_a_voice_note_says_how_to_make_one(self):
        with StubServer() as server:
            completed = run_script(
                "preferences", "--vault", str(self.vault), "--feedback", "x", "--base-url", server.url
            )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("Global voice", completed.stdout)

    def test_a_per_type_rule_round_trips(self):
        path = self.write_voice()
        edit = {
            "section": "per_type",
            "operation": "add",
            "type": "journal",
            "text": "Chronological paragraphs, no headings.",
            "reason": "asked",
        }
        proposed = self.propose(None, "journals should not have headings", [edit])
        self.result_of(
            run_script(
                "preferences", "--vault", str(self.vault),
                "--run", proposed["data"]["run_directory"], "--accept", "p-001",
            )
        )
        import vault_voice

        voice = vault_voice.parse_voice_note(path.read_text(encoding="utf-8"))
        self.assertEqual(voice["per_type"]["journal"], "Chronological paragraphs, no headings.")
        self.assertIn("task", voice["per_type"], "the existing row survives")

    def test_applying_preferences_preserves_frontmatter_and_unknown_sections(self):
        text = (
            "---\ntype: system\nstatus: active\ndomain: meta\nsubdomain: schemas\ncapture_type: manual\n---\n\n"
            + VOICE_NOTE
            + "\n## Human notes\n\nKeep this paragraph exactly.\n"
        )
        path = self.write_voice(text)
        edit = {
            "section": "global",
            "scope": "owner-authored",
            "operation": "add",
            "text": "Keep the owner's uncertainty visible.",
            "reason": "asked",
        }
        proposed = self.propose(None, "preserve uncertainty", [edit])
        self.result_of(
            run_script(
                "preferences",
                "--vault",
                str(self.vault),
                "--run",
                proposed["data"]["run_directory"],
                "--accept",
                "p-001",
            )
        )
        rendered = path.read_text(encoding="utf-8")
        self.assertTrue(rendered.startswith("---\ntype: system\n"))
        self.assertIn("## Human notes\n\nKeep this paragraph exactly.", rendered)
        self.assertIn("### Owner-Authored", rendered)


class VerificationTests(CaptureFixture):
    def verify_run(self, *extra, chat_scripted=None, think_scripted=None):
        with StubServer(scripted=chat_scripted) as chat, StubServer(
            scripted=think_scripted, handler_cls=SecondStubChatHandler
        ) as think:
            result = self.result_of(self.capture(chat.url, "--think-url", think.url, *extra))
            return result, chat, think

    def test_every_note_is_reviewed_with_the_braindump_in_front_of_it(self):
        result, _chat, think = self.verify_run()
        packets = think.stage_requests("verify")
        self.assertTrue(packets)
        reviewed = json.loads(packets[0]["messages"][-1]["content"])["items"]
        self.assertIn("braindump", reviewed[0])
        self.assertIn("Gillian thinks the kettle is fine", reviewed[0]["braindump"])
        self.assertEqual(result["data"]["verification"]["ok"], 1)
        self.assertEqual(self.notes_in_inbox(), ["Espresso Machine Gasket Replacement.md"])

    def test_a_flagged_note_is_redone_on_the_thinking_service(self):
        flag = {"verdicts": [{"id": "in-001-01", "verdict": "flag", "reason": "the title says nothing"}]}
        redrafted = {"title": "Espresso Gasket Warranty Check", "body": "Check the warranty before ordering a gasket."}
        result, _chat, think = self.verify_run(
            think_scripted={"verify": [flag, {"verdicts": []}], "draft": [redrafted]}
        )
        self.assertEqual(result["data"]["verification"]["escalated"], 1)
        self.assertEqual(self.notes_in_inbox(), ["Espresso Machine Gasket Replacement.md"])
        self.assertIn("Check the warranty", self.read_note("Espresso Machine Gasket Replacement.md"))

    def test_a_flag_that_cannot_be_redone_is_left_for_a_human(self):
        flag = {"verdicts": [{"id": "in-001-01", "verdict": "flag", "reason": "invented a supplier"}]}
        result, _chat, _think = self.verify_run(
            think_scripted={"verify": [flag, {"verdicts": []}], "draft": ["not json at all"]}
        )
        self.assertEqual(self.notes_in_inbox(), [])
        self.assertEqual(result["data"]["notes"][0]["status"], "review")
        self.assertIn("re-drafting failed", result["data"]["notes"][0]["held_reason"])

    def test_an_unreachable_verifier_does_not_read_as_approval(self):
        with StubServer() as chat:
            result = self.result_of(
                self.capture(chat.url, "--think-url", "http://127.0.0.1:9/v1/chat/completions", "--request-timeout", "2")
            )
        self.assertIn("skipped", result["data"]["verification"])
        report = (Path(result["data"]["run_directory"]) / "report.md").read_text(encoding="utf-8")
        self.assertIn("not the same as approval", report)


PROFILE = {
    "cards": [
        {
            "order": 0, "name": "Core Identity", "link": "[[Core Identity]]",
            "tier": "always", "scope": "universal", "routes": frozenset(),
            "triggers": [], "note": "", "facts": ["Sociologist of knowledge."],
        },
        {
            "order": 1, "name": "People in My Life", "link": "[[People in My Life]]",
            "tier": "when-relevant", "scope": "owner-authored", "routes": frozenset({"personal"}),
            "triggers": ["Gillian"], "note": "", "facts": ["Gillian Eorwyn is my spouse."],
        },
    ]
}


class PersonalContextTests(unittest.TestCase):
    def test_the_always_tier_reaches_the_draft_system_prompt(self):
        system = vault_capture.draft_system_prompt("", PROFILE)
        self.assertIn("Sociologist of knowledge.", system)

    def test_drafting_knows_no_route_so_gated_cards_stay_out(self):
        system = vault_capture.draft_system_prompt("", PROFILE)
        self.assertNotIn("Gillian", system)

    def test_the_draft_payload_carries_no_personal_context(self):
        """Regression lock. check_draft makes a name absent from the braindump a
        hard problem, so a card naming someone would make the gate discard the
        note. Nothing profile-derived may enter this payload."""
        payload = vault_capture.draft_payload(
            {"id": "i-001", "text": "Wrote about the thing today."},
            {"kind": "note", "title": "A note", "gist": "Something", "covers": ["the thing"]},
        )
        serialized = json.dumps(payload)
        self.assertNotIn("personalContext", payload)
        self.assertNotIn("Gillian", serialized)
        self.assertNotIn("Sociologist", serialized)

    def test_without_a_profile_the_system_prompt_is_unchanged(self):
        self.assertEqual(vault_capture.draft_system_prompt(), vault_capture.draft_system_prompt("", None))


if __name__ == "__main__":
    unittest.main(verbosity=2)

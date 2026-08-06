#!/usr/bin/env python3
"""Tests for the vault-compose skill.

The point of this skill is that it proposes rather than writes, and that a
specific with no source holds a note. Most of what follows breaks one of those
on purpose and asserts the break is caught.
"""

import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "vault-compose.py"
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("vault_compose_skill", SCRIPT)
vcs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vcs)

FORMAT = (Path(__file__).resolve().parents[3] / "lib" / "vault-format" / "note-format.md").read_text(encoding="utf-8")

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
| `capture_type` | no | controlled scalar | Capture type. |
| `date` | no | scalar, human-owned | The date the note is about. |

### Property constraints

- `source_kind` is required when `type: source` and forbidden for other types.

## Note types

- `note` — General note.
- `journal` — Journal note.

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
- `chat` — Captured from a conversation.

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

## Legacy normalization map

| Legacy input | Canonical output |
| --- | --- |
| `type: daily` | `type: journal` |
"""

SOURCE_A = (
    "The gasket on the espresso machine is cracked around the rim and it leaks "
    "whenever a double shot is pulled. Gillian thinks the kettle is fine."
)
SOURCE_B = (
    "Marcus recommended a supplier for the brackets. The pantry shelving still "
    "needs anchoring and the stud spacing was never measured."
)

# What a well-behaved model returns. Body lines echo the sources so the
# deterministic gates pass and a test can measure everything else.
GOOD_BODY = [
    "The espresso machine gasket is cracked around the rim and leaks whenever a double shot is pulled.",
    "",
    "The pantry shelving still needs anchoring; the stud spacing was never measured, and Marcus "
    "recommended a supplier for the brackets. Gillian thinks the kettle is fine and can be left alone.",
]


class StubChatHandler(BaseHTTPRequestHandler):
    """Answers each stage plausibly, keyed off its system prompt."""

    scripted = {}
    requests = []

    def stage_of(self, payload):
        system = payload["messages"][0]["content"]
        if system.startswith("You plan the shape"):
            return "outline"
        if system.startswith("You write one note"):
            return "draft"
        if system.startswith("You review notes"):
            return "verify"
        return "probe"

    def default_for(self, stage, payload):
        if stage == "outline":
            return {"notes": [{"title": "Espresso And Shelving", "blocks": [{"block": "body", "sourceIds": ["s-0001", "s-0002"]}]}]}
        if stage == "draft":
            return {"blocks": {"body": GOOD_BODY}}
        if stage == "verify":
            user = json.loads(payload["messages"][1]["content"])
            return {"verdicts": [{"id": item["id"], "verdict": "ok"} for item in user["items"]]}
        return "ready"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        self.__class__.requests.append(payload)
        stage = self.stage_of(payload)
        queue = self.__class__.scripted.get(stage)
        response = queue.pop(0) if queue else self.default_for(stage, payload)
        content = response if isinstance(response, str) else json.dumps(response)
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True


class StubServer:
    def __enter__(self):
        StubChatHandler.scripted = {}
        StubChatHandler.requests = []
        self.server = QuietServer(("127.0.0.1", 0), StubChatHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/chat/completions"
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()

    def script(self, stage, *responses):
        StubChatHandler.scripted[stage] = list(responses)


class ComposeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name).resolve()
        (self.vault / "00 Inbox").mkdir(parents=True)
        schemas = self.vault / "99 Meta" / "99.02 Schemas"
        schemas.mkdir(parents=True)
        (schemas / "0.00 Vault Schema.md").write_text(SCHEMA, encoding="utf-8")
        (schemas / "0.04 Note Format.md").write_text(FORMAT, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def write_spec(self, **overrides):
        spec = {
            "version": 1,
            "intent": "synthesis",
            "request": "Draw the household repairs together.",
            "noteType": "note",
            "date": "2026-08-05",
            "maxNotes": 2,
            "sources": [
                {"kind": "vault-note", "label": "Espresso Machine", "text": SOURCE_A, "wikilink": "[[Espresso Machine]]"},
                {"kind": "vault-note", "label": "Pantry Shelving", "text": SOURCE_B, "wikilink": "[[Pantry Shelving]]"},
            ],
        }
        spec.update(overrides)
        path = self.vault / "spec.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path

    def compose(self, server, spec_path=None, *extra):
        # The reviewer is pointed at the stub too. Without this the think service
        # resolves to the real endpoint, so the suite needs a GPU to pass and its
        # verdicts are whatever a live model thinks of a fixture.
        arguments = [
            "compose",
            "--vault", str(self.vault),
            "--spec", str(spec_path or self.write_spec()),
            "--base-url", server.url,
            "--model", "stub",
            *extra,
        ]
        if "--think-url" not in arguments:
            arguments.extend(["--think-url", server.url, "--think-model", "stub"])
        return vcs.run(arguments)

    def run_dir_of(self, result):
        return result["data"]["run_directory"]

    def test_a_clean_note_is_proposed_and_not_written(self):
        with StubServer() as server:
            result = self.compose(server)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["counts"]["proposed"], 1)
        self.assertEqual(result["data"]["counts"]["held"], 0)
        # The whole point: proposing writes nothing.
        self.assertEqual(list((self.vault / "00 Inbox").iterdir()), [])

    def test_apply_writes_only_what_was_accepted(self):
        with StubServer() as server:
            result = self.compose(server)
        run_dir = self.run_dir_of(result)
        applied = vcs.run(["apply", "--vault", str(self.vault), "--run", run_dir, "--accept", "n-001"])
        self.assertEqual(applied["data"]["written"], ["00 Inbox/Espresso And Shelving.md"])
        text = (self.vault / "00 Inbox" / "Espresso And Shelving.md").read_text(encoding="utf-8")
        self.assertIn("# Espresso And Shelving", text)
        self.assertIn("capture_type: generated", text)

    def test_apply_without_an_id_writes_nothing(self):
        with StubServer() as server:
            result = self.compose(server)
        with self.assertRaises(vcs.UserError):
            vcs.run(["apply", "--vault", str(self.vault), "--run", self.run_dir_of(result)])
        self.assertEqual(list((self.vault / "00 Inbox").iterdir()), [])

    def test_an_unknown_id_is_refused(self):
        with StubServer() as server:
            result = self.compose(server)
        with self.assertRaises(vcs.UserError):
            vcs.run(["apply", "--vault", str(self.vault), "--run", self.run_dir_of(result), "--accept", "n-999"])

    def test_the_provenance_block_is_written_in_code(self):
        """`0.04` requires it to be accurate about what made the note, and a model
        cannot be accurate about that."""
        with StubServer() as server:
            result = self.compose(server)
        run_dir = Path(self.run_dir_of(result))
        text = (run_dir / "proposed" / "n-001.md").read_text(encoding="utf-8")
        self.assertIn("> [!provenance]- How this note was made", text)
        self.assertIn("Composed by `vault-compose` (synthesis)", text)
        self.assertIn("`s-0001` Espresso Machine", text)

    def test_an_invented_name_holds_the_note(self):
        with StubServer() as server:
            server.script("draft", {"blocks": {"body": GOOD_BODY + ["The supplier list was short, and Priya found a cheaper one."]}})
            result = self.compose(server)
        record = result["data"]["proposals"][0]
        self.assertTrue(record["needs_review"])
        self.assertTrue(any("Priya" in line for line in record["review"]))

    def test_a_name_from_an_uncited_source_holds_the_note(self):
        """A block may not borrow a specific from a source it never cited."""
        with StubServer() as server:
            server.script(
                "outline",
                {"notes": [{"title": "Espresso Only", "blocks": [{"block": "body", "sourceIds": ["s-0001"]}]}]},
            )
            server.script("draft", {"blocks": {"body": ["The gasket leaks, and Marcus recommended a supplier."]}})
            result = self.compose(server)
        record = result["data"]["proposals"][0]
        self.assertTrue(any("Marcus" in line for line in record["review"]))

    def test_a_link_no_source_carries_holds_the_note(self):
        with StubServer() as server:
            server.script("draft", {"blocks": {"body": GOOD_BODY + ["See [[Some Other Note]] for the rest."]}})
            result = self.compose(server)
        record = result["data"]["proposals"][0]
        self.assertTrue(any("Some Other Note" in line for line in record["review"]))

    def test_a_drafter_writing_its_own_callout_holds_the_note(self):
        """Structure is added by the renderer once a block has passed its checks."""
        with StubServer() as server:
            server.script("draft", {"blocks": {"body": ["> [!summary]", "> A lead the drafter wrote."] + GOOD_BODY}})
            result = self.compose(server)
        record = result["data"]["proposals"][0]
        self.assertTrue(any("callout syntax" in line for line in record["review"]))

    def test_a_drafter_writing_its_own_title_holds_the_note(self):
        with StubServer() as server:
            server.script("draft", {"blocks": {"body": ["# My Own Title"] + GOOD_BODY}})
            result = self.compose(server)
        record = result["data"]["proposals"][0]
        self.assertTrue(any("level-one heading" in line for line in record["review"]))

    def test_a_held_note_is_not_written_even_when_accepted(self):
        with StubServer() as server:
            server.script("draft", {"blocks": {"body": GOOD_BODY + ["The supplier list was short, and Priya found a cheaper one."]}})
            result = self.compose(server)
        applied = vcs.run(
            ["apply", "--vault", str(self.vault), "--run", self.run_dir_of(result), "--accept", "n-001"]
        )
        self.assertEqual(applied["data"]["written"], [])
        self.assertTrue(any("held for review" in line for line in applied["warnings"]))

    def test_a_flagged_review_holds_the_note(self):
        with StubServer() as server:
            server.script("verify", {"verdicts": [{"id": "n-001", "verdict": "flag", "reason": "invented a claim"}]})
            result = self.compose(server)
        record = result["data"]["proposals"][0]
        self.assertTrue(record["needs_review"])
        self.assertTrue(any("invented a claim" in line for line in record["review"]))

    def test_an_unreachable_reviewer_is_not_approval(self):
        with StubServer() as server:
            result = self.compose(server, None, "--think-url", "http://127.0.0.1:9/v1/chat/completions")
        self.assertEqual(result["data"]["verification"]["reviewed"], 0)
        self.assertIn("skipped", result["data"]["verification"])
        self.assertTrue(any("not reviewed" in line for line in result["warnings"]))

    def test_a_block_the_vault_does_not_declare_is_dropped_not_fatal(self):
        with StubServer() as server:
            server.script(
                "outline",
                {
                    "notes": [
                        {
                            "title": "Espresso And Shelving",
                            "blocks": [{"block": "abstract", "sourceIds": []}, {"block": "body", "sourceIds": ["s-0001"]}],
                        }
                    ]
                },
            )
            result = self.compose(server)
        self.assertEqual(result["data"]["counts"]["proposed"], 1)
        self.assertTrue(any("does not declare" in line for line in result["warnings"]))

    def test_the_missing_domain_is_a_warning_not_a_hold(self):
        """Every composed note lacks `domain` by design; holding for it would hold
        every note this skill ever makes."""
        with StubServer() as server:
            result = self.compose(server)
        self.assertEqual(result["data"]["counts"]["held"], 0)
        self.assertTrue(any("vault-organizer to fill" in line for line in result["warnings"]))

    def test_a_conversation_records_its_channel(self):
        spec = self.write_spec(
            intent="conversation",
            sources=[
                {"kind": "chat", "label": "this conversation", "text": SOURCE_A},
                {"kind": "chat", "label": "this conversation", "text": SOURCE_B},
            ],
        )
        with StubServer() as server:
            result = self.compose(server, spec)
        run_dir = Path(self.run_dir_of(result))
        text = (run_dir / "proposed" / "n-001.md").read_text(encoding="utf-8")
        self.assertIn("capture_type: chat", text)

    def test_an_intent_refuses_a_source_kind_it_does_not_take(self):
        spec = self.write_spec(
            intent="synthesis",
            sources=[{"kind": "web-claim", "label": "a claim", "text": SOURCE_A, "url": "https://example.com"}],
        )
        with StubServer() as server:
            with self.assertRaises(vcs.UserError):
                self.compose(server, spec)

    def test_a_vault_with_no_block_order_is_told_so(self):
        (self.vault / "99 Meta" / "99.02 Schemas" / "0.04 Note Format.md").write_text(
            FORMAT.replace("### Block order", "### Something else"), encoding="utf-8"
        )
        with StubServer() as server:
            with self.assertRaises(vcs.UserError) as caught:
                self.compose(server)
        self.assertIn("Block order", str(caught.exception))

    def test_a_spec_with_no_sources_is_refused(self):
        spec = self.write_spec(sources=[])
        with StubServer() as server:
            with self.assertRaises(vcs.UserError):
                self.compose(server, spec)


if __name__ == "__main__":
    unittest.main(verbosity=2)

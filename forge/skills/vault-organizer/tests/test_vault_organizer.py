#!/usr/bin/env python3

import datetime
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "vault-organizer.py"
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("vault_organizer", SCRIPT)
vault_organizer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vault_organizer)

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
| `project` | no | registered quoted wikilink | Registered project. |
| `parent` | no | quoted wikilink | Parent hub. |
| `people` | no | list of quoted wikilinks | People. |
| `organization` | no | quoted wikilink | Organization. |
| `related` | no | list of quoted wikilinks | Related links. |
| `source_kind` | conditional | controlled scalar | Source kind. |
| `capture_type` | no | controlled scalar | Capture type. |
| `processed_by` | no | list | Automated workflows that transformed this note. |
| `date` | no | scalar, human-owned | Subject date. |
| `cover` | no | scalar, human-owned | Cover image. |
| `created` | yes | scalar, derived | Date this note came into existence. |

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

## Status values

- `raw` — Unprocessed.
- `active` — Active.

## Domains

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `personal` | `1` | `Personal` | Personal material. |
| `technology` | `4` | `Technology` | Technical work. |
| `administration` | `7` | `Administration` | Admin work. |
| `meta` | `99` | `Meta` | System notes. |

### Domain decision rules

- Choose the primary purpose.

## Subdomains

### personal

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `journal` | `1` | `Journal` | Dated records. |

### technology

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `software-development` | `2` | `Software Development` | Code projects. |
| `obsidian` | `3` | `Obsidian` | Vault tooling. |

### administration

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `health` | `1` | `Health` | Medical notes. |
| `housing` | `3` | `Housing` | Housing notes. |

### meta

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `schemas` | `2` | `Schemas` | Schema notes. |
| `maintenance` | `7` | `Maintenance` | Maintenance logs. |

## Project registry

| Approved value | Domain | Subdomain | Number | Definition |
| --- | --- | --- | --- | --- |
| `"[[Pi Forge]]"` | `technology` | `software-development` | `1` | Local agent harness. |
| `"[[RAPID]]"` | `technology` |  | `90` | Domain-root project. |

### Project assignment rules

- Assign only when direct.

## Source kinds

- `book` — Book.
- `manual` — Manual.

## Capture types

- `manual` — Typed.
- `chat` — Chat.
- `generated` — Made by a script, agent, or model.

## Non-routing topic hubs

- Local LLMs

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
| `domain: health` | `domain: administration` + `subdomain: health` |

## Dashboard rules

- Dashboards do not affect routing.
"""


# The same vault, opted in to filing sources by kind. Derived from SCHEMA rather
# than written out again, so the two cannot drift apart on anything but the one
# section under test.
SOURCES_SCHEMA = SCHEMA.replace(
    """## Source kinds

- `book` — Book.
- `manual` — Manual.
""",
    """## Sources root

| Number | Label | Definition |
| --- | --- | --- |
| `10` | `Sources` | External source notes, filed by source kind. |

## Source kinds

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `book` | `1` | `Book` | Book. |
| `article` | `2` | `Article` | Article. |
| `transcript` | `3` | `Transcript` | Transcript. |
| `manual` | `7` | `Manual` | Manual. |
""",
)


def with_other_vaults(
    schema_text,
    inbox,
    name="Work",
    scope="Professional employment and work-project content not tied to research.",
):
    """Return SCHEMA with an ``## Other vaults`` row declaring one sibling vault."""
    section = (
        "## Other vaults\n\n"
        "| Name | Scope | Inbox path |\n"
        "| --- | --- | --- |\n"
        f"| `{name}` | {scope} | `{inbox}` |\n\n"
    )
    return schema_text.replace("## Dashboard rules", section + "## Dashboard rules")


class StubChatHandler(BaseHTTPRequestHandler):
    responses = []
    requests = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        self.__class__.requests.append(payload)
        response = self.__class__.responses.pop(0) if self.__class__.responses else {
            "metadata": {
                "type": "note",
                "status": "active",
                "domain": "technology",
                "subdomain": "obsidian",
                "project": None,
                "parent": None,
                "people": [],
                "organization": None,
                "related": [],
                "source_kind": None,
                "capture_type": "manual",
            },
            "needs_review": False,
            "review_reason": None,
        }
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


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        return


class StubServer:
    def __init__(self, responses, handler_cls=StubChatHandler):
        self.responses = list(responses)
        self.handler_cls = handler_cls

    def __enter__(self):
        self.handler_cls.responses = list(self.responses)
        self.handler_cls.requests = []
        self.server = QuietServer(("127.0.0.1", 0), self.handler_cls)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.url = f"http://{host}:{port}/v1/chat/completions"
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    @property
    def requests(self):
        return self.handler_cls.requests


class BlockingChatHandler(StubChatHandler):
    block_after = 1
    release = None

    def do_POST(self):
        if len(self.__class__.requests) >= self.__class__.block_after and self.__class__.release:
            self.__class__.release.wait(30)
        super().do_POST()


class StubEmbeddingsHandler(BaseHTTPRequestHandler):
    rules = []
    requests = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        self.__class__.requests.append(payload)
        data = []
        for index, text in enumerate(payload["input"]):
            vector = None
            for marker, ruled in self.__class__.rules:
                if marker in text:
                    vector = ruled
                    break
            if vector is None:
                slot = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16) % 512
                vector = [1.0 if position == slot else 0.0 for position in range(512)]
            data.append({"index": index, "embedding": vector})
        body = json.dumps({"data": data}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


class StubEmbeddingsServer:
    def __init__(self, rules=None):
        self.rules = list(rules or [])

    def __enter__(self):
        StubEmbeddingsHandler.rules = list(self.rules)
        StubEmbeddingsHandler.requests = []
        self.server = QuietServer(("127.0.0.1", 0), StubEmbeddingsHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.url = f"http://{host}:{port}/v1/embeddings"
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    @property
    def requests(self):
        return StubEmbeddingsHandler.requests


def run_script(*args, environment=None):
    # Point the agent directory at nothing by default so endpoint resolution
    # cannot pick up the settings of whoever is running the tests. An explicit
    # environment replaces the inherited one outright, so a test can prove what
    # happens in the *absence* of a variable.
    base = environment if environment is not None else os.environ
    env = {**base, "PYTHONDONTWRITEBYTECODE": "1"}
    env.setdefault("PI_FORGE_AGENT_DIR", "/nonexistent-agent-directory")
    # Verification talks to a second endpoint. Tests that are not about it opt
    # out, so they neither reach a real server nor consume stub responses.
    arguments = list(args)
    if arguments and arguments[0] in {"inbox", "vault"} and not {"--no-verify", "--think-url"} & set(arguments):
        arguments.append("--no-verify")
    return subprocess.run([sys.executable, str(SCRIPT), *arguments], capture_output=True, text=True, env=env)


class VaultOrganizerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.vault = self.root / "vault"
        (self.vault / "99 Meta").mkdir(parents=True)
        (self.vault / "00 Inbox").mkdir()
        (self.vault / "99 Meta" / "0.00 Vault Schema.md").write_text(SCHEMA, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def schema(self, text=SCHEMA):
        return vault_organizer.parse_schema_note(text)

    def test_schema_parses_and_derives_paths(self):
        schema = self.schema()
        self.assertEqual(schema["property_order"][0:3], ["type", "status", "domain"])
        destination = vault_organizer.compile_destination(
            schema,
            {"type": "note", "status": "active", "domain": "technology", "subdomain": "obsidian"},
        )
        self.assertEqual(destination.as_posix(), "04 Technology/4.03 Obsidian")

    def test_other_vaults_section_parses_into_the_schema(self):
        schema = self.schema(with_other_vaults(SCHEMA, "/Users/x/Obsidian/Work/00 Inbox"))
        self.assertIn("Work", schema["other_vaults"])
        self.assertEqual(schema["other_vaults"]["Work"]["inbox"], "/Users/x/Obsidian/Work/00 Inbox")
        self.assertIn("Professional", schema["other_vaults"]["Work"]["scope"])
        self.assertTrue(vault_organizer.other_vaults_enabled(schema))

    def test_other_vaults_absent_leaves_routing_off(self):
        schema = self.schema()
        self.assertEqual(schema["other_vaults"], {})
        self.assertFalse(vault_organizer.other_vaults_enabled(schema))

    def test_other_vaults_rejects_a_relative_inbox_path(self):
        with self.assertRaises(vault_organizer.UserError):
            self.schema(with_other_vaults(SCHEMA, "Work/00 Inbox"))

    def test_safe_title_is_idempotent_and_keeps_names_readable(self):
        # Brackets and pipes have readable equivalents; the rest are dropped.
        self.assertEqual(vault_organizer.safe_title("Notes [Draft] | v2"), "Notes (Draft) - v2")
        self.assertEqual(vault_organizer.safe_title("VPP Insiders #4: Open Discussion"), "VPP Insiders 4 Open Discussion")
        # Idempotence is what lets safe_title double as the validator.
        for value in ("Notes [Draft] | v2", "A # B", "^caret^", "already safe"):
            once = vault_organizer.safe_title(value)
            self.assertEqual(vault_organizer.safe_title(once), once, value)

    def test_unsafe_filename_reason_names_the_offending_characters(self):
        reason = vault_organizer.unsafe_filename_reason("Meeting #3 [draft].md")
        self.assertIn("#", reason)
        self.assertIn("[", reason)
        self.assertIn("wikilinks and mobile sync", reason)
        self.assertIsNone(vault_organizer.unsafe_filename_reason("2026-07-24 - Memo - Groceries.md"))

    def test_filing_repairs_names_that_break_wikilinks(self):
        record = {}
        warnings = []
        filed = vault_organizer.filing_name(record, "VPP Insiders #4: Open Discussion.md", warnings)
        self.assertEqual(filed, "VPP Insiders 4 Open Discussion.md")
        self.assertTrue(record["filename_repaired"])
        self.assertEqual(record["original_name"], "VPP Insiders #4: Open Discussion.md")
        self.assertTrue(any("wikilinks and mobile sync" in warning for warning in warnings))

    def test_filing_preserves_a_safe_name_exactly(self):
        record = {}
        warnings = []
        filed = vault_organizer.filing_name(record, "2026-07-24 - Therapy - Family Dynamics.md", warnings)
        self.assertEqual(filed, "2026-07-24 - Therapy - Family Dynamics.md")
        self.assertNotIn("filename_repaired", record)
        self.assertEqual(warnings, [])

    def test_filing_holds_a_name_that_cannot_be_repaired(self):
        record = {}
        warnings = []
        filed = vault_organizer.filing_name(record, "#.md", warnings)
        self.assertEqual(filed, "#.md")
        self.assertTrue(record["needs_review"])
        self.assertIn("cannot be repaired", warnings[0])

    def test_project_inheritance_and_domain_project_path(self):
        schema = self.schema()
        metadata, warnings = vault_organizer.normalize_metadata(
            {"type": "note", "status": "active", "domain": "personal", "subdomain": "journal", "project": "[[Pi Forge]]"},
            schema,
        )
        self.assertEqual(metadata["domain"], "technology")
        self.assertEqual(metadata["subdomain"], "software-development")
        self.assertTrue(warnings)
        self.assertEqual(
            vault_organizer.compile_destination(schema, metadata).as_posix(),
            "04 Technology/4.02 Software Development/4.02.01 Pi Forge",
        )
        self.assertEqual(
            vault_organizer.compile_destination(
                schema,
                {"type": "note", "status": "active", "domain": "technology", "project": "[[RAPID]]"},
            ).as_posix(),
            "04 Technology/4.90 RAPID",
        )

    def test_duplicate_domain_number_fails_closed(self):
        text = SCHEMA.replace("| `technology` | `4` |", "| `technology` | `1` |")
        with self.assertRaisesRegex(vault_organizer.UserError, "duplicate"):
            self.schema(text)

    def test_legacy_normalization(self):
        metadata, warnings = vault_organizer.normalize_metadata(
            {"type": "daily", "status": "active", "domain": "health"},
            self.schema(),
        )
        self.assertEqual(metadata["type"], "journal")
        self.assertEqual(metadata["domain"], "administration")
        self.assertEqual(metadata["subdomain"], "health")
        self.assertEqual(len(warnings), 2)

    def test_yaml_serialization_lists_and_quotes(self):
        text = vault_organizer.serialize_frontmatter(
            {
                "type": "note",
                "status": "active",
                "domain": "technology",
                "project": "[[Pi Forge]]",
                "people": ["[[Ellie Eorwyn]]"],
                "related": ["[[Buddhism]]", "[[UC Davis]]"],
                "capture_type": "chat",
            },
            self.schema(),
        )
        self.assertIn('project: "[[Pi Forge]]"', text)
        self.assertIn('people:\n  - "[[Ellie Eorwyn]]"', text)
        self.assertIn('related:\n  - "[[Buddhism]]"\n  - "[[UC Davis]]"', text)
        self.assertNotIn("[]", text)
        with self.assertRaises(vault_organizer.UserError):
            vault_organizer.serialize_frontmatter(
                {"type": "note", "status": "active", "domain": "technology", "parent": "[[Bad\nLink]]"},
                self.schema(),
            )

    def test_frontmatter_split_and_body_preservation(self):
        data = b"---\ntype: old\nbad: value\n---\n# Title\n\nBody\n"
        split = vault_organizer.split_frontmatter(data)
        self.assertFalse(split["malformed"])
        self.assertEqual(split["body"], "# Title\n\nBody\n")
        revised = vault_organizer.revised_note_text(
            {"type": "note", "status": "active", "domain": "technology"},
            self.schema(),
            split["body"],
        )
        self.assertTrue(revised.endswith("# Title\n\nBody\n"))
        malformed = vault_organizer.split_frontmatter(b"---\ntype: old\n# Title\n")
        self.assertTrue(malformed["malformed"])

    def test_selection_excludes_schema_hidden_and_symlinks(self):
        (self.vault / "00 Inbox" / "a.md").write_text("A", encoding="utf-8")
        (self.vault / ".hidden").mkdir()
        (self.vault / ".hidden" / "b.md").write_text("B", encoding="utf-8")
        (self.vault / "node_modules").mkdir()
        (self.vault / "node_modules" / "c.md").write_text("C", encoding="utf-8")
        target = self.vault / "00 Inbox" / "a.md"
        os.symlink(target, self.vault / "link.md")
        selected = [path.relative_to(self.vault).as_posix() for path in vault_organizer.selected_notes(self.vault, self.vault / "99 Meta" / "0.00 Vault Schema.md", "vault", None)]
        self.assertEqual(selected, ["00 Inbox/a.md"])

    def test_dry_run_no_mutation_and_cache_reuse(self):
        note = self.vault / "00 Inbox" / "Note.md"
        original = "---\ntype: old\n---\n# Note\n\nPi Forge body.\n"
        note.write_text(original, encoding="utf-8")
        response = {
            "metadata": {
                "type": "note",
                "status": "active",
                "domain": "technology",
                "subdomain": "obsidian",
                "project": None,
                "parent": None,
                "people": [],
                "organization": None,
                "related": [],
                "source_kind": None,
                "capture_type": "manual",
            },
            "needs_review": False,
            "review_reason": None,
        }
        with StubServer([response]) as server:
            result = run_script("inbox", "--vault", str(self.vault), "--base-url", server.url)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["data"]["counts"]["classified"], 1)
            self.assertEqual(note.read_text(encoding="utf-8"), original)
            result2 = run_script("inbox", "--vault", str(self.vault), "--base-url", server.url)
            self.assertEqual(result2.returncode, 0, result2.stderr + result2.stdout)
            payload2 = json.loads(result2.stdout)
            self.assertEqual(payload2["data"]["counts"]["cached"], 1)
            self.assertEqual(len(server.requests), 1)

    def test_repair_attempt_and_apply_backup(self):
        note = self.vault / "00 Inbox" / "Repair.md"
        note.write_text("# Repair\n\nBody\n", encoding="utf-8")
        bad = {
            "metadata": {"type": "note", "status": "active", "domain": "nope"},
            "needs_review": False,
            "review_reason": None,
        }
        fixed = {
            "metadata": {
                "type": "note",
                "status": "active",
                "domain": "technology",
                "subdomain": "obsidian",
                "project": None,
                "parent": None,
                "people": [],
                "organization": None,
                "related": [],
                "source_kind": None,
                "capture_type": "manual",
            },
            "needs_review": False,
            "review_reason": None,
        }
        with StubServer([bad, fixed]) as server:
            result = run_script("inbox", "--vault", str(self.vault), "--base-url", server.url, "--apply")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["data"]["counts"]["applied"], 1)
        destination = self.vault / "04 Technology" / "4.03 Obsidian" / "Repair.md"
        self.assertTrue(destination.is_file())
        self.assertIn("type: note", destination.read_text(encoding="utf-8"))
        backup = Path(payload["data"]["run_directory"]) / "backup" / "00 Inbox" / "Repair.md"
        self.assertEqual(backup.read_text(encoding="utf-8"), "# Repair\n\nBody\n")
        self.assertGreaterEqual(len(server.requests), 2)

    def test_apply_refuses_destination_collision(self):
        (self.vault / "00 Inbox" / "Collision.md").write_text("# Collision\n", encoding="utf-8")
        destination_dir = self.vault / "04 Technology" / "4.03 Obsidian"
        destination_dir.mkdir(parents=True)
        (destination_dir / "Collision.md").write_text("existing\n", encoding="utf-8")
        with StubServer([]) as server:
            result = run_script("inbox", "--vault", str(self.vault), "--base-url", server.url, "--apply")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertGreaterEqual(payload["data"]["counts"]["skipped"], 1)
        self.assertEqual((destination_dir / "Collision.md").read_text(encoding="utf-8"), "existing\n")
        self.assertTrue((self.vault / "00 Inbox" / "Collision.md").exists())


def ok_response(**overrides):
    response = {
        "metadata": {
            "type": "note",
            "status": "active",
            "domain": "technology",
            "subdomain": "obsidian",
            "project": None,
            "parent": None,
            "people": [],
            "organization": None,
            "related": [],
            "source_kind": None,
            "capture_type": "manual",
        },
        "needs_review": False,
        "review_reason": None,
    }
    response.update(overrides)
    return response


class VerdictHandler(SecondStubChatHandler):
    """The thinking endpoint.

    Verification requests are answered from `flags` (a note path -> objection
    map), so tests do not have to predict note ordering. Re-classification
    requests are answered from `responses`.
    """

    flags = {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        self.__class__.requests.append(payload)
        sent = json.loads(payload["messages"][-1]["content"])
        if "items" in sent:
            verdicts = [
                {
                    "id": item["id"],
                    "verdict": "flag" if self.__class__.flags.get(item["id"]) else "ok",
                    "reason": self.__class__.flags.get(item["id"], ""),
                }
                for item in sent["items"]
            ]
            content = json.dumps({"verdicts": verdicts})
        else:
            content = self.__class__.responses.pop(0) if self.__class__.responses else json.dumps(ok_response())
            if not isinstance(content, str):
                content = json.dumps(content)
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class VaultOrganizerV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.vault = self.root / "vault"
        (self.vault / "99 Meta").mkdir(parents=True)
        (self.vault / "00 Inbox").mkdir()
        (self.vault / "99 Meta" / "0.00 Vault Schema.md").write_text(SCHEMA, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def write_note(self, relative, text):
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def run_ok(self, *args, environment=None):
        result = run_script(*args, environment=environment)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads(result.stdout)

    def test_prompt_cache_structure_and_advisory_frontmatter(self):
        self.write_note("00 Inbox/First.md", "---\nold: value\n---\n# First\n\nBody one.\n")
        self.write_note("00 Inbox/Second.md", "# Second\n\nBody two.\n")
        with StubServer([ok_response(), ok_response()]) as server:
            self.run_ok("inbox", "--vault", str(self.vault), "--base-url", server.url, "--no-embeddings")
            self.assertEqual(len(server.requests), 2)
            for request in server.requests:
                self.assertIs(request.get("cache_prompt"), True)
                self.assertEqual(request["messages"][0]["role"], "system")
                self.assertEqual(request["messages"][-1]["role"], "user")
            first_system = server.requests[0]["messages"][0]["content"]
            second_system = server.requests[1]["messages"][0]["content"]
            self.assertEqual(first_system, second_system)
            self.assertIn('"domains"', first_system)
            payloads = [json.loads(request["messages"][1]["content"]) for request in server.requests]
            by_title = {payload["title"]: payload for payload in payloads}
            self.assertIn("old: value", by_title["First"]["untrusted_existing_frontmatter"])
            self.assertEqual(by_title["Second"]["untrusted_existing_frontmatter"], "")

    def test_think_prefill_flag_adds_assistant_turn(self):
        self.write_note("00 Inbox/Prefill.md", "# Prefill\n\nBody.\n")
        with StubServer([ok_response()]) as server:
            self.run_ok("inbox", "--vault", str(self.vault), "--base-url", server.url, "--no-embeddings", "--think-prefill")
            prefill = server.requests[0]["messages"][-1]
            self.assertEqual(prefill["role"], "assistant")
            self.assertIn("<think>", prefill["content"])

    def record_for(self, result, source):
        plan = json.loads((Path(result["data"]["run_directory"]) / "plan.json").read_text(encoding="utf-8"))
        return next(row for row in plan["records"] if row["source"] == source)

    def classify_one(self, note_text, response, relative="00 Inbox/Marked.md"):
        self.write_note(relative, note_text)
        with StubServer([response]) as server:
            result = self.run_ok("inbox", "--vault", str(self.vault), "--base-url", server.url, "--no-embeddings")
        return self.record_for(result, relative)

    def test_machine_provenance_survives_filing(self):
        # Filing rewrites frontmatter from the model's answer. A note that was
        # generated, or cleaned by another skill, must not be able to lose that
        # fact just because a classifier read it as ordinary prose.
        record = self.classify_one(
            '---\ntype: note\nstatus: raw\ncapture_type: generated\nprocessed_by:\n  - "vault-transcripts"\n---\n'
            "# Marked\n\nBody.\n",
            ok_response(),
        )
        self.assertEqual(record["metadata"]["capture_type"], "generated")
        self.assertEqual(record["metadata"]["processed_by"], ["vault-transcripts"])
        self.assertIn("kept capture_type: generated (classified as manual)", record["warnings"])

    def test_the_classifier_does_not_get_to_write_processed_by(self):
        record = self.classify_one(
            "# Plain\n\nA note nobody has processed.\n",
            ok_response(metadata=dict(ok_response()["metadata"], processed_by=["vault-organizer"])),
            relative="00 Inbox/Plain.md",
        )
        self.assertNotIn("processed_by", record["metadata"])

    def test_provenance_is_dropped_loudly_when_the_schema_lacks_the_property(self):
        (self.vault / "99 Meta" / "0.00 Vault Schema.md").write_text(
            SCHEMA.replace("| `processed_by` | no | list | Automated workflows that transformed this note. |\n", ""),
            encoding="utf-8",
        )
        record = self.classify_one(
            '---\ntype: note\nstatus: raw\nprocessed_by:\n  - "vault-transcripts"\n---\n# Marked\n\nBody.\n',
            ok_response(),
        )
        self.assertNotIn("processed_by", record["metadata"])
        self.assertIn("previous processed_by dropped: schema does not define it as a list property", record["warnings"])

    def verify_run(self, chat_responses, flags=None, escalations=None, *extra):
        """Classification and verification against two separate stub endpoints."""
        VerdictHandler.flags = flags or {}
        with StubServer(chat_responses) as chat, StubServer(escalations or [], handler_cls=VerdictHandler) as think:
            result = self.run_ok(
                "inbox", "--vault", str(self.vault), "--base-url", chat.url,
                "--think-url", think.url, "--no-embeddings", *extra,
            )
            return result, chat, think

    def test_verification_reviews_every_classification_in_one_call(self):
        for index in range(3):
            self.write_note(f"00 Inbox/Note {index}.md", f"# Note {index}\n\nBody {index}.\n")
        result, _chat, think = self.verify_run([ok_response(), ok_response(), ok_response()])
        self.assertEqual(len(think.requests), 1, "three notes reviewed in one thinking call")
        reviewed = json.loads(think.requests[0]["messages"][-1]["content"])["items"]
        self.assertEqual(len(reviewed), 3)
        self.assertIn("proposedDestination", reviewed[0])
        run = Path(result["data"]["run_directory"])
        self.assertIn("Reviewed by the thinking model: 3", (run / "report.md").read_text(encoding="utf-8"))

    def test_a_flagged_note_is_redone_with_reasoning_and_the_new_answer_wins(self):
        self.write_note("00 Inbox/Misfiled.md", "# Misfiled\n\nBody.\n")
        corrected = ok_response(metadata={**ok_response()["metadata"], "subdomain": "software-development"})
        result, _chat, think = self.verify_run(
            [ok_response()],
            flags={"00 Inbox/Misfiled.md": "filed under the wrong subdomain"},
            escalations=[corrected],
        )
        record = self.record_for(result, "00 Inbox/Misfiled.md")
        self.assertEqual(record["destination"], "04 Technology/4.02 Software Development/Misfiled.md")
        self.assertEqual(record["classification_source"], "model-think")
        # The escalation is asked to reconsider, and told what the objection was.
        repair = json.loads(think.requests[-1]["messages"][-1]["content"])["repair"]
        self.assertIn("wrong subdomain", repair["reviewer_objection"])
        report = (Path(result["data"]["run_directory"]) / "report.md").read_text(encoding="utf-8")
        self.assertIn("re-done with reasoning", report)

    def test_a_flagged_note_that_cannot_be_redone_goes_to_a_human(self):
        self.write_note("00 Inbox/Broken.md", "# Broken\n\nBody.\n")
        unusable = ok_response(metadata={"type": "note", "domain": "not-a-real-domain"})
        result, _chat, _think = self.verify_run(
            [ok_response()],
            flags={"00 Inbox/Broken.md": "wrong domain"},
            escalations=[unusable, unusable],
        )
        record = self.record_for(result, "00 Inbox/Broken.md")
        self.assertTrue(record["needs_review"])
        self.assertIn("re-classification failed", record["review_reason"])

    def test_an_escalated_classification_is_cached_and_survives_no_verify(self):
        # Regression: the verify pass used to write the escalated answer only to the
        # run journal, never back to classifications.json. A later --no-verify rebuild
        # then read the stale pre-escalation entry and silently reverted the note.
        self.write_note("00 Inbox/Misfiled.md", "# Misfiled\n\nBody.\n")
        corrected = ok_response(metadata={**ok_response()["metadata"], "subdomain": "software-development"})
        escalated_destination = "04 Technology/4.02 Software Development/Misfiled.md"
        VerdictHandler.flags = {"00 Inbox/Misfiled.md": "filed under the wrong subdomain"}
        # One chat endpoint for both runs: the base URL is part of the cache key, so a
        # fresh port would miss the warm cache and re-classify from scratch.
        with StubServer([ok_response()]) as chat, StubServer([corrected], handler_cls=VerdictHandler) as think:
            common = ("inbox", "--vault", str(self.vault), "--base-url", chat.url, "--no-embeddings")
            verified = self.run_ok(*common, "--think-url", think.url)
            self.assertEqual(
                self.record_for(verified, "00 Inbox/Misfiled.md")["destination"], escalated_destination
            )
            chat_calls, think_calls = len(chat.requests), len(think.requests)

            # A fast rebuild off the warm cache, verification disabled.
            rebuilt = self.run_ok(*common, "--no-verify")
        record = self.record_for(rebuilt, "00 Inbox/Misfiled.md")
        self.assertEqual(record["destination"], escalated_destination, "escalated destination must survive")
        self.assertEqual(record["classification_source"], "cache", "served from cache, not re-classified")
        # (b) the escalation is reused, not recomputed: neither model is called again.
        self.assertEqual(len(chat.requests), chat_calls)
        self.assertEqual(len(think.requests), think_calls)

    def test_a_failed_escalation_keeps_the_note_in_review_on_no_verify(self):
        # The reviewer objected and no valid replacement was produced, so the note is
        # for a human. The cache must reflect that; otherwise a --no-verify rebuild reads
        # the stale confident entry and files the objected-to destination anyway.
        self.write_note("00 Inbox/Rejected.md", "# Rejected\n\nBody.\n")
        unusable = ok_response(metadata={"type": "note", "domain": "not-a-real-domain"})
        VerdictHandler.flags = {"00 Inbox/Rejected.md": "wrong domain"}
        with StubServer([ok_response()]) as chat, StubServer([unusable, unusable], handler_cls=VerdictHandler) as think:
            common = ("inbox", "--vault", str(self.vault), "--base-url", chat.url, "--no-embeddings")
            verified = self.run_ok(*common, "--think-url", think.url)
            self.assertTrue(self.record_for(verified, "00 Inbox/Rejected.md")["needs_review"])
            chat_calls = len(chat.requests)

            rebuilt = self.run_ok(*common, "--no-verify")
        record = self.record_for(rebuilt, "00 Inbox/Rejected.md")
        self.assertTrue(record["needs_review"], "the review flag must survive a cache rebuild")
        self.assertEqual(record["classification_source"], "cache")
        self.assertEqual(len(chat.requests), chat_calls, "no re-classification on the warm cache")

    def test_an_unreachable_verifier_does_not_read_as_approval(self):
        self.write_note("00 Inbox/Unverified.md", "# Unverified\n\nBody.\n")
        with StubServer([ok_response()]) as chat:
            result = self.run_ok(
                "inbox",
                "--vault",
                str(self.vault),
                "--base-url",
                chat.url,
                "--think-url",
                "http://127.0.0.1:9/v1/chat/completions",
                "--no-embeddings",
            )
        report = (Path(result["data"]["run_directory"]) / "report.md").read_text(encoding="utf-8")
        self.assertIn("**Not verified**", report)

    def test_verification_is_skippable_and_says_so(self):
        self.write_note("00 Inbox/Skipped.md", "# Skipped\n\nBody.\n")
        with StubServer([ok_response()]) as chat:
            result = self.run_ok("inbox", "--vault", str(self.vault), "--base-url", chat.url, "--no-embeddings", "--no-verify")
        report = (Path(result["data"]["run_directory"]) / "report.md").read_text(encoding="utf-8")
        self.assertIn("were not reviewed", report)

    def test_a_resumed_run_does_not_re_verify(self):
        self.write_note("00 Inbox/Resumed.md", "# Resumed\n\nBody.\n")
        VerdictHandler.flags = {}
        # One pair of endpoints for both runs: resuming refuses a changed URL.
        with StubServer([ok_response()]) as chat, StubServer([], handler_cls=VerdictHandler) as think:
            arguments = ("--vault", str(self.vault), "--base-url", chat.url, "--think-url", think.url, "--no-embeddings")
            result = self.run_ok("inbox", *arguments)
            self.assertEqual(len(think.requests), 1)

            self.run_ok("inbox", *arguments, "--run", result["data"]["run_directory"])
            self.assertEqual(len(think.requests), 1, "verdicts were journaled, so nothing is reviewed twice")

    def test_classification_requests_the_non_thinking_model_by_default(self):
        self.write_note("00 Inbox/Default.md", "# Default\n\nBody.\n")
        with StubServer([ok_response()]) as server:
            self.run_ok("inbox", "--vault", str(self.vault), "--base-url", server.url, "--no-embeddings")
            self.assertEqual(server.requests[0]["model"], "chat")
            self.assertNotEqual(server.requests[0]["messages"][-1]["role"], "assistant")

    def test_endpoint_falls_back_to_the_configured_chat_service(self):
        with StubServer([ok_response()]) as server:
            agent = self.root / "agent"
            agent.mkdir(exist_ok=True)
            (agent / "settings.json").write_text(
                json.dumps({"connectedServices": {"chat": {"baseUrl": server.url, "model": "configured"}}}),
                encoding="utf-8",
            )
            # Environment beats settings, so clear any inherited endpoint to
            # exercise the settings layer itself.
            environment = {
                key: value
                for key, value in os.environ.items()
                if key not in {"FORGE_BASE_CHAT_URL", "FORGE_CHAT_URL", "FORGE_BASE_MODEL"}
            }
            environment["PI_FORGE_AGENT_DIR"] = str(agent)
            self.write_note("00 Inbox/Configured.md", "# Configured\n\nBody.\n")
            self.run_ok("inbox", "--vault", str(self.vault), "--no-embeddings", environment=environment)
            self.assertEqual(server.requests[0]["model"], "configured")

    def test_extract_json_content_strips_think_and_fences(self):
        wrapped = "<think>\n\nreasoning here\n</think>\n\n{\"ok\": true}"
        self.assertEqual(json.loads(vault_organizer.extract_json_content(wrapped)), {"ok": True})
        prefilled = "<think>\n\n</think>\n\n```json\n{\"ok\": true}\n```"
        self.assertEqual(json.loads(vault_organizer.extract_json_content(prefilled)), {"ok": True})
        plain = "{\"ok\": true}"
        self.assertEqual(json.loads(vault_organizer.extract_json_content(plain)), {"ok": True})

    def test_exact_dupes_quarantined_with_one_llm_call(self):
        body = "# Duplicate\n\nShared body content that is identical.\n"
        self.write_note("Sources/Dup.md", "---\ntype: old\ncreated: 2024\nextra: rich\n---\n" + body)
        self.write_note("05 Sources/Dup.md", "---\ntype: old\n---\n" + body)
        with StubServer([ok_response()]) as server:
            payload = self.run_ok("vault", "--vault", str(self.vault), "--base-url", server.url, "--no-embeddings", "--apply")
        counts = payload["data"]["counts"]
        self.assertEqual(counts["duplicates_exact"], 1)
        self.assertEqual(counts["quarantined"], 1)
        self.assertEqual(len(server.requests), 1)
        quarantined = self.vault / ".vault-organizer" / "duplicates" / "05 Sources" / "Dup.md"
        self.assertTrue(quarantined.is_file())
        self.assertEqual(quarantined.read_text(encoding="utf-8"), "---\ntype: old\n---\n" + body)
        self.assertFalse((self.vault / "05 Sources" / "Dup.md").exists())
        self.assertTrue((self.vault / "04 Technology" / "4.03 Obsidian" / "Dup.md").is_file())
        plan = json.loads((Path(payload["data"]["run_directory"]) / "plan.json").read_text(encoding="utf-8"))
        groups = plan["dedupe"]["groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["winner"], "Sources/Dup.md")
        self.assertEqual(groups[0]["losers"][0]["path"], "05 Sources/Dup.md")

    def test_near_dupe_auto_and_review_band(self):
        shared = [f"Shared research line number {index} with substantive content." for index in range(1, 13)]
        long_body = "\n".join(shared + ["Marker VECA1 anchor line.", "Additional provenance line one.", "Additional provenance line two."]) + "\n"
        short_body = "\n".join(shared + ["Marker VECA2 anchor line."]) + "\n"
        self.write_note("Research/Report.md", long_body)
        self.write_note("05 Research/Report.md", short_body)
        concept_a = "# Concept\n\n" + "\n".join(f"Idea exploration line {index} alpha." for index in range(1, 13)) + "\nMarker VECB1 anchor.\n"
        concept_b = "# Concept\n\n" + "\n".join(f"Concept sketch line {index} beta." for index in range(1, 13)) + "\nMarker VECB2 anchor.\n"
        self.write_note("Ideas/Concept A.md", concept_a)
        self.write_note("Old/Concept B.md", concept_b)
        rules = [
            ("VECA1", [1.0, 0.0, 0.0, 0.0]),
            ("VECA2", [0.98, 0.199, 0.0, 0.0]),
            ("VECB1", [0.0, 0.0, 1.0, 0.0]),
            ("VECB2", [0.0, 0.0, 0.93, 0.3676]),
        ]
        with StubServer([ok_response(), ok_response(), ok_response()]) as server, StubEmbeddingsServer(rules) as embeddings:
            payload = self.run_ok(
                "vault", "--vault", str(self.vault), "--base-url", server.url,
                "--embeddings-url", embeddings.url, "--apply",
            )
            self.assertEqual(len(server.requests), 3)
            self.assertGreaterEqual(len(embeddings.requests), 1)
        counts = payload["data"]["counts"]
        self.assertEqual(counts["duplicates_near"], 1)
        self.assertEqual(counts["duplicate_review"], 1)
        self.assertTrue((self.vault / ".vault-organizer" / "duplicates" / "05 Research" / "Report.md").is_file())
        self.assertTrue((self.vault / "04 Technology" / "4.03 Obsidian" / "Report.md").is_file())
        plan = json.loads((Path(payload["data"]["run_directory"]) / "plan.json").read_text(encoding="utf-8"))
        near_groups = [group for group in plan["dedupe"]["groups"] if group["kind"] == "near"]
        self.assertEqual(len(near_groups), 1)
        self.assertEqual(near_groups[0]["winner"], "Research/Report.md")
        review_pair = plan["dedupe"]["review_pairs"][0]
        self.assertEqual({review_pair["a"], review_pair["b"]}, {"Ideas/Concept A.md", "Old/Concept B.md"})
        report = (Path(payload["data"]["run_directory"]) / "report.md").read_text(encoding="utf-8")
        self.assertIn("## Duplicate Review", report)
        self.assertIn("Concept A.md", report)

    def test_inbox_dedupe_against_vault_index_zero_llm(self):
        filed_body = "# Filed\n\nAlready organized content lives here.\n"
        self.write_note("04 Technology/4.03 Obsidian/Filed.md", "---\ntype: note\n---\n" + filed_body)
        self.write_note("00 Inbox/Filed copy.md", filed_body)
        with StubServer([]) as server:
            payload = self.run_ok("inbox", "--vault", str(self.vault), "--base-url", server.url, "--no-embeddings", "--apply")
            self.assertEqual(len(server.requests), 0)
        counts = payload["data"]["counts"]
        self.assertEqual(counts["duplicates_exact"], 1)
        self.assertEqual(counts["quarantined"], 1)
        self.assertTrue((self.vault / ".vault-organizer" / "duplicates" / "00 Inbox" / "Filed copy.md").is_file())
        self.assertFalse((self.vault / "00 Inbox" / "Filed copy.md").exists())
        self.assertTrue((self.vault / "04 Technology" / "4.03 Obsidian" / "Filed.md").is_file())
        plan = json.loads((Path(payload["data"]["run_directory"]) / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["dedupe"]["groups"][0]["winner"], "04 Technology/4.03 Obsidian/Filed.md")

    def test_vault_mode_unresolved_moves_to_inbox_untouched(self):
        original = "---\nweird: junk\n---\nMystery body that resists classification.\n"
        self.write_note("Random/Mystery.md", original)
        with StubServer([ok_response(needs_review=True, review_reason="ambiguous domain")]) as server:
            payload = self.run_ok("vault", "--vault", str(self.vault), "--base-url", server.url, "--no-embeddings", "--apply")
        counts = payload["data"]["counts"]
        self.assertEqual(counts["moved_to_inbox"], 1)
        self.assertEqual(counts["review_required"], 1)
        moved = self.vault / "00 Inbox" / "Mystery.md"
        self.assertTrue(moved.is_file())
        self.assertEqual(moved.read_text(encoding="utf-8"), original)
        self.assertFalse((self.vault / "Random" / "Mystery.md").exists())
        review_queue = (Path(payload["data"]["run_directory"]) / "review-queue.jsonl").read_text(encoding="utf-8")
        self.assertIn("ambiguous domain", review_queue)

    def test_empty_body_skips_llm_and_stays_in_inbox(self):
        self.write_note("00 Inbox/Untitled.md", "")
        with StubServer([]) as server:
            payload = self.run_ok("inbox", "--vault", str(self.vault), "--base-url", server.url, "--no-embeddings", "--apply")
            self.assertEqual(len(server.requests), 0)
        counts = payload["data"]["counts"]
        self.assertEqual(counts["empty"], 1)
        self.assertEqual(counts["review_required"], 1)
        self.assertTrue((self.vault / "00 Inbox" / "Untitled.md").is_file())

    def test_quarantine_collision_suffix(self):
        body = "# Same\n\nIdentical content in both copies.\n"
        self.write_note("A/Same.md", "---\ntype: old\nrich: extra\n---\n" + body)
        self.write_note("B/Same.md", body)
        self.write_note(".vault-organizer/duplicates/B/Same.md", "occupied\n")
        with StubServer([ok_response()]) as server:
            payload = self.run_ok("vault", "--vault", str(self.vault), "--base-url", server.url, "--no-embeddings", "--apply")
        suffixed = self.vault / ".vault-organizer" / "duplicates" / "B" / "Same-1.md"
        self.assertTrue(suffixed.is_file())
        self.assertEqual(suffixed.read_text(encoding="utf-8"), body)
        self.assertEqual((self.vault / ".vault-organizer" / "duplicates" / "B" / "Same.md").read_text(encoding="utf-8"), "occupied\n")
        self.assertEqual(payload["data"]["counts"]["quarantined"], 1)

    def test_suggestions_reported_never_applied(self):
        self.write_note("00 Inbox/Garden.md", "# Garden\n\nNotes about tomato propagation.\n")
        response = ok_response(suggestions=["Add subdomain gardening under personal"])
        with StubServer([response]) as server:
            payload = self.run_ok("inbox", "--vault", str(self.vault), "--base-url", server.url, "--no-embeddings", "--apply")
        report = (Path(payload["data"]["run_directory"]) / "report.md").read_text(encoding="utf-8")
        self.assertIn("## Schema Suggestions", report)
        self.assertIn("Add subdomain gardening under personal", report)
        destination = self.vault / "04 Technology" / "4.03 Obsidian" / "Garden.md"
        self.assertTrue(destination.is_file())
        self.assertNotIn("suggestions", destination.read_text(encoding="utf-8"))

    def test_resume_after_kill_skips_journaled_notes(self):
        for name in ("Alpha", "Beta", "Gamma"):
            self.write_note(f"00 Inbox/{name}.md", f"# {name}\n\n{name} unique body content.\n")
        release = threading.Event()
        BlockingChatHandler.release = release
        BlockingChatHandler.block_after = 1
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        with StubServer([], handler_cls=BlockingChatHandler) as server:
            process = subprocess.Popen(
                [sys.executable, str(SCRIPT), "inbox", "--vault", str(self.vault), "--base-url", server.url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
            journal = None
            deadline = time.time() + 20
            while time.time() < deadline:
                candidates = sorted((self.vault / ".vault-organizer" / "runs").glob("*/classified.jsonl"))
                if candidates and candidates[0].read_text(encoding="utf-8").strip():
                    journal = candidates[0]
                    break
                time.sleep(0.1)
            self.assertIsNotNone(journal, "first classification was never journaled")
            process.kill()
            process.wait()
            release.set()
            run_dir = journal.parent
            payload = self.run_ok(
                "inbox", "--vault", str(self.vault), "--base-url", server.url, "--run", str(run_dir)
            )
            titles = [json.loads(request["messages"][1]["content"])["title"] for request in server.requests]
            self.assertEqual(titles.count("Alpha"), 1, titles)
            self.assertEqual(payload["data"]["counts"]["classified"], 3)
            self.assertEqual(payload["data"]["run_directory"], str(run_dir))
            journal_rows = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(journal_rows), 3)

    def test_resume_refuses_changed_options(self):
        self.write_note("00 Inbox/Note.md", "# Note\n\nBody.\n")
        with StubServer([ok_response()]) as server:
            payload = self.run_ok("inbox", "--vault", str(self.vault), "--base-url", server.url, "--no-embeddings")
            run_dir = payload["data"]["run_directory"]
            result = run_script(
                "inbox", "--vault", str(self.vault), "--base-url", server.url,
                "--run", run_dir, "--model", "other-model",
            )
        self.assertEqual(result.returncode, 1)
        failure = json.loads(result.stdout)
        self.assertIn("--model differs", failure["errors"][0]["message"])

    def test_status_and_doctor(self):
        self.write_note("00 Inbox/Note.md", "# Note\n\nBody.\n")
        with StubServer([ok_response(), ok_response()]) as server, StubEmbeddingsServer() as embeddings:
            payload = self.run_ok(
                "inbox", "--vault", str(self.vault), "--base-url", server.url,
                "--embeddings-url", embeddings.url,
            )
            run_dir = payload["data"]["run_directory"]
            status_payload = self.run_ok("status", "--run", run_dir)
            self.assertEqual(status_payload["data"]["phase"], "planned")
            self.assertEqual(status_payload["data"]["selected"], 1)
            self.assertEqual(status_payload["data"]["classified"], 1)
            doctor_payload = self.run_ok(
                "doctor", "--vault", str(self.vault), "--base-url", server.url,
                "--embeddings-url", embeddings.url,
            )
            checks = doctor_payload["data"]["checks"]
            self.assertTrue(checks["vault"]["ok"])
            self.assertTrue(checks["schema"]["ok"])
            self.assertEqual(checks["schema"]["domains"], 4)
            self.assertTrue(checks["chat"]["ok"])
            self.assertTrue(checks["embeddings"]["ok"])

    def test_doctor_reports_unadopted_conventions_without_failing(self):
        # A vault with no voice note, no lexicon, and no generated guide is not a
        # broken vault -- it is where every vault starts, and where a newly split
        # second vault sits until each convention is written.
        with StubServer([]) as server:
            payload = self.run_ok(
                "doctor", "--vault", str(self.vault), "--base-url", server.url, "--no-embeddings",
            )
        checks = payload["data"]["checks"]
        self.assertEqual(payload["status"], "ok")
        for name in ("voice", "lexicon"):
            self.assertTrue(checks[name]["ok"], name)
            self.assertFalse(checks[name]["configured"], name)
            self.assertIn("default is", checks[name]["detail"])
        self.assertTrue(checks["guide"]["ok"])
        self.assertFalse(checks["guide"]["installed"])
        self.assertIn("no guide installed", " ".join(checks["guide"]["stale"]))

    def test_doctor_reads_the_conventions_a_vault_has_adopted(self):
        self.write_note(
            "99 Meta/0.01 Voice and Style.md",
            "# Voice\n\n## Global voice\n\n- Say it plainly.\n- Cut the throat-clearing.\n",
        )
        self.write_note(
            "99 Meta/0.02 Speakers and Terms.md",
            "# Terms\n\n## Terms\n\n| Term | Variants |\n| --- | --- |\n| pi-forge | pi forge |\n",
        )
        self.run_ok("guide", "--vault", str(self.vault), "--apply")
        with StubServer([]) as server:
            payload = self.run_ok(
                "doctor", "--vault", str(self.vault), "--base-url", server.url, "--no-embeddings",
            )
        checks = payload["data"]["checks"]
        self.assertTrue(checks["voice"]["configured"])
        self.assertEqual(checks["voice"]["rules"]["global"], 2)
        self.assertTrue(checks["lexicon"]["configured"])
        self.assertEqual(checks["lexicon"]["terms"], 1)
        self.assertTrue(checks["guide"]["installed"])
        self.assertTrue(checks["guide"]["current"])
        self.assertEqual(checks["guide"]["stale"], [])

    def test_doctor_warns_about_a_stale_guide_without_calling_the_vault_broken(self):
        self.run_ok("guide", "--vault", str(self.vault), "--apply")
        (self.vault / "01 Personal" / "1.99 Late Addition").mkdir(parents=True)
        with StubServer([]) as server:
            payload = self.run_ok(
                "doctor", "--vault", str(self.vault), "--base-url", server.url, "--no-embeddings",
            )
        checks = payload["data"]["checks"]
        # Still installed and still describing this vault, just behind it.
        self.assertTrue(checks["guide"]["ok"])
        self.assertTrue(checks["guide"]["installed"])
        self.assertFalse(checks["guide"]["current"])
        self.assertIn("folder tree changed", " ".join(payload["warnings"]))

    def test_doctor_lists_the_skills_that_have_left_state_in_this_vault(self):
        # The question a second vault raises: is it as worked-in as the first.
        (self.vault / ".vault-connections").mkdir()
        with StubServer([]) as server:
            payload = self.run_ok(
                "doctor", "--vault", str(self.vault), "--base-url", server.url, "--no-embeddings",
            )
        state = payload["data"]["checks"]["skills"]["state"]
        self.assertIn(".vault-connections", state)
        self.assertNotIn(".vault-wiki", state)

    def test_apply_resume_skips_completed_operations(self):
        self.write_note("00 Inbox/Move.md", "# Move\n\nBody to file.\n")
        with StubServer([ok_response()]) as server:
            payload = self.run_ok("inbox", "--vault", str(self.vault), "--base-url", server.url, "--no-embeddings", "--apply")
            run_dir = payload["data"]["run_directory"]
            self.assertEqual(payload["data"]["counts"]["applied"], 1)
            second = self.run_ok(
                "inbox", "--vault", str(self.vault), "--base-url", server.url,
                "--run", run_dir, "--apply",
            )
        self.assertEqual(second["data"]["counts"]["applied"], 1)
        destination = self.vault / "04 Technology" / "4.03 Obsidian" / "Move.md"
        self.assertTrue(destination.is_file())
        log_rows = [
            json.loads(line)
            for line in (Path(run_dir) / "apply-log.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        # Filing this note is two journalled steps — the frontmatter is rewritten
        # at the old path, then the note moves — because a move can rewrite links
        # inside other notes and would invalidate their planning hashes. The
        # resume must repeat neither, so each step appears exactly once.
        self.assertEqual(
            [row["op"] for row in log_rows if row["status"] == "ok"],
            ["rewrite", "rewrite_move"],
        )


class PersonalContextTests(unittest.TestCase):
    def test_the_cache_key_changes_with_the_profile(self):
        """Regression lock. The profile shapes the system prompt, so it has to
        shape the key; otherwise editing a context card re-classifies nothing."""
        base = dict(
            title="A note", body_hash="b", frontmatter_hash="f", schema_hash="s",
            model="chat", base_url="http://llms:8004/v1/chat/completions",
        )
        first = vault_organizer.cache_key(**base, profile_hash="aaa")
        second = vault_organizer.cache_key(**base, profile_hash="bbb")
        self.assertNotEqual(first, second)

    def test_the_cache_key_defaults_to_no_profile(self):
        base = dict(
            title="A note", body_hash="b", frontmatter_hash="f", schema_hash="s",
            model="chat", base_url="http://llms:8004/v1/chat/completions",
        )
        self.assertEqual(vault_organizer.cache_key(**base), vault_organizer.cache_key(**base, profile_hash="none"))

    def test_classification_asserts_no_route_so_gated_cards_stay_out(self):
        self.assertEqual(vault_organizer.classification_site()["routes"], frozenset())

    def test_the_always_tier_reaches_the_classification_system_prompt(self):
        profile = {
            "cards": [
                {
                    "order": 0, "name": "Information Preferences", "link": "[[Information Preferences]]",
                    "tier": "always", "scope": "universal", "routes": frozenset(),
                    "triggers": [], "note": "", "facts": ["Filenames never use colons."],
                },
                {
                    "order": 1, "name": "Mental Health", "link": "[[Mental Health]]",
                    "tier": "when-relevant", "scope": "owner-authored",
                    "routes": frozenset({"personal/therapy"}),
                    "triggers": ["OCD"], "note": "", "facts": ["Clinical detail."],
                },
            ]
        }
        args = SimpleNamespace(compiled_profile=profile)
        prefix = vault_organizer.classification_profile_prefix(args)
        self.assertIn("Filenames never use colons.", prefix)
        self.assertNotIn("Clinical detail.", prefix)


class HumanOwnedPropertyTests(unittest.TestCase):
    """Properties the author owns and the classifier must never touch.

    Filing rebuilds frontmatter from a model response, so a property the model
    is shown is a property the model fills in, and a property it is not shown is
    one that vanishes. Human-owned properties have to be absent from the prompt
    and restored from the note in the same change, or one of those two failures
    happens on the first run.
    """

    def schema(self, text=SCHEMA):
        return vault_organizer.parse_schema_note(text)

    def test_schema_marks_human_owned_properties(self):
        schema = self.schema()
        self.assertTrue(schema["properties"]["date"]["human_owned"])
        self.assertTrue(schema["properties"]["cover"]["human_owned"])
        self.assertFalse(schema["properties"]["type"]["human_owned"])
        self.assertEqual(vault_organizer.human_owned_properties(schema), ["date", "cover"])

    def test_human_owned_shape_still_parses_normally(self):
        schema = self.schema()
        self.assertEqual(schema["properties"]["date"]["shape"], "scalar")
        self.assertEqual(schema["properties"]["date"]["value_mode"], "free")
        self.assertTrue(vault_organizer.property_human_owned("quoted wikilink, human-owned"))
        self.assertEqual(vault_organizer.property_value_mode("quoted wikilink, human-owned"), "wikilink")

    def test_required_human_owned_property_is_rejected(self):
        text = SCHEMA.replace(
            "| `date` | no | scalar, human-owned | Subject date. |",
            "| `date` | yes | scalar, human-owned | Subject date. |",
        )
        with self.assertRaises(vault_organizer.UserError) as caught:
            vault_organizer.parse_schema_note(text)
        self.assertIn("must be Required: no", str(caught.exception))

    def test_classifier_prompt_never_offers_human_owned_properties(self):
        """Asserted against the structured payloads, not a substring search of
        the prompt: real schemas say things like "coverage" and "Dated records",
        so a substring check would pass or fail on vocabulary rather than on
        whether the model was actually offered the property."""
        schema = self.schema()
        compact = vault_organizer.compact_schema_for_prompt(schema)
        self.assertNotIn("date", compact["property_order"])
        self.assertNotIn("cover", compact["properties"])
        self.assertIn("type", compact["property_order"])
        prompt = vault_organizer.system_prompt(schema)
        shape = json.loads(prompt.split("Required response shape:\n")[1])
        self.assertNotIn("date", shape["metadata"])
        self.assertNotIn("cover", shape["metadata"])
        self.assertIn("type", shape["metadata"])

    def test_classifier_values_for_human_owned_properties_are_dropped(self):
        schema = self.schema()
        response = ok_response(metadata={
            "type": "note",
            "status": "active",
            "domain": "personal",
            "date": "sometime in 2024",
            "cover": "[[invented.png]]",
        })
        validated, warnings, errors = vault_organizer.validate_classification(response, schema)
        self.assertEqual(errors, [])
        self.assertNotIn("date", validated["metadata"])
        self.assertNotIn("cover", validated["metadata"])
        self.assertTrue(any("withheld" in warning for warning in warnings))

    def test_carry_forward_restores_human_owned_properties(self):
        schema = self.schema()
        validated = {"metadata": {"type": "note", "status": "active", "domain": "personal"}}
        warnings = []
        vault_organizer.carry_forward_provenance(
            validated, 'date: 2026-01-15\ncover: "[[banner.png]]"\n', schema, warnings
        )
        self.assertEqual(validated["metadata"]["date"], "2026-01-15")
        self.assertEqual(validated["metadata"]["cover"], "[[banner.png]]")

    def test_carry_forward_prefers_the_note_over_the_classifier(self):
        schema = self.schema()
        validated = {"metadata": {"type": "note", "status": "active", "domain": "personal", "date": "2099-12-31"}}
        vault_organizer.carry_forward_provenance(validated, "date: 2026-01-15\n", schema, [])
        self.assertEqual(validated["metadata"]["date"], "2026-01-15")

    def test_carry_forward_drops_a_list_where_the_schema_wants_a_scalar(self):
        schema = self.schema()
        validated = {"metadata": {"type": "note", "status": "active", "domain": "personal"}}
        warnings = []
        vault_organizer.carry_forward_provenance(validated, "date:\n  - 2026-01-15\n  - 2026-02-01\n", schema, warnings)
        self.assertNotIn("date", validated["metadata"])
        self.assertTrue(any("scalar" in warning for warning in warnings))

    def test_carry_forward_leaves_absent_properties_absent(self):
        schema = self.schema()
        validated = {"metadata": {"type": "note", "status": "active", "domain": "personal"}}
        vault_organizer.carry_forward_provenance(validated, "type: note\n", schema, [])
        self.assertNotIn("date", validated["metadata"])
        self.assertNotIn("cover", validated["metadata"])

    def test_serialized_frontmatter_is_readable_by_obsidian(self):
        """A bare date so Obsidian types it as a Date, a quoted cover so the
        wikilink survives YAML."""
        schema = self.schema()
        text = vault_organizer.serialize_frontmatter(
            {"type": "note", "status": "active", "domain": "personal",
             "date": "2026-01-15", "cover": "[[banner.png]]"},
            schema,
        )
        self.assertIn("\ndate: 2026-01-15\n", text)
        self.assertIn('\ncover: "[[banner.png]]"\n', text)

    def test_a_url_cover_survives_serialization(self):
        schema = self.schema()
        text = vault_organizer.serialize_frontmatter(
            {"type": "note", "status": "active", "domain": "personal",
             "cover": "https://example.com/a.png"},
            schema,
        )
        self.assertIn('\ncover: "https://example.com/a.png"\n', text)


class DerivedPropertyTests(unittest.TestCase):
    """``created``: a property neither the model nor the owner supplies.

    The vault this serves lost its creation dates to a bulk reorganization that
    flattened every file timestamp, so the point of the marker is not only to
    withhold the property from the classifier -- ``human-owned`` already does
    that -- but to let it be *required* while code, not a person, keeps it filled.
    """

    def schema(self, text=SCHEMA):
        return vault_organizer.parse_schema_note(text)

    def test_schema_marks_derived_properties(self):
        schema = self.schema()
        self.assertTrue(schema["properties"]["created"]["derived"])
        self.assertFalse(schema["properties"]["date"]["derived"])
        self.assertFalse(schema["properties"]["created"]["human_owned"])
        self.assertEqual(vault_organizer.derived_properties(schema), ["created"])
        self.assertEqual(vault_organizer.withheld_properties(schema), ["date", "cover", "created"])

    def test_a_derived_property_may_be_required(self):
        """The rule human-owned properties fall foul of, and the reason for the
        second marker: something always supplies a derived value."""
        self.assertEqual(self.schema()["properties"]["created"]["required"], "yes")

    def test_a_property_cannot_be_both_human_owned_and_derived(self):
        text = SCHEMA.replace(
            "| `created` | yes | scalar, derived | Date this note came into existence. |",
            "| `created` | no | scalar, derived, human-owned | Date this note came into existence. |",
        )
        with self.assertRaises(vault_organizer.UserError) as caught:
            vault_organizer.parse_schema_note(text)
        self.assertIn("cannot be both", str(caught.exception))

    def test_classifier_is_never_offered_a_derived_property(self):
        schema = self.schema()
        self.assertNotIn("created", vault_organizer.compact_schema_for_prompt(schema)["property_order"])
        prompt = vault_organizer.system_prompt(schema)
        self.assertNotIn("created", json.loads(prompt.split("Required response shape:\n")[1])["metadata"])

    def test_a_classifier_supplied_created_is_dropped(self):
        schema = self.schema()
        response = ok_response(metadata={
            "type": "note", "status": "active", "domain": "personal", "created": "2019-04-04",
        })
        validated, warnings, errors = vault_organizer.validate_classification(response, schema)
        self.assertEqual(errors, [])
        self.assertNotIn("created", validated["metadata"])
        self.assertTrue(any("withheld" in warning for warning in warnings))

    def test_an_existing_created_is_carried_forward_untouched(self):
        """Write-once. Filing the same note twice must not change its birthday."""
        schema = self.schema()
        validated = {"metadata": {"type": "note", "status": "active", "domain": "personal"}}
        vault_organizer.carry_forward_provenance(
            validated, "created: 2021-06-09\n", schema, [], Path("2026-01-01 Later.md")
        )
        self.assertEqual(validated["metadata"]["created"], "2021-06-09")
        self.assertEqual(validated["created_evidence"], "carried")

    def test_a_filename_date_prefix_beats_every_other_tier(self):
        schema = self.schema()
        validated = {"metadata": {"type": "note", "status": "active", "domain": "personal"}}
        vault_organizer.carry_forward_provenance(
            validated, "date: 2024-02-02\n", schema, [], Path("2019-11-30 Trip north.md")
        )
        self.assertEqual(validated["metadata"]["created"], "2019-11-30")
        self.assertEqual(validated["created_evidence"], "filename")

    def test_the_subject_date_is_used_when_the_filename_carries_none(self):
        schema = self.schema()
        validated = {"metadata": {"type": "note", "status": "active", "domain": "personal"}}
        vault_organizer.carry_forward_provenance(validated, "date: 2024-02-02\n", schema, [], Path("Trip north.md"))
        self.assertEqual(validated["metadata"]["created"], "2024-02-02")
        self.assertEqual(validated["created_evidence"], "date_property")

    def test_a_note_with_no_evidence_at_all_is_stamped_with_the_run_date(self):
        schema = self.schema()
        validated = {"metadata": {"type": "note", "status": "active", "domain": "personal"}}
        vault_organizer.carry_forward_provenance(validated, "", schema, [], None)
        self.assertEqual(validated["metadata"]["created"], datetime.date.today().isoformat())
        self.assertEqual(validated["created_evidence"], "run_date")

    def test_an_invalid_calendar_date_in_a_filename_is_not_used(self):
        schema = self.schema()
        validated = {"metadata": {"type": "note", "status": "active", "domain": "personal"}}
        vault_organizer.carry_forward_provenance(validated, "date: 2024-02-02\n", schema, [], Path("2026-02-30 No.md"))
        self.assertEqual(validated["metadata"]["created"], "2024-02-02")

    def test_a_vault_whose_schema_has_no_created_is_left_alone(self):
        text = SCHEMA.replace("| `created` | yes | scalar, derived | Date this note came into existence. |\n", "")
        schema = vault_organizer.parse_schema_note(text)
        validated = {"metadata": {"type": "note", "status": "active", "domain": "personal"}}
        vault_organizer.carry_forward_provenance(validated, "", schema, [], None)
        self.assertNotIn("created", validated["metadata"])
        self.assertNotIn("created_evidence", validated)

    def test_a_template_is_never_stamped(self):
        """``template-install`` compares the installed file byte-for-byte with the
        shipped one, so a stamped template is refused as owner-modified forever
        after."""
        schema = self.schema()
        validated = {"metadata": {"type": "template", "status": "active", "domain": "meta"}}
        vault_organizer.carry_forward_provenance(validated, "", schema, [], Path("Wiki Concept.md"))
        self.assertNotIn("created", validated["metadata"])
        self.assertEqual(vault_organizer.missing_required_properties(validated["metadata"], schema), [])

    def test_required_properties_are_reported_when_missing(self):
        schema = self.schema()
        self.assertEqual(
            vault_organizer.missing_required_properties({"type": "note", "status": "active"}, schema),
            ["domain", "created"],
        )
        self.assertEqual(
            vault_organizer.missing_required_properties(
                {"type": "note", "status": "active", "domain": "personal", "created": "2026-01-01"}, schema
            ),
            [],
        )

    def test_reuse_frontmatter_does_not_replay_a_derived_value(self):
        """``--reuse-frontmatter`` pushes existing values through validation, and
        a derived key there would be rejected as unapproved for the classifier.
        Carry-forward is what restores it."""
        schema = self.schema()
        reused = vault_organizer.reuse_frontmatter_classification(
            schema, "type: note\nstatus: active\ndomain: personal\ncreated: 2020-01-01\n"
        )
        self.assertIsNotNone(reused)
        validated, _warnings = reused
        self.assertNotIn("created", validated["metadata"])

    def test_the_report_names_the_evidence_behind_each_date(self):
        lines = "\n".join(vault_organizer.created_report_lines([
            {"created_evidence": "filename"},
            {"created_evidence": "filename"},
            {"created_evidence": "filesystem"},
            {"created_evidence": None},
        ]))
        self.assertIn("## Created Dates", lines)
        self.assertIn("2 read from a `YYYY-MM-DD` filename prefix", lines)
        self.assertIn("weakest evidence", lines)

    def test_the_report_section_is_absent_when_nothing_was_stamped(self):
        self.assertEqual(vault_organizer.created_report_lines([{"created_evidence": None}]), [])


class SourcesTreeTests(unittest.TestCase):
    """Filing ``type: source`` notes by source kind rather than by domain.

    The routing itself is proven in ``forge/lib/tests/test_vault_sources.py``.
    What is proven here is the organizer's half: that a run actually files into
    the tree, and that a source note's identity survives a pass that would
    otherwise re-derive it from the note's text.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.vault = self.root / "vault"
        (self.vault / "99 Meta").mkdir(parents=True)
        (self.vault / "00 Inbox").mkdir()
        (self.vault / "99 Meta" / "0.00 Vault Schema.md").write_text(SOURCES_SCHEMA, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def schema(self):
        return vault_organizer.parse_schema_note(SOURCES_SCHEMA)

    def test_a_source_files_into_the_tree_end_to_end(self):
        note = self.vault / "00 Inbox" / "Isbister - 2016 - How Games Move Us.md"
        note.write_text("# How Games Move Us\n\nEmotion by design.\n", encoding="utf-8")
        response = ok_response(metadata={
            "type": "source",
            "status": "active",
            "domain": "technology",
            "subdomain": "obsidian",
            "source_kind": "book",
            "capture_type": "manual",
        })
        with StubServer([response]) as server:
            result = run_script("inbox", "--vault", str(self.vault), "--base-url", server.url, "--apply")
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        filed = self.vault / "10 Sources" / "10.01 Book" / "Technology" / "Obsidian" / note.name
        self.assertTrue(filed.is_file(), sorted(path.name for path in self.vault.rglob("*.md")))
        self.assertFalse(note.exists())
        self.assertIn("source_kind: book", filed.read_text(encoding="utf-8"))

    def test_reuse_frontmatter_files_without_calling_the_model(self):
        note = self.vault / "00 Inbox" / "Filed Already.md"
        note.write_text(
            "---\ntype: source\nstatus: active\ndomain: technology\nsubdomain: obsidian\n"
            "source_kind: manual\ncapture_type: manual\n---\n\n# Filed Already\n\nBody.\n",
            encoding="utf-8",
        )
        with StubServer([]) as server:
            result = run_script(
                "inbox", "--vault", str(self.vault), "--base-url", server.url, "--reuse-frontmatter", "--apply"
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["data"]["counts"]["reused"], 1)
            self.assertEqual(payload["data"]["counts"]["classified"], 0)
            self.assertEqual(server.requests, [])
        filed = self.vault / "10 Sources" / "10.07 Manual" / "Technology" / "Obsidian" / note.name
        self.assertTrue(filed.is_file(), sorted(path.name for path in self.vault.rglob("*.md")))

    def test_reuse_falls_through_to_the_model_when_frontmatter_is_incomplete(self):
        note = self.vault / "00 Inbox" / "Half Done.md"
        note.write_text("---\ntype: source\n---\n\n# Half Done\n\nBody.\n", encoding="utf-8")
        response = ok_response(metadata={
            "type": "note", "status": "active", "domain": "technology", "subdomain": "obsidian",
        })
        with StubServer([response]) as server:
            result = run_script(
                "inbox", "--vault", str(self.vault), "--base-url", server.url, "--reuse-frontmatter"
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["data"]["counts"]["classified"], 1)
            self.assertEqual(payload["data"]["counts"]["reused"], 0)
            self.assertEqual(len(server.requests), 1)

    def test_reuse_never_invents_a_human_owned_value(self):
        schema = self.schema()
        reused = vault_organizer.reuse_frontmatter_classification(
            schema,
            "type: source\nstatus: active\ndomain: technology\nsource_kind: book\n"
            "date: 2026-01-15\nprocessed_by:\n  - vault-transcripts\n",
        )
        self.assertIsNotNone(reused)
        validated, _ = reused
        # Withheld from the candidate, then restored from the note by the same
        # carry-forward the model path uses.
        self.assertNotIn("date", validated["metadata"])
        self.assertNotIn("processed_by", validated["metadata"])

    def test_reused_records_are_not_sent_to_the_verifier(self):
        records = {
            "a.md": {"destination": "10 Sources/10.01 Book/x.md", "classification_source": "frontmatter"},
            "b.md": {"destination": "04 Technology/y.md", "classification_source": "model"},
        }
        self.assertEqual([rel for rel, _ in vault_organizer.verifiable_records(records)], ["b.md"])

    def test_a_source_note_keeps_its_kind_through_reclassification(self):
        # The raw half of a transcript is a wall of timestamped speech, and a
        # classifier reading it calls it a meeting. The note already knew.
        schema = self.schema()
        validated = {"metadata": {
            "type": "note", "status": "active", "domain": "personal", "subdomain": "journal",
        }}
        warnings = []
        vault_organizer.carry_forward_provenance(
            validated,
            'type: source\nsource_kind: transcript\nparent: "[[2026-07-24 - Therapy]]"\n',
            schema,
            warnings,
        )
        self.assertEqual(validated["metadata"]["type"], "source")
        self.assertEqual(validated["metadata"]["source_kind"], "transcript")
        self.assertEqual(validated["metadata"]["parent"], "[[2026-07-24 - Therapy]]")
        self.assertTrue(any("kept type: source" in warning for warning in warnings))
        self.assertEqual(
            vault_organizer.compile_destination(schema, validated["metadata"]).as_posix(),
            "10 Sources/10.03 Transcript/Personal/Journal",
        )

    def test_a_stale_kind_the_schema_dropped_is_not_pinned(self):
        schema = self.schema()
        validated = {"metadata": {"type": "note", "status": "active", "domain": "personal"}}
        vault_organizer.carry_forward_provenance(
            validated, "type: source\nsource_kind: papyrus\n", schema, []
        )
        self.assertEqual(validated["metadata"]["type"], "note")
        self.assertNotIn("source_kind", validated["metadata"])

    def test_pinning_is_off_for_a_vault_that_files_sources_by_domain(self):
        schema = vault_organizer.parse_schema_note(SCHEMA)
        validated = {"metadata": {"type": "note", "status": "active", "domain": "personal"}}
        vault_organizer.carry_forward_provenance(
            validated, "type: source\nsource_kind: book\n", schema, []
        )
        self.assertEqual(validated["metadata"]["type"], "note")

    def test_only_sources_leaves_every_other_note_alone(self):
        # Turning on the sources tree is a migration for sources. A whole-vault
        # run would also refile hand-made folder trees that sit below a declared
        # route -- legitimate structure the schema does not describe.
        handmade = self.vault / "04 Technology" / "4.03 Obsidian" / "Utopia" / "Primary Sources"
        handmade.mkdir(parents=True)
        (handmade / "Field Notes.md").write_text(
            "---\ntype: note\nstatus: active\ndomain: technology\n---\n\nHand-filed.\n", encoding="utf-8"
        )
        source = self.vault / "04 Technology" / "4.03 Obsidian" / "A Manual.md"
        source.write_text(
            "---\ntype: source\nstatus: active\ndomain: technology\nsubdomain: obsidian\n"
            "source_kind: manual\n---\n\n# A Manual\n\nBody.\n",
            encoding="utf-8",
        )
        with StubServer([]) as server:
            result = run_script(
                "vault", "--vault", str(self.vault), "--base-url", server.url,
                "--only-sources", "--reuse-frontmatter", "--apply",
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(server.requests, [])
        self.assertTrue((handmade / "Field Notes.md").is_file())
        self.assertFalse(source.exists())
        self.assertTrue(
            (self.vault / "10 Sources" / "10.07 Manual" / "Technology" / "Obsidian" / "A Manual.md").is_file(),
            sorted(path.as_posix() for path in self.vault.rglob("*.md")),
        )

    def test_only_sources_counts_only_sources_as_selected(self):
        (self.vault / "00 Inbox" / "Loose.md").write_text(
            "---\ntype: note\nstatus: active\ndomain: technology\n---\n\nBody.\n", encoding="utf-8"
        )
        with StubServer([]) as server:
            result = run_script(
                "vault", "--vault", str(self.vault), "--base-url", server.url,
                "--only-sources", "--reuse-frontmatter",
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)["data"]["counts"]["selected"], 0)

    def test_the_classifier_is_never_shown_a_folder_number(self):
        compact = vault_organizer.compact_schema_for_prompt(self.schema())
        self.assertEqual(compact["source_kinds"]["book"], "Book.")


class LinkSafeMoveTests(unittest.TestCase):
    """Filing a note moves it; the notes pointing at it should come along.

    Without Obsidian, pi-forge renames the file and leaves inbound `[[links]]`
    to basename resolution — fine for a folder-only move, silently wrong when
    the filename changes too. With Obsidian, the CLI rewrites those links across
    the vault, which is a much wider blast radius than `os.rename`. These tests
    are mostly about the price of that: backups before the call, hashes checked
    after it, and a full restore the moment a rewrite touches a line that never
    had a link on it.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.vault = self.root / "vault"
        (self.vault / "99 Meta").mkdir(parents=True)
        (self.vault / "00 Inbox").mkdir()
        (self.vault / "99 Meta" / "0.00 Vault Schema.md").write_text(SCHEMA, encoding="utf-8")
        self.filed = self.vault / "04 Technology" / "4.03 Obsidian" / "Move.md"

    def tearDown(self):
        self.tmp.cleanup()

    def write_note(self, relative, text):
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def shim(self, script=None, **kwargs):
        env = ShimEnvironment(script, vault_path=self.vault, vault_name="vault", **kwargs)
        self.addCleanup(env.cleanup)
        return env

    def linked_pair(self):
        """One note to file, and an already-filed note that links to it.

        The Markdown link is the one that actually moves here. Filing keeps a
        note's basename, so `[[Move]]` resolves before and after; a Markdown link
        carrying an explicit path does not, and repairing it is what pi-forge has
        never been able to do on its own.
        """
        self.write_note("00 Inbox/Move.md", "# Move\n\nBody to file.\n")
        return self.write_note(
            "04 Technology/4.02 Software Development/Refers.md",
            '---\ntype: note\nrelated:\n  - "[[Move]]"\n---\n\n'
            "The prose line.\n\nSee [[Move]] and [Move](../../00 Inbox/Move.md).\n",
        )

    def organize(self, *extra, responses=None):
        with StubServer(responses or [ok_response()]) as server:
            result = run_script(
                "inbox", "--vault", str(self.vault), "--base-url", server.url,
                "--no-embeddings", "--apply", *extra,
            )
        return result

    def test_inbound_links_follow_the_note_and_the_move_is_journalled(self):
        refers = self.linked_pair()
        env = self.shim()
        payload = json.loads(self.organize().stdout)
        self.assertEqual(payload["status"], "ok", payload)
        self.assertTrue(self.filed.is_file())
        self.assertEqual(payload["data"]["link_rewrite"]["mode"], "obsidian-cli")

        text = refers.read_text(encoding="utf-8")
        self.assertNotIn("../../00 Inbox/Move.md", text, "the stale path is gone")
        self.assertIn("[[Move]]", text, "the wikilink still resolves by basename and is left alone")
        run_dir = Path(payload["data"]["run_directory"])
        rows = [json.loads(line) for line in (run_dir / "apply-log.jsonl").read_text(encoding="utf-8").splitlines()]
        move = [row for row in rows if row["op"] == "rewrite_move"][0]
        self.assertEqual(move["status"], "ok", move)
        self.assertEqual(move["linkRewrite"], "obsidian-cli")
        self.assertEqual(move["inboundBefore"], 1)
        self.assertEqual(move["linksRewrittenIn"], ["04 Technology/4.02 Software Development/Refers.md"])
        self.assertTrue(any("vault=vault" in argv for argv in env.calls()))

    def test_an_obsidian_only_link_form_is_reported_not_swallowed(self):
        # Obsidian rewrites a Markdown target to the shortest path unique in the
        # vault and resolves that like a wikilink. From another folder the result
        # is correct in Obsidian and broken everywhere else, which a vault that
        # promises to be plain Markdown needs to hear about.
        self.linked_pair()
        self.shim()
        payload = json.loads(self.organize().stdout)
        self.assertTrue(
            any("only Obsidian resolves" in warning for warning in payload["warnings"]),
            payload["warnings"],
        )

    def test_every_note_the_move_rewrites_is_backed_up_first(self):
        self.linked_pair()
        self.shim()
        payload = json.loads(self.organize().stdout)
        run_dir = Path(payload["data"]["run_directory"])
        backup = run_dir / "backup" / "04 Technology" / "4.02 Software Development" / "Refers.md"
        self.assertTrue(backup.is_file(), "the linking note is backed up before the CLI touches it")
        self.assertIn(
            "../../00 Inbox/Move.md", backup.read_text(encoding="utf-8"), "the backup predates the rewrite"
        )

    def test_a_rewrite_that_touches_prose_is_restored_and_fails(self):
        refers = self.linked_pair()
        original = refers.read_text(encoding="utf-8")
        env = self.shim()
        env.set_env(SHIM_MANGLE="1")
        payload = json.loads(self.organize().stdout)

        self.assertEqual(refers.read_text(encoding="utf-8"), original, "restored byte for byte")
        run_dir = Path(payload["data"]["run_directory"])
        rows = [json.loads(line) for line in (run_dir / "apply-log.jsonl").read_text(encoding="utf-8").splitlines()]
        move = [row for row in rows if row["op"] == "rewrite_move"][0]
        self.assertEqual(move["status"], "error")
        self.assertIn("more than links", move["error"])
        self.assertEqual(payload["data"]["counts"]["failed"], 1)

    def test_one_strike_disables_the_cli_for_the_rest_of_the_run(self):
        self.write_note("00 Inbox/First.md", "# First\n\nBody one.\n")
        self.write_note("00 Inbox/Second.md", "# Second\n\nBody two.\n")
        self.write_note(
            "04 Technology/4.02 Software Development/Refers.md",
            "---\ntype: note\n---\n\nThe prose line.\n\n"
            "See [First](../../00 Inbox/First.md) and [Second](../../00 Inbox/Second.md).\n",
        )
        env = self.shim()
        env.set_env(SHIM_MANGLE="1")
        payload = json.loads(self.organize(responses=[ok_response(), ok_response()]).stdout)

        run_dir = Path(payload["data"]["run_directory"])
        rows = [json.loads(line) for line in (run_dir / "apply-log.jsonl").read_text(encoding="utf-8").splitlines()]
        moves = [row for row in rows if row["op"] == "rewrite_move"]
        self.assertEqual(len(moves), 2)
        self.assertEqual(moves[0]["status"], "error")
        # The second move still happens — it just stops asking Obsidian to do it.
        self.assertEqual(moves[1]["status"], "ok", moves[1])
        self.assertEqual(moves[1]["linkRewrite"], "rename")

    def test_link_rewrite_off_never_calls_the_cli(self):
        refers = self.linked_pair()
        env = self.shim()
        payload = json.loads(self.organize("--link-rewrite", "off").stdout)

        self.assertTrue(self.filed.is_file())
        self.assertEqual(payload["data"]["link_rewrite"]["mode"], "rename")
        self.assertFalse(any("move" in argv for argv in env.calls()), env.calls())
        self.assertIn(
            "../../00 Inbox/Move.md", refers.read_text(encoding="utf-8"), "links are left exactly as they were"
        )

    def test_link_rewrite_require_fails_when_links_would_not_be_updated(self):
        self.linked_pair()
        self.shim(link_updates="unset")
        result = self.organize("--link-rewrite", "require")
        self.assertEqual(result.returncode, 1, result.stdout)
        message = json.loads(result.stdout)["errors"][0]["message"]
        self.assertIn("link-safe moves are unavailable", message)

    def test_without_obsidian_the_result_is_exactly_what_it_always_was(self):
        refers = self.linked_pair()
        payload = json.loads(self.organize().stdout)
        self.assertTrue(self.filed.is_file())
        self.assertEqual(payload["data"]["link_rewrite"]["mode"], "rename")
        self.assertIn("../../00 Inbox/Move.md", refers.read_text(encoding="utf-8"))
        report = Path(payload["data"]["run_directory"], "report.md").read_text(encoding="utf-8")
        self.assertIn("Moves use a plain rename", report)

    def test_quarantine_never_goes_through_the_cli(self):
        # A duplicate is moved into a dot-directory Obsidian does not index.
        # Rewriting inbound links to chase it there would point them at nothing.
        body = "# Twin\n\nExactly the same body.\n"
        self.write_note("00 Inbox/Twin A.md", body)
        self.write_note("00 Inbox/Twin B.md", body)
        env = self.shim()
        payload = json.loads(self.organize(responses=[ok_response()]).stdout)
        run_dir = Path(payload["data"]["run_directory"])
        rows = [json.loads(line) for line in (run_dir / "apply-log.jsonl").read_text(encoding="utf-8").splitlines()]
        quarantines = [row for row in rows if row["op"] == "quarantine"]
        self.assertTrue(quarantines)
        for row in quarantines:
            self.assertEqual(row["linkRewrite"], "rename")
        quarantined = {row["destination"] for row in quarantines}
        moved = {
            argv[argv.index("move") + 2].split("=", 1)[1]
            for argv in env.calls()
            if "move" in argv
        }
        self.assertFalse(moved & quarantined, "no quarantine destination was handed to the CLI")

    def test_a_base_is_evaluated_rather_than_string_matched(self):
        """A base selects notes by property, so its text is the wrong thing to grep.

        It can hold a note whose path appears nowhere in the file, and mention a
        path in a filter it never matches. Running the view answers the question
        the report is actually asking.
        """
        self.write_note("00 Inbox/Move.md", "# Move\n\nBody to file.\n")
        (self.vault / "99 Meta").mkdir(parents=True, exist_ok=True)
        (self.vault / "99 Meta" / "Dash.base").write_text(
            "filters:\n  and:\n    - type == \"note\"\n", encoding="utf-8"
        )
        self.shim({
            "bases": "99 Meta/Dash.base\n",
            "base:query": "00 Inbox/Move.md\n01 Personal/Other.md\n",
        })
        payload = json.loads(self.organize().stdout)
        plan = json.loads(Path(payload["data"]["run_directory"], "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(
            plan["base_references"],
            [{
                "base": "99 Meta/Dash.base",
                "references": ["00 Inbox/Move.md"],
                "returned": 2,
                "source": "base:query",
            }],
        )
        report = Path(payload["data"]["run_directory"], "report.md").read_text(encoding="utf-8")
        self.assertIn("returns 2 note(s), 1 of which this run moves", report)
        self.assertEqual(
            (self.vault / "99 Meta" / "Dash.base").read_text(encoding="utf-8"),
            'filters:\n  and:\n    - type == "note"\n',
            "a base file is never rewritten",
        )

    def test_without_obsidian_a_base_is_still_text_matched(self):
        self.write_note("00 Inbox/Move.md", "# Move\n\nBody to file.\n")
        (self.vault / "99 Meta").mkdir(parents=True, exist_ok=True)
        (self.vault / "99 Meta" / "Dash.base").write_text("note: 00 Inbox/Move.md\n", encoding="utf-8")
        payload = json.loads(self.organize().stdout)
        plan = json.loads(Path(payload["data"]["run_directory"], "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["base_references"], [{"base": "99 Meta/Dash.base", "references": ["00 Inbox/Move.md"]}])
        report = Path(payload["data"]["run_directory"], "report.md").read_text(encoding="utf-8")
        self.assertIn("text match; Obsidian was not running", report)


class DateEvidenceTests(unittest.TestCase):
    """Reading a date off a file without guessing.

    Every case here is one a real archive produces. The ones that assert *None*
    matter most: a date this code declines to read becomes a note a person
    looks at, and a date it reads wrongly becomes a note nobody looks at again.
    """

    dates = vault_organizer.vault_dates

    def first(self, text):
        found = self.dates.first_date_in(text)
        return found[0].isoformat() if found else None

    def spaced(self, stem, month_first=None):
        found = self.dates.resolve_spaced_date(stem, month_first)
        return found[0].isoformat() if found else None

    def test_spaced_filename_dates_read_the_last_run(self):
        # `Day 1 7 15 2013` must read the date and not the "Day 1".
        self.assertEqual(self.spaced("Day 1 7 15 2013"), "2013-07-15")
        self.assertEqual(self.spaced("Dream 05 15 2013"), "2013-05-15")
        # Same day either way, so no convention is needed to read it.
        self.assertEqual(self.spaced("Morning Coffee 6 6 2013"), "2013-06-06")
        self.assertIsNone(self.spaced("nothing here 2013"))

    def test_an_ambiguous_spaced_date_waits_for_the_corpus(self):
        # 6 4 2013 is June 4th or April 6th and the string cannot say which.
        self.assertIsNone(self.spaced("Long Week Past 6 4 2013"))
        self.assertEqual(self.spaced("Long Week Past 6 4 2013", True), "2013-06-04")
        self.assertEqual(self.spaced("Long Week Past 6 4 2013", False), "2013-04-06")

    def test_the_corpus_decides_the_ordering_only_when_it_agrees(self):
        month = [{"path": "/v/Dream 05 15 2013.md"}, {"path": "/v/Storm 6 27 2013.md"}]
        self.assertEqual(self.dates.filename_convention(month)[0], True)
        mixed = month + [{"path": "/v/Other 15 05 2013.md"}]
        self.assertIsNone(self.dates.filename_convention(mixed)[0])
        self.assertIsNone(self.dates.filename_convention([{"path": "/v/plain.md"}])[0])

    def test_a_separated_date_outranks_a_spaced_one(self):
        found = self.dates.filename_evidence(Path("/v/2013-05-15 notes 1 2 2014.md"), True)
        self.assertEqual([c["date"] for c in found], ["2013-05-15"])

    def test_a_year_folder_says_the_year_and_no_day(self):
        found = self.dates.year_evidence("Journal/12.01 Daily/2015/Depression.md")
        self.assertEqual([(c["date"], c["tier"]) for c in found], [("2015-01-01", self.dates.YEAR)])
        self.assertEqual(self.dates.year_evidence("Journal/Daily/Depression.md"), [])

    def test_an_import_stamp_is_found_by_how_many_notes_share_it(self):
        # One day carrying many distinct notes is a copy; a day carrying one or
        # two is somebody writing.
        entries = [{"birthtime": "2015-12-01", "body_hash": "h%d" % n, "relative": str(n)} for n in range(40)]
        entries += [{"birthtime": "2015-06-02", "body_hash": "x", "relative": "x"}]
        stamps = self.dates.stamp_days(entries)
        self.assertEqual(sorted(stamps), ["2015-12-01"])
        # Four copies of one note must not look like four things made that day.
        copies = [{"birthtime": "2020-01-01", "body_hash": "same", "relative": str(n)} for n in range(40)]
        self.assertEqual(self.dates.stamp_days(copies), {})

    def test_a_stamped_birthtime_gives_nothing_not_even_its_year(self):
        # The stamp is the day the copy ran, so its year is the copy's year.
        stamped = self.dates.filesystem_evidence(
            datetime.date(2026, 7, 1), None, trust_birthtime=True, stamp_days={"2026-07-01"}
        )
        self.assertEqual(stamped, [])
        genuine = self.dates.filesystem_evidence(
            datetime.date(2020, 7, 14), None, trust_birthtime=True, stamp_days={"2026-07-01"}
        )
        self.assertEqual([(c["date"], c["tier"]) for c in genuine], [("2020-07-14", self.dates.EXPLICIT)])

    def test_a_creation_date_in_the_future_is_not_believed(self):
        # An organization note named "Architecture 2030" arrived carrying this.
        text = "created: '2030-01-01'\n"
        self.assertEqual(self.dates.frontmatter_evidence(text, today=datetime.date(2026, 8, 1)), [])
        past = self.dates.frontmatter_evidence("created: '2024-03-02'\n", today=datetime.date(2026, 8, 1))
        self.assertEqual([c["date"] for c in past], ["2024-03-02"])

    def test_year_evidence_never_reaches_the_auto_apply_tier(self):
        self.assertEqual(self.dates.confidence(self.dates.IDENTICAL, self.dates.YEAR), "year")
        self.assertEqual(self.dates.confidence(self.dates.IDENTICAL, self.dates.EXPLICIT), "high")

    def test_unambiguous_numeric_dates_parse(self):
        self.assertEqual(self.first("2023-04-11"), "2023-04-11")
        self.assertEqual(self.first("2023.04.11"), "2023-04-11")
        self.assertEqual(self.first("note 20230411"), "2023-04-11")
        self.assertEqual(self.first("202304111530 note"), "2023-04-11", "an Obsidian unique-note id")
        self.assertEqual(self.first("25-04-2023"), "2023-04-25", "25 cannot be a month")
        self.assertEqual(self.first("04-25-2023"), "2023-04-25")
        self.assertEqual(self.first("11 April 2023"), "2023-04-11")
        self.assertEqual(self.first("April 11, 2023"), "2023-04-11")

    def test_an_ambiguous_numeric_date_is_dropped(self):
        # Day-first and month-first are both in live use and the string says
        # nothing about which. Two readings means no reading.
        self.assertIsNone(self.first("11-04-2023"))

    def test_impossible_and_incomplete_dates_are_not_invented(self):
        self.assertIsNone(self.first("2023-02-31"))
        self.assertIsNone(self.first("2023-13-01"))
        self.assertIsNone(self.first("budget 2023"), "a bare year is not a date")

    def test_creation_keys_are_read_however_they_are_spelled(self):
        for line in ("Created: 2019-03-04", "date-created: 2019-03-04", "createdAt: 2019-03-04 09:30", "ctime: 2019-03-04"):
            found = self.dates.frontmatter_evidence(line + "\n")
            self.assertEqual([entry["date"] for entry in found], ["2019-03-04"], line)

    def test_an_unrelated_frontmatter_date_is_not_a_creation_date(self):
        self.assertEqual(self.dates.frontmatter_evidence("updated: 2019-03-04\n"), [])
        self.assertEqual(self.dates.frontmatter_evidence("due: 2019-03-04\n"), [])

    def test_a_daily_note_path_is_a_date_and_a_folder_of_notes_is_not(self):
        self.assertEqual(
            [entry["date"] for entry in self.dates.path_evidence("Journal/2018/04/11.md")], ["2018-04-11"]
        )
        self.assertEqual(
            [entry["date"] for entry in self.dates.path_evidence("Journal/2018/04 April/11.md")], ["2018-04-11"]
        )
        self.assertEqual(self.dates.path_evidence("Journal/2018/04/Weekly Review.md"), [])

    def test_a_stated_date_outranks_a_date_in_prose(self):
        stated = self.dates.body_evidence("# T\n\nCreated: 2017-05-06\n\nback in 1999-01-01 we\n")
        self.assertEqual([(entry["tier"], entry["date"]) for entry in stated], [("stated", "2017-05-06")])
        prose = self.dates.body_evidence("# T\n\nback in 1999-01-01 we\n")
        self.assertEqual([(entry["tier"], entry["date"]) for entry in prose], [("weak", "1999-01-01")])

    def test_a_daily_note_backlink_counts_as_stated(self):
        found = self.dates.body_evidence("# T\n\nwritten on [[2016-07-08]]\n")
        self.assertEqual([(entry["tier"], entry["date"]) for entry in found], [("stated", "2016-07-08")])

    def test_filesystem_times_are_weak_until_they_are_calibrated(self):
        day = datetime.date(2019, 3, 4)
        found = self.dates.filesystem_evidence(day, day)
        self.assertEqual([(entry["tier"], entry["source"]) for entry in found],
                         [("weak", "finder created"), ("weak", "filesystem modified")])
        self.assertEqual(self.dates.confidence(self.dates.IDENTICAL, "weak"), "low")

        # Trusting it is a decision the caller records, and only birthtime is
        # ever eligible: nothing makes a modification time a creation date.
        trusted = self.dates.filesystem_evidence(day, day, trust_birthtime=True)
        self.assertEqual([(entry["tier"], entry["source"]) for entry in trusted],
                         [("explicit", "finder created"), ("weak", "filesystem modified")])
        self.assertEqual(self.dates.confidence(self.dates.IDENTICAL, "explicit"), "high")

    def test_supplied_file_times_reach_the_tier_the_flags_ask_for(self):
        # The end-to-end path only runs on a filesystem that records a creation
        # time, so the wiring from flags to tiers is proven here instead.
        times = (datetime.date(2019, 3, 4), datetime.date(2024, 8, 1))
        plain = self.dates.extract_dates("Note.md", "Note.md", "", "# Note\n", times=times)
        self.assertEqual(plain, [], "no filesystem evidence unless it is asked for")

        weak = self.dates.extract_dates("Note.md", "Note.md", "", "# Note\n", times=times, include_file_times=True)
        self.assertEqual(
            [(entry["tier"], entry["source"], entry["date"]) for entry in weak],
            [("weak", "finder created", "2019-03-04"), ("weak", "filesystem modified", "2024-08-01")],
        )

        trusted = self.dates.extract_dates("Note.md", "Note.md", "", "# Note\n", times=times, trust_birthtime=True)
        self.assertEqual(
            [(entry["tier"], entry["source"], entry["date"]) for entry in trusted],
            [("explicit", "finder created", "2019-03-04")],
            "trusting the creation date does not drag the modification time along",
        )

    def test_calibration_measures_birthtime_against_files_that_state_a_date(self):
        def entry(birthtime, stated):
            candidates = []
            if stated:
                candidates.append(self.dates.candidate(datetime.date.fromisoformat(stated), "explicit", "filename", stated))
            return {"birthtime": birthtime, "candidates": candidates}

        report = self.dates.calibrate_birthtime([
            entry("2019-03-04", "2019-03-04"),   # agrees
            entry("2020-01-02", "2020-01-03"),   # a day out
            entry("2021-05-05", "2015-01-01"),   # disagrees
            entry("2019-03-04", None),           # unlabelled, still counted as a file
        ])
        self.assertEqual((report["with_birthtime"], report["labelled"]), (4, 3))
        self.assertEqual((report["same_day"], report["within_a_day"]), (1, 2))
        self.assertAlmostEqual(report["agreement"], 1 / 3, places=3)
        self.assertEqual(report["largest_cluster"], ("2019-03-04", 2))

    def test_calibration_says_nothing_when_no_file_carries_a_creation_date(self):
        report = self.dates.calibrate_birthtime([{"birthtime": "", "candidates": []}])
        self.assertEqual(report["with_birthtime"], 0)
        self.assertIsNone(report["agreement"])

    def test_a_stem_matches_across_a_date_prefix_and_a_copy_suffix(self):
        self.assertEqual(self.dates.match_stem("2023-04-11 Standup Notes.md"), "standup notes")
        self.assertEqual(self.dates.match_stem("202304111530 Standup Notes.md"), "standup notes")
        self.assertEqual(self.dates.match_stem("Standup Notes (1).md"), "standup notes")
        self.assertEqual(self.dates.match_stem("Standup Notes 20230411.md"), "standup notes")
        self.assertEqual(self.dates.match_stem("2023 Budget.md"), "2023 budget", "a year in a title is part of it")

    def test_a_file_that_dates_itself_twice_is_contradicted(self):
        disagreeing = self.dates.extract_dates("2023-04-11 N.md", "2023-04-11 N.md", "created: 2022-01-01\n", "# N\n")
        self.assertTrue(self.dates.self_contradicts(disagreeing))
        agreeing = self.dates.extract_dates("2023-04-11 N.md", "2023-04-11 N.md", "created: 2023-04-11\n", "# N\n")
        self.assertFalse(self.dates.self_contradicts(agreeing))

    def test_confidence_is_the_weaker_of_the_two_axes(self):
        self.assertEqual(self.dates.confidence(self.dates.IDENTICAL, self.dates.EXPLICIT), "high")
        self.assertEqual(self.dates.confidence(self.dates.TITLED, self.dates.EXPLICIT), "high")
        self.assertEqual(self.dates.confidence(self.dates.SIMILAR, self.dates.EXPLICIT), "medium")
        self.assertEqual(self.dates.confidence(self.dates.IDENTICAL, self.dates.STATED), "medium")
        self.assertEqual(self.dates.confidence(self.dates.IDENTICAL, self.dates.WEAK), "low")

    def test_one_property_is_added_and_nothing_else_moves(self):
        order = ["type", "status", "domain", "date"]
        original = b"---\ntype: note\nstatus: active\ndomain: work\n---\n\n# Title\n\nbody\n"
        revised, reason = self.dates.insert_scalar_property(original, "date", "2023-04-11", order)
        self.assertIsNone(reason)
        self.assertEqual(
            revised,
            b"---\ntype: note\nstatus: active\ndomain: work\ndate: 2023-04-11\n---\n\n# Title\n\nbody\n",
        )

    def test_line_endings_and_a_byte_order_mark_survive(self):
        order = ["type", "date"]
        crlf = b"---\r\ntype: note\r\n---\r\n\r\nbody\r\n"
        revised, _ = self.dates.insert_scalar_property(crlf, "date", "2023-04-11", order)
        self.assertEqual(revised, b"---\r\ntype: note\r\ndate: 2023-04-11\r\n---\r\n\r\nbody\r\n")
        bom = b"\xef\xbb\xbf---\ntype: note\n---\nbody\n"
        revised, _ = self.dates.insert_scalar_property(bom, "date", "2023-04-11", order)
        self.assertEqual(revised, b"\xef\xbb\xbf---\ntype: note\ndate: 2023-04-11\n---\nbody\n")

    def test_a_note_this_tool_cannot_write_is_refused_rather_than_repaired(self):
        order = ["type", "date"]
        _, reason = self.dates.insert_scalar_property(b"# Bare\n", "date", "2023-04-11", order)
        self.assertIn("no frontmatter block", reason)
        _, reason = self.dates.insert_scalar_property(b"---\ntype: note\n\n# oops\n", "date", "2023-04-11", order)
        self.assertIn("no closing delimiter", reason)
        dated = b"---\ntype: note\ndate: 2015-01-01\n---\nbody\n"
        revised, reason = self.dates.insert_scalar_property(dated, "date", "2023-04-11", order)
        self.assertIsNone(revised)
        self.assertEqual(reason, "date is already set to 2015-01-01")

    def test_an_empty_property_is_the_slot_this_fills(self):
        # Obsidian writes a bare `date:` for a property it knows and has no value
        # for. Refusing that would leave every note it touched unfillable.
        order = ["type", "date"]
        revised, reason = self.dates.insert_scalar_property(b"---\ntype: note\ndate:\n---\nbody\n", "date", "2023-04-11", order)
        self.assertIsNone(reason)
        self.assertEqual(revised, b"---\ntype: note\ndate: 2023-04-11\n---\nbody\n")

    def test_a_bare_key_above_list_items_is_a_list_and_is_left_alone(self):
        order = ["type", "date"]
        listed = b"---\ntype: note\ndate:\n  - 2015-01-01\n---\nbody\n"
        revised, reason = self.dates.insert_scalar_property(listed, "date", "2023-04-11", order)
        self.assertIsNone(revised)
        self.assertEqual(reason, "date is already set to a list")


class DateBackfillTests(unittest.TestCase):
    """The `dates` mode end to end, against a vault and an archive on disk."""

    BODY = "# Standup\n\nthe original body line\nsecond line\n"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.vault = self.root / "Loom"
        self.archive = self.root / "Archive"
        self.archive.mkdir(parents=True)
        self.write(self.vault / "99 Meta/99.02 Schemas/0.00 Vault Schema.md", SCHEMA)

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def note(self, relative, body, properties="type: note\nstatus: active\ndomain: technology\n"):
        return self.write(self.vault / relative, "---\n" + properties + "---\n\n" + body)

    def dates(self, *extra, **kwargs):
        argv = ["dates", "--vault", str(self.vault)]
        if kwargs.get("archive", True):
            argv += ["--archive", str(kwargs.get("archive_path", self.archive))]
        result = run_script(*(argv + list(extra)))
        return json.loads(result.stdout)

    def plan(self, payload):
        return {row["note"]: row for row in payload["data"]["plan"]}

    def test_the_earliest_archive_copy_dates_the_note(self):
        # Two copies of one note: the older is when it was written, and a later
        # revision saved under a newer name must not overwrite that.
        self.note("04 Technology/4.03 Obsidian/Standup.md", self.BODY)
        self.write(self.archive / "a/2021-11-30 Standup.md", self.BODY)
        self.write(self.archive / "b/2019-03-04 Standup.md", self.BODY)
        row = self.plan(self.dates())["04 Technology/4.03 Obsidian/Standup.md"]
        self.assertEqual((row["date"], row["match"], row["confidence"]), ("2019-03-04", "identical", "high"))

    def test_a_name_match_needs_to_be_unique_on_both_sides(self):
        self.note("04 Technology/4.03 Obsidian/Roadmap.md", "# Roadmap\n\nrewritten since\n")
        self.write(self.archive / "x/2020-02-03 Roadmap.md", "# Roadmap\n\nthe first draft\n")
        row = self.plan(self.dates())["04 Technology/4.03 Obsidian/Roadmap.md"]
        self.assertEqual((row["date"], row["match"]), ("2020-02-03", "named"))

        # A second archive file of the same name makes the pairing a guess.
        self.write(self.archive / "y/2011-01-01 Roadmap.md", "# Roadmap\n\nsomething else entirely\n")
        self.assertNotIn("04 Technology/4.03 Obsidian/Roadmap.md", self.plan(self.dates()))

    def test_a_note_dates_itself_from_its_own_name(self):
        self.note(
            "01 Personal/1.01 Journal/2021-12-25 Gift List.md",
            "# Gift List\n\nsocks\n",
            "type: journal\nstatus: active\ndomain: personal\nsubdomain: journal\n",
        )
        payload = self.dates("--self-only", archive=False)
        row = self.plan(payload)["01 Personal/1.01 Journal/2021-12-25 Gift List.md"]
        self.assertEqual((row["date"], row["match"], row["confidence"]), ("2021-12-25", "self", "high"))

    def test_a_dry_run_writes_nothing(self):
        path = self.note("04 Technology/4.03 Obsidian/Standup.md", self.BODY)
        before = path.read_bytes()
        payload = self.dates()
        self.assertTrue(payload["data"]["dryRun"])
        self.assertEqual(path.read_bytes(), before)

    def test_apply_adds_one_line_and_leaves_every_other_byte_alone(self):
        path = self.note("04 Technology/4.03 Obsidian/Standup.md", self.BODY)
        self.write(self.archive / "a/2019-03-04 Standup.md", self.BODY)
        payload = self.dates("--apply")
        self.assertEqual(len(payload["data"]["applied"]), 1)
        self.assertEqual(payload["data"]["refused"], [])
        self.assertEqual(
            path.read_text(encoding="utf-8"),
            "---\ntype: note\nstatus: active\ndomain: technology\ndate: 2019-03-04\n---\n\n" + self.BODY,
            "the date lands in schema order and nothing else changes",
        )

    def test_applying_twice_changes_nothing_the_second_time(self):
        self.note("04 Technology/4.03 Obsidian/Standup.md", self.BODY)
        self.write(self.archive / "a/2019-03-04 Standup.md", self.BODY)
        self.dates("--apply")
        second = self.dates("--apply")
        self.assertEqual(second["data"]["applied"], [])
        self.assertEqual(second["data"]["counts"]["already_dated"], 1)

    def test_an_existing_date_is_never_overwritten(self):
        path = self.note(
            "04 Technology/4.03 Obsidian/Already Dated.md",
            "# Already Dated\n\nbody\n",
            "type: note\nstatus: active\ndomain: technology\ndate: 2015-01-01\n",
        )
        self.write(self.archive / "a/1999-09-09 Already Dated.md", "# Already Dated\n\nbody\n")
        payload = self.dates("--apply")
        self.assertEqual(payload["data"]["counts"]["already_dated"], 1)
        self.assertNotIn("04 Technology/4.03 Obsidian/Already Dated.md", self.plan(payload))
        self.assertIn("date: 2015-01-01", path.read_text(encoding="utf-8"))

    def test_a_note_carrying_an_empty_date_key_is_filled(self):
        path = self.note(
            "04 Technology/4.03 Obsidian/Standup.md",
            self.BODY,
            "type: note\nstatus: active\ndomain: technology\ndate:\n",
        )
        self.write(self.archive / "a/2019-03-04 Standup.md", self.BODY)
        payload = self.dates("--apply")
        self.assertEqual(payload["data"]["counts"]["already_dated"], 0, "an empty key is not a value")
        self.assertEqual(len(payload["data"]["applied"]), 1)
        self.assertEqual(
            path.read_text(encoding="utf-8"),
            "---\ntype: note\nstatus: active\ndomain: technology\ndate: 2019-03-04\n---\n\n" + self.BODY,
        )

    def test_a_source_note_is_held_for_review(self):
        # A source's subject date is the work's, not the day the note was made,
        # so perfect evidence still does not write one unattended.
        path = self.note(
            "04 Technology/4.03 Obsidian/Some Paper.md",
            "# Some Paper\n\nabstract\n",
            "type: source\nstatus: active\ndomain: technology\nsource_kind: article\n",
        )
        self.write(self.archive / "a/2018-06-07 Some Paper.md", "# Some Paper\n\nabstract\n")
        payload = self.dates("--apply")
        row = self.plan(payload)["04 Technology/4.03 Obsidian/Some Paper.md"]
        self.assertEqual(row["confidence"], "medium")
        self.assertEqual(payload["data"]["applied"], [])
        self.assertNotIn("date:", path.read_text(encoding="utf-8"))

        # Named explicitly, it is written — and the rest of the frontmatter survives.
        self.dates("--apply", "--ids", row["id"])
        self.assertIn("date: 2018-06-07", path.read_text(encoding="utf-8"))
        self.assertIn("source_kind: article", path.read_text(encoding="utf-8"))

    def test_a_date_stated_only_in_prose_is_held_for_review(self):
        self.note("07 Administration/7.01 Health/Checkup.md", "# Checkup\n\nresults were fine\n",
                  "type: note\nstatus: active\ndomain: administration\nsubdomain: health\n")
        self.write(self.archive / "a/Checkup.md", "# Checkup\n\nCreated: 14 August 2017\n\nresults were fine\n")
        row = self.plan(self.dates())["07 Administration/7.01 Health/Checkup.md"]
        self.assertEqual((row["date"], row["evidence"], row["confidence"]), ("2017-08-14", "stated", "medium"))

    def test_a_note_with_no_evidence_is_reported_not_guessed(self):
        self.note("04 Technology/4.03 Obsidian/Undated.md", "# Undated\n\nno dates here at all\n")
        payload = self.dates()
        self.assertEqual(payload["data"]["counts"]["no_evidence"], 1)
        self.assertEqual(payload["data"]["plan"], [])
        report = Path(payload["artifacts"][0]).read_text(encoding="utf-8")
        self.assertIn("These need a date typed by hand.", report)

    def test_a_creation_date_is_weak_until_it_is_trusted(self):
        # Only birthtime can be promoted, and only on a filesystem that records
        # one, so this asserts the tiering rather than a specific date.
        self.note("04 Technology/4.03 Obsidian/Standup.md", self.BODY)
        copy = self.write(self.archive / "a/Standup.md", self.BODY)
        if not hasattr(copy.stat(), "st_birthtime"):
            self.skipTest("this filesystem records no creation time")

        weak = self.plan(self.dates("--include-file-times"))["04 Technology/4.03 Obsidian/Standup.md"]
        self.assertEqual((weak["evidence"], weak["confidence"]), ("weak", "low"))
        self.assertEqual(self.dates("--include-file-times", "--apply")["data"]["applied"], [])

        trusted = self.plan(self.dates("--trust-birthtime"))["04 Technology/4.03 Obsidian/Standup.md"]
        self.assertEqual((trusted["evidence"], trusted["confidence"]), ("explicit", "high"))

    def test_the_report_calibrates_creation_dates_against_stated_ones(self):
        self.note("04 Technology/4.03 Obsidian/Standup.md", self.BODY)
        self.write(self.archive / "a/2019-03-04 Standup.md", self.BODY)
        payload = self.dates()
        calibration = payload["data"]["birthtimeCalibration"]
        self.assertEqual(calibration["files"], 1)
        report = Path(payload["artifacts"][0]).read_text(encoding="utf-8")
        self.assertIn("## Finder creation dates", report)

    def test_an_unknown_id_is_refused_with_the_ids_on_offer(self):
        self.note("04 Technology/4.03 Obsidian/Standup.md", self.BODY)
        self.write(self.archive / "a/2019-03-04 Standup.md", self.BODY)
        payload = self.dates("--apply", "--ids", "deadbeef1234")
        self.assertEqual(payload["status"], "error")
        self.assertTrue(payload["errors"][0]["message"].startswith("unknown id deadbeef1234"))

    def test_an_archive_kept_inside_the_vault_is_a_source_and_not_a_target(self):
        self.note("04 Technology/4.03 Obsidian/Standup.md", self.BODY)
        inside = self.write(self.vault / "98 Archive/2019-03-04 Standup.md", self.BODY)
        payload = self.dates("--apply", archive_path=self.vault / "98 Archive")
        self.assertEqual([entry["note"] for entry in payload["data"]["applied"]], ["04 Technology/4.03 Obsidian/Standup.md"])
        self.assertEqual(inside.read_text(encoding="utf-8"), self.BODY, "the archive copy is never written")

    def test_the_archive_is_opened_read_only(self):
        self.note("04 Technology/4.03 Obsidian/Standup.md", self.BODY)
        copy = self.write(self.archive / "a/2019-03-04 Standup.md", self.BODY)
        before = copy.read_bytes()
        self.dates("--apply")
        self.assertEqual(copy.read_bytes(), before)

    def test_a_property_the_schema_does_not_approve_is_refused(self):
        self.note("04 Technology/4.03 Obsidian/Standup.md", self.BODY)
        payload = self.dates("--date-property", "invented")
        self.assertEqual(payload["status"], "error")
        self.assertIn("not an approved property", payload["errors"][0]["message"])

    def test_the_mode_needs_a_source_of_evidence(self):
        self.note("04 Technology/4.03 Obsidian/Standup.md", self.BODY)
        payload = json.loads(run_script("dates", "--vault", str(self.vault)).stdout)
        self.assertEqual(payload["status"], "error")
        self.assertIn("--archive", payload["errors"][0]["message"])

    def test_an_archive_path_that_is_not_there_is_an_error(self):
        # Silently finding nothing is how a typo'd path reads as "no dates survive".
        self.note("04 Technology/4.03 Obsidian/Standup.md", self.BODY)
        payload = self.dates(archive_path=self.root / "Nowhere")
        self.assertEqual(payload["status"], "error")
        self.assertIn("archive root does not exist", payload["errors"][0]["message"])

    def test_the_dates_flags_belong_to_dates_mode(self):
        payload = json.loads(run_script("drift", "--vault", str(self.vault), "--archive", str(self.archive)).stdout)
        self.assertEqual(payload["status"], "error")
        self.assertIn("belong to dates mode", payload["errors"][0]["message"])


class RoutingChatHandler(StubChatHandler):
    """Answer each classification by the note's title, so a routing test need not
    predict the order classify_items visits notes in."""

    by_title = {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        self.__class__.requests.append(payload)
        user = next(message for message in payload["messages"] if message["role"] == "user")
        title = json.loads(user["content"]).get("title", "")
        response = self.__class__.by_title.get(title, ok_response())
        content = response if isinstance(response, str) else json.dumps(response)
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class CrossVaultRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.vault = self.root / "vault"
        (self.vault / "99 Meta").mkdir(parents=True)
        (self.vault / "00 Inbox").mkdir()
        # A sibling vault's inbox, outside this vault entirely.
        self.work_inbox = self.root / "Work" / "00 Inbox"
        self.work_inbox.mkdir(parents=True)
        self.set_schema(self.work_inbox)

    def tearDown(self):
        self.tmp.cleanup()

    def set_schema(self, inbox, base=None):
        # SOURCES_SCHEMA defines the transcript source kind the pair tests rely on.
        (self.vault / "99 Meta" / "0.00 Vault Schema.md").write_text(
            with_other_vaults(SOURCES_SCHEMA if base is None else base, inbox), encoding="utf-8"
        )

    def write_note(self, relative, text):
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def record_for(self, result, source):
        plan = json.loads((Path(result["data"]["run_directory"]) / "plan.json").read_text(encoding="utf-8"))
        return next(row for row in plan["records"] if row["source"] == source)

    def route_run(self, by_title, *extra):
        RoutingChatHandler.by_title = by_title
        with StubServer([], handler_cls=RoutingChatHandler) as server:
            result = run_script(
                "inbox", "--vault", str(self.vault), "--base-url", server.url, "--no-embeddings", *extra
            )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads(result.stdout)

    def test_belongs_to_plans_a_route_to_the_sibling_inbox(self):
        self.write_note("00 Inbox/Waste Heat Meeting.md", "# Waste Heat Meeting\n\nQuarterly work-project sync.\n")
        result = self.route_run({"Waste Heat Meeting": ok_response(belongs_to="Work")})
        record = self.record_for(result, "00 Inbox/Waste Heat Meeting.md")
        self.assertEqual(record["action"], "route_vault")
        self.assertEqual(record["route_to"], "Work")
        self.assertEqual(record["route_destination"], str(self.work_inbox / "Waste Heat Meeting.md"))
        self.assertFalse(record["needs_review"])
        self.assertEqual(result["data"]["counts"]["routed_to_vault"], 1)
        self.assertEqual(result["data"]["counts"]["review_required"], 0)
        # Dry run: the note is planned as a route but nothing has moved.
        self.assertTrue((self.vault / "00 Inbox" / "Waste Heat Meeting.md").exists())
        self.assertFalse((self.work_inbox / "Waste Heat Meeting.md").exists())

    def test_apply_moves_a_routed_note_into_the_sibling_inbox(self):
        self.write_note("00 Inbox/RPAE Prep.md", "# RPAE Prep\n\nWork-project planning notes.\n")
        result = self.route_run({"RPAE Prep": ok_response(belongs_to="Work")}, "--apply")
        self.assertEqual(result["data"]["counts"]["routed_to_vault"], 1)
        self.assertFalse((self.vault / "00 Inbox" / "RPAE Prep.md").exists())
        moved = self.work_inbox / "RPAE Prep.md"
        self.assertTrue(moved.exists())
        self.assertIn("Work-project planning notes", moved.read_text(encoding="utf-8"))

    def write_transcript_pair(self):
        """A processed note and its raw transcript half, paired by a parent link."""
        self.write_note("00 Inbox/Load Shift Session.md", "# Load Shift Session\n\nA work meeting.\n")
        self.write_note(
            "00 Inbox/Load Shift Session - Transcript.md",
            '---\ntype: source\nstatus: raw\nsource_kind: transcript\n'
            'parent: "[[Load Shift Session]]"\n---\n'
            "# Load Shift Session - Transcript\n\n**Speaker** the verbatim record.\n",
        )

    def test_routing_the_processed_half_pulls_its_transcript(self):
        self.write_transcript_pair()
        result = self.route_run(
            {
                "Load Shift Session": ok_response(belongs_to="Work"),
                # The raw half files to Sources on its own; the pair keeps it together.
                "Load Shift Session - Transcript": ok_response(
                    metadata={"type": "source", "status": "raw", "domain": "technology", "source_kind": "transcript"}
                ),
            },
            "--apply",
        )
        self.assertEqual(result["data"]["counts"]["routed_to_vault"], 2)
        pair = self.record_for(result, "00 Inbox/Load Shift Session - Transcript.md")
        self.assertEqual(pair["action"], "route_vault")
        self.assertEqual(pair["route_to"], "Work")
        for name in ("Load Shift Session.md", "Load Shift Session - Transcript.md"):
            self.assertTrue((self.work_inbox / name).exists())
            self.assertFalse((self.vault / "00 Inbox" / name).exists())

    def test_routing_the_transcript_half_pulls_its_processed_note(self):
        # The model routes the raw half and files the processed half elsewhere:
        # the pair must still move together rather than split across vaults.
        self.write_transcript_pair()
        result = self.route_run(
            {
                "Load Shift Session": ok_response(),  # classified locally, not routed
                "Load Shift Session - Transcript": ok_response(belongs_to="Work"),
            },
            "--apply",
        )
        self.assertEqual(result["data"]["counts"]["routed_to_vault"], 2)
        processed = self.record_for(result, "00 Inbox/Load Shift Session.md")
        self.assertEqual(processed["action"], "route_vault")
        self.assertEqual(processed["route_to"], "Work")
        for name in ("Load Shift Session.md", "Load Shift Session - Transcript.md"):
            self.assertTrue((self.work_inbox / name).exists())
            self.assertFalse((self.vault / "00 Inbox" / name).exists())

    def test_an_undeclared_vault_name_falls_back_to_review(self):
        self.write_note("00 Inbox/Mystery.md", "# Mystery\n\nBelongs to a vault nobody declared.\n")
        result = self.route_run({"Mystery": ok_response(belongs_to="Archive Vault")})
        record = self.record_for(result, "00 Inbox/Mystery.md")
        self.assertTrue(record["needs_review"])
        self.assertNotEqual(record["action"], "route_vault")
        self.assertIn("not a declared sibling vault", record["review_reason"])
        self.assertEqual(result["data"]["counts"]["routed_to_vault"], 0)
        # Held, not moved.
        self.assertTrue((self.vault / "00 Inbox" / "Mystery.md").exists())

    def test_a_missing_sibling_inbox_falls_back_to_review(self):
        self.set_schema(self.root / "Nowhere" / "00 Inbox")
        self.write_note("00 Inbox/Orphan.md", "# Orphan\n\nWork content, but the target vault is gone.\n")
        result = self.route_run({"Orphan": ok_response(belongs_to="Work")}, "--apply")
        record = self.record_for(result, "00 Inbox/Orphan.md")
        self.assertTrue(record["needs_review"])
        self.assertNotEqual(record["action"], "route_vault")
        self.assertIn("inbox does not exist", record["review_reason"])
        self.assertEqual(result["data"]["counts"]["routed_to_vault"], 0)
        # Apply must not have invented the missing folder or moved the note.
        self.assertTrue((self.vault / "00 Inbox" / "Orphan.md").exists())
        self.assertFalse((self.root / "Nowhere").exists())


if __name__ == "__main__":
    unittest.main()

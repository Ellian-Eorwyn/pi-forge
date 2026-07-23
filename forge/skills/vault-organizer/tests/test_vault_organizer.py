#!/usr/bin/env python3

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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "vault-organizer.py"
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("vault_organizer", SCRIPT)
vault_organizer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vault_organizer)


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
        body = json.dumps({"choices": [{"message": {"content": json.dumps(response)}}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


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


def run_script(*args):
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env)


class VaultOrganizerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.vault = self.root / "vault"
        (self.vault / "99 System").mkdir(parents=True)
        (self.vault / "00 Inbox").mkdir()
        (self.vault / "99 System" / "0.00 Vault Schema.md").write_text(SCHEMA, encoding="utf-8")

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
        selected = [path.relative_to(self.vault).as_posix() for path in vault_organizer.selected_notes(self.vault, self.vault / "99 System" / "0.00 Vault Schema.md", "vault", None)]
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


class VaultOrganizerV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.vault = self.root / "vault"
        (self.vault / "99 System").mkdir(parents=True)
        (self.vault / "00 Inbox").mkdir()
        (self.vault / "99 System" / "0.00 Vault Schema.md").write_text(SCHEMA, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def write_note(self, relative, text):
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def run_ok(self, *args):
        result = run_script(*args)
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
        self.write_note("04 Sources/Dup.md", "---\ntype: old\n---\n" + body)
        with StubServer([ok_response()]) as server:
            payload = self.run_ok("vault", "--vault", str(self.vault), "--base-url", server.url, "--no-embeddings", "--apply")
        counts = payload["data"]["counts"]
        self.assertEqual(counts["duplicates_exact"], 1)
        self.assertEqual(counts["quarantined"], 1)
        self.assertEqual(len(server.requests), 1)
        quarantined = self.vault / ".vault-organizer" / "duplicates" / "04 Sources" / "Dup.md"
        self.assertTrue(quarantined.is_file())
        self.assertEqual(quarantined.read_text(encoding="utf-8"), "---\ntype: old\n---\n" + body)
        self.assertFalse((self.vault / "04 Sources" / "Dup.md").exists())
        self.assertTrue((self.vault / "04 Technology" / "4.03 Obsidian" / "Dup.md").is_file())
        plan = json.loads((Path(payload["data"]["run_directory"]) / "plan.json").read_text(encoding="utf-8"))
        groups = plan["dedupe"]["groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["winner"], "Sources/Dup.md")
        self.assertEqual(groups[0]["losers"][0]["path"], "04 Sources/Dup.md")

    def test_near_dupe_auto_and_review_band(self):
        shared = [f"Shared research line number {index} with substantive content." for index in range(1, 13)]
        long_body = "\n".join(shared + ["Marker VECA1 anchor line.", "Additional provenance line one.", "Additional provenance line two."]) + "\n"
        short_body = "\n".join(shared + ["Marker VECA2 anchor line."]) + "\n"
        self.write_note("Research/Report.md", long_body)
        self.write_note("04 Research/Report.md", short_body)
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
        self.assertTrue((self.vault / ".vault-organizer" / "duplicates" / "04 Research" / "Report.md").is_file())
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
        self.assertEqual(len([row for row in log_rows if row["status"] == "ok"]), 1)


if __name__ == "__main__":
    unittest.main()

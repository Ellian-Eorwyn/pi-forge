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


class StubServer:
    def __init__(self, responses):
        self.responses = list(responses)

    def __enter__(self):
        StubChatHandler.responses = list(self.responses)
        StubChatHandler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), StubChatHandler)
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
        return StubChatHandler.requests


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


if __name__ == "__main__":
    unittest.main()

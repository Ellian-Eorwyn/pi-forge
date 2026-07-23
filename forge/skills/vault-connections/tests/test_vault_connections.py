#!/usr/bin/env python3

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "vault-connections.py"
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("vault_connections", SCRIPT)
vault_connections = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vault_connections)


SCHEMA = """---
type: system
status: active
domain: meta
subdomain: schemas
---

# Vault Schema

## Approved properties

| Property | Required | Shape | Definition |
| --- | --- | --- | --- |
| `type` | yes | controlled scalar | Kind. |
| `status` | yes | controlled scalar | Lifecycle. |
| `domain` | yes | controlled scalar | Broad area. |
| `subdomain` | no | controlled scalar | Nested area. |
| `parent` | no | quoted wikilink | Nearest hub. |
| `related` | no | list of quoted wikilinks | Cross-cutting links. |
| `capture_type` | no | controlled scalar | How it arrived. |

## Note types

- `note` — General note.
- `concept` — A named idea.
- `place` — A location.
- `event` — A happening.
- `work` — A named work.

## Status values

- `active` — Current.
- `raw` — Unprocessed.

## Domains

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `personal` | `1` | `Personal` | Personal material. |
| `craft` | `2` | `Craft` | Making things. |
| `wiki` | `9` | `Wiki` | Cross-cutting entity notes. |
| `meta` | `99` | `Meta` | System notes. |

## Subdomains

### personal

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `journal` | `1` | `Journal` | Dated records. |

### wiki

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `concepts` | `1` | `Concepts` | Named ideas. |
| `practices` | `2` | `Practices` | Named methods. |
| `places` | `3` | `Places` | Locations. |
| `events` | `4` | `Events` | Happenings. |
| `terms` | `5` | `Terms` | Jargon. |
| `works` | `6` | `Works` | Named works. |

### meta

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `schemas` | `2` | `Schemas` | Schema notes. |

## Project registry

| Approved value | Domain | Subdomain | Number | Definition |
| --- | --- | --- | --- | --- |
| `"[[Pi Forge]]"` | `craft` |  | `90` | Tooling. |

## Source kinds

- `book` — A book.

## Capture types

- `manual` — Typed directly.
- `generated` — Produced by a tool.

## Legacy normalization map

| Legacy input | Canonical output |
| --- | --- |
| `type: daily` | `type: note` |

## Folder routing

Paths are derived from the registries above.
"""

EMBED_WORDS = ["buddhism", "emptiness", "meditation", "garden", "compost", "energy", "cooling", "misc"]


def stub_vector(text):
    """Topic dimensions plus a per-text signature.

    The signature keeps two notes on the same topic from scoring ~1.0, so tests
    exercise the real near-duplicate ceiling instead of tripping over it.
    """
    lowered = text.lower()
    topic = [3.0 if word in lowered else 0.1 for word in EMBED_WORDS]
    digest = hashlib.sha256(lowered.encode("utf-8")).digest()
    signature = [byte / 255.0 * 2.0 for byte in digest[:8]]
    return topic + signature


class StubHandler(BaseHTTPRequestHandler):
    connect = True
    embeddings_ok = True
    canonical_titles = {}
    kinds = {}

    def log_message(self, *args):
        pass

    def _send(self, status, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path.endswith("/embeddings"):
            if not StubHandler.embeddings_ok:
                self._send(503, {"error": "embeddings offline"})
                return
            vectors = [{"index": index, "embedding": stub_vector(text)} for index, text in enumerate(payload["input"])]
            self._send(200, {"data": vectors})
            return
        message = payload["messages"][-1]["content"]
        if "LINK TARGET" in message:
            target = message.splitlines()[0].replace("LINK TARGET:", "").strip()
            title = StubHandler.canonical_titles.get(target.lower(), target)
            kind = StubHandler.kinds.get(target.lower(), "concept")
            self._send(200, {"choices": [{"message": {"content": json.dumps({"kind": kind, "title": title, "summary": f"About {title}."})}}]})
            return
        judgment = (
            {"connect": True, "strength": "strong", "kind": "generalization", "reason": "one idea carried across domains"}
            if StubHandler.connect
            else {"connect": False, "reason": "only shared vocabulary"}
        )
        self._send(200, {"choices": [{"message": {"content": json.dumps(judgment)}}]})


class VaultConnectionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        StubHandler.connect = True
        StubHandler.embeddings_ok = True
        StubHandler.canonical_titles = {}
        StubHandler.kinds = {}
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.vault = Path(self.temporary.name) / "vault"
        self.write("99 Meta/99.02 Schemas/0.00 Vault Schema.md", SCHEMA)

    def write(self, relative, text):
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def write_bytes(self, relative, data):
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def run_command(self, *argv):
        process = subprocess.run(
            [sys.executable, str(SCRIPT), *argv, "--vault", str(self.vault), "--base-url", f"{self.base}/v1/chat/completions"],
            capture_output=True,
            text=True,
            env={
                "FORGE_EMBEDDINGS_URL": f"{self.base}/v1/embeddings",
                "FORGE_EMBEDDINGS_MODEL": "stub-embed",
                "PATH": "/usr/bin:/bin",
            },
        )
        self.assertTrue(process.stdout.strip(), f"no stdout; stderr={process.stderr[-2000:]}")
        return json.loads(process.stdout), process

    def seed_pair(self):
        """Two notes long enough to clear the minimum body length for embedding."""
        self.write(
            "01 Personal/Emptiness Practice.md",
            "---\ntype: note\nstatus: active\ndomain: personal\nsubdomain: journal\ncapture_type: manual\n---\n"
            "# Emptiness Practice\n\nSitting with emptiness in meditation this morning. Buddhism teaches that\n"
            "phenomena lack inherent existence, and holding that lightly changes how the\n"
            "rest of the day lands. Emptiness is not nothingness.\n",
        )
        self.write(
            "01 Personal/Meditation Log.md",
            "---\ntype: note\nstatus: active\ndomain: personal\nsubdomain: journal\ncapture_type: manual\n---\n"
            "# Meditation Log\n\nDaily meditation practice notes across the week. Buddhism, emptiness, and\n"
            "sitting with whatever arises. Shorter sessions on work days, longer on the\n"
            "weekend, and the same emptiness question underneath each one.\n",
        )

    # -- frontmatter merge ------------------------------------------------- #

    def schema(self):
        from vault_schema import parse_schema_note

        return parse_schema_note(SCHEMA)

    def merge(self, text, additions):
        return vault_connections.merge_related(text.encode("utf-8"), additions, self.schema())

    def test_merge_inserts_related_in_schema_property_order(self):
        original = "---\ntype: note\nstatus: active\ndomain: personal\nsubdomain: journal\ncapture_type: manual\n---\n# Body\n\nText.\n"
        merged, added, reason = self.merge(original, ['[[Other Note]]'])
        self.assertIsNone(reason)
        self.assertEqual(added, ["[[Other Note]]"])
        text = merged.decode("utf-8")
        self.assertIn('related:\n  - "[[Other Note]]"\n', text)
        # related sits after subdomain and before capture_type, per property_order
        self.assertLess(text.index("subdomain:"), text.index("related:"))
        self.assertLess(text.index("related:"), text.index("capture_type:"))
        self.assertTrue(text.endswith("# Body\n\nText.\n"))

    def test_merge_appends_to_existing_related_and_preserves_unapproved_keys(self):
        original = (
            "---\n"
            "type: note\n"
            "status: active\n"
            "domain: personal\n"
            "aliases:\n"
            "  - Alt Name\n"
            "cssclass: wide\n"
            "related:\n"
            '  - "[[First]]"\n'
            "---\n"
            "# Body\n\nParagraph with [[Inline]] link.\n"
        )
        merged, added, reason = self.merge(original, ['[[Second]]', '[[First]]'])
        self.assertIsNone(reason)
        self.assertEqual(added, ["[[Second]]"], "an already-present link must not be added twice")
        text = merged.decode("utf-8")
        self.assertIn("aliases:\n  - Alt Name\n", text)
        self.assertIn("cssclass: wide\n", text)
        self.assertIn('  - "[[First]]"\n  - "[[Second]]"\n', text)
        self.assertTrue(text.endswith("# Body\n\nParagraph with [[Inline]] link.\n"))

    def test_merge_body_bytes_are_untouched(self):
        body = "# Body\n\n\tTabbed line\n\n```\ncode --apply\n```\n\nTrailing   spaces   \n"
        original = f"---\ntype: note\nstatus: active\ndomain: personal\n---\n{body}"
        merged, _, reason = self.merge(original, ['[[Link]]'])
        self.assertIsNone(reason)
        self.assertTrue(merged.decode("utf-8").endswith(body))

    def test_merge_preserves_bom_and_crlf(self):
        original = "﻿---\r\ntype: note\r\nstatus: active\r\ndomain: personal\r\n---\r\n# Body\r\n"
        merged, _, reason = vault_connections.merge_related(original.encode("utf-8"), ['[[Link]]'], self.schema())
        self.assertIsNone(reason)
        self.assertTrue(merged.startswith(b"\xef\xbb\xbf"))
        text = merged.decode("utf-8-sig")
        self.assertIn('related:\r\n  - "[[Link]]"\r\n', text)
        self.assertTrue(text.endswith("# Body\r\n"))

    def test_merge_refuses_note_without_frontmatter(self):
        merged, _, reason = self.merge("# Body\n\nNo YAML block.\n", ['[[Link]]'])
        self.assertIsNone(merged)
        self.assertIn("no frontmatter", reason)

    def test_merge_refuses_unclosed_frontmatter(self):
        merged, _, reason = self.merge("---\ntype: note\n# Body\n", ['[[Link]]'])
        self.assertIsNone(merged)
        self.assertIn("closing delimiter", reason)

    def test_merge_refuses_inline_related_list(self):
        original = '---\ntype: note\nstatus: active\ndomain: personal\nrelated: ["[[First]]"]\n---\n# Body\n'
        merged, _, reason = self.merge(original, ['[[Second]]'])
        self.assertIsNone(merged)
        self.assertIn("inline value", reason)

    def test_merge_fills_an_empty_related_key(self):
        original = "---\ntype: note\nstatus: active\ndomain: personal\nrelated:\n---\n# Body\n"
        merged, added, reason = self.merge(original, ['[[Link]]'])
        self.assertIsNone(reason)
        self.assertEqual(added, ["[[Link]]"])
        self.assertIn('related:\n  - "[[Link]]"\n', merged.decode("utf-8"))

    def test_merge_reports_already_linked(self):
        original = '---\ntype: note\nstatus: active\ndomain: personal\nparent: "[[Hub]]"\n---\n# Body\n'
        merged, _, reason = self.merge(original, ['[[Hub]]'])
        self.assertIsNone(merged)
        self.assertEqual(reason, "already linked")

    # -- search ------------------------------------------------------------ #

    def test_search_ranks_semantically_and_degrades_without_embeddings(self):
        self.seed_pair()
        self.write(
            "02 Craft/Garden Beds.md",
            "---\ntype: note\nstatus: active\ndomain: craft\ncapture_type: manual\n---\n# Garden Beds\n\nCompost and garden soil.\n",
        )
        result, _ = self.run_command("index")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["notes"], 3)

        result, _ = self.run_command("search", "emptiness meditation")
        self.assertEqual(result["data"]["ranking"], "hybrid")
        self.assertEqual(result["data"]["hits"][0]["path"], "01 Personal/Emptiness Practice.md")

        StubHandler.embeddings_ok = False
        result, _ = self.run_command("search", "garden")
        self.assertEqual(result["data"]["ranking"], "lexical")
        self.assertEqual(result["data"]["hits"][0]["path"], "02 Craft/Garden Beds.md")
        self.assertTrue(any("semantic ranking unavailable" in warning for warning in result["warnings"]))

    # -- propose / apply --------------------------------------------------- #

    def test_propose_apply_writes_both_sides_and_is_idempotent(self):
        self.seed_pair()
        result, _ = self.run_command("propose")
        self.assertEqual(result["data"]["counts"]["proposed"], 1)
        proposal = result["data"]["proposals"][0]
        run_directory = result["data"]["runDirectory"]

        preview, _ = self.run_command("apply", "--run", run_directory, "--accept", proposal["id"], "--dry-run")
        self.assertEqual(preview["data"]["results"]["notes_updated"], 2)
        self.assertNotIn("related:", (self.vault / "01 Personal/Meditation Log.md").read_text(encoding="utf-8"))

        applied, _ = self.run_command("apply", "--run", run_directory, "--accept", proposal["id"])
        self.assertEqual(applied["data"]["results"]["links_added"], 2)
        left = (self.vault / "01 Personal/Emptiness Practice.md").read_text(encoding="utf-8")
        right = (self.vault / "01 Personal/Meditation Log.md").read_text(encoding="utf-8")
        self.assertIn('  - "[[Meditation Log]]"', left)
        self.assertIn('  - "[[Emptiness Practice]]"', right)

        again, _ = self.run_command("apply", "--run", run_directory, "--accept", proposal["id"])
        self.assertEqual(again["data"]["results"]["links_added"], 0)
        self.assertEqual(again["data"]["results"]["skipped"], 2)
        self.assertEqual(left, (self.vault / "01 Personal/Emptiness Practice.md").read_text(encoding="utf-8"))

    def test_apply_backs_up_every_note_it_rewrites(self):
        self.seed_pair()
        original = (self.vault / "01 Personal/Meditation Log.md").read_bytes()
        result, _ = self.run_command("propose")
        run_directory = Path(result["data"]["runDirectory"])
        self.run_command("apply", "--run", str(run_directory), "--accept", result["data"]["proposals"][0]["id"])
        backup = run_directory / "backup" / "01 Personal/Meditation Log.md"
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_bytes(), original)

    def test_already_linked_pairs_are_never_proposed(self):
        self.seed_pair()
        path = self.vault / "01 Personal/Emptiness Practice.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "capture_type: manual\n---", 'related:\n  - "[[Meditation Log]]"\ncapture_type: manual\n---'
            ),
            encoding="utf-8",
        )
        result, _ = self.run_command("propose")
        self.assertEqual(result["data"]["counts"]["proposed"], 0)
        self.assertEqual(result["data"]["counts"]["skipped_already_linked"], 1)

    def test_rejected_pairs_do_not_reappear(self):
        self.seed_pair()
        result, _ = self.run_command("propose")
        proposal = result["data"]["proposals"][0]
        self.run_command("apply", "--run", result["data"]["runDirectory"], "--reject", proposal["id"])
        again, _ = self.run_command("propose")
        self.assertEqual(again["data"]["counts"]["proposed"], 0)
        self.assertEqual(again["data"]["counts"]["skipped_already_decided"], 1)
        self.assertNotIn("related:", (self.vault / "01 Personal/Meditation Log.md").read_text(encoding="utf-8"))

    def test_model_rejection_produces_no_proposal(self):
        self.seed_pair()
        StubHandler.connect = False
        result, _ = self.run_command("propose")
        self.assertEqual(result["data"]["counts"]["proposed"], 0)
        self.assertEqual(result["data"]["counts"]["model_rejected"], 1)

    def test_apply_rejects_unknown_proposal_ids(self):
        self.seed_pair()
        result, _ = self.run_command("propose")
        failure, process = self.run_command("apply", "--run", result["data"]["runDirectory"], "--accept", "c-999")
        self.assertEqual(failure["status"], "error")
        self.assertEqual(process.returncode, 1)
        self.assertIn("unknown proposal ids", failure["errors"][0]["message"])

    # -- wiki -------------------------------------------------------------- #

    def seed_unresolved(self, target="Sunyata"):
        for index, name in enumerate(["Alpha", "Beta"]):
            self.write(
                f"01 Personal/{name}.md",
                f"---\ntype: note\nstatus: active\ndomain: personal\ncapture_type: manual\n---\n"
                f"# {name}\n\nBuddhism and emptiness note {index}, which references [[{target}]] in passing\n"
                f"and then keeps going for long enough to clear the minimum body length that\n"
                f"the embedding pass requires before it will consider a note at all.\n",
            )

    def test_wiki_creates_a_stub_at_the_compiled_path(self):
        self.seed_unresolved()
        result, _ = self.run_command("wiki", "--min-mentions", "2")
        proposals = [item for item in result["data"]["proposals"] if item["action"] == "create_wiki_note"]
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["destination"], "09 Wiki/9.01 Concepts/Sunyata.md")

        self.run_command("apply", "--run", result["data"]["runDirectory"], "--accept", proposals[0]["id"])
        created = self.vault / "09 Wiki/9.01 Concepts/Sunyata.md"
        self.assertTrue(created.is_file())
        text = created.read_text(encoding="utf-8")
        self.assertIn("type: concept\n", text)
        self.assertIn("domain: wiki\n", text)
        self.assertIn("subdomain: concepts\n", text)
        self.assertIn('  - "[[Alpha]]"', text)
        self.assertIn("## Mentioned in", text)

    def test_wiki_blocks_a_stub_that_collides_with_an_existing_basename(self):
        self.write(
            "02 Craft/Sunyata.md",
            "---\ntype: note\nstatus: active\ndomain: craft\n---\n# Sunyata\n\nAn existing note about emptiness.\n",
        )
        self.seed_unresolved(target="sunyataa")
        StubHandler.canonical_titles = {"sunyataa": "Sunyata"}
        result, _ = self.run_command("wiki", "--min-mentions", "2")
        self.assertEqual(result["data"]["counts"]["stubs_proposed"], 0)
        self.assertEqual(result["data"]["counts"]["blocked_by_collision"], 1)
        self.assertIn("already exists", result["data"]["blocked"][0]["reason"])
        self.assertFalse((self.vault / "09 Wiki/9.01 Concepts/Sunyata.md").exists())

    def test_wiki_reports_people_and_organizations_without_creating_them(self):
        self.seed_unresolved(target="Gillian")
        StubHandler.kinds = {"gillian": "person"}
        result, _ = self.run_command("wiki", "--min-mentions", "2")
        self.assertEqual(result["data"]["counts"]["stubs_proposed"], 0)
        self.assertEqual(result["data"]["directoryCandidates"], [{"title": "Gillian", "kind": "person", "mentions": 2}])
        self.assertFalse(any(path.parts[0] == "09 Wiki" for path in self.vault.rglob("*.md")))

    def test_wiki_never_turns_a_registered_project_into_a_concept_note(self):
        self.seed_unresolved(target="Pi Forge")
        result, _ = self.run_command("wiki", "--min-mentions", "2")
        self.assertEqual(result["data"]["counts"]["stubs_proposed"], 0)
        self.assertEqual(
            result["data"]["registeredProjectsMissingNotes"],
            [{"title": "Pi Forge", "project": "[[Pi Forge]]", "mentions": 2}],
        )
        self.assertFalse((self.vault / "09 Wiki").exists())

    def test_wiki_respects_the_mention_threshold(self):
        self.write(
            "01 Personal/Solo.md",
            "---\ntype: note\nstatus: active\ndomain: personal\n---\n# Solo\n\nOnly one mention of [[Rare Idea]].\n",
        )
        result, _ = self.run_command("wiki", "--min-mentions", "2")
        self.assertEqual(result["data"]["counts"]["unresolved_targets"], 0)

    def test_wiki_backfills_notes_that_name_an_existing_wiki_note(self):
        self.write(
            "09 Wiki/9.01 Concepts/Sunyata.md",
            "---\ntype: concept\nstatus: active\ndomain: wiki\nsubdomain: concepts\n---\n# Sunyata\n\nEmptiness as a concept.\n",
        )
        self.write(
            "01 Personal/Compost.md",
            "---\ntype: note\nstatus: active\ndomain: personal\ncapture_type: manual\n---\n"
            "# Compost\n\nTurning compost while thinking about Sunyata, unlinked.\n",
        )
        result, _ = self.run_command("wiki", "--min-mentions", "2")
        backfill = [item for item in result["data"]["proposals"] if item["id"].startswith("b-")]
        self.assertEqual(len(backfill), 1)
        self.assertEqual(backfill[0]["right"], "01 Personal/Compost.md")

        self.run_command("apply", "--run", result["data"]["runDirectory"], "--accept", backfill[0]["id"])
        self.assertIn('  - "[[Sunyata]]"', (self.vault / "01 Personal/Compost.md").read_text(encoding="utf-8"))
        self.assertIn('  - "[[Compost]]"', (self.vault / "09 Wiki/9.01 Concepts/Sunyata.md").read_text(encoding="utf-8"))

    def test_wiki_fails_closed_when_the_schema_has_no_wiki_domain(self):
        without_wiki = SCHEMA.replace("| `wiki` | `9` | `Wiki` | Cross-cutting entity notes. |\n", "")
        start = without_wiki.index("### wiki")
        without_wiki = without_wiki[:start] + without_wiki[without_wiki.index("### meta"):]
        self.write("99 Meta/99.02 Schemas/0.00 Vault Schema.md", without_wiki)
        self.seed_unresolved()
        result, process = self.run_command("wiki")
        self.assertEqual(result["status"], "error")
        self.assertEqual(process.returncode, 1)
        self.assertIn("no 'wiki' domain", result["errors"][0]["message"])

    # -- vector store ------------------------------------------------------ #

    def test_vectors_are_cached_and_reused_across_runs(self):
        self.seed_pair()
        first, _ = self.run_command("index")
        self.assertEqual(first["data"]["embeddings"]["embedded"], 2)
        second, _ = self.run_command("index")
        self.assertEqual(second["data"]["embeddings"]["embedded"], 0)
        self.assertEqual(second["data"]["embeddings"]["cached"], 2)
        self.assertTrue((self.vault / ".vault-connections/cache/vectors.f32").is_file())

    def test_vector_store_rebuilds_when_the_binary_is_truncated(self):
        self.seed_pair()
        self.run_command("index")
        binary = self.vault / ".vault-connections/cache/vectors.f32"
        binary.write_bytes(binary.read_bytes()[:7])
        result, _ = self.run_command("index")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["embeddings"]["embedded"], 2)

    def test_state_directory_is_invisible_to_note_selection(self):
        self.seed_pair()
        self.run_command("index")
        self.write(".vault-connections/runs/fake/report.md", "# Not a note\n")
        result, _ = self.run_command("index")
        self.assertEqual(result["data"]["notes"], 2)

    # -- status / doctor --------------------------------------------------- #

    def test_status_reports_the_run_phase(self):
        self.seed_pair()
        result, _ = self.run_command("propose")
        run_directory = result["data"]["runDirectory"]
        status = subprocess.run(
            [sys.executable, str(SCRIPT), "status", "--run", run_directory],
            capture_output=True,
            text=True,
        )
        payload = json.loads(status.stdout)
        self.assertEqual(payload["data"]["workflow"], "vault-connections")
        self.assertEqual(payload["data"]["phase"], "proposed")
        self.assertEqual(payload["data"]["proposals"], 1)

    def test_doctor_reports_schema_endpoints_and_wiki_readiness(self):
        self.seed_pair()
        result, _ = self.run_command("doctor")
        checks = result["data"]["checks"]
        self.assertTrue(checks["schema"]["ok"])
        self.assertTrue(checks["schema"]["wikiDomain"])
        self.assertEqual(checks["schema"]["wikiSubdomains"], ["concepts", "events", "places", "practices", "terms", "works"])
        self.assertTrue(checks["chat"]["ok"])
        self.assertTrue(checks["embeddings"]["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=1)

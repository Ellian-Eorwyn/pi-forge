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
from types import SimpleNamespace
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "vault-connections.py"
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("vault_connections", SCRIPT)
vault_connections = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vault_connections)

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
| `source_kind` | no | controlled scalar | Source format. |
| `capture_type` | no | controlled scalar | How it arrived. |

## Note types

- `note` — General note.
- `concept` — A named idea.
- `place` — A location.
- `event` — A happening.
- `work` — A named work.
- `person` — A named person.
- `source` — A source artifact.
- `template` — A reusable template.

## Status values

- `active` — Current.
- `raw` — Unprocessed.
- `complete` — Finished.

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
| `figures` | `7` | `Figures` | Named figures. |

### meta

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `schemas` | `2` | `Schemas` | Schema notes. |
| `templates` | `3` | `Templates` | Reusable note templates. |

## Project registry

| Approved value | Domain | Subdomain | Number | Definition |
| --- | --- | --- | --- | --- |
| `"[[Pi Forge]]"` | `craft` |  | `90` | Tooling. |

## Source kinds

- `book` — A book.
- `generated` — A generated research artifact.

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


def review_items(message):
    """The verification payload, when this request is a review rather than a
    pair judgment."""
    try:
        parsed = json.loads(message)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed.get("items") if isinstance(parsed, dict) else None


class StubHandler(BaseHTTPRequestHandler):
    connect = True
    embeddings_ok = True
    canonical_titles = {}
    kinds = {}
    entities = []
    classification_type = "note"
    chat_requests = []
    flags = {}
    grouping = None
    note_summary = None

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
        StubHandler.chat_requests.append(payload)
        message = payload["messages"][-1]["content"]
        review = review_items(message)
        if review is not None:
            verdicts = [
                {
                    "id": item["id"],
                    "verdict": "flag" if StubHandler.flags.get(item["id"]) else "ok",
                    "reason": StubHandler.flags.get(item["id"], ""),
                }
                for item in review
            ]
            self._send(200, {"choices": [{"message": {"content": json.dumps({"verdicts": verdicts})}}]})
            return
        messages = payload["messages"]
        joined = "\n".join(message["content"] for message in messages)
        message = messages[-1]["content"]
        if "RESEARCH ENTITY CANDIDATES" in joined:
            self._send(200, {"choices": [{"message": {"content": json.dumps({"entities": StubHandler.entities})}}]})
            return
        if "You group the claims from one research run" in joined:
            grouping = StubHandler.grouping
            if grouping is None:
                claim_ids = [claim["claimId"] for claim in json.loads(message).get("claims", [])]
                grouping = {"notes": [{"title": "Heat And Health", "claimIds": claim_ids}]}
            self._send(200, {"choices": [{"message": {"content": json.dumps(grouping)}}]})
            return
        if "You write the opening paragraph of a research note" in joined:
            title = json.loads(message).get("title", "")
            self._send(
                200,
                {"choices": [{"message": {"content": json.dumps({"summary": StubHandler.note_summary or f"What {title} establishes."})}}]},
            )
            return
        if "You classify Obsidian Markdown notes" in joined:
            classification = {
                "metadata": {
                    "type": StubHandler.classification_type,
                    "status": "active",
                    "domain": "personal",
                    "capture_type": "manual",
                },
                "needs_review": False,
                "review_reason": None,
                "suggestions": [],
            }
            self._send(200, {"choices": [{"message": {"content": json.dumps(classification)}}]})
            return
            return
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
        StubHandler.entities = []
        StubHandler.classification_type = "note"
        StubHandler.chat_requests = []
        StubHandler.flags = {}
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

    def seed_wiki_templates(self):
        body = "# {{title}}\n\n{{summary}}\n\n## Evidence\n\n{{evidence}}\n\n## Sources\n\n{{sources}}\n\n## Provenance\n\n{{provenance}}\n"
        frontmatter = (
            "---\ntype: template\nstatus: active\ndomain: meta\nsubdomain: templates\n"
            "capture_type: manual\n---\n"
        )
        for name in vault_connections.WIKI_TEMPLATE_NAMES.values():
            self.write(f"99 Meta/99.03 Templates/{name}", frontmatter + body)

    def import_args(self, run_directory, **overrides):
        values = {
            "vault": str(self.vault),
            "schema": None,
            "query": str(run_directory),
            "wiki_kinds": "concept,term",
            "include_artifact": [],
            "title_prefix": None,
            "limit": None,
            "base_url": f"{self.base}/v1/chat/completions",
            "model": "stub",
            "api_key": "",
            "request_timeout": 30,
            "cache_prompt": True,
            "think_prefill": False,
            "embeddings_model": "stub-embed",
            "embeddings_url": f"{self.base}/v1/embeddings",
            "per_note": 5,
            "min_similarity": 0.75,
            "max_similarity": 0.97,
            "prefer": "cross-domain",
            "max_candidates": 400,
            "min_mentions": 2,
            "notes": False,
            "notes_limit": vault_connections.DEFAULT_SUBTOPIC_NOTES,
            "verify": False,
            "think_url": None,
            "think_model": None,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def make_literature_run(self):
        root = Path(self.temporary.name) / "literature-run"
        root.mkdir()
        (root / "run_config.json").write_text(
            json.dumps({"input": {"path": "/research/Climate Corpus"}}) + "\n",
            encoding="utf-8",
        )
        (root / "run_state.json").write_text(json.dumps({"status": "complete"}) + "\n", encoding="utf-8")
        (root / "item_index.jsonl").write_text(
            json.dumps(
                {
                    "itemId": "item-1",
                    "documentId": "doc-1",
                    "sourceTitle": "Study One",
                    "itemType": "definition",
                    "itemText": "Thermal justice describes unequal exposure to heat.",
                    "directQuotes": "unequal exposure to heat",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "literature_summary.md").write_text(
            "---\nold: metadata\n---\n# Literature Summary\r\n\r\nBody  with  spaces.\r\n",
            encoding="utf-8",
            newline="",
        )
        (root / "key_terms.md").write_text(
            "# Key Terms\n\n| Term | Definition | Evidence |\n| --- | --- | --- |\n"
            "| Thermal justice | Unequal heat exposure. | item-1 |\n",
            encoding="utf-8",
        )
        return root

    def run_command(self, *argv, env_extra=None):
        # Review talks to a second endpoint. Tests that are not about it opt
        # out, so they neither reach a real server nor add stub traffic.
        arguments = list(argv)
        if not {"--no-verify", "--think-url"} & set(arguments):
            arguments.append("--no-verify")
        environment = {
            "FORGE_EMBEDDINGS_URL": f"{self.base}/v1/embeddings",
            "FORGE_EMBEDDINGS_MODEL": "stub-embed",
            "PATH": "/usr/bin:/bin",
            # Never resolve endpoints from the settings of whoever runs the tests.
            "PI_FORGE_AGENT_DIR": "/nonexistent-agent-directory",
        }
        environment.update(env_extra or {})
        process = subprocess.run(
            [sys.executable, str(SCRIPT), *arguments, "--vault", str(self.vault), "--base-url", f"{self.base}/v1/chat/completions"],
            capture_output=True,
            text=True,
            env=environment,
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

    def test_review_marks_doubted_proposals_and_puts_them_first(self):
        self.seed_pair()
        StubHandler.flags = {"c-001": "the notes only share vocabulary"}
        result, _ = self.run_command("propose", "--think-url", f"{self.base}/v1/chat/completions")
        proposal = result["data"]["proposals"][0]
        self.assertEqual(proposal["verified"], "flag")
        self.assertIn("share vocabulary", proposal["verifyReason"])
        self.assertEqual(result["data"]["counts"]["verification_flagged"], 1)
        report = (Path(result["data"]["runDirectory"]) / "report.md").read_text(encoding="utf-8")
        self.assertIn("## Needs attention", report)

    def test_review_never_drops_a_proposal(self):
        self.seed_pair()
        StubHandler.flags = {"c-001": "not a real connection"}
        result, _ = self.run_command("propose", "--think-url", f"{self.base}/v1/chat/completions")
        # Flagged, but still proposed: the human decides, not the reviewer.
        self.assertEqual(result["data"]["counts"]["proposed"], 1)
        self.assertEqual(len(result["data"]["proposals"]), 1)

    def test_an_unreachable_reviewer_leaves_proposals_unannotated(self):
        self.seed_pair()
        result, _ = self.run_command("propose", "--think-url", "http://127.0.0.1:9/v1/chat/completions")
        self.assertEqual(result["data"]["counts"]["proposed"], 1)
        self.assertNotIn("verified", result["data"]["proposals"][0])
        self.assertTrue(any("review skipped" in warning for warning in result["warnings"]))

    def test_propose_lists_the_notes_nothing_links_to(self):
        """A capability this skill has never had.

        The note index records outgoing links only, so nothing here can answer
        "what points at this?" without building a reverse index over the whole
        vault. Obsidian keeps one in memory. It is advisory — a note nobody links
        to is the best place to start looking for connections, not a fault.
        """
        self.seed_pair()
        _, env_extra = self.shim_env({"orphans": "01 Personal/Meditation Log.md\n01 Personal/Stray.md\n"})
        result, _ = self.run_command("propose", env_extra=env_extra)
        self.assertEqual(
            result["data"]["unlinkedNotes"], ["01 Personal/Meditation Log.md", "01 Personal/Stray.md"]
        )
        self.assertEqual(result["data"]["counts"]["unlinked_notes"], 2)
        report = Path(result["data"]["runDirectory"], "report.md").read_text(encoding="utf-8")
        self.assertIn("## Notes nothing links to (2)", report)

    def test_propose_without_obsidian_says_nothing_about_orphans(self):
        self.seed_pair()
        result, _ = self.run_command("propose")
        self.assertEqual(result["data"]["unlinkedNotes"], [])
        self.assertNotIn("unlinked_notes", result["data"]["counts"])
        report = Path(result["data"]["runDirectory"], "report.md").read_text(encoding="utf-8")
        self.assertNotIn("Notes nothing links to", report)

    def test_pair_judgments_use_the_non_thinking_model(self):
        self.seed_pair()
        self.run_command("propose")
        self.assertTrue(StubHandler.chat_requests)
        for request in StubHandler.chat_requests:
            self.assertEqual(request["model"], "chat")
            self.assertNotEqual(request["messages"][-1]["role"], "assistant")

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

    def test_apply_refuses_link_source_drift_before_rewriting_either_note(self):
        self.seed_pair()
        result, _ = self.run_command("propose")
        left = self.vault / "01 Personal/Emptiness Practice.md"
        right = self.vault / "01 Personal/Meditation Log.md"
        original_right = right.read_bytes()
        left.write_text(left.read_text(encoding="utf-8") + "\nChanged after review.\n", encoding="utf-8")
        rejected, process = self.run_command(
            "apply",
            "--run",
            result["data"]["runDirectory"],
            "--accept",
            result["data"]["proposals"][0]["id"],
        )
        self.assertEqual(process.returncode, 1)
        self.assertIn("link source changed", rejected["errors"][0]["message"])
        self.assertEqual(right.read_bytes(), original_right)

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

    def shim_env(self, script=None, **kwargs):
        """Fake Obsidian CLI, plus the env vars a subprocess needs to find it."""
        env = ShimEnvironment(script, vault_path=self.vault, vault_name="vault", **kwargs)
        self.addCleanup(env.cleanup)
        import os

        return env, {
            "PATH": str(env.bin) + os.pathsep + "/usr/bin:/bin",
            "FORGE_OBSIDIAN_CONFIG_DIR": str(env.config),
            "SHIM_SCRIPT": str(env.script_path),
            "SHIM_LOG": str(env.log),
            "SHIM_STATE": str(env.state),
            "SHIM_VAULT": str(env.vault),
        }

    def test_wiki_does_not_stub_a_note_the_vault_already_has_under_an_alias(self):
        """Obsidian leaves a bare `[[Alias]]` unresolved, and so do we.

        That agreement is the point: the link really is unresolved. But an
        unresolved target that an existing note already answers to is not a
        missing note, it is a link written the short way — and stubbing it would
        leave the vault with two notes for one thing.
        """
        self.write(
            "02 Craft/Emptiness.md",
            "---\ntype: note\nstatus: active\ndomain: craft\naliases:\n  - Sunyata\n---\n"
            "# Emptiness\n\nAn existing note about emptiness.\n",
        )
        self.seed_unresolved()
        result, _ = self.run_command("wiki", "--min-mentions", "2")
        self.assertEqual(result["data"]["counts"]["stubs_proposed"], 0)
        reason = result["data"]["blocked"][0]["reason"]
        self.assertIn("already declares `Sunyata` as an alias", reason)
        self.assertIn("[[Emptiness|Sunyata]]", reason)
        self.assertFalse((self.vault / "09 Wiki/9.01 Concepts/Sunyata.md").exists())

    def test_the_alias_guard_needs_no_obsidian(self):
        # Deliberately unconditional: it is a fix to our own resolution, not a
        # capability borrowed from the app.
        self.write(
            "02 Craft/Emptiness.md",
            "---\ntype: note\nstatus: active\ndomain: craft\naliases:\n  - Sunyata\n---\n# Emptiness\n\nBody.\n",
        )
        self.seed_unresolved()
        result, _ = self.run_command("wiki", "--min-mentions", "2", env_extra={"FORGE_OBSIDIAN_CLI": "off"})
        self.assertEqual(result["data"]["counts"]["stubs_proposed"], 0)
        self.assertEqual(result["data"]["counts"]["blocked_by_collision"], 1)

    def test_wiki_reports_where_our_unresolved_set_disagrees_with_obsidians(self):
        self.seed_unresolved()
        _, env_extra = self.shim_env({"unresolved": json.dumps([{"link": "Ghost", "count": "3"}])})
        result, _ = self.run_command("wiki", "--min-mentions", "2", env_extra=env_extra)
        check = result["data"]["unresolvedCrossCheck"]
        self.assertTrue(check["ok"])
        self.assertIn("sunyata", check["oursOnly"])
        self.assertIn("ghost", check["theirsOnly"])
        self.assertTrue(any("missing a rule Obsidian has" in warning for warning in result["warnings"]))
        self.assertTrue(any("index is probably stale" in warning for warning in result["warnings"]))

    def test_the_cross_check_is_absent_without_obsidian(self):
        self.seed_unresolved()
        result, _ = self.run_command("wiki", "--min-mentions", "2")
        self.assertFalse(result["data"]["unresolvedCrossCheck"]["ok"])
        self.assertEqual(result["warnings"], [])

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

    # -- reviewed research import ---------------------------------------- #

    def test_import_run_proposes_inbox_and_supported_wiki_notes(self):
        self.seed_wiki_templates()
        source_run = self.make_literature_run()
        StubHandler.classification_type = "source"
        StubHandler.entities = [
            {
                "kind": "concept",
                "title": "Thermal Justice",
                "summary": "Unequal exposure to heat and cooling.",
                "evidenceIds": ["item-1"],
                "sourceIds": ["doc-1"],
            }
        ]
        original_body = vault_connections.split_frontmatter((source_run / "literature_summary.md").read_bytes())["body"]
        with patch.object(
            vault_connections,
            "invoke_upstream_validator",
            return_value={"valid": True, "complete": True, "warnings": ["minor warning"]},
        ):
            result = vault_connections.command_import_run(self.import_args(source_run))
        self.assertEqual(result["data"]["sourceRunType"], "literature")
        proposals = result["data"]["proposals"]
        self.assertEqual([proposal["id"] for proposal in proposals], ["i-001", "i-002", "w-001"])
        self.assertEqual(proposals[0]["destination"], "00 Inbox/Climate Corpus - Literature Overview.md")
        self.assertEqual(proposals[2]["destination"], "09 Wiki/9.01 Concepts/Thermal Justice.md")
        imported = proposals[0]["content"]
        self.assertEqual(vault_connections.split_frontmatter(imported.encode("utf-8"))["body"], original_body)
        self.assertLess(imported.index("type:"), imported.index("status:"))
        self.assertLess(imported.index("status:"), imported.index("domain:"))
        self.assertLess(imported.index("domain:"), imported.index("capture_type:"))
        self.assertIn("status: complete\n", imported)
        self.assertIn("source_kind: generated\n", imported)
        self.assertIn("capture_type: generated\n", imported)
        self.assertTrue(any("minor warning" in warning for warning in result["warnings"]))
        self.assertFalse((self.vault / "00 Inbox").exists())
        self.assertFalse((self.vault / "09 Wiki").exists())
        self.assertFalse((self.vault / ".vault-connections/cache").exists())

    def test_import_apply_is_selective_dry_run_and_idempotent(self):
        self.seed_wiki_templates()
        source_run = self.make_literature_run()
        StubHandler.entities = []
        with patch.object(
            vault_connections,
            "invoke_upstream_validator",
            return_value={"valid": True, "complete": True, "warnings": []},
        ):
            proposed = vault_connections.command_import_run(self.import_args(source_run))
        run_directory = proposed["data"]["runDirectory"]
        apply_args = SimpleNamespace(
            vault=str(self.vault),
            schema=None,
            run=run_directory,
            accept="i-001",
            reject="i-002",
            dry_run=True,
        )
        preview = vault_connections.command_apply(apply_args)
        self.assertEqual(preview["data"]["results"]["notes_created"], 1)
        destination = self.vault / "00 Inbox/Climate Corpus - Literature Overview.md"
        self.assertFalse(destination.exists())
        apply_args.dry_run = False
        applied = vault_connections.command_apply(apply_args)
        self.assertEqual(applied["data"]["results"]["notes_created"], 1)
        self.assertTrue(destination.is_file())
        again = vault_connections.command_apply(apply_args)
        self.assertEqual(again["data"]["results"]["notes_created"], 0)
        self.assertEqual(again["data"]["results"]["skipped"], 1)
        self.assertFalse((self.vault / "00 Inbox/Climate Corpus - Key Terms.md").exists())

    def test_import_apply_refuses_source_drift_before_writing(self):
        self.seed_wiki_templates()
        source_run = self.make_literature_run()
        with patch.object(
            vault_connections,
            "invoke_upstream_validator",
            return_value={"valid": True, "complete": True, "warnings": []},
        ):
            proposed = vault_connections.command_import_run(self.import_args(source_run))
        (source_run / "key_terms.md").write_text("# Changed\n", encoding="utf-8")
        apply_args = SimpleNamespace(
            vault=str(self.vault),
            schema=None,
            run=proposed["data"]["runDirectory"],
            accept="i-001,i-002",
            reject=None,
            dry_run=False,
        )
        with self.assertRaisesRegex(vault_connections.UserError, "source artifact changed"):
            vault_connections.command_apply(apply_args)
        self.assertFalse((self.vault / "00 Inbox").exists())

    def test_import_apply_is_bound_to_originating_vault(self):
        self.seed_wiki_templates()
        source_run = self.make_literature_run()
        with patch.object(
            vault_connections,
            "invoke_upstream_validator",
            return_value={"valid": True, "complete": True, "warnings": []},
        ):
            proposed = vault_connections.command_import_run(self.import_args(source_run))
        other_vault = Path(self.temporary.name) / "other-vault"
        schema_path = other_vault / "99 Meta/99.02 Schemas/0.00 Vault Schema.md"
        schema_path.parent.mkdir(parents=True)
        schema_path.write_text(SCHEMA, encoding="utf-8")
        template_root = other_vault / "99 Meta/99.03 Templates"
        template_root.mkdir(parents=True)
        source_template_root = self.vault / "99 Meta/99.03 Templates"
        for name in vault_connections.WIKI_TEMPLATE_NAMES.values():
            (template_root / name).write_bytes((source_template_root / name).read_bytes())
        apply_args = SimpleNamespace(
            vault=str(other_vault),
            schema=None,
            run=proposed["data"]["runDirectory"],
            accept="i-001",
            reject=None,
            dry_run=False,
        )
        with self.assertRaisesRegex(vault_connections.UserError, "originating vault"):
            vault_connections.command_apply(apply_args)
        self.assertFalse((other_vault / "00 Inbox").exists())

    def test_import_apply_refuses_a_tampered_proposal_manifest(self):
        self.seed_wiki_templates()
        source_run = self.make_literature_run()
        with patch.object(
            vault_connections,
            "invoke_upstream_validator",
            return_value={"valid": True, "complete": True, "warnings": []},
        ):
            proposed = vault_connections.command_import_run(self.import_args(source_run))
        proposals_path = Path(proposed["data"]["runDirectory"]) / "proposals.jsonl"
        proposals_path.write_text(
            proposals_path.read_text(encoding="utf-8").replace("00 Inbox", "02 Craft", 1),
            encoding="utf-8",
        )
        apply_args = SimpleNamespace(
            vault=str(self.vault),
            schema=None,
            run=proposed["data"]["runDirectory"],
            accept="i-001",
            reject=None,
            dry_run=False,
        )
        with self.assertRaisesRegex(vault_connections.UserError, "proposals changed"):
            vault_connections.command_apply(apply_args)
        self.assertFalse((self.vault / "00 Inbox").exists())
        self.assertFalse((self.vault / "02 Craft").exists())

    def test_import_apply_rolls_back_a_partially_created_batch(self):
        self.seed_wiki_templates()
        source_run = self.make_literature_run()
        with patch.object(
            vault_connections,
            "invoke_upstream_validator",
            return_value={"valid": True, "complete": True, "warnings": []},
        ):
            proposed = vault_connections.command_import_run(self.import_args(source_run))
        apply_args = SimpleNamespace(
            vault=str(self.vault),
            schema=None,
            run=proposed["data"]["runDirectory"],
            accept="i-001,i-002",
            reject=None,
            dry_run=False,
        )
        original_create = vault_connections.atomic_create_bytes
        calls = 0

        def fail_second(path, data):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated second create failure")
            original_create(path, data)

        with patch.object(vault_connections, "atomic_create_bytes", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "simulated second create failure"):
                vault_connections.command_apply(apply_args)
        self.assertFalse((self.vault / "00 Inbox/Climate Corpus - Literature Overview.md").exists())
        self.assertFalse((self.vault / "00 Inbox/Climate Corpus - Key Terms.md").exists())

    def test_import_supports_an_explicit_extra_markdown_artifact(self):
        self.seed_wiki_templates()
        source_run = self.make_literature_run()
        (source_run / "appendix.md").write_text("# Appendix\n\nAdditional synthesis.\n", encoding="utf-8")
        with patch.object(
            vault_connections,
            "invoke_upstream_validator",
            return_value={"valid": True, "complete": True, "warnings": []},
        ):
            proposed = vault_connections.command_import_run(
                self.import_args(source_run, include_artifact=["appendix.md"])
            )
        inbox = [item for item in proposed["data"]["proposals"] if item["id"].startswith("i-")]
        self.assertEqual(len(inbox), 3)
        self.assertEqual(inbox[2]["sourceArtifact"], "appendix.md")
        with self.assertRaisesRegex(vault_connections.UserError, "unsafe import artifact path"):
            vault_connections.selected_import_artifacts(source_run, "literature", ["../escape.md"])

    def test_import_requires_selected_templates_and_doctor_is_non_breaking(self):
        source_run = self.make_literature_run()
        with patch.object(
            vault_connections,
            "invoke_upstream_validator",
            return_value={"valid": True, "complete": True, "warnings": []},
        ):
            with self.assertRaisesRegex(vault_connections.UserError, "Wiki Concept.md"):
                vault_connections.command_import_run(self.import_args(source_run))
        result, _ = self.run_command("doctor")
        self.assertFalse(result["data"]["checks"]["wikiTemplates"]["ok"])
        self.assertEqual(result["data"]["checks"]["wikiTemplates"]["requiredFor"], "import-run only")

    def test_import_detects_all_supported_run_types(self):
        root = Path(self.temporary.name)
        literature = root / "normal"
        literature.mkdir()
        (literature / "run_config.json").touch()
        (literature / "item_index.jsonl").touch()
        meta = root / "meta"
        meta.mkdir()
        (meta / "meta_config.json").touch()
        (meta / "meta_items.jsonl").touch()
        deep = root / "deep"
        deep.mkdir()
        (deep / "research_run.json").touch()
        (deep / "claim_register.jsonl").touch()
        self.assertEqual(vault_connections.detect_source_run(literature), "literature")
        self.assertEqual(vault_connections.detect_source_run(meta), "meta-literature")
        self.assertEqual(vault_connections.detect_source_run(deep), "deep-research")

    def test_meta_and_deep_imports_select_their_default_reports(self):
        self.seed_wiki_templates()
        root = Path(self.temporary.name)
        meta = root / "meta-run"
        meta.mkdir()
        (meta / "meta_config.json").write_text(
            json.dumps({"researchQuestion": "Heat Adaptation"}) + "\n",
            encoding="utf-8",
        )
        (meta / "run_state.json").write_text('{"status":"complete"}\n', encoding="utf-8")
        (meta / "meta_items.jsonl").write_text(
            json.dumps(
                {
                    "itemId": "meta-1",
                    "documentId": "doc-1",
                    "itemType": "synthesis",
                    "itemText": "Heat adaptation requires public infrastructure.",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (meta / "meta_artifacts.jsonl").touch()
        (meta / "meta_synthesis.md").write_text("# Meta Synthesis\n", encoding="utf-8")
        (meta / "concept_register.md").write_text("# Concept Register\n", encoding="utf-8")

        deep = root / "deep-run"
        deep.mkdir()
        (deep / "research_run.json").write_text(
            json.dumps({"question": "Heat Evidence"}) + "\n",
            encoding="utf-8",
        )
        (deep / "run_state.json").write_text('{"status":"complete"}\n', encoding="utf-8")
        (deep / "source_index.json").write_text("{}\n", encoding="utf-8")
        (deep / "evidence_items.jsonl").write_text(
            json.dumps({"evidenceId": "e-1", "sourceId": "source-1", "text": "Supported evidence."}) + "\n",
            encoding="utf-8",
        )
        (deep / "claim_register.jsonl").write_text(
            json.dumps(
                {
                    "claimId": "claim-1",
                    "text": "Supported claim.",
                    "sourceIds": ["source-1"],
                    "evidenceIds": ["e-1"],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (deep / "deep_research_report.md").write_text("# Deep Research\n", encoding="utf-8")

        with patch.object(
            vault_connections,
            "invoke_upstream_validator",
            return_value={"valid": True, "complete": True, "warnings": []},
        ):
            meta_result = vault_connections.command_import_run(self.import_args(meta))
            deep_result = vault_connections.command_import_run(self.import_args(deep))
        self.assertEqual(meta_result["data"]["sourceRunType"], "meta-literature")
        self.assertEqual(
            [proposal["sourceArtifact"] for proposal in meta_result["data"]["proposals"] if proposal["id"].startswith("i-")],
            ["meta_synthesis.md", "concept_register.md"],
        )
        self.assertEqual(deep_result["data"]["sourceRunType"], "deep-research")
        self.assertEqual(
            [proposal["sourceArtifact"] for proposal in deep_result["data"]["proposals"] if proposal["id"].startswith("i-")],
            ["deep_research_report.md"],
        )

    def make_deep_run(self, claims=None, evidence=None, sources=None):
        root = Path(self.temporary.name) / f"deep-notes-{len(list(Path(self.temporary.name).iterdir()))}"
        root.mkdir()
        (root / "research_run.json").write_text(json.dumps({"question": "Urban heat"}) + "\n", encoding="utf-8")
        (root / "run_state.json").write_text('{"status":"complete"}\n', encoding="utf-8")
        (root / "source_index.json").write_text(
            json.dumps(
                {
                    "sources": sources
                    or [{"sourceId": "src-1", "finalUrl": "https://example.org/heat", "title": "Heat Study"}]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "evidence_items.jsonl").write_text(
            "".join(
                json.dumps(row) + "\n"
                for row in (
                    evidence
                    or [
                        {
                            "evidenceId": "ev-1",
                            "sourceId": "src-1",
                            "text": "Shade lowers surface temperature.",
                            "directQuote": "Shade lowered surface temperature by nine degrees.",
                        }
                    ]
                )
            ),
            encoding="utf-8",
        )
        (root / "claim_register.jsonl").write_text(
            "".join(
                json.dumps(row) + "\n"
                for row in (
                    claims
                    or [
                        {
                            "claimId": "cl-1",
                            "text": "Tree canopy reduces surface temperature.",
                            "sourceIds": ["src-1"],
                            "evidenceIds": ["ev-1"],
                            "confidence": "high",
                        }
                    ]
                )
            ),
            encoding="utf-8",
        )
        (root / "deep_research_report.md").write_text("# Deep Research\n", encoding="utf-8")
        return root

    def import_notes(self, run, **overrides):
        with patch.object(
            vault_connections,
            "invoke_upstream_validator",
            return_value={"valid": True, "complete": True, "warnings": []},
        ):
            return vault_connections.command_import_run(self.import_args(run, notes=True, **overrides))

    def test_a_deep_run_becomes_subtopic_notes_with_quotes_and_provenance(self):
        result = self.import_notes(self.make_deep_run())
        notes = [proposal for proposal in result["data"]["proposals"] if proposal["id"].startswith("n-")]
        self.assertEqual(len(notes), 1)
        self.assertEqual(result["data"]["counts"]["subtopic_notes_proposed"], 1)
        content = notes[0]["content"]
        self.assertIn("## Synthesis", content)
        self.assertLess(content.index("## Synthesis"), content.index("## Findings"))
        self.assertIn("## Findings", content)
        self.assertIn("Tree canopy reduces surface temperature.", content)
        self.assertIn('"Shade lowered surface temperature by nine degrees."', content)
        self.assertIn("https://example.org/heat", content)
        self.assertIn("## Provenance", content)
        self.assertIn("`cl-1`", content)
        # Forced, not model-chosen: a research note is machine-made.
        self.assertIn("capture_type: generated", content)
        self.assertIn("status: complete", content)

    def test_subtopic_notes_are_proposed_not_written(self):
        run = self.make_deep_run()
        before = sorted(path.name for path in (self.vault / "00 Inbox").glob("*.md"))
        self.import_notes(run)
        self.assertEqual(sorted(path.name for path in (self.vault / "00 Inbox").glob("*.md")), before)

    def test_no_claim_is_dropped_when_the_grouping_misses_one(self):
        claims = [
            {"claimId": "cl-1", "text": "Canopy cools streets.", "sourceIds": ["src-1"], "evidenceIds": ["ev-1"]},
            {"claimId": "cl-2", "text": "Cool roofs cut peak load.", "sourceIds": ["src-1"], "evidenceIds": ["ev-1"]},
        ]
        StubHandler.grouping = {"notes": [{"title": "Canopy Cooling", "claimIds": ["cl-1"]}]}
        try:
            result = self.import_notes(self.make_deep_run(claims=claims))
        finally:
            StubHandler.grouping = None
        notes = [proposal for proposal in result["data"]["proposals"] if proposal["id"].startswith("n-")]
        self.assertEqual([note["claimIds"] for note in notes], [["cl-1"], ["cl-2"]])
        self.assertIn("Further Findings", notes[1]["title"])

    def test_a_claim_flagged_upstream_is_excluded_and_said_so(self):
        claims = [
            {"claimId": "cl-1", "text": "Canopy cools streets.", "sourceIds": ["src-1"], "evidenceIds": ["ev-1"]},
            {
                "claimId": "cl-2",
                "text": "Canopy eliminates heat deaths.",
                "sourceIds": ["src-1"],
                "evidenceIds": ["ev-1"],
                "verification": {"verdict": "flag", "reason": "not supported by the excerpt"},
            },
        ]
        result = self.import_notes(self.make_deep_run(claims=claims))
        notes = [proposal for proposal in result["data"]["proposals"] if proposal["id"].startswith("n-")]
        self.assertNotIn("cl-2", notes[0]["claimIds"])
        self.assertIn("Claims excluded as flagged in review", notes[0]["content"])
        self.assertTrue(any("flagged in the source run" in warning for warning in result["warnings"]))

    def test_a_quote_flagged_upstream_never_reaches_a_note(self):
        # A claim can pass review on its wording while the extraction beneath it
        # was rejected. The quote must not ride into the vault on that.
        evidence = [
            {"evidenceId": "ev-1", "sourceId": "src-1", "text": "Canopy cools.", "directQuote": "Canopy cooled the street."},
            {
                "evidenceId": "ev-2",
                "sourceId": "src-1",
                "text": "Canopy cut deaths by a quarter.",
                "directQuote": "Canopy cut heat deaths by twenty-five percent.",
                "verification": {"verdict": "flag", "reason": "the excerpt does not contain this figure"},
            },
        ]
        claims = [
            {"claimId": "cl-1", "text": "Canopy cools streets.", "sourceIds": ["src-1"], "evidenceIds": ["ev-1", "ev-2"]},
        ]
        result = self.import_notes(self.make_deep_run(claims=claims, evidence=evidence))
        notes = [proposal for proposal in result["data"]["proposals"] if proposal["id"].startswith("n-")]
        content = notes[0]["content"]
        self.assertIn("Canopy cooled the street.", content)
        self.assertNotIn("twenty-five percent", content)
        self.assertIn("Quotes excluded as flagged in review", content)
        self.assertIn("`ev-2`", content)

    def test_a_claim_whose_evidence_was_all_flagged_is_dropped(self):
        evidence = [
            {
                "evidenceId": "ev-1",
                "sourceId": "src-1",
                "text": "Unsupported.",
                "directQuote": "An invented figure.",
                "verification": {"verdict": "flag", "reason": "not in the excerpt"},
            },
            {"evidenceId": "ev-2", "sourceId": "src-1", "text": "Solid.", "directQuote": "A real quote."},
        ]
        claims = [
            {"claimId": "cl-1", "text": "Rests only on rejected evidence.", "sourceIds": ["src-1"], "evidenceIds": ["ev-1"]},
            {"claimId": "cl-2", "text": "Rests on kept evidence.", "sourceIds": ["src-1"], "evidenceIds": ["ev-2"]},
        ]
        result = self.import_notes(self.make_deep_run(claims=claims, evidence=evidence))
        notes = [proposal for proposal in result["data"]["proposals"] if proposal["id"].startswith("n-")]
        covered = {claim_id for note in notes for claim_id in note["claimIds"]}
        self.assertEqual(covered, {"cl-2"})
        self.assertIn("cl-1", notes[0]["content"], "the dropped claim is still named under Provenance")
        self.assertTrue(any("every piece of evidence" in warning for warning in result["warnings"]))

    def test_notes_are_refused_for_a_non_deep_run(self):
        with self.assertRaisesRegex(vault_connections.UserError, "deep-research run"):
            self.import_notes(self.make_literature_run())

    def test_a_flagged_note_is_marked_rather_than_dropped(self):
        StubHandler.flags = {"Heat And Health": "the summary overstates the evidence"}
        try:
            result = self.import_notes(self.make_deep_run(), verify=True, think_url=f"{self.base}/v1/chat/completions")
        finally:
            StubHandler.flags = {}
        notes = [proposal for proposal in result["data"]["proposals"] if proposal["id"].startswith("n-")]
        self.assertEqual(len(notes), 1, "a flagged note is still proposed")
        self.assertIn("overstates", notes[0]["needsReview"])
        self.assertIn("Flagged in review", notes[0]["content"])
        self.assertEqual(result["data"]["verification"]["flagged"], 1)

    def test_literature_key_term_table_is_added_to_supported_item_context(self):
        source_run = self.make_literature_run()
        records = vault_connections.import_source_records(source_run, "literature")
        self.assertEqual(len(records), 1)
        self.assertIn("Table context: Thermal justice", records[0]["text"])

    def test_import_invokes_each_validator_in_read_only_mode(self):
        root = Path(self.temporary.name)
        for run_type in ("literature", "meta-literature", "deep-research"):
            run_directory = root / run_type
            run_directory.mkdir()
            if run_type == "deep-research":
                (run_directory / "run_state.json").write_text('{"status":"complete"}\n', encoding="utf-8")
            completed = SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"valid": True, "complete": True, "warnings": []}),
                stderr="",
            )
            with patch.object(vault_connections.subprocess, "run", return_value=completed) as runner:
                result = vault_connections.invoke_upstream_validator(run_directory, run_type)
            self.assertTrue(result["valid"])
            self.assertIn("--read-only", runner.call_args.args[0])
        invalid = SimpleNamespace(
            returncode=1,
            stdout=json.dumps({"valid": False, "errors": ["incomplete output"]}),
            stderr="",
        )
        with patch.object(vault_connections.subprocess, "run", return_value=invalid):
            with self.assertRaisesRegex(vault_connections.UserError, "incomplete output"):
                vault_connections.invoke_upstream_validator(root / "literature", "literature")

    def test_import_apply_refuses_template_drift_and_new_basename_collision(self):
        self.seed_wiki_templates()
        source_run = self.make_literature_run()
        with patch.object(
            vault_connections,
            "invoke_upstream_validator",
            return_value={"valid": True, "complete": True, "warnings": []},
        ):
            proposed = vault_connections.command_import_run(self.import_args(source_run))
        concept_template = self.vault / "99 Meta/99.03 Templates/Wiki Concept.md"
        concept_template.write_text(concept_template.read_text(encoding="utf-8") + "\nChanged.\n", encoding="utf-8")
        self.write(
            "02 Craft/Climate Corpus - Key Terms.md",
            "---\ntype: note\nstatus: active\ndomain: craft\n---\n# Collision\n",
        )
        apply_args = SimpleNamespace(
            vault=str(self.vault),
            schema=None,
            run=proposed["data"]["runDirectory"],
            accept="i-001,i-002",
            reject=None,
            dry_run=False,
        )
        with self.assertRaisesRegex(vault_connections.UserError, "wiki template changed"):
            vault_connections.command_apply(apply_args)
        concept_template.write_text(concept_template.read_text(encoding="utf-8").replace("\nChanged.\n", ""), encoding="utf-8")
        with self.assertRaisesRegex(vault_connections.UserError, "basename collides"):
            vault_connections.command_apply(apply_args)
        self.assertFalse((self.vault / "00 Inbox").exists())

    def test_import_apply_refuses_schema_drift(self):
        self.seed_wiki_templates()
        source_run = self.make_literature_run()
        with patch.object(
            vault_connections,
            "invoke_upstream_validator",
            return_value={"valid": True, "complete": True, "warnings": []},
        ):
            proposed = vault_connections.command_import_run(self.import_args(source_run))
        schema_path = self.vault / "99 Meta/99.02 Schemas/0.00 Vault Schema.md"
        schema_path.write_text(schema_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        apply_args = SimpleNamespace(
            vault=str(self.vault),
            schema=None,
            run=proposed["data"]["runDirectory"],
            accept="i-001",
            reject=None,
            dry_run=False,
        )
        with self.assertRaisesRegex(vault_connections.UserError, "schema hash changed"):
            vault_connections.command_apply(apply_args)
        self.assertFalse((self.vault / "00 Inbox").exists())

    def test_import_apply_refuses_voice_policy_drift(self):
        self.seed_wiki_templates()
        voice = self.write(
            "99 Meta/99.02 Schemas/0.01 Voice and Style.md",
            "## Global voice\n\n### Source-derived\n\n- Describe source claims analytically.\n",
        )
        source_run = self.make_literature_run()
        StubHandler.entities = []
        with patch.object(
            vault_connections,
            "invoke_upstream_validator",
            return_value={"valid": True, "complete": True, "warnings": []},
        ):
            proposed = vault_connections.command_import_run(self.import_args(source_run))
        voice.write_text(
            "## Global voice\n\n### Source-derived\n\n- Describe source claims plainly.\n",
            encoding="utf-8",
        )
        apply_args = SimpleNamespace(
            vault=str(self.vault),
            schema=None,
            run=proposed["data"]["runDirectory"],
            accept="i-001",
            reject=None,
            dry_run=False,
        )
        with self.assertRaisesRegex(vault_connections.UserError, "voice_hash changed"):
            vault_connections.command_apply(apply_args)

    def test_all_seven_wiki_kinds_use_schema_types_and_compiled_folders(self):
        self.seed_wiki_templates()
        schema = self.schema()
        expected_types = {
            "concept": "concept",
            "practice": "concept",
            "place": "place",
            "event": "event",
            "term": "concept",
            "work": "work",
            "figure": "person",
        }
        for kind, expected_type in expected_types.items():
            destination = vault_connections.wiki_destination(schema, kind, f"Example {kind.title()}")
            self.assertIn(vault_connections.WIKI_KIND_SUBDOMAIN[kind].title(), destination)
            template = vault_connections.inspect_wiki_template(self.vault, schema, kind)
            candidate = {
                "kind": kind,
                "title": f"Example {kind.title()}",
                "summary": "Supported summary.",
                "evidenceIds": ["item-1"],
                "sourceIds": ["doc-1"],
            }
            content = vault_connections.render_wiki_entity(
                schema,
                template,
                candidate,
                [{"id": "item-1", "text": "Supported evidence.", "sourceIds": ["doc-1"]}],
                "/tmp/source-run",
                "fingerprint",
            )
            self.assertIn(f"type: {expected_type}\n", content)
            self.assertIn(f"subdomain: {vault_connections.WIKI_KIND_SUBDOMAIN[kind]}\n", content)
            self.assertNotIn("{{", content)

    def test_entity_candidates_are_deduplicated_and_unsupported_ids_are_discarded(self):
        records = [
            {"id": "item-1", "text": "Supported.", "sourceIds": ["doc-1"]},
            {"id": "item-2", "text": "Other evidence.", "sourceIds": ["doc-2"]},
        ]
        StubHandler.entities = [
            {
                "kind": "concept",
                "title": "Thermal Justice",
                "summary": "First summary.",
                "evidenceIds": ["item-1"],
                "sourceIds": ["doc-1"],
            },
            {
                "kind": "term",
                "title": "thermal justice",
                "summary": "Second summary.",
                "evidenceIds": ["item-1"],
                "sourceIds": [],
            },
            {
                "kind": "concept",
                "title": "Unsupported",
                "summary": "Invented.",
                "evidenceIds": ["missing"],
                "sourceIds": [],
            },
            {
                "kind": "concept",
                "title": "Wrong Namespace",
                "summary": "A source ID was used as a record ID.",
                "evidenceIds": ["doc-1"],
                "sourceIds": [],
            },
            {
                "kind": "concept",
                "title": "Unrelated Source",
                "summary": "The source exists but belongs to another item.",
                "evidenceIds": ["item-1"],
                "sourceIds": ["doc-2"],
            },
        ]
        candidates, warnings = vault_connections.harvest_entity_candidates(
            self.import_args(Path(self.temporary.name)),
            records,
            ("concept", "term"),
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["evidenceIds"], ["item-1"])
        self.assertTrue(any("unsupported provenance" in warning for warning in warnings))
        self.assertTrue(any("unrelated to cited records" in warning for warning in warnings))

    def test_entity_harvest_batches_records_and_limit_bounds_each_request(self):
        records = [
            {"id": f"item-{index}", "text": "Supported evidence. " * 20, "sourceIds": [f"doc-{index}"]}
            for index in range(205)
        ]
        seen_batch_sizes = []

        def respond(_args, messages):
            content = messages[-1]["content"]
            payload = json.loads(content.split("RESEARCH ENTITY CANDIDATES\n", 1)[1])
            seen_batch_sizes.append(len(payload["records"]))
            record = payload["records"][0]
            return {
                "entities": [
                    {
                        "kind": "concept",
                        "title": f"Concept {record['id']}",
                        "summary": "Supported summary.",
                        "evidenceIds": [record["id"]],
                        "sourceIds": [record["sourceIds"][0]],
                    }
                ]
            }

        with patch.object(vault_connections, "request_with_retry", side_effect=respond):
            candidates, warnings = vault_connections.harvest_entity_candidates(
                self.import_args(Path(self.temporary.name)),
                records,
                ("concept",),
                limit=1,
            )
        self.assertEqual(warnings, [])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(seen_batch_sizes, [10])

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
        self.assertEqual(
            checks["schema"]["wikiSubdomains"],
            ["concepts", "events", "figures", "places", "practices", "terms", "works"],
        )
        self.assertTrue(checks["chat"]["ok"])
        self.assertTrue(checks["embeddings"]["ok"])

    def test_the_endpoint_comes_from_the_configured_chat_service(self):
        """Every other test passes --base-url, so none of them prove that a run
        with no flag finds the agent's configured service. Classification shares
        one resolution for all commands, so this covers the publishing path too."""
        agent = Path(self.temporary.name) / "agent"
        agent.mkdir()
        (agent / "settings.json").write_text(
            json.dumps(
                {
                    "connectedServices": {
                        "chat": {
                            "enabled": True,
                            "baseUrl": f"{self.base}/v1/chat/completions",
                            "model": "settings-resolved-model",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        process = subprocess.run(
            [sys.executable, str(SCRIPT), "doctor", "--vault", str(self.vault), "--no-verify"],
            capture_output=True,
            text=True,
            env={
                "FORGE_EMBEDDINGS_URL": f"{self.base}/v1/embeddings",
                "FORGE_EMBEDDINGS_MODEL": "stub-embed",
                "PATH": "/usr/bin:/bin",
                "PI_FORGE_AGENT_DIR": str(agent),
            },
        )
        self.assertTrue(process.stdout.strip(), f"no stdout; stderr={process.stderr[-2000:]}")
        checks = json.loads(process.stdout)["data"]["checks"]
        self.assertEqual(checks["chat"]["url"], f"{self.base}/v1/chat/completions")
        self.assertEqual(checks["chat"]["model"], "settings-resolved-model")
        self.assertTrue(StubHandler.chat_requests, "the resolved endpoint was never contacted")
        self.assertEqual(StubHandler.chat_requests[-1]["model"], "settings-resolved-model")




class PersonalContextTests(unittest.TestCase):
    """The profile layer replaced a hardcoded biography in CONNECTION_SYSTEM."""

    def test_the_judgment_prompt_names_no_particular_person(self):
        """Regression lock. This prompt ships in a skill other people run, so it
        must not carry one vault owner's biography."""
        for token in ("Buddhist", "Ellie", "philosophical, religious, epistemic"):
            self.assertNotIn(token, vault_connections.CONNECTION_SYSTEM)

    def test_without_a_profile_the_system_prompt_is_unchanged(self):
        args = SimpleNamespace(compiled_profile=None)
        self.assertEqual(vault_connections.connection_system(args), vault_connections.CONNECTION_SYSTEM)

    def test_the_always_tier_reaches_the_system_prompt(self):
        profile = {
            "cards": [
                {
                    "order": 0,
                    "name": "Core Identity",
                    "link": "[[Core Identity]]",
                    "tier": "always",
                    "scope": "universal",
                    "routes": frozenset(),
                    "triggers": [],
                    "note": "",
                    "facts": ["Sociologist of knowledge."],
                }
            ]
        }
        system = vault_connections.connection_system(SimpleNamespace(compiled_profile=profile))
        self.assertIn("Sociologist of knowledge.", system)
        self.assertTrue(system.startswith(vault_connections.CONNECTION_SYSTEM))

    def test_the_judge_site_unions_both_notes_routes(self):
        site = vault_connections.judge_site(
            {"domain": "personal", "subdomain": "therapy"},
            {"domain": "work", "subdomain": None},
        )
        self.assertEqual(site["routes"], frozenset({"personal", "personal/therapy", "work"}))

    def test_a_note_with_no_domain_contributes_no_route(self):
        self.assertEqual(vault_connections.judge_site({"domain": None}, {})["routes"], frozenset())


if __name__ == "__main__":
    unittest.main(verbosity=1)

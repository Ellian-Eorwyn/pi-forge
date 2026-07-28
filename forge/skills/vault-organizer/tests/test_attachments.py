#!/usr/bin/env python3
"""Tests for the `attachments` mode: auditing and repairing asset embeds.

A vault reorganization moves notes and leaves every relative image path dangling,
because nothing in forge rewrites embeds. This mode repairs what it can prove and
reports the rest. The dangerous half is what it must *not* touch: embed syntax
inside code spans and fenced blocks is documentation, and a filename it cannot
resolve uniquely is a guess it must refuse to make.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "vault-organizer.py"
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("vault_organizer_attachments", SCRIPT)
vault_organizer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vault_organizer)

SCHEMA = """# Vault Schema

## Approved properties

| Property | Required | Shape | Definition |
| --- | --- | --- | --- |
| `type` | yes | controlled scalar | Kind. |
| `status` | yes | controlled scalar | Lifecycle. |
| `domain` | yes | controlled scalar | Broad area. |
| `subdomain` | no | controlled scalar | Nested area. |

## Note types

- `note` — General note.

## Status values

- `active` — Active.

## Domains

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `craft` | `2` | `Craft` | Making things. |
| `meta` | `99` | `Meta` | System notes. |

## Subdomains

### craft

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `gardening` | `1` | `Gardening` | Plants. |

### meta

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `attachments` | `5` | `Attachments` | Binary assets. |

## Project registry

| Approved value | Domain | Subdomain | Number | Definition |
| --- | --- | --- | --- | --- |

## Source kinds

- `book` — Book.

## Capture types

- `manual` — Typed.

## Folder routing

### Derived names

```text
domain-folder(domain):
  <pad2(domain.number)> <domain.label>
```

## Legacy normalization map

| Legacy input | Canonical output |
| --- | --- |
| `type: daily` | `type: note` |
"""


def run_script(*args):
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PI_FORGE_AGENT_DIR": "/nonexistent-agent-directory"}
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env)


class AttachmentsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name).resolve() / "vault"
        (self.vault / "99 Meta" / "99.02 Schemas").mkdir(parents=True)
        (self.vault / "99 Meta" / "99.02 Schemas" / "0.00 Vault Schema.md").write_text(SCHEMA, encoding="utf-8")
        self.images = self.vault / "99 Meta" / "99.05 Attachments" / "Images"
        self.images.mkdir(parents=True)
        (self.images / "logo.png").write_bytes(b"\x89PNG")
        (self.images / "chart.jpg").write_bytes(b"\xff\xd8\xff")

    def tearDown(self):
        self.tmp.cleanup()

    def note(self, relative, text):
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def audit(self, *extra):
        result = run_script("attachments", "--vault", str(self.vault), *extra)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads(result.stdout)

    def read(self, relative):
        return (self.vault / relative).read_text(encoding="utf-8")

    # --- repair ---------------------------------------------------------

    def test_a_moved_image_is_relinked_as_a_wikilink(self):
        self.note("02 Craft/Note.md", "# Note\n\n![](old/path/logo.png)\n")
        payload = self.audit("--apply")
        self.assertEqual(payload["data"]["counts"]["repairable"], 1)
        self.assertEqual(self.read("02 Craft/Note.md"), "# Note\n\n![[logo.png]]\n")

    def test_a_link_that_already_resolves_is_left_alone(self):
        body = "# Note\n\n![](99%20Meta/99.05%20Attachments/Images/logo.png)\n"
        self.note("02 Craft/Note.md", body)
        payload = self.audit("--apply")
        self.assertEqual(payload["data"]["counts"]["resolves"], 1)
        self.assertEqual(self.read("02 Craft/Note.md"), body)

    def test_running_twice_is_a_no_op_the_second_time(self):
        self.note("02 Craft/Note.md", "# Note\n\n![](old/path/logo.png)\n")
        self.audit("--apply")
        after_first = self.read("02 Craft/Note.md")
        payload = self.audit("--apply")
        # A repaired wikilink already resolves; reporting it as repairable again
        # would rewrite it to itself and call a healthy vault broken.
        self.assertEqual(payload["data"]["counts"]["resolves"], 1)
        self.assertEqual(payload["data"]["counts"]["repairable"], 0)
        self.assertEqual(payload["data"]["notesChanged"], 0)
        self.assertEqual(self.read("02 Craft/Note.md"), after_first)

    def test_a_wikilink_embed_to_an_existing_asset_already_resolves(self):
        body = "# Note\n\n![[logo.png]]\n"
        self.note("02 Craft/Note.md", body)
        payload = self.audit("--apply")
        self.assertEqual(payload["data"]["counts"]["resolves"], 1)
        self.assertEqual(self.read("02 Craft/Note.md"), body)

    def test_an_ambiguous_basename_is_reported_and_never_guessed(self):
        second = self.vault / "02 Craft" / "Images"
        second.mkdir(parents=True)
        (second / "logo.png").write_bytes(b"\x89PNG")
        body = "# Note\n\n![](gone/logo.png)\n"
        self.note("02 Craft/Note.md", body)
        payload = self.audit("--apply")
        self.assertEqual(payload["data"]["counts"]["ambiguous"], 1)
        self.assertEqual(payload["data"]["counts"]["repairable"], 0)
        self.assertEqual(self.read("02 Craft/Note.md"), body)
        self.assertTrue(any("several files" in warning for warning in payload["warnings"]))

    def test_two_embeds_on_one_line_are_both_repaired(self):
        self.note("02 Craft/Note.md", "# Note\n\n![](a/logo.png) ![](b/chart.jpg)\n")
        self.audit("--apply")
        self.assertEqual(self.read("02 Craft/Note.md"), "# Note\n\n![[logo.png]] ![[chart.jpg]]\n")

    # --- stripping the unrecoverable ------------------------------------

    def test_a_dead_embed_with_alt_text_keeps_the_alt_text(self):
        self.note("02 Craft/Note.md", "# Note\n\n![Text Box: DATE](file:////tmp/clip_image005.jpg)\n")
        payload = self.audit("--apply")
        self.assertEqual(payload["data"]["counts"]["missing"], 1)
        self.assertEqual(self.read("02 Craft/Note.md"), "# Note\n\nText Box: DATE\n")

    def test_a_dead_embed_alone_on_its_line_removes_the_line(self):
        self.note("02 Craft/Note.md", "# Note\n\n![](Recipe/step-01.jpg)\n\nAfter.\n")
        self.audit("--apply")
        self.assertEqual(self.read("02 Craft/Note.md"), "# Note\n\n\nAfter.\n")

    def test_a_dead_embed_as_a_whole_list_item_removes_the_item(self):
        self.note("02 Craft/Note.md", "# Note\n\n* Before\n* ![](missing.pdf)\n* After\n")
        self.audit("--apply")
        self.assertEqual(self.read("02 Craft/Note.md"), "# Note\n\n* Before\n* After\n")

    def test_a_dead_embed_in_a_checkbox_removes_the_task(self):
        self.note("02 Craft/Note.md", "# Note\n\n- [ ] ![](missing.png)\n- [ ] Real task\n")
        self.audit("--apply")
        self.assertEqual(self.read("02 Craft/Note.md"), "# Note\n\n- [ ] Real task\n")

    def test_a_dead_wikilink_embed_is_stripped_too(self):
        self.note("02 Craft/Note.md", "# Note\n\n* Context here\n\t* ![[Roadmap 2.pdf]]\n")
        payload = self.audit("--apply")
        self.assertEqual(payload["data"]["counts"]["missing"], 1)
        self.assertEqual(self.read("02 Craft/Note.md"), "# Note\n\n* Context here\n")

    def test_adjacent_dead_embeds_do_not_concatenate_their_alt_text(self):
        self.note("02 Craft/Note.md", "# Note\n\n![Text Box: DATE](x/a.jpg)![Text Box: DATE](x/b.jpg)\n")
        self.audit("--apply")
        self.assertEqual(self.read("02 Craft/Note.md"), "# Note\n\nText Box: DATE Text Box: DATE\n")

    def test_a_dead_embed_abutting_text_gains_a_word_boundary(self):
        self.note("02 Craft/Note.md", "# Note\n\n![Text Box: DATE](x/a.jpg)Draft Report\n")
        self.audit("--apply")
        self.assertEqual(self.read("02 Craft/Note.md"), "# Note\n\nText Box: DATE Draft Report\n")

    def test_alt_text_already_separated_gains_no_extra_space(self):
        self.note("02 Craft/Note.md", "# Note\n\nBefore ![Caption](x/a.jpg) after\n")
        self.audit("--apply")
        self.assertEqual(self.read("02 Craft/Note.md"), "# Note\n\nBefore Caption after\n")

    def test_a_surviving_sibling_on_the_line_keeps_the_line(self):
        self.note("02 Craft/Note.md", "# Note\n\nBefore ![](missing.png) after\n")
        self.audit("--apply")
        self.assertEqual(self.read("02 Craft/Note.md"), "# Note\n\nBefore after\n")

    # --- what must never be touched --------------------------------------

    def test_embed_syntax_inside_a_code_span_survives_byte_identical(self):
        body = '# Docs\n\n- A link to a local attachment `"[[link/to/attachment.jpg]]"`\n- Or `![](x/y.png)` inline.\n'
        self.note("02 Craft/Docs.md", body)
        payload = self.audit("--apply")
        self.assertEqual(payload["data"]["counts"]["embeds"], 0)
        self.assertEqual(self.read("02 Craft/Docs.md"), body)

    def test_embed_syntax_inside_a_fenced_block_survives_byte_identical(self):
        body = "# Docs\n\n```markdown\n![](Recipe/step-01.jpg)\n![[logo.png]]\n```\n\nDone.\n"
        self.note("02 Craft/Docs.md", body)
        payload = self.audit("--apply")
        self.assertEqual(payload["data"]["counts"]["embeds"], 0)
        self.assertEqual(self.read("02 Craft/Docs.md"), body)

    def test_external_urls_and_note_wikilinks_are_ignored(self):
        body = "# Note\n\n![](https://example.com/x.png)\n\n[[Some Note]] and [text](https://example.com)\n"
        self.note("02 Craft/Note.md", body)
        payload = self.audit("--apply")
        self.assertEqual(payload["data"]["counts"]["embeds"], 0)
        self.assertEqual(self.read("02 Craft/Note.md"), body)

    def test_a_note_with_no_asset_links_is_not_rewritten(self):
        body = "# Plain\n\nJust prose, no embeds at all.\n"
        self.note("02 Craft/Plain.md", body)
        self.audit("--apply")
        self.assertEqual(self.read("02 Craft/Plain.md"), body)

    def test_notes_inside_a_marked_workspace_are_skipped(self):
        workspace = self.vault / "99 Meta" / "99.06 Workflows" / "Web Research"
        workspace.mkdir(parents=True)
        (workspace / vault_organizer.WORKSPACE_MARKER).write_text("", encoding="utf-8")
        body = "# Run report\n\n![](gone/logo.png)\n"
        (workspace / "research_report.md").write_text(body, encoding="utf-8")
        payload = self.audit("--apply")
        self.assertEqual(payload["data"]["counts"]["embeds"], 0)
        self.assertEqual((workspace / "research_report.md").read_text(encoding="utf-8"), body)

    # --- safety contract --------------------------------------------------

    def test_a_dry_run_reports_without_writing_anything(self):
        body = "# Note\n\n![](old/logo.png)\n"
        self.note("02 Craft/Note.md", body)
        payload = self.audit()
        self.assertTrue(payload["data"]["dryRun"])
        self.assertEqual(payload["data"]["notesChanged"], 1)
        self.assertEqual(payload["data"]["applied"], [])
        self.assertEqual(self.read("02 Craft/Note.md"), body)

    def test_the_report_records_every_embed_before_any_edit(self):
        self.note("02 Craft/Note.md", "# Note\n\n![](old/logo.png)\n\n![](Recipe/step-01.jpg)\n")
        payload = self.audit("--apply")
        report = json.loads(Path(payload["data"]["runDirectory"], "attachment_report.json").read_text(encoding="utf-8"))
        targets = [f["target"] for note in report["notes"] for f in note["findings"]]
        # The stripped filename is gone from the note; the report is its only record.
        self.assertIn("Recipe/step-01.jpg", targets)
        self.assertIn("old/logo.png", targets)
        self.assertTrue(Path(payload["data"]["runDirectory"], "attachment_report.md").is_file())

    def test_apply_backs_up_every_note_it_rewrites(self):
        original = "# Note\n\n![](old/logo.png)\n"
        self.note("02 Craft/Note.md", original)
        payload = self.audit("--apply")
        backup = Path(payload["data"]["runDirectory"], "backup", "02 Craft", "Note.md")
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_text(encoding="utf-8"), original)

    def test_the_mode_runs_with_no_model_endpoint_configured(self):
        self.note("02 Craft/Note.md", "# Note\n\n![](old/logo.png)\n")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "attachments", "--vault", str(self.vault)],
            capture_output=True,
            text=True,
            env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)["data"]["counts"]["repairable"], 1)


class HelperTests(unittest.TestCase):
    def test_code_span_ranges_finds_single_and_multi_backtick_spans(self):
        line = "text `one` more ``two `nested` two`` end"
        ranges = vault_organizer.code_span_ranges(line)
        covered = "".join(line[start:end] for start, end in ranges)
        self.assertIn("`one`", covered)
        self.assertIn("``two `nested` two``", covered)

    def test_an_unclosed_backtick_does_not_swallow_the_line(self):
        self.assertEqual(vault_organizer.code_span_ranges("a ` b"), [])

    def test_normalize_target_decodes_percent_escapes_and_drops_titles(self):
        self.assertEqual(vault_organizer.normalize_target("99%20Meta/x.png"), "99 Meta/x.png")
        self.assertEqual(vault_organizer.normalize_target('x.png "A title"'), "x.png")
        self.assertEqual(vault_organizer.normalize_target("<x.png>"), "x.png")


if __name__ == "__main__":
    unittest.main(verbosity=2)

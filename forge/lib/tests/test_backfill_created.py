#!/usr/bin/env python3
"""Tests for the one-off ``created`` backfill.

The script exists because a bulk reorganization destroyed this vault's file
timestamps, so what is being proven here is mostly about *which* evidence a date
came from. A backfill that cannot tell a filename prefix from a flattened mtime
would fill the vault with confident wrong dates and leave no way to find them
again, which is worse than the empty property it replaced.
"""

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "forge" / "lib"))
import vault_schema as vs  # noqa: E402

_spec = importlib.util.spec_from_file_location("backfill_created", ROOT / "scripts" / "backfill-vault-created.py")
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)

SCHEMA = """# Vault Schema

## Approved properties

| Property | Required | Shape | Definition |
| --- | --- | --- | --- |
| `type` | yes | controlled scalar | Kind. |
| `status` | yes | controlled scalar | Lifecycle. |
| `domain` | yes | controlled scalar | Broad area. |
| `date` | no | scalar, human-owned | Subject date. |
| `created` | yes | scalar, derived | Date this note came into existence. |

## Note types

- `note` — General note.

## Status values

- `active` — Active.

## Domains

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `personal` | `1` | `Personal` | Personal material. |
| `meta` | `99` | `Meta` | System notes. |

## Subdomains

### meta

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `schemas` | `2` | `Schemas` | Schema notes. |

## Project registry

| Approved value | Domain | Subdomain | Number | Definition |
| --- | --- | --- | --- | --- |
| `"[[Pi Forge]]"` | `personal` |  | `1` | Placeholder project. |

## Source kinds

- `book` — Book.

## Capture types

- `manual` — Typed.

## Legacy normalization map

| Legacy input | Canonical output |
| --- | --- |
| `type: daily` | `type: note` |

## Folder routing

Derived by code.
"""

NOTE = "---\ntype: note\nstatus: active\ndomain: personal\n---\n\n# Note\n\nBody.\n"


class BackfillCreatedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name).resolve()
        (self.vault / ".obsidian").mkdir()
        self.write(vs.DEFAULT_SCHEMA, SCHEMA)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text):
        path = self.vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def plan(self, min_tier="date", use_git=False):
        schema_path = vs.resolve_schema_path(self.vault, None)
        _schema, planned, present, skipped, unresolved = backfill.plan_backfill(
            self.vault, schema_path, min_tier, use_git
        )
        return {item["path"]: item for item in planned}, present, skipped, unresolved

    def test_a_filename_prefix_is_recovered(self):
        self.write("01 Personal/2019-11-30 Trip north.md", NOTE)
        planned, _present, _skipped, _unresolved = self.plan()
        item = planned["01 Personal/2019-11-30 Trip north.md"]
        self.assertEqual(item["created"], "2019-11-30")
        self.assertEqual(item["tier"], "filename")

    def test_the_subject_date_is_used_when_there_is_no_prefix(self):
        self.write("01 Personal/Trip.md", NOTE.replace("domain: personal\n", "domain: personal\ndate: 2020-03-04\n"))
        planned, _present, _skipped, _unresolved = self.plan()
        self.assertEqual(planned["01 Personal/Trip.md"]["tier"], "date")
        self.assertEqual(planned["01 Personal/Trip.md"]["created"], "2020-03-04")

    def test_an_organizer_backup_beats_the_filename(self):
        """The backup predates the migration, so it is the better witness even
        when a later filename also carries a date."""
        self.write("01 Personal/2024-01-01 Renamed.md", NOTE)
        self.write(
            ".vault-organizer/runs/2026-07-01T120000Z/backup/01 Personal/2024-01-01 Renamed.md",
            "---\ntype: note\ncreated: 2016-05-05\n---\n\nBody.\n",
        )
        planned, _present, _skipped, _unresolved = self.plan()
        item = planned["01 Personal/2024-01-01 Renamed.md"]
        self.assertEqual(item["created"], "2016-05-05")
        self.assertEqual(item["tier"], "backup")

    def test_a_backup_key_the_schema_would_have_stripped_is_still_read(self):
        """``Created:`` and ``date created:`` never matched the canonical key
        pattern, which is exactly why they were dropped and why the raw block is
        what gets scanned."""
        self.assertEqual(backfill.created_from_frontmatter_text("Created: 2015-02-02\n"), "2015-02-02")
        self.assertEqual(backfill.created_from_frontmatter_text("date created: 2015-02-03\n"), "2015-02-03")
        self.assertEqual(backfill.created_from_frontmatter_text("createdAt: 2015-02-04T09:00:00\n"), "2015-02-04")
        self.assertIsNone(backfill.created_from_frontmatter_text("updated: 2015-02-05\n"))

    def test_the_earliest_backup_wins(self):
        for run, value in (("2026-07-01T120000Z", "2016-05-05"), ("2026-07-09T120000Z", "2022-09-09")):
            self.write(
                f".vault-organizer/runs/{run}/backup/01 Personal/Note.md",
                f"---\ntype: note\ncreated: {value}\n---\n\nBody.\n",
            )
        self.write("01 Personal/Note.md", NOTE)
        planned, _present, _skipped, _unresolved = self.plan()
        self.assertEqual(planned["01 Personal/Note.md"]["created"], "2016-05-05")

    def test_file_timestamps_are_below_the_default_tier(self):
        """The default refuses the one tier this vault's migration destroyed."""
        self.write("01 Personal/Undated.md", NOTE)
        planned, _present, skipped, _unresolved = self.plan()
        self.assertNotIn("01 Personal/Undated.md", planned)
        self.assertTrue(any("below --min-tier" in item["reason"] for item in skipped))

    def test_file_timestamps_are_used_when_explicitly_allowed(self):
        self.write("01 Personal/Undated.md", NOTE)
        planned, _present, _skipped, _unresolved = self.plan(min_tier="file")
        self.assertEqual(planned["01 Personal/Undated.md"]["tier"], "file")

    def test_an_installed_template_is_never_backfilled(self):
        """``template-install`` compares the installed bytes with the shipped
        copy, so a stamped template is refused as owner-modified forever after."""
        self.write("99 Meta/99.03 Templates/Wiki Animal.md",
                   "---\ntype: template\nstatus: active\ndomain: meta\n---\n\n# {{title}}\n")
        planned, _present, skipped, _unresolved = self.plan(min_tier="file")
        self.assertNotIn("99 Meta/99.03 Templates/Wiki Animal.md", planned)
        self.assertNotIn("99 Meta/99.03 Templates/Wiki Animal.md", {item["path"] for item in skipped})

    def test_a_note_that_already_has_created_is_left_alone(self):
        self.write("01 Personal/Done.md", NOTE.replace("domain: personal\n", "domain: personal\ncreated: 2011-01-01\n"))
        planned, present, _skipped, _unresolved = self.plan()
        self.assertNotIn("01 Personal/Done.md", planned)
        self.assertEqual(present, 1)

    def test_a_schema_without_created_refuses_to_run(self):
        self.write(vs.DEFAULT_SCHEMA, SCHEMA.replace(
            "| `created` | yes | scalar, derived | Date this note came into existence. |\n", ""
        ))
        with self.assertRaises(vs.UserError) as caught:
            self.plan()
        self.assertIn("does not define a 'created' property", str(caught.exception))

    def test_a_schema_that_does_not_mark_created_derived_refuses_to_run(self):
        self.write(vs.DEFAULT_SCHEMA, SCHEMA.replace(
            "| `created` | yes | scalar, derived | Date this note came into existence. |",
            "| `created` | no | scalar | Date this note came into existence. |",
        ))
        with self.assertRaises(vs.UserError) as caught:
            self.plan()
        self.assertIn("derived", str(caught.exception))

    def test_apply_writes_the_property_and_leaves_the_body_byte_intact(self):
        path = self.write("01 Personal/2019-11-30 Trip north.md", NOTE)
        before = path.read_text(encoding="utf-8").split("---\n", 2)[2]
        schema_path = vs.resolve_schema_path(self.vault, None)
        schema, planned, _present, _skipped, _unresolved = backfill.plan_backfill(
            self.vault, schema_path, "date", False
        )
        written, failed = backfill.apply_backfill(self.vault, schema, planned)
        self.assertEqual(failed, [])
        self.assertEqual(len(written), 1)
        after = path.read_text(encoding="utf-8")
        self.assertIn("created: 2019-11-30", after)
        self.assertEqual(after.split("---\n", 2)[2], before)

    def test_apply_is_idempotent(self):
        self.write("01 Personal/2019-11-30 Trip north.md", NOTE)
        schema_path = vs.resolve_schema_path(self.vault, None)
        for _ in range(2):
            schema, planned, _present, _skipped, _unresolved = backfill.plan_backfill(
                self.vault, schema_path, "date", False
            )
            backfill.apply_backfill(self.vault, schema, planned)
        _schema, planned, present, _skipped, _unresolved = backfill.plan_backfill(
            self.vault, schema_path, "date", False
        )
        self.assertEqual(planned, [])
        self.assertEqual(present, 1)


class GitTierTests(unittest.TestCase):
    """The git tier, exercised against a real repository rather than a stub."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name).resolve()
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *argv):
        return subprocess.run(
            ["git", "-C", str(self.vault), *argv], capture_output=True, text=True, timeout=30, check=False
        )

    def commit(self, rel, text, when):
        path = self.vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self.git("add", rel)
        subprocess.run(
            ["git", "-C", str(self.vault), "commit", "-q", "-m", f"add {rel}"],
            capture_output=True, text=True, timeout=30, check=False,
            env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(self.vault),
                 "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when,
                 "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                 "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"},
        )

    def test_a_vault_outside_git_reports_the_tier_unavailable(self):
        with tempfile.TemporaryDirectory() as plain:
            self.assertFalse(backfill.git_available(Path(plain)))

    def test_the_first_commit_date_is_recovered(self):
        if not backfill.git_available(self.vault):
            self.skipTest("git is unavailable")
        self.commit("01 Personal/Note.md", NOTE, "2018-04-17T10:00:00+00:00")
        self.commit("01 Personal/Note.md", NOTE + "More.\n", "2026-01-02T10:00:00+00:00")
        self.assertEqual(backfill.created_from_git(self.vault, "01 Personal/Note.md"), "2018-04-17")

    def test_a_note_git_has_never_seen_returns_nothing(self):
        if not backfill.git_available(self.vault):
            self.skipTest("git is unavailable")
        self.assertIsNone(backfill.created_from_git(self.vault, "01 Personal/Absent.md"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

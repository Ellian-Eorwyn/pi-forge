#!/usr/bin/env python3
"""Tests for workspace exclusion during vault note discovery.

pi-forge writes generated run directories into the vault under a folder marked
with ``.forge-workspace``. Those artifacts are not notes: classifying them would
refile them out of the run directory and break the run's own path references, and
embedding them would fill the index with working files. Discovery must skip them.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vault_schema as vs

SCHEMA = """# Vault Schema

## Domains

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `meta` | `99` | `Meta` | Notes about the knowledge system itself. |
"""


def write(path, text="# Note\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class WorkspaceExclusionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.vault = Path(self.temporary.name).resolve()
        (self.vault / ".obsidian").mkdir()
        self.schema = write(self.vault / vs.DEFAULT_SCHEMA, SCHEMA)
        self.workspace = self.vault / "99 Meta" / "99.06 Workflows" / "Web Research"
        write(self.workspace / vs.WORKSPACE_MARKER, "")
        write(self.workspace / "run-a" / "research_report.md")
        write(self.workspace / "run-a" / "nested" / "memo.md")
        write(self.vault / "99 Meta" / "99.06 Workflows" / "How I Process The Inbox.md")
        write(self.vault / "01 Personal" / "Journal.md")
        write(self.vault / vs.INBOX_DIR / "Dropped.md")

    def tearDown(self):
        self.temporary.cleanup()

    def selected(self, mode):
        return [vs.relative_path(self.vault, path) for path in vs.selected_notes(self.vault, self.schema, mode, None)]

    def test_a_marked_workspace_is_absent_from_a_vault_walk(self):
        selected = self.selected("vault")
        self.assertNotIn("99 Meta/99.06 Workflows/Web Research/run-a/research_report.md", selected)
        self.assertNotIn("99 Meta/99.06 Workflows/Web Research/run-a/nested/memo.md", selected)

    def test_hand_written_notes_beside_a_workspace_are_still_selected(self):
        selected = self.selected("vault")
        self.assertIn("99 Meta/99.06 Workflows/How I Process The Inbox.md", selected)
        self.assertIn("01 Personal/Journal.md", selected)

    def test_a_marked_workspace_inside_the_inbox_is_skipped(self):
        workspace = self.vault / vs.INBOX_DIR / "Captures"
        write(workspace / vs.WORKSPACE_MARKER, "")
        write(workspace / "braindump.md")
        selected = self.selected("inbox")
        self.assertIn("00 Inbox/Dropped.md", selected)
        self.assertNotIn("00 Inbox/Captures/braindump.md", selected)

    def test_the_marker_only_excludes_the_directory_holding_it(self):
        write(self.vault / "99 Meta" / "99.06 Workflows" / "Notes On Workflows.md")
        self.assertIn("99 Meta/99.06 Workflows/Notes On Workflows.md", self.selected("vault"))

    def test_a_schema_note_inside_a_workspace_is_not_adopted(self):
        # A run that archived a copy of the schema must not become the schema.
        self.schema.unlink()
        write(self.workspace / "run-a" / vs.SCHEMA_BASENAME, SCHEMA)
        real = write(self.vault / "99 Meta" / "Alternate" / vs.SCHEMA_BASENAME, SCHEMA)
        self.assertEqual(vs.resolve_schema_path(self.vault, None), real.resolve())

    def test_is_inside_workspace_stops_at_the_vault_root(self):
        self.assertTrue(vs.is_inside_workspace(self.vault, self.workspace / "run-a" / "research_report.md"))
        self.assertFalse(vs.is_inside_workspace(self.vault, self.vault / "01 Personal" / "Journal.md"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

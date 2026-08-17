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


class WorkspaceMarkerWritingTests(unittest.TestCase):
    """``ensure_workspace_marker`` is what makes the exclusion above reachable.

    Every one of these directories used to be created by a bare mkdir, so the
    exclusion tested above was a promise the injected vault context made and only
    two skills kept.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self):
        self.temporary.cleanup()

    def test_a_marked_directory_becomes_invisible_to_discovery(self):
        directory = self.root / "Project Extractions" / "Waste-Heat"
        write(directory / "packets" / "pkt-01.md")
        self.assertFalse(vs.is_workspace_dir(directory))
        vs.ensure_workspace_marker(directory)
        self.assertTrue(vs.is_workspace_dir(directory))
        self.assertEqual(vs.count_notes(self.root), 0)

    def test_it_creates_a_directory_that_does_not_exist_yet(self):
        marker = vs.ensure_workspace_marker(self.root / "fresh")
        self.assertTrue(marker.is_file())
        self.assertEqual(marker.read_text(encoding="utf-8"), vs.WORKSPACE_MARKER_CONTENT)

    def test_it_leaves_a_hand_written_marker_alone(self):
        # The markers already in the owner's vault carry their own wording.
        directory = self.root / "Literature Extractions"
        existing = write(directory / vs.WORKSPACE_MARKER, "Written by hand.\n")
        vs.ensure_workspace_marker(directory)
        self.assertEqual(existing.read_text(encoding="utf-8"), "Written by hand.\n")

    def test_the_marker_text_matches_the_javascript_definition(self):
        # A vault holds markers written by both languages. `vault-workspace.mjs`
        # is the JavaScript and TypeScript copy; a reader comparing two markers
        # should not have to wonder whether a difference means anything.
        source = (Path(__file__).resolve().parents[1] / "vault-workspace.mjs").read_text(encoding="utf-8")
        for line in vs.WORKSPACE_MARKER_CONTENT.splitlines():
            self.assertIn(f'"{line}",', source)


class SelectionTests(unittest.TestCase):
    """--note scoping: selected_notes filters to a set, and resolve_selection turns
    however a note was named into one canonical identity so the run fingerprint does
    not depend on the spelling."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.vault = Path(self.temporary.name).resolve()
        self.schema = write(self.vault / vs.DEFAULT_SCHEMA, SCHEMA)
        write(self.vault / "00 Inbox" / "Alpha.md")
        write(self.vault / "00 Inbox" / "Beta.md")
        write(self.vault / "10 Sources" / "Gamma.md")

    def tearDown(self):
        self.temporary.cleanup()

    def selected(self, select):
        return [
            vs.relative_path(self.vault, path)
            for path in vs.selected_notes(self.vault, self.schema, "vault", None, select=select)
        ]

    def test_select_narrows_the_scan_to_the_named_notes(self):
        self.assertEqual(self.selected(["00 Inbox/Alpha.md"]), ["00 Inbox/Alpha.md"])

    def test_select_none_returns_the_whole_set(self):
        whole = self.selected(None)
        self.assertIn("00 Inbox/Alpha.md", whole)
        self.assertIn("10 Sources/Gamma.md", whole)

    def test_a_note_named_five_ways_resolves_to_one_identity(self):
        canonical = ["00 Inbox/Alpha.md"]
        for spelling in (
            "00 Inbox/Alpha.md",
            "00 Inbox/Alpha",
            "Alpha.md",
            "Alpha",
            str(self.vault / "00 Inbox" / "Alpha.md"),
        ):
            self.assertEqual(vs.resolve_selection(self.vault, [spelling]), canonical, spelling)

    def test_multiple_selectors_merge_and_sort(self):
        self.assertEqual(
            vs.resolve_selection(self.vault, ["Beta", "00 Inbox/Alpha.md"]),
            ["00 Inbox/Alpha.md", "00 Inbox/Beta.md"],
        )

    def test_a_selector_that_matches_nothing_raises(self):
        with self.assertRaises(vs.UserError):
            vs.resolve_selection(self.vault, ["Nonexistent"])

    def test_resolving_is_idempotent_for_a_canonical_path(self):
        once = vs.resolve_selection(self.vault, ["Alpha"])
        self.assertEqual(vs.resolve_selection(self.vault, once), once)

    def test_an_absolute_path_outside_the_vault_matches_nothing(self):
        with self.assertRaises(vs.UserError):
            vs.resolve_selection(self.vault, ["/etc/hosts"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

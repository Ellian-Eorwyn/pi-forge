#!/usr/bin/env python3
"""Tests for the shared reflection-source harvest."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vault_reflection as vr


class CitedLineTests(unittest.TestCase):
    def test_a_claim_with_a_url_is_cited(self):
        found = vr.cited_lines("Peer review predates the journal, per https://example.com/history", "this braindump")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["url"], "https://example.com/history")
        self.assertEqual(found[0]["source"], "this braindump")

    def test_a_bare_link_cites_nothing(self):
        """The `## Sources` case: a link with no statement behind it."""
        self.assertEqual(vr.cited_lines("- https://example.com/x", "this recording"), [])

    def test_a_line_without_a_url_is_not_a_source(self):
        self.assertEqual(vr.cited_lines("A claim with no link at all, however well put.", "x"), [])

    def test_prose_length_is_measured_without_the_url(self):
        """A long URL must not buy a short claim its way past the floor."""
        line = f"Yes, see https://example.com/{'x' * 400}"
        self.assertEqual(vr.cited_lines(line, "x"), [])

    def test_list_markers_and_quotes_are_stripped_before_measuring(self):
        found = vr.cited_lines("> - Hermeneutical injustice is structural, https://example.com/a", "x")
        self.assertEqual(len(found), 1)
        self.assertFalse(found[0]["excerpt"].startswith(">"))

    def test_trailing_punctuation_leaves_the_url(self):
        found = vr.cited_lines("The rating held through 2024, https://example.com/seal.", "x")
        self.assertEqual(found[0]["url"], "https://example.com/seal")

    def test_the_excerpt_is_capped(self):
        line = f"{'A claim worth citing. ' * 40} https://example.com/a"
        found = vr.cited_lines(line, "x")
        self.assertEqual(len(found[0]["excerpt"]), vr.OUTSIDE_SOURCE_EXCERPT_CHARS)

    def test_non_string_material_does_not_crash(self):
        self.assertEqual(vr.cited_lines(None, "x"), [])


class OutsideSourceTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.vault = Path(self._directory.name)

    def tearDown(self):
        self._directory.cleanup()

    def _note(self, name, text):
        (self.vault / name).write_text(text, encoding="utf-8")
        return {"path": name, "wikilink": f"[[{Path(name).stem}]]"}

    def test_the_material_label_is_carried_onto_its_own_citations(self):
        found = vr.outside_sources(
            "A claim genuinely worth citing, https://example.com/a", "this braindump", self.vault, []
        )
        self.assertEqual(found[0]["source"], "this braindump")

    def test_a_candidate_note_is_credited_to_its_wikilink(self):
        candidate = self._note("Imported.md", "The finding replicated twice, https://example.com/b")
        found = vr.outside_sources("nothing here", "this recording", self.vault, [candidate])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["source"], "[[Imported]]")

    def test_an_unreadable_candidate_is_skipped_rather_than_fatal(self):
        candidates = [{"path": "missing.md", "wikilink": "[[missing]]"}]
        self.assertEqual(vr.outside_sources("no links", "this recording", self.vault, candidates), [])

    def test_a_repeated_url_is_harvested_once(self):
        candidate = self._note("Note.md", "The same claim worth citing, https://example.com/dupe")
        found = vr.outside_sources(
            "The same claim worth citing, https://example.com/dupe", "this braindump", self.vault, [candidate]
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["source"], "this braindump")

    def test_harvesting_is_capped(self):
        material = "\n".join(
            f"A claim worth citing here about part {index} at https://example.com/{index}" for index in range(40)
        )
        found = vr.outside_sources(material, "this braindump", self.vault, [])
        self.assertEqual(len(found), vr.OUTSIDE_SOURCE_LIMIT)

    def test_nothing_is_fetched(self):
        """The invariant both skills depend on: a URL in the material is never
        opened, only recorded. A candidate path is read; a network host is not."""
        found = vr.outside_sources(
            "A claim genuinely worth citing, https://198.51.100.9/unreachable", "this braindump", self.vault, []
        )
        self.assertEqual(len(found), 1)


class VocabularyTests(unittest.TestCase):
    def test_both_skills_agree_on_the_shared_headings(self):
        self.assertEqual(vr.REFLECTION_HEADINGS, frozenset(vr.JOURNAL_HEADINGS) | frozenset(vr.WORKING_HEADINGS))

    def test_connections_and_open_questions_are_common_to_both_sets(self):
        shared = frozenset(vr.JOURNAL_HEADINGS) & frozenset(vr.WORKING_HEADINGS)
        self.assertEqual(shared, {"Open questions", "Connections"})

    def test_interpretations_is_journal_only(self):
        """Guarding the reason the split exists: introspective sections must not
        reach a working note."""
        self.assertIn("Interpretations", vr.JOURNAL_HEADINGS)
        self.assertNotIn("Interpretations", vr.WORKING_HEADINGS)


class CalloutTests(unittest.TestCase):
    """How a generated section is marked, for both note-writing skills."""

    def test_a_section_renders_collapsed_with_its_title(self):
        self.assertEqual(
            vr.render_callout("reflection", "Observations", ["- The week was difficult."]),
            "> [!reflection]- Observations\n> - The week was difficult.",
        )

    def test_an_open_callout_drops_the_fold_marker_and_can_go_untitled(self):
        """The summary case: open, and its title is the callout type itself."""
        self.assertEqual(vr.render_callout("summary", None, ["One paragraph."], collapsed=False), "> [!summary]\n> One paragraph.")

    def test_a_blank_line_becomes_a_bare_marker(self):
        """What holds a callout together across a paragraph break, so a section
        written as prose survives as prose rather than as two callouts."""
        rendered = vr.render_callout("reflection", "Context", ["First paragraph.", "", "Second paragraph."])
        self.assertEqual(rendered.splitlines()[2], ">")
        self.assertTrue(all(line.startswith(">") for line in rendered.splitlines()))

    def test_a_section_with_nothing_under_it_is_just_its_head(self):
        self.assertEqual(vr.render_callout("reflection", "Next steps", []), "> [!reflection]- Next steps")

    def test_connections_is_the_one_section_marked_differently(self):
        self.assertEqual(vr.callout_type_for("Connections"), "connections")
        for heading in vr.REFLECTION_HEADINGS - {"Connections"}:
            self.assertEqual(vr.callout_type_for(heading), "reflection", heading)


if __name__ == "__main__":
    unittest.main()

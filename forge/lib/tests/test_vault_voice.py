#!/usr/bin/env python3
"""Tests for the shared voice-and-style note parser."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vault_voice as vv
from vault_schema import UserError

VOICE = """---
type: system
status: active
domain: meta
subdomain: schemas
capture_type: manual
---

# Voice and Style

## Global voice

### Universal

- Write in first person, present tense.

### Owner-authored

- Keep contractions. "I don't know" is how I talk.
- Say what is unresolved as unresolved.

### Source-derived

- Describe source claims without imitating the source.

## Per-type style

### Universal

| Type | Style |
| --- | --- |
| `note` | Flowing paragraphs, no headings under 300 words. |
| `task` | Bullets for the things to do, one sentence of context above them. |

## Vocabulary

- vault — the Obsidian vault, never "knowledge base".
- braindump — what I call unedited thinking.
- mechanism — how or why something happens.

## Formatting

- No trailing periods on bullet items that are not sentences.

## Never do

- Never call me "the user" in a note.
- Never resolve an open question on my behalf.
"""


class ParsingTests(unittest.TestCase):
    def test_every_section_parses(self):
        voice = vv.parse_voice_note(VOICE)
        self.assertEqual(len(voice["global"]), 4)
        self.assertEqual(voice["per_type"]["task"], "Bullets for the things to do, one sentence of context above them.")
        self.assertEqual(len(voice["vocabulary"]), 3)
        self.assertEqual(voice["formatting"], ["No trailing periods on bullet items that are not sentences."])
        self.assertEqual(len(voice["never"]), 2)
        self.assertEqual(voice["recognized_scopes"], ["universal", "owner-authored", "source-derived"])

    def test_a_partial_note_is_fine(self):
        voice = vv.parse_voice_note("# Voice\n\n## Never do\n\n- Never call me the user.\n")
        self.assertEqual(voice["never"], ["Never call me the user."])
        self.assertEqual(voice["global"], [])

    def test_a_note_with_no_known_section_is_refused(self):
        with self.assertRaisesRegex(UserError, "none of its sections"):
            vv.parse_voice_note("# Voice and Style\n\nSome prose about how I write.\n")

    def test_an_empty_section_fails_closed(self):
        with self.assertRaisesRegex(UserError, "no bullets"):
            vv.parse_voice_note("## Global voice\n\nI write plainly.\n")

    def test_long_rules_are_preserved_complete_and_dropped_as_a_unit(self):
        rule = "Keep " + ("meaningful complexity " * 80).strip() + "."
        voice = vv.parse_voice_note(f"## Global voice\n\n- {rule}\n")
        self.assertEqual(voice["global"], [rule])
        self.assertNotIn(rule[:200], vv.prompt_prefix(voice, budget=300))

    def test_a_malformed_table_fails_closed(self):
        with self.assertRaisesRegex(UserError, "Per-type style"):
            vv.parse_voice_note("## Per-type style\n\n| Type |\n| --- |\n| `note` |\n")

    def test_a_duplicate_type_row_fails_closed(self):
        note = "## Per-type style\n\n| Type | Style |\n| --- | --- |\n| `note` | A. |\n| `note` | B. |\n"
        with self.assertRaisesRegex(UserError, "duplicate type"):
            vv.parse_voice_note(note)


class PromptSegmentTests(unittest.TestCase):
    def setUp(self):
        self.voice = vv.parse_voice_note(VOICE)

    def test_the_segment_is_deterministic(self):
        first = vv.prompt_segment(self.voice, "task")
        self.assertEqual(first, vv.prompt_segment(self.voice, "task"))

    def test_only_the_matching_type_row_is_included(self):
        segment = vv.prompt_segment(self.voice, "task")
        self.assertIn("Bullets for the things to do", segment)
        self.assertNotIn("Flowing paragraphs", segment)

    def test_an_unknown_type_still_gets_the_global_rules(self):
        segment = vv.prompt_segment(self.voice, "recipe")
        self.assertIn("first person, present tense", segment)
        self.assertNotIn("Bullets for the things to do", segment)

    def test_context_selects_only_applicable_scopes(self):
        owner = vv.prompt_prefix(self.voice, vv.CONTEXT_OWNER)
        source = vv.prompt_prefix(self.voice, vv.CONTEXT_SOURCE)
        self.assertIn("Keep contractions", owner)
        self.assertNotIn("Describe source claims", owner)
        self.assertIn("Describe source claims", source)
        self.assertNotIn("Keep contractions", source)

    def test_vocabulary_is_relevant_to_current_material(self):
        compiled = vv.compile_voice(
            self.voice,
            vv.CONTEXT_OWNER,
            note_type="note",
            material="Explain the mechanism in this vault.",
        )
        self.assertEqual(
            compiled["vocabulary"],
            [
                'vault — the Obsidian vault, never "knowledge base".',
                "mechanism — how or why something happens.",
            ],
        )
        self.assertNotIn("braindump", compiled["context"])

    def test_formatting_rules_stay_out_of_the_prompt(self):
        # Naming a formatting rule makes a non-thinking model apply it wherever
        # it half-fits, so these are checked afterwards instead.
        self.assertNotIn("trailing periods", vv.prompt_segment(self.voice, "note"))
        self.assertEqual(vv.formatting_rules(self.voice), ["No trailing periods on bullet items that are not sentences."])

    def test_prohibitions_survive_a_tight_budget(self):
        segment = vv.prompt_segment(self.voice, "note", budget=320)
        self.assertIn("Never call me", segment)
        self.assertNotIn("braindump —", segment)

    def test_whole_bullets_are_dropped_rather_than_cut(self):
        for budget in range(200, 1400, 37):
            segment = vv.prompt_segment(self.voice, "note", budget=budget)
            for line in segment.splitlines():
                if not line.startswith("- "):
                    continue
                self.assertIn(line[2:], VOICE, f"a bullet was truncated at budget {budget}")

    def test_no_voice_note_means_no_segment(self):
        self.assertEqual(vv.prompt_segment(None, "note"), "")
        self.assertEqual(vv.prompt_segment({}, "note"), "")

    def test_none_context_never_applies_voice(self):
        compiled = vv.compile_voice(self.voice, vv.CONTEXT_NONE, note_type="note", material="vault")
        self.assertEqual(compiled["prefix"], "")
        self.assertEqual(compiled["context"], "")

    def test_prefix_and_context_budgets_keep_complete_bullets(self):
        compiled = vv.compile_voice(
            self.voice,
            vv.CONTEXT_OWNER,
            note_type="note",
            material="vault braindump mechanism",
            prefix_budget=320,
            context_budget=180,
        )
        self.assertLessEqual(len(compiled["prefix"]), 320)
        self.assertLessEqual(len(compiled["context"]), 180)
        for line in (compiled["prefix"] + "\n" + compiled["context"]).splitlines():
            if line.startswith("- "):
                self.assertIn(line[2:], VOICE)


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name).resolve()
        (self.vault / "99 Meta" / "99.02 Schemas").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write_default(self):
        path = self.vault / vv.DEFAULT_VOICE
        path.write_text(VOICE, encoding="utf-8")
        return path

    def test_a_vault_without_a_voice_note_is_supported(self):
        self.assertIsNone(vv.resolve_voice_path(self.vault))
        voice, voice_hash = vv.compiled_voice_for(self.vault, None)
        self.assertIsNone(voice)
        self.assertIsNone(voice_hash)
        self.assertEqual(vv.prompt_segment(voice, "note"), "")

    def test_the_canonical_path_wins(self):
        path = self.write_default()
        self.assertEqual(vv.resolve_voice_path(self.vault), path.resolve())

    def test_a_note_elsewhere_is_found(self):
        stray = self.vault / "Elsewhere" / vv.VOICE_BASENAME
        stray.parent.mkdir()
        stray.write_text(VOICE, encoding="utf-8")
        self.assertEqual(vv.resolve_voice_path(self.vault), stray.resolve())

    def test_the_canonical_path_settles_an_otherwise_ambiguous_vault(self):
        path = self.write_default()
        stray = self.vault / "Elsewhere" / vv.VOICE_BASENAME
        stray.parent.mkdir()
        stray.write_text(VOICE, encoding="utf-8")
        self.assertEqual(vv.resolve_voice_path(self.vault), path.resolve())

    def test_two_candidates_outside_the_canonical_path_fail_closed(self):
        for folder in ("Elsewhere", "Somewhere"):
            stray = self.vault / folder / vv.VOICE_BASENAME
            stray.parent.mkdir()
            stray.write_text(VOICE, encoding="utf-8")
        with self.assertRaisesRegex(UserError, "more than one"):
            vv.resolve_voice_path(self.vault)

    def test_an_unfiled_copy_in_the_inbox_is_not_the_voice_note(self):
        inbox = self.vault / "00 Inbox" / vv.VOICE_BASENAME
        inbox.parent.mkdir()
        inbox.write_text(VOICE, encoding="utf-8")
        self.assertIsNone(vv.resolve_voice_path(self.vault))

    def test_an_explicitly_named_missing_note_is_an_error(self):
        with self.assertRaisesRegex(UserError, "does not exist"):
            vv.resolve_voice_path(self.vault, "99 Meta/nope.md")

    def test_the_compiled_cache_is_keyed_on_the_note(self):
        path = self.write_default()
        cache = self.vault / ".cache"
        voice, first_hash = vv.compiled_voice_for(self.vault, path, cache_dir=cache)
        self.assertTrue((cache / "compiled-voice.json").is_file())
        again, again_hash = vv.compiled_voice_for(self.vault, path, cache_dir=cache)
        self.assertEqual((voice, first_hash), (again, again_hash))
        path.write_text(VOICE.replace("first person, present tense", "third person"), encoding="utf-8")
        changed, changed_hash = vv.compiled_voice_for(self.vault, path, cache_dir=cache)
        self.assertNotEqual(changed_hash, first_hash)
        self.assertIn("Write in third person.", changed["global"])


class RoundTripTests(unittest.TestCase):
    def test_a_rendered_note_parses_back_to_the_same_thing(self):
        voice = vv.parse_voice_note(VOICE)
        reparsed = vv.parse_voice_note(vv.render_voice_note(voice))
        self.assertEqual(vv.voice_digest(voice), vv.voice_digest(reparsed))

    def test_rendering_a_note_with_one_section_round_trips(self):
        voice = {
            "global": [],
            "per_type": {},
            "vocabulary": [],
            "formatting": [],
            "never": ["Never guess a date."],
            "scope_map": {
                "global": [],
                "per_type": {},
                "vocabulary": [],
                "formatting": [],
                "never": ["universal"],
            },
        }
        self.assertEqual(vv.parse_voice_note(vv.render_voice_note(voice))["never"], ["Never guess a date."])

    def test_preference_render_preserves_frontmatter_and_unknown_content(self):
        original = VOICE + "\n## Human notes\n\nDo not delete this paragraph.\n"
        voice = vv.parse_voice_note(original)
        voice["global"].append("A newly accepted preference.")
        voice["scope_map"]["global"].append("owner-authored")
        rendered = vv.render_voice_note(voice, original_text=original)
        self.assertTrue(rendered.startswith("---\ntype: system\n"))
        self.assertIn("## Human notes\n\nDo not delete this paragraph.", rendered)
        self.assertIn("### Owner-Authored", rendered)
        self.assertIn("- A newly accepted preference.", rendered)

    def test_unknown_scope_is_preserved_but_not_compiled(self):
        original = VOICE + "\n### Experimental\n\n- Keep this private rule untouched.\n"
        voice = vv.parse_voice_note(original)
        self.assertIn("Experimental", voice["unknown_scopes"])
        rendered = vv.render_voice_note(voice, original_text=original)
        self.assertIn("### Experimental\n\n- Keep this private rule untouched.", rendered)


if __name__ == "__main__":
    unittest.main(verbosity=2)

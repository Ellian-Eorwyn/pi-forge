#!/usr/bin/env python3
"""Tests for the note-format registry and its agreement check.

The registry's whole value is that it fails when an implementation drifts, so
most of what follows breaks one thing on purpose and asserts the break is seen.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vault_format as vf
import vault_reflection as vr
from vault_schema import UserError

NOTE = """# Note Format

## Callout registry

| Callout | Means | Accent | Icon | Folded | Not for |
| --- | --- | --- | --- | --- | --- |
| `summary` | Orientation | cyan `--color-cyan-rgb` | `lucide-align-left` | no | A second lead |
| `key` | The claims | blue `--color-blue-rgb` | `lucide-key` | no | Restating the lead |
| `connections` | Relations | quiet `--callout-quote` | `lucide-link` | yes | An inline link |

## Headings

Sentence case.
"""

CSS = """
.callout[data-callout="summary"],
.callout[data-callout="abstract"],
.callout[data-callout="tldr"] {
  --callout-color: var(--color-cyan-rgb);
  --callout-icon: lucide-align-left;
}

.callout[data-callout="key"] {
  --callout-color: var(--color-blue-rgb);
  --callout-icon: lucide-key;
}

.callout[data-callout="connections"] {
  --callout-color: var(--callout-quote);
  --callout-icon: lucide-link;
}

.callout[data-callout="summary"],
.callout[data-callout="key"],
.callout[data-callout="connections"] {
  border-radius: 8px;
}
"""

CODE = ("summary", "key", "connections")


class RegistryParseTests(unittest.TestCase):
    def test_the_registry_parses(self):
        registry = vf.parse_format_note(NOTE)
        self.assertEqual(sorted(registry), ["connections", "key", "summary"])
        self.assertEqual(registry["key"]["accent"], "--color-blue-rgb")
        self.assertEqual(registry["key"]["icon"], "lucide-key")

    def test_folded_is_read_as_a_boolean(self):
        registry = vf.parse_format_note(NOTE)
        self.assertTrue(registry["connections"]["folded"])
        self.assertFalse(registry["summary"]["folded"])

    def test_a_duplicated_row_is_refused(self):
        doubled = NOTE.replace("| `key` | The claims", "| `key` | again | blue `--color-blue-rgb` | `lucide-key` | no | x |\n| `key` | The claims", 1)
        with self.assertRaises(UserError):
            vf.parse_format_note(doubled)

    def test_an_alias_may_not_be_registered(self):
        """`tldr` and `summary` are one Obsidian callout; registering both would
        let the two rows disagree with no way to tell which won."""
        with self.assertRaises(UserError):
            vf.parse_format_note(NOTE.replace("| `summary` |", "| `tldr` |", 1))

    def test_a_stock_callout_may_not_be_registered(self):
        with self.assertRaises(UserError):
            vf.parse_format_note(NOTE.replace("| `key` |", "| `quote` |", 1))

    def test_an_icon_must_be_a_lucide_name(self):
        with self.assertRaises(UserError):
            vf.parse_format_note(NOTE.replace("`lucide-key`", "`key`", 1))

    def test_an_accent_must_name_a_variable(self):
        with self.assertRaises(UserError):
            vf.parse_format_note(NOTE.replace("blue `--color-blue-rgb`", "blue", 1))

    def test_a_missing_registry_section_is_refused(self):
        with self.assertRaises(UserError):
            vf.parse_format_note("# Note Format\n\nNo registry here.\n")


class StylesheetParseTests(unittest.TestCase):
    def test_identities_are_read_including_aliases(self):
        styled = vf.parse_stylesheet(CSS)
        self.assertEqual(styled["tldr"]["accent"], "--color-cyan-rgb")
        self.assertEqual(styled["key"]["icon"], "lucide-key")

    def test_a_shared_appearance_block_is_not_an_identity(self):
        """The block giving every callout the same border sets neither an accent
        nor an icon, and reading it as an identity would report each twice."""
        self.assertEqual(len(vf.parse_stylesheet(CSS)), 5)

    def test_canonical_folds_an_alias_onto_its_registry_name(self):
        self.assertEqual(vf.canonical("tldr"), "summary")
        self.assertEqual(vf.canonical("attention"), "caution")
        self.assertEqual(vf.canonical("key"), "key")


class CalloutUseTests(unittest.TestCase):
    def test_callouts_are_found_in_a_body(self):
        body = "# T\n\n> [!summary]\n> x\n\n> [!connections]- Connections\n> - [[A]]\n"
        self.assertEqual(vf.callouts_used(body), {"summary", "connections"})

    def test_an_alias_is_reported_as_its_registry_name(self):
        self.assertEqual(vf.callouts_used("> [!tldr] x"), {"summary"})

    def test_a_blockquote_is_not_a_callout(self):
        self.assertEqual(vf.callouts_used("> just a quotation\n"), set())


class AgreementTests(unittest.TestCase):
    def check(self, note=NOTE, css=CSS, code=CODE, templates=()):
        return vf.check_agreement(note, css, code, templates)

    def test_a_matching_set_is_clean(self):
        self.assertEqual(self.check(), [])

    def test_a_changed_icon_is_caught(self):
        findings = self.check(css=CSS.replace("lucide-key", "lucide-star"))
        self.assertEqual(len(findings), 1)
        self.assertIn("icon", findings[0][1])
        self.assertEqual(findings[0][0], "error")

    def test_a_changed_accent_is_caught(self):
        findings = self.check(css=CSS.replace("var(--color-blue-rgb)", "var(--color-red-rgb)"))
        self.assertEqual(len(findings), 1)
        self.assertIn("accent", findings[0][1])

    def test_an_alias_carrying_the_wrong_accent_is_caught(self):
        """The alias block is where a drift hides: `summary` looks right while
        `tldr` renders differently."""
        drifted = CSS.replace(
            '.callout[data-callout="tldr"] {\n  --callout-color: var(--color-cyan-rgb);',
            '.callout[data-callout="tldr"] {\n  --callout-color: var(--color-red-rgb);',
        )
        findings = self.check(css=drifted)
        self.assertTrue(any("tldr" in message for _, message in findings))

    def test_a_registered_callout_with_no_style_is_caught(self):
        stripped = CSS.replace(
            '.callout[data-callout="key"] {\n  --callout-color: var(--color-blue-rgb);\n  --callout-icon: lucide-key;\n}\n', ""
        )
        findings = self.check(css=stripped)
        self.assertTrue(any("no identity" in message for _, message in findings))

    def test_a_styled_callout_with_no_row_is_caught(self):
        findings = self.check(note=NOTE.replace("| `key` | The claims | blue `--color-blue-rgb` | `lucide-key` | no | Restating the lead |\n", ""))
        self.assertTrue(any("not registered" in message for _, message in findings))

    def test_code_emitting_an_unregistered_callout_is_an_error(self):
        findings = self.check(code=CODE + ("hypothesis",))
        self.assertEqual(findings, [("error", "callout 'hypothesis' is emitted by vault_reflection but not registered")])

    def test_a_registered_callout_the_code_never_emits_is_only_a_warning(self):
        """Most of the registry is written by a model into a note body, not by
        `render_callout`, so this is untidy rather than broken."""
        findings = self.check(code=("summary", "key"))
        self.assertEqual([severity for severity, _ in findings], ["warning"])

    def test_a_template_using_an_unregistered_callout_is_caught(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Bad Template.md"
            path.write_text("# X\n\n> [!hypothesis] Nope\n> body\n", encoding="utf-8")
            findings = self.check(templates=[path])
        self.assertTrue(any("unregistered callout 'hypothesis'" in message for _, message in findings))

    def test_a_template_using_a_stock_callout_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Fine Template.md"
            path.write_text("# X\n\n> [!quote] Source\n> body\n", encoding="utf-8")
            self.assertEqual(self.check(templates=[path]), [])


class NamespaceTests(unittest.TestCase):
    """`reviewer-2` owns the `r2-` prefix, and the registry does not describe it."""

    def test_a_prefixed_callout_is_recognised(self):
        self.assertTrue(vf.is_namespaced("r2-gap"))
        self.assertFalse(vf.is_namespaced("evidence"))

    def test_a_namespaced_style_does_not_need_a_registry_row(self):
        css = CSS + '\n.callout[data-callout="r2-logic"] {\n  --callout-color: var(--color-red-rgb);\n  --callout-icon: lucide-x-circle;\n}\n'
        self.assertEqual(vf.check_agreement(NOTE, css, CODE, ()), [])

    def test_a_template_may_not_borrow_the_namespace(self):
        """A note is not a review, and a template reaching for a review callout
        is the collision the prefix exists to prevent."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Wrong Template.md"
            path.write_text("# X\n\n> [!r2-gap] Nope\n> body\n", encoding="utf-8")
            findings = vf.check_agreement(NOTE, CSS, CODE, [path])
        self.assertTrue(any("belongs to a skill" in message for _, message in findings))

    def test_the_shipped_stylesheet_carries_the_reviewer_vocabulary(self):
        css = (Path(__file__).resolve().parents[1] / "vault-format" / "loom-notes.css").read_text(encoding="utf-8")
        styled = {name for name in vf.parse_stylesheet(css) if vf.is_namespaced(name)}
        self.assertEqual(len(styled), 6)


class LiveVaultTests(unittest.TestCase):
    """The registry as it actually ships, checked against the files beside it."""

    def setUp(self):
        self.assets = Path(__file__).resolve().parents[1] / "vault-format"

    def test_the_shipped_registry_parses(self):
        registry = vf.parse_format_note((self.assets / "note-format.md").read_text(encoding="utf-8"))
        self.assertEqual(len(registry), len(vr.VAULT_CALLOUTS))

    def test_the_shipped_files_agree(self):
        findings = vf.check_agreement(
            (self.assets / "note-format.md").read_text(encoding="utf-8"),
            (self.assets / "loom-notes.css").read_text(encoding="utf-8"),
            vr.VAULT_CALLOUTS,
            [self.assets / "template-blueprint.md"],
        )
        self.assertEqual(findings, [])

    def test_every_registered_callout_is_demonstrated_by_the_blueprint(self):
        """The blueprint is the file a template author copies, so a block missing
        from it is a block nobody will discover."""
        used = vf.callouts_used((self.assets / "template-blueprint.md").read_text(encoding="utf-8"))
        self.assertEqual(used, set(vr.VAULT_CALLOUTS))

    def test_red_stays_unclaimed(self):
        """Obsidian's danger, error, and failure keep red, so a real problem still
        reads as one."""
        registry = vf.parse_format_note((self.assets / "note-format.md").read_text(encoding="utf-8"))
        self.assertNotIn("--color-red-rgb", {entry["accent"] for entry in registry.values()})


GRAMMAR_NOTE = """# Note Format

## Block grammar

A note is assembled from the blocks below.

```
frontmatter          schema-controlled
# Title              exactly one level-one heading
> [!summary]         the lead
<body>               prose and ## sections
> [!provenance]-     how this note was made
## Notes             owner-authored; never written, never read
```

### Block order

| Block | Syntax | Required | Written by | Means |
| --- | --- | --- | --- | --- |
| `frontmatter` | `frontmatter` | no | schema | schema-controlled |
| `title` | `# Title` | yes | either | exactly one level-one heading |
| `summary` | `> [!summary]` | no | either | the lead |
| `body` | `<body>` | no | either | prose and ## sections |
| `provenance` | `> [!provenance]-` | no | machine | how this note was made |
| `notes` | `## Notes` | no | owner | owner-authored; never written, never read |

## Callout registry

| Callout | Means | Accent | Icon | Folded | Not for |
| --- | --- | --- | --- | --- | --- |
| `summary` | Orientation | cyan `--color-cyan-rgb` | `lucide-align-left` | no | A second lead |
| `provenance` | How it was made | quiet `--callout-quote` | `lucide-history` | yes | Anything a reader needs |

## Per-type shapes

| Type | Shape |
| --- | --- |
| `note` | Lead, then prose. |
| `concept` / wiki card | Lead as `summary`, then the kind's sections. |

## Never do

- Never invent a callout type. Add it to the registry above
  first, or use prose.
"""


class BlockGrammarTests(unittest.TestCase):
    def test_the_grammar_parses_in_row_order(self):
        blocks = vf.parse_block_grammar(GRAMMAR_NOTE)
        self.assertEqual(
            [entry["block"] for entry in blocks],
            ["frontmatter", "title", "summary", "body", "provenance", "notes"],
        )
        self.assertTrue(blocks[1]["required"])
        self.assertEqual(blocks[4]["written_by"], vf.WRITTEN_BY_MACHINE)
        self.assertEqual(vf.block_index(vf.parse_format(GRAMMAR_NOTE))["notes"], 5)

    def test_a_vault_that_has_not_declared_an_order_is_not_a_broken_vault(self):
        """Absence and malformation are different findings: every vault started
        without a block order, and none of them was malformed for it."""
        self.assertEqual(vf.parse_block_grammar(NOTE), [])
        self.assertEqual(vf.parse_format(NOTE)["blocks"], [])
        self.assertEqual(vf.prompt_prefix(vf.parse_format(NOTE)), "")

    def test_the_fence_and_the_table_must_agree(self):
        """Two statements of the same thing in one file drift silently, and the
        fence is the half a person actually reads."""
        reordered = GRAMMAR_NOTE.replace(
            "| `summary` | `> [!summary]` | no | either | the lead |\n"
            "| `body` | `<body>` | no | either | prose and ## sections |\n",
            "| `body` | `<body>` | no | either | prose and ## sections |\n"
            "| `summary` | `> [!summary]` | no | either | the lead |\n",
        )
        with self.assertRaises(UserError) as caught:
            vf.parse_block_grammar(reordered)
        self.assertIn("fence and the Block order table disagree", str(caught.exception))

    def test_a_declared_order_still_fails_closed(self):
        for broken, expected in (
            ("| `notes` | `## Notes` | no | owner |", "malformed row"),
            ("| `notes` | `## Notes` | maybe | owner | owner-authored; never written, never read |", "Required"),
            ("| `notes` | `## Notes` | no | nobody | owner-authored; never written, never read |", "Written by"),
        ):
            note = GRAMMAR_NOTE.replace(
                "| `notes` | `## Notes` | no | owner | owner-authored; never written, never read |", broken
            )
            with self.assertRaises(UserError) as caught:
                vf.parse_block_grammar(note)
            self.assertIn(expected, str(caught.exception))

    def test_a_duplicate_block_is_refused(self):
        note = GRAMMAR_NOTE.replace(
            "| `notes` | `## Notes` | no | owner | owner-authored; never written, never read |",
            "| `notes` | `## Notes` | no | owner | owner-authored |\n"
            "| `notes` | `## Notes` | no | owner | owner-authored |",
        )
        with self.assertRaises(UserError):
            vf.parse_block_grammar(note)


class TypeShapeTests(unittest.TestCase):
    def test_shapes_key_on_the_first_backticked_type(self):
        shapes = vf.parse_type_shapes(GRAMMAR_NOTE)
        self.assertEqual(sorted(shapes), ["concept", "note"])
        self.assertEqual(shapes["concept"]["label"], "`concept` / wiki card")

    def test_the_shipped_note_already_declares_shapes(self):
        """This table needs no vault edit -- it parses as shipped."""
        assets = Path(__file__).resolve().parents[1] / "vault-format"
        shapes = vf.parse_type_shapes((assets / "note-format.md").read_text(encoding="utf-8"))
        self.assertIn("journal", shapes)
        self.assertIn("reflection", shapes["journal"]["shape"])


class PromptPrefixTests(unittest.TestCase):
    def test_the_prefix_lists_only_blocks_a_generator_may_write(self):
        """Telling a model about `## Notes` is how a model comes to write one."""
        prefix = vf.prompt_prefix(vf.parse_format(GRAMMAR_NOTE), "note")
        self.assertIn("summary (> [!summary])", prefix)
        self.assertIn("provenance", prefix)
        self.assertNotIn("## Notes", prefix)
        self.assertNotIn("frontmatter", prefix)

    def test_the_named_type_brings_its_own_shape(self):
        prefix = vf.prompt_prefix(vf.parse_format(GRAMMAR_NOTE), "note")
        self.assertIn("Shape for `note`", prefix)
        self.assertNotIn("wiki card", prefix)

    def test_a_wrapped_prohibition_survives_whole(self):
        """A rule that stops mid-sentence is worse than no rule at all."""
        prefix = vf.prompt_prefix(vf.parse_format(GRAMMAR_NOTE))
        self.assertIn("Add it to the registry above first, or use prose.", prefix)

    def test_the_shipped_note_fits_its_own_budget(self):
        """The groups are appended in order, so a budget that fits only the first
        two spends the whole prefix on the block list and states no prohibition."""
        assets = Path(__file__).resolve().parents[1] / "vault-format"
        text = (assets / "note-format.md").read_text(encoding="utf-8")
        fmt = vf.parse_format(text)
        if not fmt["blocks"]:
            self.skipTest("the shipped note has not adopted the block order table yet")
        prefix = vf.prompt_prefix(fmt, "journal")
        self.assertIn("Never do:", prefix)
        self.assertLessEqual(len(prefix), vf.DEFAULT_PREFIX_BUDGET)


class CompiledFormatTests(unittest.TestCase):
    def test_the_cache_is_keyed_by_the_note_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            note = root / "0.04 Note Format.md"
            note.write_text(GRAMMAR_NOTE, encoding="utf-8")
            cache = root / "cache"
            first, first_hash = vf.compiled_format_for(root, note, cache_dir=cache)
            self.assertTrue((cache / "compiled-format.json").is_file())
            cached, cached_hash = vf.compiled_format_for(root, note, cache_dir=cache)
            self.assertEqual(cached, first)
            self.assertEqual(cached_hash, first_hash)

            note.write_text(GRAMMAR_NOTE.replace("the lead", "the opening"), encoding="utf-8")
            changed, changed_hash = vf.compiled_format_for(root, note, cache_dir=cache)
            self.assertNotEqual(changed_hash, first_hash)
            self.assertEqual(changed["blocks"][2]["means"], "the opening")

    def test_format_state_is_serializable(self):
        state = vf.format_state("/vault/0.04 Note Format.md", "abc123")
        self.assertEqual(state["format_compiler_version"], vf.COMPILED_FORMAT_VERSION)
        self.assertEqual(state["format_hash"], "abc123")


if __name__ == "__main__":
    unittest.main()

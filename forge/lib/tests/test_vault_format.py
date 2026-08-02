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


if __name__ == "__main__":
    unittest.main()

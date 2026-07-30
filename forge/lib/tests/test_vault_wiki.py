#!/usr/bin/env python3

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vault_wiki as vw  # noqa: E402
from vault_schema import UserError  # noqa: E402

SPEC_JSON = {
    "kinds": {
        kind: {
            "max_managed_chars": 1600,
            "sections": [
                {"id": "_lead", "placeholder": "summary", "fill": "lead", "max_chars": 320},
                {
                    "id": "key_points",
                    "heading": "Key Points",
                    "aliases": ["Key Ideas"],
                    "placeholder": "key_points",
                    "fill": "bullets",
                    "max_bullets": 5,
                    "max_chars": 160,
                },
                {"id": "origin", "heading": "Origin", "placeholder": "origin", "fill": "prose", "max_chars": 220, "optional": True},
                {"id": "links", "heading": "Associated Concepts", "placeholder": "links", "fill": "links"},
                {"id": "sources", "heading": "Sources", "placeholder": "sources", "fill": "links"},
                {"id": "notes", "heading": "Notes", "owner": True},
                {"id": "_footnotes", "placeholder": "footnotes", "fill": "footnotes"},
            ],
        }
        for kind in vw.WIKI_KINDS
    }
}

FILL = {
    "_lead": "A new definition.",
    "key_points": "- one\n- two",
    "origin": "Coined 1970.",
    "sources": "- [SEP](https://plato.stanford.edu/entries/x/)",
    "_footnotes": '[^1]: SEP, "X" §2.',
}


def load_specs(raw=None):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(raw or SPEC_JSON, handle)
        path = handle.name
    return vw.load_kind_specs(path)


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.spec = load_specs()["concept"]

    def merge(self, body, fill=None):
        merged = vw.merge_sections(body, self.spec, fill or FILL)
        vw.assert_only_managed_changed(body, merged, self.spec)
        return merged

    def test_round_trip_is_lossless(self):
        for body in (
            "# T\n\nlead\n\n## Notes\nkeep\n",
            "## Only\ncontent",
            "",
            "no headings at all\n",
            "# T\r\n\r\nlead\r\n\r\n[^1]: a\r\n",
        ):
            self.assertEqual(vw.assemble(vw.parse_sections(body)), body)

    def test_replaces_an_existing_managed_section(self):
        merged = self.merge("# T\n\nold\n\n## Key Points\n- stale\n\n## Notes\nkeep\n")
        self.assertIn("- one\n- two", merged)
        self.assertNotIn("stale", merged)

    def test_inserts_a_missing_section_in_spec_order(self):
        merged = self.merge("# T\n\nold\n\n## Sources\n- [x](https://x)\n")
        self.assertLess(merged.index("## Key Points"), merged.index("## Origin"))
        self.assertLess(merged.index("## Origin"), merged.index("## Sources"))

    def test_preserves_owner_notes_byte_for_byte(self):
        body = "# T\n\nold\n\n## Notes\n**mine**, with  odd   spacing\nand a second line\n"
        merged = self.merge(body)
        self.assertIn("**mine**, with  odd   spacing\nand a second line", merged)

    def test_preserves_an_unknown_section(self):
        merged = self.merge("# T\n\nold\n\n## Key Texts\n- a book\n\n## Notes\nkeep\n")
        self.assertIn("## Key Texts\n- a book", merged)

    def test_alias_updates_in_place_and_keeps_the_note_heading(self):
        merged = self.merge("# T\n\nold\n\n## Key Ideas\n- stale\n\n## Notes\nkeep\n")
        self.assertIn("## Key Ideas", merged)
        self.assertNotIn("## Key Points", merged)

    def test_a_subheading_is_replaced_with_its_parent_section(self):
        merged = self.merge("# T\n\nold\n\n## Key Points\n- a\n\n### Sub\nnested\n\n## Notes\nk\n")
        self.assertNotIn("### Sub", merged)
        self.assertNotIn("nested", merged)

    def test_does_not_move_an_existing_section(self):
        body = "# T\n\nold\n\n## Notes\nkeep\n\n## Key Points\n- stale\n"
        merged = self.merge(body)
        self.assertLess(merged.index("## Notes"), merged.index("## Key Points"))

    def test_preserves_crlf(self):
        merged = self.merge("# T\r\n\r\nold\r\n\r\n## Notes\r\nkeep\r\n")
        self.assertNotIn("\n", merged.replace("\r\n", ""))

    def test_preserves_a_missing_trailing_newline(self):
        self.assertFalse(self.merge("# T\n\nold\n\n## Notes\nkeep").endswith("\n"))
        self.assertTrue(self.merge("# T\n\nold\n\n## Notes\nkeep\n").endswith("\n"))

    def test_is_idempotent(self):
        for body in (
            "# T\n\nold\n\n## Notes\nkeep\n",
            "# T\r\n\r\nold\r\n\r\n## Key Ideas\n- x\n",
            "# T\n\nold\n\n## Notes\nkeep",
            "",
            "just prose\n\n## Notes\nk\n",
        ):
            once = self.merge(body)
            self.assertEqual(vw.merge_sections(once, self.spec, FILL), once, body)

    def test_omitted_sections_are_left_alone(self):
        body = "# T\n\nold lead\n\n## Key Points\n- stale\n"
        merged = self.merge(body, {"origin": "Coined 1970."})
        self.assertIn("old lead", merged)
        self.assertIn("- stale", merged)
        self.assertIn("## Origin", merged)

    def test_never_writes_an_owner_section(self):
        with self.assertRaises(vw.MergeError):
            vw.merge_sections("# T\n\nx\n", self.spec, {"notes": "machine text"})

    def test_ownership_check_catches_a_dropped_section(self):
        original = "# T\n\nold\n\n## Notes\nkeep\n"
        with self.assertRaises(vw.MergeError) as caught:
            vw.assert_only_managed_changed(original, "# T\n\nnew\n", self.spec)
        self.assertIn("Notes", str(caught.exception))

    def test_ownership_check_catches_edited_owner_content(self):
        original = "# T\n\nold\n\n## Notes\nkeep\n"
        with self.assertRaises(vw.MergeError):
            vw.assert_only_managed_changed(original, "# T\n\nold\n\n## Notes\nchanged\n", self.spec)

    def test_ownership_check_catches_a_rewritten_title(self):
        with self.assertRaises(vw.MergeError):
            vw.assert_only_managed_changed("# T\n\nx\n", "# Other\n\nx\n", self.spec)

    def test_footnotes_are_not_attributed_to_the_last_section(self):
        merged = self.merge("# T\n\nold\n\n## Notes\nkeep\n")
        parsed = vw.parse_sections(merged)
        notes = [block for block in parsed["blocks"] if block["heading"] == "Notes"][0]
        self.assertNotIn("[^1]", "".join(notes["content"]))
        self.assertIn("[^1]", "".join(parsed["footnotes"]))

    def test_footnote_block_is_replaced_not_duplicated(self):
        merged = self.merge("# T\n\nold\n\n[^1]: stale\n")
        self.assertEqual(merged.count("[^1]:"), 1)
        self.assertNotIn("stale", merged)


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.specs = load_specs()
        self.root = Path(tempfile.mkdtemp())
        self.schema = {
            "domains": {"meta": {"number": 99, "label": "Meta"}, "wiki": {"number": 9, "label": "Wiki"}},
            "subdomains": {
                "meta": {"templates": {"number": 3, "label": "Templates"}},
                "wiki": {value: {"number": index + 1, "label": value.title()} for index, value in enumerate(vw.WIKI_KIND_SUBDOMAIN.values())},
            },
            "projects": {},
            "properties": {},
        }
        self.folder = self.root / "99 Meta" / "99.03 Templates"
        self.folder.mkdir(parents=True)

    def write(self, kind, body, frontmatter=None):
        meta = {**vw.TEMPLATE_FRONTMATTER, **(frontmatter or {})}
        text = "---\n" + "".join(f"{key}: {value}\n" for key, value in meta.items()) + "---\n" + body
        (self.folder / vw.WIKI_TEMPLATE_NAMES[kind]).write_text(text, encoding="utf-8")

    def legacy_body(self):
        return (
            "# {{title}}\n\n{{summary}}\n\n## Evidence\n\n{{evidence}}\n\n"
            "## Sources\n\n{{sources}}\n\n## Provenance\n\n{{provenance}}\n"
        )

    def full_body(self):
        return (
            "# {{title}}\n\n{{summary}}\n\n## Key Points\n\n{{key_points}}\n\n## Origin\n\n{{origin}}\n\n"
            "## Associated Concepts\n\n{{links}}\n\n## Sources\n\n{{sources}}\n\n"
            "## Evidence\n\n{{evidence}}\n\n## Provenance\n\n{{provenance}}\n\n## Notes\n\n{{footnotes}}\n"
        )

    def test_legacy_template_validates_for_the_five_fields(self):
        self.write("concept", self.legacy_body())
        self.assertTrue(vw.inspect_wiki_template(self.root, self.schema, "concept")["ok"])

    def test_legacy_caller_refuses_an_unknown_placeholder(self):
        self.write("concept", self.legacy_body() + "\n{{mystery}}\n")
        result = vw.inspect_wiki_template(self.root, self.schema, "concept")
        self.assertFalse(result["ok"])
        self.assertIn("mystery", " ".join(result["errors"]))

    def test_full_template_validates_for_both_field_sets(self):
        self.write("concept", self.full_body())
        spec = self.specs["concept"]
        self.assertTrue(
            vw.inspect_wiki_template(
                self.root, self.schema, "concept",
                required_fields=spec["required_placeholders"], known_fields=spec["placeholders"],
            )["ok"]
        )
        self.assertTrue(
            vw.inspect_wiki_template(
                self.root, self.schema, "concept",
                required_fields=vw.WIKI_TEMPLATE_FIELDS, known_fields=spec["placeholders"],
            )["ok"]
        )

    def test_wrong_frontmatter_is_refused(self):
        self.write("concept", self.legacy_body(), frontmatter={"status": "raw"})
        result = vw.inspect_wiki_template(self.root, self.schema, "concept")
        self.assertFalse(result["ok"])
        self.assertIn("status: active", " ".join(result["errors"]))

    def test_missing_required_field_is_refused(self):
        self.write("concept", "# {{title}}\n\n{{summary}}\n")
        self.assertFalse(vw.inspect_wiki_template(self.root, self.schema, "concept")["ok"])

    def test_drift_detects_a_heading_the_template_omits(self):
        body = self.full_body().replace("## Origin\n\n{{origin}}\n\n", "")
        problems = vw.template_spec_drift(body, self.specs["concept"], "t.md")
        self.assertTrue(any("Origin" in message for message in problems))

    def test_drift_detects_an_undeclared_placeholder(self):
        problems = vw.template_spec_drift(self.full_body() + "{{mystery}}", self.specs["concept"], "t.md")
        self.assertTrue(any("mystery" in message for message in problems))

    def test_drift_detects_a_declared_placeholder_the_template_never_uses(self):
        body = self.full_body().replace("{{origin}}", "")
        problems = vw.template_spec_drift(body, self.specs["concept"], "t.md")
        self.assertTrue(any("origin" in message for message in problems))

    def test_require_templates_names_every_missing_kind(self):
        with self.assertRaises(UserError) as caught:
            vw.require_wiki_templates(self.root, self.schema, ("concept", "figure"))
        message = str(caught.exception)
        self.assertIn("Wiki Concept.md", message)
        self.assertIn("Wiki Figure.md", message)


class StripUnfilledTests(unittest.TestCase):
    def test_a_fully_filled_body_is_returned_unchanged(self):
        body = "# T\n\nlead\n\n## Sources\n\n- x\n"
        self.assertIs(vw.strip_unfilled(body), body)

    def test_an_unfilled_placeholder_drops_its_heading(self):
        stripped = vw.strip_unfilled("# T\n\nlead\n\n## Key Works\n\n{{key_works}}\n\n## Sources\n\n- x\n")
        self.assertNotIn("Key Works", stripped)
        self.assertNotIn("{{", stripped)
        self.assertIn("## Sources", stripped)

    def test_a_kept_heading_survives_being_empty(self):
        stripped = vw.strip_unfilled("# T\n\nlead\n\n## Notes\n\n{{footnotes}}\n", keep_headings=["Notes"])
        self.assertIn("## Notes", stripped)

    def test_a_heading_with_static_prose_is_not_dropped(self):
        stripped = vw.strip_unfilled("# T\n\nlead\n\n## Fixed\n\nalways here\n\n## Gone\n\n{{x}}\n")
        self.assertIn("## Fixed", stripped)
        self.assertNotIn("## Gone", stripped)


class SpecValidationTests(unittest.TestCase):
    def bad(self, mutate):
        raw = json.loads(json.dumps(SPEC_JSON))
        mutate(raw["kinds"]["concept"])
        with self.assertRaises(UserError):
            load_specs(raw)

    def test_missing_kind_is_refused(self):
        raw = json.loads(json.dumps(SPEC_JSON))
        del raw["kinds"]["figure"]
        with self.assertRaises(UserError):
            load_specs(raw)

    def test_duplicate_section_id_is_refused(self):
        self.bad(lambda spec: spec["sections"].append(spec["sections"][1]))

    def test_unknown_fill_mode_is_refused(self):
        self.bad(lambda spec: spec["sections"][1].__setitem__("fill", "interpretive-dance"))

    def test_owner_section_with_a_placeholder_is_refused(self):
        self.bad(lambda spec: spec["sections"][-2].__setitem__("placeholder", "notes"))

    def test_heading_claimed_by_two_sections_is_refused(self):
        self.bad(lambda spec: spec["sections"][2].__setitem__("aliases", ["Key Points"]))

    def test_non_owner_section_needs_a_placeholder(self):
        self.bad(lambda spec: spec["sections"][1].pop("placeholder"))


class RoutingTests(unittest.TestCase):
    def test_kind_resolves_from_subdomain_not_type(self):
        self.assertEqual(vw.kind_for_metadata({"domain": "wiki", "subdomain": "terms", "type": "concept"}), "term")
        self.assertEqual(vw.kind_for_metadata({"domain": "wiki", "subdomain": "practices", "type": "concept"}), "practice")
        self.assertEqual(vw.kind_for_metadata({"domain": "wiki", "subdomain": "figures", "type": "person"}), "figure")

    def test_non_wiki_notes_have_no_kind(self):
        self.assertIsNone(vw.kind_for_metadata({"domain": "philosophy", "subdomain": "buddhism"}))
        self.assertIsNone(vw.kind_for_metadata({"domain": "wiki", "subdomain": "nonsense"}))

    def test_footnote_helpers(self):
        self.assertEqual(vw.footnote_references("a[^1] b[^note]"), ["1", "note"])
        self.assertEqual(vw.footnote_definitions("[^1]: x\nplain\n[^b]: y"), ["1", "b"])
        self.assertEqual(vw.footnote_references("[^1]: definition"), [])


if __name__ == "__main__":
    unittest.main()

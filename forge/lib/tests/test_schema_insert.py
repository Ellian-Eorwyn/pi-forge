#!/usr/bin/env python3
"""Tests for adding rows to the schema note without disturbing it.

The schema note is the owner's file. Until now the only code that wrote it
changed a single `Number` cell (`replace_schema_row_number`), and everything
else about the note was guaranteed to survive because nothing could reach it.
Insertion widens that opening, so the contract has to be proven rather than
assumed: an inserted row is indistinguishable from a hand-written one, no other
byte moves, and a value the note already carries is refused.

What is proven here is the primitives. That a *proposal* built from them parses,
validates, and does not introduce drift is proven in the vault-curator tests,
which run the whole candidate through the real compiler.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vault_schema as vs  # noqa: E402

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
- `index` — A hub.

## Status values

- `active` — Active.

## Domains

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `craft` | `2` | `Craft` | Making things. |
| `wiki` | `9` | `Wiki` | Reference cards. |
| `meta` | `99` | `Meta` | System notes. |

## Subdomains

### craft

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `gardening` | `1` | `Gardening` | Plants. |
| `cooking` | `2` | `Cooking` | Food. |

### wiki

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `concepts` | `1` | `Concepts` | Named ideas. |

## Project registry

| Approved value | Domain | Subdomain | Number | Definition |
| --- | --- | --- | --- | --- |
| `"[[Greenhouse]]"` | `craft` | `gardening` | `1` | Building the greenhouse. |
| `"[[Kiln]]"` | `craft` |  | `90` | Kiln work. |

## Source kinds

- `book` — Book.

## Capture types

- `manual` — Typed.

## Legacy normalization map

| Legacy input | Canonical output |
| --- | --- |
| `type: daily` | `type: note` |

## Folder routing

### Derived names

```text
domain-folder(domain):
  <pad2(domain.number)> <domain.label>
```
"""


def added_lines(before, after):
    """The lines `after` has that `before` did not, in order."""
    import difflib

    return [
        line[1:]
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
        if line.startswith("+") and not line.startswith("+++")
    ]


def removed_lines(before, after):
    import difflib

    return [
        line[1:]
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
        if line.startswith("-") and not line.startswith("---")
    ]


class FreeNumberTests(unittest.TestCase):
    def setUp(self):
        self.schema = vs.parse_schema_note(SCHEMA)

    def test_domain_numbers_skip_zero_and_the_taken_ones(self):
        free = vs.free_numbers(self.schema, {"kind": "domain"})
        self.assertNotIn(0, free)
        for taken in (2, 9, 99):
            self.assertNotIn(taken, free)
        self.assertEqual(free[0], 1)

    def test_a_domain_project_number_is_taken_for_subdomains_too(self):
        # `craft`'s Kiln project is registered directly under the domain at 90,
        # so it compiles to `02.90` — the same namespace a subdomain lands in.
        free = vs.free_numbers(self.schema, {"kind": "subdomain", "domain": "craft"})
        self.assertNotIn(90, free)
        for taken in (1, 2):
            self.assertNotIn(taken, free)
        self.assertEqual(free[0], 3)

    def test_the_sources_root_number_is_reserved_against_domains(self):
        schema = vs.parse_schema_note(
            SCHEMA.replace(
                "## Source kinds\n\n- `book` — Book.",
                "## Sources root\n\n| Number | Label | Definition |\n| --- | --- | --- |\n"
                "| `10` | `Sources` | External sources. |\n\n"
                "## Source kinds\n\n| Value | Number | Label | Definition |\n| --- | --- | --- | --- |\n"
                "| `book` | `1` | `Book` | Book. |",
            )
        )
        self.assertNotIn(10, vs.free_numbers(schema, {"kind": "domain"}))
        # The kind numbers are local to the tree, so 10 is free there.
        self.assertIn(10, vs.free_numbers(schema, {"kind": "source_kind"}))


class InsertRowTests(unittest.TestCase):
    def test_a_new_domain_row_matches_the_note_it_joins(self):
        text, row = vs.insert_schema_row(
            SCHEMA,
            "Domains",
            {"Value": "nature", "Number": 11, "Label": "Nature", "Definition": "Field records."},
        )
        self.assertEqual(row, "| `nature` | `11` | `Nature` | Field records. |")
        self.assertEqual(added_lines(SCHEMA, text), [row])
        self.assertEqual(removed_lines(SCHEMA, text), [])
        self.assertEqual(vs.parse_schema_note(text)["domains"]["nature"]["number"], 11)

    def test_it_appends_below_the_last_row_of_the_addressed_subsection(self):
        text, row = vs.insert_schema_row(
            "".join(SCHEMA),
            "Subdomains/wiki",
            {"Value": "animals", "Number": 2, "Label": "Animals", "Definition": "Animal cards."},
        )
        schema = vs.parse_schema_note(text)
        self.assertEqual(sorted(schema["subdomains"]["wiki"]), ["animals", "concepts"])
        # The craft subsection above it is untouched.
        self.assertEqual(sorted(schema["subdomains"]["craft"]), ["cooking", "gardening"])
        self.assertIn(row, text.split("### wiki", 1)[1])

    def test_an_unbackticked_column_stays_unbackticked(self):
        text, row = vs.insert_schema_row(
            SCHEMA,
            "Approved properties",
            {"Property": "cover", "Required": "no", "Shape": "scalar, human-owned", "Definition": "An image."},
        )
        self.assertEqual(row, "| `cover` | no | scalar, human-owned | An image. |")
        parsed = vs.parse_schema_note(text)["properties"]["cover"]
        self.assertEqual(parsed["required"], "no")
        self.assertTrue(parsed["human_owned"])

    def test_a_value_already_registered_is_refused(self):
        with self.assertRaises(vs.UserError) as caught:
            vs.insert_schema_row(
                SCHEMA, "Domains", {"Value": "craft", "Number": 12, "Label": "Craft", "Definition": "Again."}
            )
        self.assertIn("already registered", str(caught.exception))

    def test_a_registered_project_is_refused_by_its_wikilink(self):
        with self.assertRaises(vs.UserError):
            vs.insert_schema_row(
                SCHEMA,
                "Project registry",
                {
                    "Approved value": '"[[Greenhouse]]"',
                    "Domain": "craft",
                    "Subdomain": "cooking",
                    "Number": 2,
                    "Definition": "Duplicate.",
                },
            )

    def test_a_missing_or_unknown_column_is_refused(self):
        with self.assertRaises(vs.UserError) as caught:
            vs.insert_schema_row(SCHEMA, "Domains", {"Value": "nature", "Number": 11})
        self.assertIn("missing Label, Definition", str(caught.exception))
        with self.assertRaises(vs.UserError) as caught:
            vs.insert_schema_row(
                SCHEMA,
                "Domains",
                {"Value": "n", "Number": 11, "Label": "N", "Definition": "d", "Colour": "blue"},
            )
        self.assertIn("no Colour column", str(caught.exception))

    def test_crlf_and_a_missing_final_newline_both_survive(self):
        crlf = SCHEMA.replace("\n", "\r\n")
        text, _ = vs.insert_schema_row(
            crlf, "Domains", {"Value": "nature", "Number": 11, "Label": "Nature", "Definition": "Field."}
        )
        self.assertNotIn("\n\n", text.replace("\r\n", "\n").replace("\n\n", ""))
        self.assertEqual(text.count("\r\n"), crlf.count("\r\n") + 1)
        self.assertEqual(vs.parse_schema_note(text)["domains"]["nature"]["label"], "Nature")

        # A note whose Domains table is the very last line has no ending to copy.
        truncated = SCHEMA[: SCHEMA.index("\n\n## Subdomains")]
        text, row = vs.insert_schema_row(
            truncated, "Domains", {"Value": "nature", "Number": 11, "Label": "Nature", "Definition": "Field."}
        )
        self.assertTrue(text.endswith(row))
        self.assertIn("| `meta` | `99` | `Meta` | System notes. |\n|", text)


class InsertBulletTests(unittest.TestCase):
    def test_a_note_type_bullet_copies_marker_and_separator(self):
        text, bullet = vs.insert_registry_bullet(
            SCHEMA, "Note types", "organism", "A species: an animal, plant, or fungus."
        )
        self.assertEqual(bullet, "- `organism` — A species: an animal, plant, or fungus.")
        self.assertEqual(added_lines(SCHEMA, text), [bullet])
        self.assertIn("organism", vs.parse_schema_note(text)["types"])

    def test_the_style_comes_from_the_last_bullet_not_the_first(self):
        # Marker, separator, and backticks are copied from the bullet the new
        # one lands under, so a registry written in a different hand stays in it.
        source = SCHEMA.replace(
            "- `note` — General note.\n- `index` — A hub.",
            "- `note` — General note.\n* `index` - A hub.",
        )
        _, bullet = vs.insert_registry_bullet(source, "Note types", "organism", "A species.")
        self.assertEqual(bullet, "* `organism` - A species.")

    def test_a_bullet_with_no_definition_carries_no_separator(self):
        _, bullet = vs.insert_registry_bullet(SCHEMA, "Status values", "someday")
        self.assertEqual(bullet, "- `someday`")

    def test_a_value_already_registered_is_refused(self):
        with self.assertRaises(vs.UserError) as caught:
            vs.insert_registry_bullet(SCHEMA, "Note types", "note", "Again.")
        self.assertIn("already registered", str(caught.exception))


class SubdomainSectionTests(unittest.TestCase):
    def test_a_new_domain_gets_a_subsection_carrying_its_first_row(self):
        text, _ = vs.insert_schema_row(
            SCHEMA,
            "Domains",
            {"Value": "nature", "Number": 11, "Label": "Nature", "Definition": "Field records."},
        )
        text, heading = vs.insert_subdomain_section(
            text,
            "nature",
            {"Value": "observations", "Number": 1, "Label": "Observations", "Definition": "One sighting."},
        )
        self.assertEqual(heading, "### nature")
        schema = vs.parse_schema_note(text)
        self.assertEqual(list(schema["subdomains"]["nature"]), ["observations"])
        self.assertEqual(
            str(vs.compile_destination(schema, {"domain": "nature", "subdomain": "observations"})),
            "11 Nature/11.01 Observations",
        )

    def test_an_existing_subsection_is_refused(self):
        with self.assertRaises(vs.UserError) as caught:
            vs.insert_subdomain_section(
                SCHEMA, "wiki", {"Value": "x", "Number": 2, "Label": "X", "Definition": "d"}
            )
        self.assertIn("already has a subsection", str(caught.exception))

    def test_a_first_row_missing_a_column_is_refused_before_anything_is_written(self):
        text, _ = vs.insert_schema_row(
            SCHEMA, "Domains", {"Value": "nature", "Number": 11, "Label": "Nature", "Definition": "F."}
        )
        with self.assertRaises(vs.UserError):
            vs.insert_subdomain_section(text, "nature", {"Value": "observations", "Number": 1})


class CandidateSchemaTests(unittest.TestCase):
    def test_the_whole_nature_migration_applies_as_one_batch(self):
        insertions = [
            {"kind": "bullet", "heading": "Note types", "value": "organism", "definition": "A species."},
            {"kind": "bullet", "heading": "Note types", "value": "observation", "definition": "A sighting."},
            {
                "kind": "row",
                "table": "Domains",
                "cells": {"Value": "nature", "Number": 11, "Label": "Nature", "Definition": "Field records."},
            },
            {
                "kind": "subsection",
                "domain": "nature",
                "first_row": {
                    "Value": "observations",
                    "Number": 1,
                    "Label": "Observations",
                    "Definition": "One sighting each.",
                },
            },
            {
                "kind": "row",
                "table": "Subdomains/nature",
                "cells": {"Value": "field-notes", "Number": 2, "Label": "Field Notes", "Definition": "An outing."},
            },
            {
                "kind": "row",
                "table": "Subdomains/wiki",
                "cells": {"Value": "animals", "Number": 2, "Label": "Animals", "Definition": "Animal cards."},
            },
        ]
        text, rendered = vs.candidate_schema_text(SCHEMA, insertions)
        self.assertEqual(len(rendered), len(insertions))
        self.assertEqual(removed_lines(SCHEMA, text), [])

        schema = vs.parse_schema_note(text)
        vs.validate_derived_paths(schema)
        self.assertEqual(sorted(schema["subdomains"]["nature"]), ["field-notes", "observations"])
        self.assertEqual(sorted(schema["subdomains"]["wiki"]), ["animals", "concepts"])
        self.assertIn("organism", schema["types"])
        self.assertEqual(
            str(vs.compile_destination(schema, {"domain": "wiki", "subdomain": "animals"})),
            "09 Wiki/9.02 Animals",
        )

    def test_a_subsection_without_its_domain_row_fails_the_reparse(self):
        # The insertion primitives copy style; they are not the validator. A
        # subsection naming an unregistered domain applies cleanly to the text
        # and is caught when the candidate is parsed — which is why nothing is
        # ever written without that reparse.
        text, _ = vs.candidate_schema_text(
            SCHEMA,
            [
                {
                    "kind": "subsection",
                    "domain": "nature",
                    "first_row": {
                        "Value": "observations",
                        "Number": 1,
                        "Label": "Observations",
                        "Definition": "One sighting.",
                    },
                }
            ],
        )
        with self.assertRaises(vs.UserError) as caught:
            vs.parse_schema_note(text)
        self.assertIn("unknown domain nature", str(caught.exception))

    def test_a_colliding_number_survives_insertion_and_is_caught_by_the_compiler(self):
        # `insert_schema_row` is deliberately not a validator: it copies style
        # and refuses duplicate *values*. A duplicate number is the compiler's
        # to catch, which is why the candidate is always reparsed.
        text, _ = vs.insert_schema_row(
            SCHEMA, "Domains", {"Value": "nature", "Number": 9, "Label": "Nature", "Definition": "Field."}
        )
        with self.assertRaises(vs.UserError) as caught:
            vs.parse_schema_note(text)
        self.assertIn("9", str(caught.exception))

    def test_an_unknown_insertion_kind_is_refused(self):
        with self.assertRaises(vs.UserError):
            vs.candidate_schema_text(SCHEMA, [{"kind": "property", "value": "phenology"}])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Tests for detecting disagreement between compiled routes and folders on disk.

Every folder path is compiled from the schema's `Number` and `Label` cells;
nothing reads folder names off disk. Filing a note creates its destination on
demand, so a route naming a folder that does not exist quietly grows a second
folder beside the one the notes are actually in.

The hard part is not finding differences — it is refusing to report the ones
that do not matter. A real vault has ninety-odd routes and twenty raw
differences, of which three are real. A checker that lists all twenty gets
ignored by the third run, and then the dangerous two hide inside it.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vault_schema as vs

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
| `directory` | `8` | `Directory` | Entity notes for people and organizations. |
| `meta` | `99` | `Meta` | System notes. |

## Subdomains

### craft

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `gardening` | `1` | `Gardening` | Plants. |
| `cooking` | `2` | `Cooking` | Food. |

### directory

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `organizations` | `2` | `Organizations` | Institutions, companies, labs, and agencies. |

### meta

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `schemas` | `2` | `Schemas` | Controlled vocabularies. |
| `attachments` | `5` | `Attachments` | Binary assets. |

## Project registry

| Approved value | Domain | Subdomain | Number | Definition |
| --- | --- | --- | --- | --- |
| `"[[Greenhouse]]"` | `craft` | `gardening` | `1` | Building the greenhouse. |

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

# Every folder a vault in agreement with SCHEMA has.
CLEAN = [
    "00 Inbox",
    "02 Craft",
    "02 Craft/2.01 Gardening",
    "02 Craft/2.01 Gardening/2.01.01 Greenhouse",
    "02 Craft/2.02 Cooking",
    "08 Directory",
    "08 Directory/8.02 Organizations",
    "99 Meta",
    "99 Meta/99.02 Schemas",
    "99 Meta/99.05 Attachments",
]


class SchemaDriftTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.vault = Path(self.temporary.name).resolve()
        (self.vault / ".obsidian").mkdir()
        self.write(vs.DEFAULT_SCHEMA, SCHEMA)
        for folder in CLEAN:
            (self.vault / folder).mkdir(parents=True, exist_ok=True)
        self.schema = vs.parse_schema_note(SCHEMA)

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, relative, text="# Note\n"):
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def notes(self, folder, count):
        (self.vault / folder).mkdir(parents=True, exist_ok=True)
        for index in range(count):
            self.write(f"{folder}/Note {index}.md")

    def findings(self):
        return vs.check_schema_drift(self.vault, self.schema)

    def only(self):
        found = self.findings()
        self.assertEqual(len(found), 1, [entry["kind"] + " " + entry["path"] for entry in found])
        return found[0]

    def move_organizations(self, to_folder, notes):
        (self.vault / "08 Directory" / "8.02 Organizations").rmdir()
        self.notes(f"08 Directory/{to_folder}", notes)

    # --- the baseline -----------------------------------------------------

    def test_a_vault_that_matches_its_schema_has_no_findings(self):
        self.notes("02 Craft/2.01 Gardening", 2)
        self.assertEqual(self.findings(), [])

    def test_compiled_routes_cover_every_registry_and_the_inbox(self):
        routes = vs.compiled_routes(self.schema)
        self.assertIn(vs.INBOX_DIR, routes)
        self.assertIn("08 Directory/8.02 Organizations", routes)
        self.assertIn("02 Craft/2.01 Gardening/2.01.01 Greenhouse", routes)

    # --- a label that moved -----------------------------------------------

    def test_a_label_at_the_wrong_number_is_one_high_finding(self):
        self.move_organizations("8.03 Organizations", 3)
        finding = self.only()
        self.assertEqual(finding["severity"], "high")
        self.assertEqual(finding["kind"], "label_moved")
        self.assertEqual(finding["path"], "08 Directory/8.03 Organizations")
        self.assertEqual(finding["route"], "08 Directory/8.02 Organizations")
        self.assertEqual(finding["note_count"], 3)

    def test_the_fix_edits_the_schema_when_the_folder_holds_more_notes(self):
        # Editing one cell moves nothing; renaming the folder moves three notes.
        self.move_organizations("8.03 Organizations", 3)
        finding = self.only()
        self.assertEqual(finding["fix_side"], "schema")
        self.assertIn("Change `organizations`", finding["suggestion"])
        self.assertIn("from Number `2` to `3`", finding["suggestion"])
        self.assertEqual(finding["schema_row"]["to"], 3)

    def test_the_fix_renames_the_folder_when_the_folder_holds_fewer_notes(self):
        # Both exist: the split already happened, and the smaller side moves.
        self.notes("08 Directory/8.02 Organizations", 4)
        self.notes("08 Directory/8.03 Organizations", 1)
        finding = self.only()
        self.assertEqual(finding["kind"], "label_moved")
        self.assertEqual(finding["fix_side"], "folder")
        self.assertIn("Rename `08 Directory/8.03 Organizations`", finding["suggestion"])
        self.assertIn("`08 Directory/8.02 Organizations`", finding["suggestion"])
        self.assertIsNone(finding["schema_row"])

    def test_two_empty_sides_pick_the_schema_and_say_either_is_fine(self):
        self.move_organizations("8.03 Organizations", 0)
        finding = self.only()
        self.assertEqual(finding["fix_side"], "schema")
        self.assertIn("equally cheap", finding["suggestion"])

    def test_a_split_that_already_happened_says_so(self):
        self.notes("08 Directory/8.02 Organizations", 4)
        self.notes("08 Directory/8.03 Organizations", 1)
        self.assertIn("the split has happened", self.only()["detail"])

    # --- a number claimed twice -------------------------------------------

    def test_a_route_whose_number_is_taken_is_a_collision(self):
        self.move_organizations("8.02 Vendors", 2)
        finding = self.only()
        self.assertEqual(finding["severity"], "high")
        self.assertEqual(finding["kind"], "number_collision")
        self.assertEqual(finding["path"], "08 Directory/8.02 Vendors")

    def test_an_undeclared_folder_sharing_a_declared_number_is_a_collision(self):
        self.notes("08 Directory/8.02 Vendors", 2)
        finding = self.only()
        self.assertEqual(finding["severity"], "high")
        self.assertEqual(finding["kind"], "number_collision")
        self.assertEqual(finding["fix_side"], "manual")

    def test_a_swapped_pair_cannot_be_fixed_from_the_schema_side(self):
        # `gardening` is 1 and `cooking` is 2; on disk they are the other way
        # round. Neither row can take the other's number without colliding, so
        # the folders are the only side that can move.
        (self.vault / "02 Craft" / "2.01 Gardening" / "2.01.01 Greenhouse").rmdir()
        (self.vault / "02 Craft" / "2.01 Gardening").rmdir()
        (self.vault / "02 Craft" / "2.02 Cooking").rmdir()
        (self.vault / "02 Craft" / "2.01 Cooking").mkdir()
        (self.vault / "02 Craft" / "2.02 Gardening").mkdir()
        found = self.findings()
        high = [entry for entry in found if entry["severity"] == "high"]
        self.assertEqual(len(high), 2, [entry["path"] for entry in found])
        for finding in high:
            self.assertEqual(finding["fix_side"], "folder")
            self.assertIsNone(finding["schema_row"])

    # --- folders the schema never named ------------------------------------

    def test_an_undeclared_folder_holding_notes_is_medium(self):
        self.notes("98 Archive", 5)
        finding = self.only()
        self.assertEqual(finding["severity"], "medium")
        self.assertEqual(finding["kind"], "undeclared_with_notes")
        self.assertEqual(finding["note_count"], 5)

    def test_an_undeclared_folder_holding_nothing_is_low(self):
        (self.vault / "98 Archive").mkdir()
        finding = self.only()
        self.assertEqual(finding["severity"], "low")
        self.assertEqual(finding["kind"], "undeclared_empty")

    def test_notes_nested_inside_an_undeclared_folder_still_count(self):
        self.notes("98 Archive/QE/Cultural Sociology", 4)
        finding = self.only()
        self.assertEqual(finding["path"], "98 Archive")
        self.assertEqual(finding["note_count"], 4)

    # --- what must never be reported ---------------------------------------

    def test_structure_below_a_declared_route_is_never_a_finding(self):
        # The real case: `99.05 Attachments/Images` and `/PDFs` are legitimate
        # detail below a declared route, and reporting them is what makes a
        # drift checker get ignored.
        (self.vault / "99 Meta" / "99.05 Attachments" / "Images").mkdir()
        (self.vault / "99 Meta" / "99.05 Attachments" / "PDFs").mkdir()
        self.write("99 Meta/99.05 Attachments/Images/caption.md")
        self.assertEqual(self.findings(), [])

    def test_a_numbered_folder_below_a_route_is_not_treated_as_substructure(self):
        # Unnumbered detail is fine; a Johnny Decimal number is a slot claim.
        self.notes("99 Meta/99.09 Rogue", 1)
        self.assertEqual(self.only()["path"], "99 Meta/99.09 Rogue")

    def test_an_unpadded_number_is_a_slot_claim_not_detail(self):
        # `8.2 Organizations` is the same slot spelled wrong. Treating it as
        # unnumbered detail would hide it below the `08 Directory` route.
        (self.vault / "08 Directory" / "8.02 Organizations").rmdir()
        self.notes("08 Directory/8.2 Organizations", 2)
        finding = self.only()
        self.assertEqual(finding["kind"], "label_moved")
        self.assertEqual(finding["fix_side"], "folder")
        self.assertIn("Rename `08 Directory/8.2 Organizations`", finding["suggestion"])

    def test_a_declared_route_with_no_folder_is_only_informational(self):
        (self.vault / "02 Craft" / "2.01 Gardening" / "2.01.01 Greenhouse").rmdir()
        finding = self.only()
        self.assertEqual(finding["severity"], "info")
        self.assertEqual(finding["kind"], "declared_absent")
        self.assertEqual(finding["path"], "02 Craft/2.01 Gardening/2.01.01 Greenhouse")
        self.assertIsNone(finding["suggestion"])

    def test_a_renamed_inbox_reports_without_crashing(self):
        # The inbox is a constant rather than a registry row, so it has no
        # Number cell to propose editing — but it is still a route.
        (self.vault / vs.INBOX_DIR).rmdir()
        self.notes("01 Inbox", 2)
        finding = self.only()
        self.assertEqual(finding["severity"], "high")
        self.assertEqual(finding["fix_side"], "folder")
        self.assertIsNone(finding["schema_row"])

    def test_a_workspace_marked_directory_is_invisible(self):
        workspace = self.vault / "99 Meta" / "99.06 Workflows"
        self.write(f"99 Meta/99.06 Workflows/{vs.WORKSPACE_MARKER}", "")
        (workspace / "run-a" / "archive").mkdir(parents=True)
        self.write("99 Meta/99.06 Workflows/run-a/research_report.md")
        self.assertEqual(self.findings(), [])

    def test_protected_and_dot_directories_are_invisible(self):
        (self.vault / ".vault-organizer" / "runs" / "2026").mkdir(parents=True)
        (self.vault / "node_modules" / "left-pad").mkdir(parents=True)
        self.assertEqual(self.findings(), [])

    # --- ordering and identity ---------------------------------------------

    def test_findings_are_ordered_by_severity(self):
        self.move_organizations("8.03 Organizations", 3)
        self.notes("98 Archive", 2)
        (self.vault / "97 Scratch").mkdir()
        (self.vault / "02 Craft" / "2.01 Gardening" / "2.01.01 Greenhouse").rmdir()
        severities = [finding["severity"] for finding in self.findings()]
        self.assertEqual(severities, ["high", "medium", "low", "info"])

    def test_ids_are_derived_from_content_not_position(self):
        self.move_organizations("8.03 Organizations", 3)
        first = {finding["id"]: finding["path"] for finding in self.findings()}
        self.notes("98 Archive", 2)
        second = {finding["id"]: finding["path"] for finding in self.findings()}
        # Adding an unrelated finding must not renumber an existing one, or an
        # id copied from a report would address a different thing at fix time.
        for identifier, path in first.items():
            self.assertEqual(second.get(identifier), path)


class RowRewriteTests(unittest.TestCase):
    def row(self, **overrides):
        row = {
            "table": "Subdomains/directory",
            "match_column": "Value",
            "value": "organizations",
            "field": "Number",
            "from": 2,
            "to": 3,
        }
        row.update(overrides)
        return row

    def test_only_the_target_number_cell_changes(self):
        revised, before, after = vs.replace_schema_row_number(SCHEMA, self.row())
        self.assertEqual(before.replace("`2`", "`3`"), after)
        self.assertEqual(len(revised.splitlines()), len(SCHEMA.splitlines()))
        self.assertEqual(revised.replace("| `organizations` | `3` |", "| `organizations` | `2` |"), SCHEMA)

    def test_the_definition_survives_verbatim(self):
        revised, _, _ = vs.replace_schema_row_number(SCHEMA, self.row())
        self.assertIn("Institutions, companies, labs, and agencies.", revised)

    def test_a_row_in_another_subsection_with_the_same_number_is_untouched(self):
        revised, _, _ = vs.replace_schema_row_number(SCHEMA, self.row())
        self.assertIn("| `cooking` | `2` | `Cooking` | Food. |", revised)
        self.assertIn("| `schemas` | `2` | `Schemas` | Controlled vocabularies. |", revised)

    def test_a_stale_expected_number_is_refused(self):
        with self.assertRaises(vs.UserError):
            vs.replace_schema_row_number(SCHEMA, self.row(**{"from": 7}))

    def test_an_unknown_value_is_refused(self):
        with self.assertRaises(vs.UserError):
            vs.replace_schema_row_number(SCHEMA, self.row(value="nonesuch"))

    def test_the_result_still_parses(self):
        revised, _, _ = vs.replace_schema_row_number(SCHEMA, self.row())
        schema = vs.parse_schema_note(revised)
        self.assertEqual(schema["subdomains"]["directory"]["organizations"]["number"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)

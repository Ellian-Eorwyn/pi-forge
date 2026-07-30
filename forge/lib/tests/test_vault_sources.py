#!/usr/bin/env python3
"""Tests for filing ``type: source`` notes by source kind.

A vault that declares a **Sources root** stops filing sources by domain and
files them under one numbered tree instead: ``10 Sources/10.01 Book/…``. The
switch is the schema section itself, so a vault without one keeps the old
behavior byte for byte — that backward compatibility is what most of the parse
tests here are guarding.

Below each numbered kind folder the note's domain and subdomain follow as plain
unnumbered labels. That is not cosmetic: drift checking treats anything
unnumbered below a declared route as legitimate detail, so the tree grows a
folder per domain without needing ninety more registry rows, while a numbered
folder nobody declared is still caught as a slot claim.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vault_schema as vs

HEAD = """# Vault Schema

## Approved properties

| Property | Required | Shape | Definition |
| --- | --- | --- | --- |
| `type` | yes | controlled scalar | Kind. |
| `status` | yes | controlled scalar | Lifecycle. |
| `domain` | yes | controlled scalar | Broad area. |
| `subdomain` | no | controlled scalar | Nested area. |
| `project` | no | registered quoted wikilink | Project. |
| `source_kind` | conditional | controlled scalar | Kind of external source. |

## Note types

- `note` — General note.
- `source` — External source.

## Status values

- `active` — Active.
- `complete` — Finished.

## Domains

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `craft` | `2` | `Craft` | Making things. |
| `academic` | `5` | `Academic` | Scholarship. |
| `meta` | `99` | `Meta` | System notes. |

## Subdomains

### craft

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `games` | `5` | `Games` | Play. |

### academic

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `dissertation` | `1` | `Dissertation` | The dissertation. |

### meta

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `schemas` | `2` | `Schemas` | Controlled vocabularies. |

## Project registry

| Approved value | Domain | Subdomain | Number | Definition |
| --- | --- | --- | --- | --- |
| `"[[Greenhouse]]"` | `craft` | `games` | `1` | Building the greenhouse. |
"""

TAIL = """
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

SOURCES_ROOT = """
## Sources root

| Number | Label | Definition |
| --- | --- | --- |
| `10` | `Sources` | External source notes, filed by source kind. |
"""

KIND_TABLE = """
## Source kinds

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `book` | `1` | `Book` | A book or monograph. |
| `article` | `2` | `Article` | An article-format source. |
| `transcript` | `3` | `Transcript` | A verbatim record of speech. |
"""

KIND_BULLETS = """
## Source kinds

- `book` — A book or monograph.
- `article` — An article-format source.
- `transcript` — A verbatim record of speech.
"""

SOURCES_SCHEMA = HEAD + SOURCES_ROOT + KIND_TABLE + TAIL
DOMAIN_SCHEMA = HEAD + KIND_BULLETS + TAIL

# Every folder a vault in agreement with SOURCES_SCHEMA has.
CLEAN = [
    "00 Inbox",
    "02 Craft",
    "02 Craft/2.05 Games",
    "02 Craft/2.05 Games/2.05.01 Greenhouse",
    "05 Academic",
    "05 Academic/5.01 Dissertation",
    "10 Sources",
    "10 Sources/10.01 Book",
    "10 Sources/10.02 Article",
    "10 Sources/10.03 Transcript",
    "99 Meta",
    "99 Meta/99.02 Schemas",
]


class SourceParsingTests(unittest.TestCase):
    def test_bullet_kinds_keep_working_and_leave_routing_off(self):
        schema = vs.parse_schema_note(DOMAIN_SCHEMA)
        self.assertFalse(vs.sources_routing_enabled(schema))
        self.assertIsNone(schema["sources_root"])
        self.assertIn("book", schema["source_kinds"])
        self.assertIsNone(schema["source_kinds"]["book"]["number"])
        self.assertEqual(schema["source_kinds"]["book"]["definition"], "A book or monograph.")
        self.assertEqual(vs.source_kind_routes(schema), {})

    def test_a_source_files_by_domain_when_no_root_is_declared(self):
        schema = vs.parse_schema_note(DOMAIN_SCHEMA)
        destination = vs.compile_destination(
            schema, {"type": "source", "source_kind": "book", "domain": "academic", "subdomain": "dissertation"}
        )
        self.assertEqual(destination.as_posix(), "05 Academic/5.01 Dissertation")

    def test_a_declared_root_requires_a_kind_table(self):
        # Half the kinds routed and half unroutable is not a state to run in.
        with self.assertRaises(vs.UserError) as caught:
            vs.parse_schema_note(HEAD + SOURCES_ROOT + KIND_BULLETS + TAIL)
        self.assertIn("Sources root", str(caught.exception))

    def test_a_kind_table_without_a_root_still_parses(self):
        # Staging the table before flipping the switch has to be possible.
        schema = vs.parse_schema_note(HEAD + KIND_TABLE + TAIL)
        self.assertFalse(vs.sources_routing_enabled(schema))
        self.assertIn("book", schema["source_kinds"])

    def test_the_root_number_is_reserved_against_domains(self):
        clashing = SOURCES_SCHEMA.replace("| `meta` | `99` |", "| `meta` | `10` |")
        with self.assertRaises(vs.UserError) as caught:
            vs.parse_schema_note(clashing)
        self.assertIn("reserved", str(caught.exception))

    def test_duplicate_kind_numbers_fail_closed(self):
        clashing = SOURCES_SCHEMA.replace("| `article` | `2` |", "| `article` | `1` |")
        with self.assertRaises(vs.UserError) as caught:
            vs.parse_schema_note(clashing)
        self.assertIn("duplicate number", str(caught.exception))

    def test_more_than_one_root_row_fails_closed(self):
        doubled = SOURCES_SCHEMA.replace(
            "| `10` | `Sources` | External source notes, filed by source kind. |",
            "| `10` | `Sources` | External source notes, filed by source kind. |\n| `11` | `Other` | Second. |",
        )
        with self.assertRaises(vs.UserError) as caught:
            vs.parse_schema_note(doubled)
        self.assertIn("one row", str(caught.exception))


class SourceRoutingTests(unittest.TestCase):
    def setUp(self):
        self.schema = vs.parse_schema_note(SOURCES_SCHEMA)

    def destination(self, **metadata):
        return vs.compile_destination(self.schema, metadata).as_posix()

    def test_a_source_routes_by_kind_then_domain_then_subdomain(self):
        self.assertEqual(
            self.destination(type="source", source_kind="book", domain="academic", subdomain="dissertation"),
            "10 Sources/10.01 Book/Academic/Dissertation",
        )
        self.assertEqual(
            self.destination(type="source", source_kind="transcript", domain="craft", subdomain="games"),
            "10 Sources/10.03 Transcript/Craft/Games",
        )

    def test_a_source_without_a_subdomain_stops_at_the_domain(self):
        self.assertEqual(
            self.destination(type="source", source_kind="article", domain="craft"),
            "10 Sources/10.02 Article/Craft",
        )

    def test_a_project_never_moves_a_source(self):
        # A source belongs to its kind whichever projects happen to cite it.
        self.assertEqual(
            self.destination(
                type="source", source_kind="book", domain="craft", subdomain="games", project="[[Greenhouse]]"
            ),
            "10 Sources/10.01 Book/Craft/Games",
        )

    def test_a_note_that_is_not_a_source_still_routes_by_domain(self):
        self.assertEqual(
            self.destination(type="note", domain="craft", subdomain="games"),
            "02 Craft/2.05 Games",
        )
        self.assertEqual(
            self.destination(type="note", domain="craft", subdomain="games", project="[[Greenhouse]]"),
            "02 Craft/2.05 Games/2.05.01 Greenhouse",
        )

    def test_metadata_without_a_type_routes_by_domain(self):
        # vault-wiki and vault-lexicon compile destinations from a domain and
        # subdomain alone; a source branch keyed on `type` must not see them.
        self.assertEqual(self.destination(domain="meta", subdomain="schemas"), "99 Meta/99.02 Schemas")

    def test_a_source_missing_its_kind_falls_back_to_domain_filing(self):
        # Validation forbids this pairing, so reaching it means something else
        # already went wrong; filing it beside its topic beats failing the run.
        self.assertEqual(self.destination(type="source", domain="craft"), "02 Craft")
        self.assertEqual(self.destination(type="source", source_kind="poster", domain="craft"), "02 Craft")

    def test_kind_routes_are_reported_for_every_numbered_kind(self):
        self.assertEqual(
            vs.source_kind_routes(self.schema),
            {
                "book": "10 Sources/10.01 Book",
                "article": "10 Sources/10.02 Article",
                "transcript": "10 Sources/10.03 Transcript",
            },
        )


class SourceDriftTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.vault = Path(self.temporary.name).resolve()
        (self.vault / ".obsidian").mkdir()
        self.write(vs.DEFAULT_SCHEMA, SOURCES_SCHEMA)
        for folder in CLEAN:
            (self.vault / folder).mkdir(parents=True, exist_ok=True)
        self.schema = vs.parse_schema_note(SOURCES_SCHEMA)

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, relative, text="# Note\n"):
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def findings(self):
        return vs.check_schema_drift(self.vault, self.schema)

    def test_the_root_and_every_kind_are_compiled_routes(self):
        routes = vs.compiled_routes(self.schema)
        self.assertIn("10 Sources", routes)
        self.assertIn("10 Sources/10.01 Book", routes)
        self.assertIn("10 Sources/10.03 Transcript", routes)

    def test_a_vault_holding_the_whole_tree_reports_nothing(self):
        self.write("10 Sources/10.01 Book/Academic/Dissertation/Isbister - 2016.md")
        self.assertEqual(self.findings(), [])

    def test_domain_label_tails_are_never_findings(self):
        # Two unnumbered levels below a declared kind folder, holding notes.
        self.write("10 Sources/10.02 Article/Academic/Dissertation/Paper.md")
        self.write("10 Sources/10.02 Article/Craft/Games/Review.md")
        self.assertEqual(self.findings(), [])

    def test_a_missing_sources_tree_is_only_informational(self):
        for folder in ("10.01 Book", "10.02 Article", "10.03 Transcript"):
            (self.vault / "10 Sources" / folder).rmdir()
        (self.vault / "10 Sources").rmdir()
        found = self.findings()
        self.assertEqual({entry["severity"] for entry in found}, {"info"})
        self.assertEqual({entry["kind"] for entry in found}, {"declared_absent"})
        self.assertEqual(len(found), 4)

    def test_a_numbered_folder_nobody_declared_is_still_caught(self):
        # The point of numbering the kind folders: an undeclared slot beside
        # declared ones is exactly how a vault silently splits in two.
        self.write("10 Sources/10.09 Website/Note.md")
        found = self.findings()
        self.assertEqual(len(found), 1, [entry["kind"] + " " + entry["path"] for entry in found])
        self.assertEqual(found[0]["severity"], "medium")
        self.assertEqual(found[0]["path"], "10 Sources/10.09 Website")

    def test_a_kind_folder_at_the_wrong_number_is_high(self):
        (self.vault / "10 Sources" / "10.03 Transcript").rmdir()
        self.write("10 Sources/10.04 Transcript/Talk.md")
        found = self.findings()
        self.assertEqual(len(found), 1, [entry["kind"] + " " + entry["path"] for entry in found])
        self.assertEqual(found[0]["severity"], "high")
        self.assertEqual(found[0]["kind"], "label_moved")
        self.assertEqual(found[0]["route"], "10 Sources/10.03 Transcript")

    def test_a_kind_number_fix_edits_the_source_kinds_table(self):
        (self.vault / "10 Sources" / "10.03 Transcript").rmdir()
        self.write("10 Sources/10.04 Transcript/Talk.md")
        finding = self.findings()[0]
        self.assertEqual(finding["fix_side"], "schema")
        row = finding["schema_row"]
        self.assertEqual(row["table"], "Source kinds")
        self.assertEqual(row["value"], "transcript")
        self.assertEqual((row["from"], row["to"]), (3, 4))
        updated, _, _ = vs.replace_schema_row_number(SOURCES_SCHEMA, row)
        self.assertIn("| `transcript` | `4` | `Transcript` |", updated)
        # The rewrite is surgical: nothing else in the note moved.
        self.assertEqual(len(updated.splitlines()), len(SOURCES_SCHEMA.splitlines()))
        reparsed = vs.parse_schema_note(updated)
        self.assertEqual(reparsed["source_kinds"]["transcript"]["number"], 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)

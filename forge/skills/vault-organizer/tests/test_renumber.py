#!/usr/bin/env python3
"""Tests for the `renumber` mode: shifting domain numbers, and the folders under them.

Johnny Decimal numbers are positional, so inserting a domain in the middle means
moving every domain above it — a chain of swaps, which `--fix-schema` refuses by
design because no single row edit expresses one. This mode is the other side of
that refusal.

Almost everything here is about ordering. A cascade renames folders into slots
that are still occupied unless it goes top down, and a nested route's source path
stops existing the moment its parent moves. Both mistakes produce a half
renumbered vault that still parses, which is the failure worth spending tests on.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import vault_schema as vs  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "vault_organizer_renumber", Path(__file__).resolve().parents[1] / "scripts" / "vault-organizer.py"
)
vault_organizer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vault_organizer)

# A contiguous block of domains, then a deliberate gap, then two far-away ones.
# The gap is the point: a cascade started inside the block must stop at it, and
# `98 Archive` and `99 Meta` must never move however far the block is pushed.
SCHEMA = """# Vault Schema

## Approved properties

| Property | Required | Shape | Definition |
| --- | --- | --- | --- |
| `type` | yes | controlled scalar | Kind. |
| `status` | yes | controlled scalar | Lifecycle. |
| `domain` | yes | controlled scalar | Broad area. |
| `subdomain` | no | controlled scalar | Nested area. |
| `project` | no | registered quoted wikilink | Registered project. |

## Note types

- `note` — General note.

## Status values

- `active` — Active.

## Domains

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `personal` | `1` | `Personal` | Personal material. |
| `craft` | `2` | `Craft` | Making things. |
| `writing` | `3` | `Writing` | Writing. |
| `technology` | `4` | `Technology` | Technical work. |
| `work` | `6` | `Work` | Employment. |
| `archive` | `98` | `Archive` | Cold storage. |
| `meta` | `99` | `Meta` | System notes. |

## Subdomains

### technology

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `software-development` | `2` | `Software Development` | Code projects. |
| `obsidian` | `3` | `Obsidian` | Vault tooling. |

### meta

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `schemas` | `2` | `Schemas` | Schema notes. |

## Project registry

| Approved value | Domain | Subdomain | Number | Definition |
| --- | --- | --- | --- | --- |
| `"[[Pi Forge]]"` | `technology` | `software-development` | `1` | Local agent harness. |

## Source kinds

- `book` — Book.

## Capture types

- `manual` — Typed.

## Legacy normalization map

| Legacy input | Canonical output |
| --- | --- |
| `type: daily` | `type: note` |

## Folder routing

Derived by code.
"""

NOTE = "---\ntype: note\nstatus: active\ndomain: technology\nsubdomain: obsidian\n---\n\n# A note\n\nBody.\n"


def args(**overrides):
    from types import SimpleNamespace

    return SimpleNamespace(**{
        "vault": None, "schema": None, "apply": False, "insert": None, "set_numbers": None, **overrides
    })


class RenumberPlanTests(unittest.TestCase):
    """The pure planning half: no filesystem, no writes."""

    def setUp(self):
        self.schema = vs.parse_schema_note(SCHEMA)

    def test_the_cascade_stops_at_the_first_gap(self):
        """`5` is free, so inserting at 3 pushes 3 and 4 and stops. 6, 98 and 99
        are never touched — which is the whole reason no `--freeze` flag exists."""
        mapping = vs.renumber_mapping(self.schema, insert=3)
        self.assertEqual(mapping, {"writing": 4, "technology": 5})

    def test_inserting_at_a_free_number_moves_nothing(self):
        self.assertEqual(vs.renumber_mapping(self.schema, insert=5), {})
        self.assertEqual(vs.renumber_mapping(self.schema, insert=50), {})

    def test_a_whole_contiguous_block_shifts_together(self):
        mapping = vs.renumber_mapping(self.schema, insert=1)
        self.assertEqual(mapping, {"personal": 2, "craft": 3, "writing": 4, "technology": 5})

    def test_a_cascade_that_would_pass_99_is_refused(self):
        crowded = SCHEMA.replace(
            "| `work` | `6` | `Work` | Employment. |",
            "\n".join(f"| `d{number}` | `{number}` | `D{number}` | Filler. |" for number in range(6, 98)),
        )
        with self.assertRaises(vs.UserError) as caught:
            vs.renumber_mapping(vs.parse_schema_note(crowded), insert=6)
        self.assertIn("would pass 99", str(caught.exception))

    def test_an_explicit_move_that_collides_is_refused(self):
        with self.assertRaises(vs.UserError) as caught:
            vs.renumber_mapping(self.schema, moves={"craft": 4})
        self.assertIn("same number 4", str(caught.exception))

    def test_an_explicit_swap_is_allowed_because_the_result_is_consistent(self):
        """Transient collision is fine; the rename order is what prevents it on
        disk. Only a result with two domains on one number is refused."""
        mapping = vs.renumber_mapping(self.schema, moves={"craft": 3, "writing": 2})
        self.assertEqual(mapping, {"craft": 3, "writing": 2})

    def test_an_unknown_domain_is_refused_by_name(self):
        with self.assertRaises(vs.UserError) as caught:
            vs.renumber_mapping(self.schema, moves={"nature": 3})
        self.assertIn("unknown domain nature", str(caught.exception))

    def test_only_the_domains_table_changes(self):
        revised = vs.renumbered_schema(self.schema, vs.renumber_mapping(self.schema, insert=3))
        self.assertEqual(revised["domains"]["technology"]["number"], 5)
        self.assertEqual(revised["subdomains"]["technology"]["obsidian"]["number"], 3)
        self.assertEqual(revised["projects"]["[[Pi Forge]]"]["number"], 1)

    def test_domains_are_renamed_highest_first(self):
        moves = vs.renumber_folder_moves(self.schema, vs.renumber_mapping(self.schema, insert=3))
        domain_moves = [pair for pair in moves if "/" not in pair[0]]
        self.assertEqual(domain_moves, [("04 Technology", "05 Technology"), ("03 Writing", "04 Writing")])

    def test_a_nested_move_starts_from_where_its_parent_left_it(self):
        moves = dict(vs.renumber_folder_moves(self.schema, vs.renumber_mapping(self.schema, insert=3)))
        self.assertIn("05 Technology/4.03 Obsidian", moves)
        self.assertEqual(moves["05 Technology/4.03 Obsidian"], "05 Technology/5.03 Obsidian")

    def test_a_project_reflects_both_its_ancestors_moves(self):
        moves = dict(vs.renumber_folder_moves(self.schema, vs.renumber_mapping(self.schema, insert=3)))
        self.assertEqual(
            moves["05 Technology/5.02 Software Development/4.02.01 Pi Forge"],
            "05 Technology/5.02 Software Development/5.02.01 Pi Forge",
        )

    def test_the_plan_lands_exactly_on_the_new_routes(self):
        """The strongest statement available without a vault: replay every rename
        on a real tree and compare the result with what the edited schema compiles
        to."""
        mapping = vs.renumber_mapping(self.schema, insert=3)
        moves = vs.renumber_folder_moves(self.schema, mapping)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for route in vs.compiled_routes(self.schema):
                (root / route).mkdir(parents=True, exist_ok=True)
            for old, new in moves:
                self.assertTrue((root / old).is_dir(), f"source missing: {old}")
                self.assertFalse((root / new).exists(), f"target occupied: {new}")
                os.rename(root / old, root / new)
            after = sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_dir())
        self.assertEqual(after, sorted(vs.compiled_routes(vs.renumbered_schema(self.schema, mapping))))

    def test_a_route_with_no_folder_is_skipped_without_dropping_its_siblings(self):
        moves = vs.renumber_folder_moves(self.schema, vs.renumber_mapping(self.schema, insert=3))
        existing = [route for route in vs.compiled_routes(self.schema) if "Obsidian" not in route]
        kept, skipped = vs.prune_renumber_moves(moves, existing)
        self.assertEqual(skipped, ["05 Technology/4.03 Obsidian"])
        self.assertIn(
            ("05 Technology/5.02 Software Development/4.02.01 Pi Forge",
             "05 Technology/5.02 Software Development/5.02.01 Pi Forge"),
            kept,
        )


class PathReferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name).resolve()
        (self.vault / ".obsidian").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text):
        path = self.vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_an_explicit_markdown_path_is_found(self):
        self.write("Dashboard.md", "See [x](04 Technology/note.md)\n")
        self.assertEqual(vs.find_path_references(self.vault, ["04 Technology"]),
                         {"04 Technology": ["Dashboard.md"]})

    def test_a_percent_encoded_path_is_found(self):
        """Obsidian writes a link to a folder with a space in its name this way,
        so searching only the raw form would miss the commonest case."""
        self.write("Dashboard.md", "See [x](04%20Technology/note.md)\n")
        self.assertIn("Dashboard.md", vs.find_path_references(self.vault, ["04 Technology"])["04 Technology"])

    def test_a_base_file_filtering_on_a_path_is_found(self):
        self.write("Views.base", 'filters:\n  - file.path.startsWith("04 Technology")\n')
        self.assertEqual(vs.find_path_references(self.vault, ["04 Technology"]), {"04 Technology": ["Views.base"]})

    def test_an_obsidian_bookmark_is_found(self):
        """The likeliest thing to break and the quietest about it: `.obsidian` is
        excluded from every other walk in this codebase, so it has to be opted
        back in here."""
        self.write(".obsidian/bookmarks.json", '{"items":[{"type":"folder","path":"04 Technology"}]}')
        self.assertEqual(vs.find_path_references(self.vault, ["04 Technology"]),
                         {"04 Technology": [".obsidian/bookmarks.json"]})

    def test_a_wikilink_is_not_reported_because_it_survives_a_rename(self):
        self.write("Dashboard.md", "See [[A note]]\n")
        self.assertEqual(vs.find_path_references(self.vault, ["04 Technology"]), {})

    def test_a_path_nothing_mentions_is_absent_from_the_report(self):
        self.write("Dashboard.md", "Nothing relevant.\n")
        self.assertEqual(vs.find_path_references(self.vault, ["04 Technology", "03 Writing"]), {})


class RenumberApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name).resolve()
        (self.vault / ".obsidian").mkdir()
        self.schema_path = self.vault / vs.DEFAULT_SCHEMA
        self.schema_path.parent.mkdir(parents=True, exist_ok=True)
        self.schema_path.write_text(SCHEMA, encoding="utf-8")
        self.schema = vs.parse_schema_note(SCHEMA)
        for route in vs.compiled_routes(self.schema):
            (self.vault / route).mkdir(parents=True, exist_ok=True)
        self.note = self.vault / "04 Technology" / "4.03 Obsidian" / "A note.md"
        self.note.write_text(NOTE, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_renumber(self, **overrides):
        return vault_organizer.renumber(args(vault=str(self.vault), **overrides))

    def folders(self):
        return set(vs.existing_folders(self.vault))

    def test_a_dry_run_writes_nothing(self):
        before_folders = self.folders()
        before_schema = self.schema_path.read_text(encoding="utf-8")
        result = self.run_renumber(insert=3)
        self.assertTrue(result["data"]["dryRun"])
        self.assertEqual(self.folders(), before_folders)
        self.assertEqual(self.schema_path.read_text(encoding="utf-8"), before_schema)

    def test_apply_renames_the_folders_and_edits_the_schema(self):
        result = self.run_renumber(insert=3, apply=True)
        self.assertEqual(result["status"], "ok")
        folders = self.folders()
        self.assertIn("05 Technology/5.03 Obsidian", folders)
        self.assertIn("04 Writing", folders)
        self.assertNotIn("03 Writing", folders)
        self.assertNotIn("04 Technology", folders)
        revised = vs.parse_schema_note(self.schema_path.read_text(encoding="utf-8"))
        self.assertEqual(revised["domains"]["technology"]["number"], 5)
        self.assertEqual(revised["domains"]["writing"]["number"], 4)

    def test_the_far_away_domains_never_move(self):
        self.run_renumber(insert=3, apply=True)
        folders = self.folders()
        self.assertIn("98 Archive", folders)
        self.assertIn("99 Meta/99.02 Schemas", folders)

    def test_the_gap_is_free_afterwards(self):
        self.run_renumber(insert=3, apply=True)
        self.assertNotIn(3, vs.occupied_domain_numbers(
            vs.parse_schema_note(self.schema_path.read_text(encoding="utf-8"))
        ))

    def test_a_notes_bytes_are_untouched(self):
        """Frontmatter names a domain by value, never by number, so a renumbering
        refiles nothing. This is what makes it cheap."""
        self.run_renumber(insert=3, apply=True)
        moved = self.vault / "05 Technology" / "5.03 Obsidian" / "A note.md"
        self.assertTrue(moved.is_file())
        self.assertEqual(moved.read_text(encoding="utf-8"), NOTE)

    def test_the_vault_reports_no_drift_afterwards(self):
        self.run_renumber(insert=3, apply=True)
        revised = vs.parse_schema_note(self.schema_path.read_text(encoding="utf-8"))
        findings = vs.check_schema_drift(self.vault, revised)
        self.assertEqual([finding for finding in findings if finding["severity"] in {"high", "medium"}], [])

    def test_the_plan_is_written_before_anything_moves(self):
        result = self.run_renumber(insert=3, apply=True)
        plan = json.loads((Path(result["data"]["runDirectory"]) / "renumber_plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["mapping"], {"writing": 4, "technology": 5})

    def test_the_schema_note_is_backed_up(self):
        result = self.run_renumber(insert=3, apply=True)
        backup = Path(result["data"]["runDirectory"]) / "backup" / vs.DEFAULT_SCHEMA
        self.assertEqual(vs.parse_schema_note(backup.read_text(encoding="utf-8"))["domains"]["technology"]["number"], 4)

    def test_references_are_reported_but_never_rewritten(self):
        dashboard = self.vault / "01 Personal" / "Dashboard.md"
        dashboard.parent.mkdir(parents=True, exist_ok=True)
        dashboard.write_text("See [x](04%20Technology/note.md)\n", encoding="utf-8")
        result = self.run_renumber(insert=3, apply=True)
        self.assertIn("01 Personal/Dashboard.md", result["data"]["references"]["04 Technology"])
        self.assertEqual(dashboard.read_text(encoding="utf-8"), "See [x](04%20Technology/note.md)\n")

    def test_a_blocked_rename_rolls_every_earlier_one_back(self):
        """`04 Technology` cannot become `05 Technology` while something already
        sits there. Everything renamed before that point has to go back, and the
        schema note must not be written at all — a half-renumbered vault against
        an edited schema is the failure this ordering exists to prevent."""
        (self.vault / "05 Technology").mkdir()
        before_schema = self.schema_path.read_text(encoding="utf-8")
        with self.assertRaises(vs.UserError) as caught:
            self.run_renumber(insert=3, apply=True)
        self.assertIn("already exists", str(caught.exception))
        folders = self.folders()
        self.assertIn("04 Technology/4.03 Obsidian", folders)
        self.assertIn("03 Writing", folders)
        self.assertEqual(self.schema_path.read_text(encoding="utf-8"), before_schema)
        self.assertEqual(self.note.read_text(encoding="utf-8"), NOTE)

    def test_inserting_at_a_free_number_reports_that_nothing_moves(self):
        result = self.run_renumber(insert=5)
        self.assertEqual(result["data"]["moves"], [])
        self.assertTrue(any("already free" in warning for warning in result["warnings"]))

    def test_an_explicit_set_is_applied(self):
        result = self.run_renumber(set_numbers="craft=9", apply=True)
        self.assertEqual(result["data"]["mapping"], {"craft": 9})
        self.assertIn("09 Craft", self.folders())


class RenumberArgumentTests(unittest.TestCase):
    def test_set_is_parsed_into_a_mapping(self):
        self.assertEqual(vault_organizer.parse_renumber_moves("craft=4, writing=5"), {"craft": 4, "writing": 5})

    def test_a_malformed_pair_is_refused(self):
        for raw in ("craft", "craft=x", ""):
            with self.assertRaises(vs.UserError):
                vault_organizer.parse_renumber_moves(raw)

    def test_renumber_requires_exactly_one_of_insert_or_set(self):
        for argv in (
            ["renumber", "--vault", "."],
            ["renumber", "--vault", ".", "--insert", "3", "--set", "craft=4"],
        ):
            with self.assertRaises(vs.UserError) as caught:
                vault_organizer.parse_args(argv)
            self.assertIn("exactly one", str(caught.exception))

    def test_the_flags_belong_to_renumber_mode(self):
        with self.assertRaises(vs.UserError) as caught:
            vault_organizer.parse_args(["drift", "--vault", ".", "--insert", "3"])
        self.assertIn("renumber mode", str(caught.exception))

    def test_an_out_of_range_insert_is_refused(self):
        for number in ("0", "100"):
            with self.assertRaises(vs.UserError):
                vault_organizer.parse_args(["renumber", "--vault", ".", "--insert", number])


if __name__ == "__main__":
    unittest.main(verbosity=2)

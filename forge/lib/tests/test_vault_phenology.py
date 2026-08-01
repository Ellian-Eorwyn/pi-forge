#!/usr/bin/env python3
"""Tests for the phenology table compiler.

Most of these are about a single idea: a window without a region is not a fact
about anywhere. The compiler exists so that "what is due here in March" can be
answered from the vault, and every way that answer can go quietly wrong -- a
wrapped winter range sorted into a summer one, a species inheriting another
region's months, an event name the index cannot group -- is a test here.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib"))
import vault_phenology as vp  # noqa: E402
import vault_schema as vs  # noqa: E402
import vault_wiki as vw  # noqa: E402

SPECS = vw.load_kind_specs(ROOT / "skills" / "vault-wiki" / "references" / "wiki-kinds.json")
VOCABULARY = vp.load_event_vocabulary(ROOT / "skills" / "vault-wiki" / "references" / "phenology-events.json")
COLUMNS = vw.section_by_id(SPECS["animal"], "phenology")["columns"]

SCHEMA = """# Vault Schema

## Approved properties

| Property | Required | Shape | Definition |
| --- | --- | --- | --- |
| `type` | yes | controlled scalar | Kind. |
| `status` | yes | controlled scalar | Lifecycle. |
| `domain` | yes | controlled scalar | Broad area. |
| `subdomain` | no | controlled scalar | Nested area. |

## Note types

- `organism` — A living thing.
- `note` — General note.

## Status values

- `active` — Active.

## Domains

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `wiki` | `3` | `Wiki` | Reference cards. |
| `meta` | `99` | `Meta` | System notes. |

## Subdomains

### wiki

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `animals` | `8` | `Animals` | Animal cards. |
| `plants` | `9` | `Plants` | Plant cards. |
| `fungi` | `10` | `Fungi` | Fungus cards. |

### meta

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `schemas` | `2` | `Schemas` | Schema notes. |

## Project registry

| Approved value | Domain | Subdomain | Number | Definition |
| --- | --- | --- | --- | --- |
| `"[[Pi Forge]]"` | `meta` |  | `1` | Placeholder. |

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

RACCOON = """---
type: organism
status: active
domain: wiki
subdomain: animals
---

# Raccoon, Procyon lotor

> [!abstract]
> A mid-sized omnivore.

## Phenology

| Event | Window | Region | Basis |
| --- | --- | --- | --- |
| mating | Jan-Mar | [[Puget Lowland]] | sourced[^1] |
| birth | Apr-May | [[Puget Lowland]] | sourced[^1] |
| present | year-round | [[Puget Lowland]] | sourced |
| hibernation | Nov-Feb | [[Upper Midwest]] | sourced |

## Notes

Mine.
"""


class WindowTests(unittest.TestCase):
    def test_a_single_month_is_a_one_month_window(self):
        self.assertEqual(vp.parse_window("Feb"), (2, 2))
        self.assertEqual(vp.parse_window("february"), (2, 2))

    def test_a_range_keeps_its_direction_across_the_new_year(self):
        """Sorting the pair would turn a four-month winter into an eight-month
        spring-to-autumn, which is worse than having no window."""
        self.assertEqual(vp.parse_window("Nov-Feb"), (11, 2))
        self.assertEqual(vp.months_in((11, 2)), [11, 12, 1, 2])

    def test_every_dash_a_source_might_use_is_accepted(self):
        for dash in ("-", "–", "—"):
            self.assertEqual(vp.parse_window(f"Jan{dash}Mar"), (1, 3))

    def test_year_round_covers_every_month(self):
        self.assertEqual(vp.months_in(vp.parse_window("year-round")), list(range(1, 13)))

    def test_an_unreadable_window_is_none_rather_than_a_guess(self):
        for value in ("", "spring", "2026-03-01", "Jan-Feb-Mar", "Smarch"):
            self.assertIsNone(vp.parse_window(value), value)


class RegionTests(unittest.TestCase):
    def test_a_wikilink_names_a_region(self):
        self.assertEqual(vp.region_of("[[Puget Lowland]]"), ("Puget Lowland", None))
        self.assertEqual(vp.region_of("[[Puget Lowland|here]]"), ("Puget Lowland", None))

    def test_global_is_the_one_unlinked_value(self):
        self.assertEqual(vp.region_of("global"), ("global", None))

    def test_a_bare_name_is_refused(self):
        region, problem = vp.region_of("Seattle")
        self.assertIsNone(region)
        self.assertIn("must be a [[wikilink]]", problem)

    def test_a_backticked_link_is_refused_and_says_why(self):
        _region, problem = vp.region_of("`[[Puget Lowland]]`")
        self.assertIn("not a link", problem)


class TitleTests(unittest.TestCase):
    def test_the_scientific_name_comes_off_the_title(self):
        self.assertEqual(vp.note_names("Raccoon, Procyon lotor"), ("Raccoon", "Procyon lotor"))

    def test_a_title_without_a_comma_keeps_its_whole_text(self):
        self.assertEqual(vp.note_names("Raccoon"), ("Raccoon", ""))


class CompileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name).resolve()
        (self.vault / ".obsidian").mkdir()
        self.write(vs.DEFAULT_SCHEMA, SCHEMA)
        self.schema_path = vs.resolve_schema_path(self.vault, None)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text):
        path = self.vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def index(self):
        return vp.build_index(self.vault, self.schema_path, SPECS, VOCABULARY)

    def test_a_species_card_compiles_to_rows(self):
        self.write("03 Wiki/3.08 Animals/Raccoon, Procyon lotor.md", RACCOON)
        index = self.index()
        self.assertEqual(index["counts"]["species"], 1)
        record = index["species"][0]
        self.assertEqual(record["common"], "Raccoon")
        self.assertEqual(record["scientific"], "Procyon lotor")
        self.assertEqual(record["kind"], "animal")
        self.assertEqual([event["event"] for event in record["events"]],
                         ["mating", "birth", "present", "hibernation"])
        self.assertEqual(sorted(index["regions"]), ["Puget Lowland", "Upper Midwest"])

    def test_a_footnote_marker_in_the_basis_cell_does_not_break_it(self):
        self.write("03 Wiki/3.08 Animals/Raccoon, Procyon lotor.md", RACCOON)
        record = self.index()["species"][0]
        self.assertEqual({event["basis"] for event in record["events"]}, {"sourced"})

    def test_a_note_outside_the_species_subdomains_is_ignored(self):
        self.write("03 Wiki/3.08 Animals/Raccoon, Procyon lotor.md", RACCOON)
        self.write("03 Wiki/Other.md", RACCOON.replace("subdomain: animals", "subdomain: schemas"))
        self.assertEqual(self.index()["counts"]["species"], 1)

    def test_a_species_card_with_no_phenology_heading_is_absent(self):
        body = RACCOON.split("## Phenology")[0] + "## Notes\n\nMine.\n"
        self.write("03 Wiki/3.08 Animals/Skunk, Mephitis mephitis.md", body)
        self.assertEqual(self.index()["counts"]["species"], 0)

    def test_an_unknown_event_is_reported_and_the_rest_survive(self):
        self.write(
            "03 Wiki/3.08 Animals/Raccoon, Procyon lotor.md",
            RACCOON.replace("| mating | Jan-Mar |", "| whelping | Jan-Mar |"),
        )
        index = self.index()
        self.assertTrue(any("unknown animal event 'whelping'" in problem for problem in index["problems"]))
        self.assertEqual(len(index["species"][0]["events"]), 3)

    def test_an_event_valid_for_another_kind_is_still_refused(self):
        """``flowering`` is a real event, just not one an animal has."""
        self.write(
            "03 Wiki/3.08 Animals/Raccoon, Procyon lotor.md",
            RACCOON.replace("| mating | Jan-Mar |", "| flowering | Jan-Mar |"),
        )
        self.assertTrue(any("flowering" in problem for problem in self.index()["problems"]))

    def test_a_row_with_the_wrong_cell_count_is_reported_not_padded(self):
        self.write(
            "03 Wiki/3.08 Animals/Raccoon, Procyon lotor.md",
            RACCOON.replace("| mating | Jan-Mar | [[Puget Lowland]] | sourced[^1] |", "| mating | Jan-Mar |"),
        )
        index = self.index()
        self.assertTrue(any("expected 4" in problem for problem in index["problems"]))
        self.assertEqual(len(index["species"][0]["events"]), 3)

    def test_a_row_with_no_region_is_refused(self):
        self.write(
            "03 Wiki/3.08 Animals/Raccoon, Procyon lotor.md",
            RACCOON.replace("| mating | Jan-Mar | [[Puget Lowland]] | sourced[^1] |", "| mating | Jan-Mar |  | sourced |"),
        )
        self.assertTrue(any("region is empty" in problem for problem in self.index()["problems"]))

    def test_the_index_is_byte_stable_across_runs(self):
        self.write("03 Wiki/3.08 Animals/Raccoon, Procyon lotor.md", RACCOON)
        self.write("03 Wiki/3.09 Plants/Salmonberry, Rubus spectabilis.md", RACCOON.replace(
            "subdomain: animals", "subdomain: plants"
        ).replace("mating", "flowering").replace("birth", "fruiting").replace("hibernation", "dormancy"))
        self.assertEqual(vp.index_hash(self.index()), vp.index_hash(self.index()))

    def test_writing_and_reading_the_index_round_trips(self):
        self.write("03 Wiki/3.08 Animals/Raccoon, Procyon lotor.md", RACCOON)
        index = self.index()
        path, digest = vp.write_index(self.vault / ".vault-naturalist" / "cache", index)
        self.assertTrue(path.is_file())
        again = vp.read_index(self.vault / ".vault-naturalist" / "cache")
        self.assertEqual(again["hash"], digest)
        self.assertEqual(again["counts"], index["counts"])

    def test_a_cache_from_another_version_is_ignored(self):
        cache = self.vault / ".vault-naturalist" / "cache"
        cache.mkdir(parents=True)
        (cache / "phenology.json").write_text(json.dumps({"version": 0, "species": []}), encoding="utf-8")
        self.assertIsNone(vp.read_index(cache))


class QueryTests(unittest.TestCase):
    INDEX = {
        "species": [
            {
                "note": "a.md", "title": "Raccoon, Procyon lotor", "common": "Raccoon",
                "scientific": "Procyon lotor", "kind": "animal",
                "events": [
                    {"event": "mating", "start_month": 1, "end_month": 3, "months": [1, 2, 3],
                     "region": "Puget Lowland", "basis": "sourced"},
                    {"event": "hibernation", "start_month": 11, "end_month": 2, "months": [11, 12, 1, 2],
                     "region": "Upper Midwest", "basis": "sourced"},
                ],
            },
            {
                "note": "b.md", "title": "Barn Owl, Tyto alba", "common": "Barn Owl",
                "scientific": "Tyto alba", "kind": "animal",
                "events": [
                    {"event": "present", "start_month": 1, "end_month": 12, "months": list(range(1, 13)),
                     "region": "global", "basis": "sourced"},
                ],
            },
            {
                "note": "c.md", "title": "Vine Maple, Acer circinatum", "common": "Vine Maple",
                "scientific": "Acer circinatum", "kind": "plant",
                "events": [
                    {"event": "flowering", "start_month": 4, "end_month": 5, "months": [4, 5],
                     "region": "Upper Midwest", "basis": "sourced"},
                ],
            },
        ]
    }

    def test_a_month_query_finds_the_species_due_then(self):
        matches = vp.expected_in_month(self.INDEX, 2, region="Puget Lowland")
        self.assertEqual({record["common"] for record in matches}, {"Raccoon", "Barn Owl"})

    def test_another_regions_window_never_answers_for_this_one(self):
        """The Raccoon hibernates in the Upper Midwest in December and is not
        recorded as doing anything in Puget Sound then. It must not appear."""
        matches = vp.expected_in_month(self.INDEX, 12, region="Puget Lowland")
        self.assertEqual({record["common"] for record in matches}, {"Barn Owl"})

    def test_a_global_row_answers_for_every_region(self):
        matches = vp.expected_in_month(self.INDEX, 7, region="Somewhere Else")
        self.assertEqual({record["common"] for record in matches}, {"Barn Owl"})

    def test_no_region_filter_matches_everywhere(self):
        matches = vp.expected_in_month(self.INDEX, 12)
        self.assertEqual({record["common"] for record in matches}, {"Raccoon", "Barn Owl"})

    def test_a_month_outside_the_calendar_is_refused(self):
        with self.assertRaises(vs.UserError):
            vp.expected_in_month(self.INDEX, 13)

    def test_species_missing_a_region_are_the_work_still_to_do(self):
        self.assertEqual(vp.species_without_region(self.INDEX, "Puget Lowland"), ["c.md"])


class VocabularyTests(unittest.TestCase):
    def test_every_species_kind_has_events(self):
        self.assertEqual(sorted(VOCABULARY), sorted(vw.SPECIES_KINDS))

    def test_the_kind_specs_and_the_vocabulary_agree(self):
        """Two files name the same events, so a test has to hold them together:
        the specs are prompt text the drafter sees, the vocabulary is what the
        compiler validates against, and a drift between them is a whole class of
        rows drafted and then silently refused."""
        for kind in vw.SPECIES_KINDS:
            column = next(
                column
                for column in vw.section_by_id(SPECS[kind], "phenology")["columns"]
                if column["id"] == "event"
            )
            self.assertEqual(sorted(column["values"]), sorted(VOCABULARY[kind]), kind)

    def test_the_pipeline_may_never_claim_to_have_observed_anything(self):
        for kind in vw.SPECIES_KINDS:
            column = next(
                column
                for column in vw.section_by_id(SPECS[kind], "phenology")["columns"]
                if column["id"] == "basis"
            )
            self.assertNotIn("observed", column["values"], kind)


if __name__ == "__main__":
    unittest.main(verbosity=2)

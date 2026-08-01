#!/usr/bin/env python3
"""End-to-end tests for the naturalist skill against a synthetic vault.

The compiler's own rules are proven in ``forge/lib/tests/test_vault_phenology.py``.
What is proven here is the skill's half: that a report asked in one region never
answers with another region's calendar, that moving house is one row in the
Personal Context note, and that an observation note is schema-valid and links
back to the card it is about.
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "lib"))
import vault_schema as vs  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "vault_naturalist", ROOT / "skills" / "vault-naturalist" / "scripts" / "vault-naturalist.py"
)
skill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(skill)

SCHEMA = """# Vault Schema

## Approved properties

| Property | Required | Shape | Definition |
| --- | --- | --- | --- |
| `type` | yes | controlled scalar | Kind. |
| `status` | yes | controlled scalar | Lifecycle. |
| `domain` | yes | controlled scalar | Broad area. |
| `subdomain` | no | controlled scalar | Nested area. |
| `parent` | no | quoted wikilink | Parent hub. |
| `related` | no | list of quoted wikilinks | Related links. |
| `capture_type` | no | controlled scalar | Capture type. |
| `date` | no | scalar, human-owned | Subject date. |
| `created` | yes | scalar, derived | Date this note came into existence. |

## Note types

- `organism` — A living thing.
- `observation` — Something seen in the field.
- `place` — A place.
- `note` — General note.

## Status values

- `raw` — Unprocessed.
- `active` — Active.

## Domains

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `wiki` | `3` | `Wiki` | Reference cards. |
| `nature` | `6` | `Nature` | Field records. |
| `meta` | `99` | `Meta` | System notes. |

## Subdomains

### wiki

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `places` | `4` | `Places` | Place cards. |
| `animals` | `8` | `Animals` | Animal cards. |
| `plants` | `9` | `Plants` | Plant cards. |
| `fungi` | `10` | `Fungi` | Fungus cards. |

### nature

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `observations` | `1` | `Observations` | What was seen. |

### meta

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `schemas` | `2` | `Schemas` | Schema notes. |
| `templates` | `3` | `Templates` | Templates. |

## Project registry

| Approved value | Domain | Subdomain | Number | Definition |
| --- | --- | --- | --- | --- |
| `"[[Pi Forge]]"` | `meta` |  | `1` | Placeholder. |

## Source kinds

- `book` — Book.

## Capture types

- `manual` — Typed.
- `generated` — Made by a script, agent, or model.

## Legacy normalization map

| Legacy input | Canonical output |
| --- | --- |
| `type: daily` | `type: note` |

## Folder routing

Derived by code.
"""

PROFILE = """---
type: note
status: active
domain: meta
subdomain: schemas
---

# Personal Context

## Owner

| Field | Value |
| --- | --- |
| `name` | Ellian |
| `home region` | Puget Lowland |

## Cards

| Note | Tier | Scope | Applies |
| --- | --- | --- | --- |
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
| mating | Jan-Mar | [[Puget Lowland]] | sourced |
| birth | Apr-May | [[Puget Lowland]] | sourced |
| hibernation | Nov-Feb | [[Upper Midwest]] | sourced |

## Notes

Mine.
"""

SALMONBERRY = """---
type: organism
status: active
domain: wiki
subdomain: plants
---

# Salmonberry, Rubus spectabilis

## Phenology

| Event | Window | Region | Basis |
| --- | --- | --- | --- |
| flowering | Mar-Apr | [[Puget Lowland]] | sourced |
| fruiting | Jun-Jul | [[Puget Lowland]] | observed |

## Notes
"""

CHANTERELLE = """---
type: organism
status: active
domain: wiki
subdomain: fungi
---

# Golden Chanterelle, Cantharellus formosus

## Phenology

| Event | Window | Region | Basis |
| --- | --- | --- | --- |
| fruiting | Sep-Nov | [[Oregon Coast Range]] | sourced |

## Notes
"""


class NaturalistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name).resolve()
        (self.vault / ".obsidian").mkdir()
        self.write(vs.DEFAULT_SCHEMA, SCHEMA)
        self.write(skill.vault_profile.DEFAULT_PROFILE, PROFILE)
        self.write("03 Wiki/3.08 Animals/Raccoon, Procyon lotor.md", RACCOON)
        self.write("03 Wiki/3.09 Plants/Salmonberry, Rubus spectabilis.md", SALMONBERRY)
        self.write("03 Wiki/3.10 Fungi/Golden Chanterelle, Cantharellus formosus.md", CHANTERELLE)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text):
        path = self.vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def run_skill(self, *argv):
        result = skill.COMMANDS[argv[0]](skill.parse_args(list(argv)))
        self.assertEqual(result["status"], "ok", result.get("errors"))
        return result

    # ---- compile ----------------------------------------------------------

    def test_compile_writes_an_index_of_every_species_kind(self):
        result = self.run_skill("compile", "--vault", str(self.vault))
        self.assertEqual(result["data"]["counts"]["species"], 3)
        self.assertEqual(result["data"]["counts"]["events"], 6)
        self.assertEqual(result["data"]["problems"], [])
        written = self.vault / ".vault-naturalist" / "cache" / "phenology.json"
        self.assertTrue(written.is_file())
        index = json.loads(written.read_text(encoding="utf-8"))
        self.assertEqual({record["kind"] for record in index["species"]}, {"animal", "plant", "fungus"})

    def test_a_dry_run_compile_writes_nothing(self):
        self.run_skill("compile", "--vault", str(self.vault), "--dry-run")
        self.assertFalse((self.vault / ".vault-naturalist" / "cache" / "phenology.json").exists())

    def test_an_unreadable_row_is_reported_rather_than_dropped_silently(self):
        self.write(
            "03 Wiki/3.08 Animals/Raccoon, Procyon lotor.md",
            RACCOON.replace("| mating | Jan-Mar |", "| courtship | Jan-Mar |"),
        )
        result = self.run_skill("compile", "--vault", str(self.vault))
        self.assertTrue(any("courtship" in problem for problem in result["data"]["problems"]))
        self.assertTrue(result["warnings"])

    # ---- report -----------------------------------------------------------

    def test_the_home_region_comes_from_the_personal_context_note(self):
        self.run_skill("compile", "--vault", str(self.vault))
        result = self.run_skill("report", "--vault", str(self.vault), "--month", "3")
        self.assertEqual(result["data"]["region"], "Puget Lowland")
        self.assertEqual(result["data"]["regionSource"], "profile")
        self.assertEqual(
            {record["common"] for record in result["data"]["expected"]}, {"Raccoon", "Salmonberry"}
        )

    def test_another_regions_window_is_never_reported_as_local(self):
        """The raccoon hibernates in the Upper Midwest in December. In Puget
        Sound it is recorded as doing nothing, and must not appear."""
        self.run_skill("compile", "--vault", str(self.vault))
        result = self.run_skill("report", "--vault", str(self.vault), "--month", "12")
        self.assertEqual(result["data"]["expected"], [])

    def test_moving_house_is_one_row_in_the_owner_table(self):
        self.run_skill("compile", "--vault", str(self.vault))
        self.write(
            skill.vault_profile.DEFAULT_PROFILE,
            PROFILE.replace("| `home region` | Puget Lowland |", "| `home region` | Oregon Coast Range |"),
        )
        result = self.run_skill("report", "--vault", str(self.vault), "--month", "10")
        self.assertEqual(result["data"]["region"], "Oregon Coast Range")
        self.assertEqual({record["common"] for record in result["data"]["expected"]}, {"Golden Chanterelle"})

    def test_an_explicit_region_overrides_the_declared_one(self):
        self.run_skill("compile", "--vault", str(self.vault))
        result = self.run_skill(
            "report", "--vault", str(self.vault), "--month", "12", "--region", "Upper Midwest"
        )
        self.assertEqual(result["data"]["regionSource"], "flag")
        self.assertEqual({record["common"] for record in result["data"]["expected"]}, {"Raccoon"})

    def test_cards_with_no_local_data_are_named(self):
        self.run_skill("compile", "--vault", str(self.vault))
        result = self.run_skill("report", "--vault", str(self.vault), "--month", "3")
        self.assertEqual(
            result["data"]["cardsWithoutLocalData"],
            ["03 Wiki/3.10 Fungi/Golden Chanterelle, Cantharellus formosus.md"],
        )

    def test_a_report_without_a_compiled_index_says_so(self):
        result = self.run_skill("report", "--vault", str(self.vault), "--month", "3")
        self.assertTrue(any("no compiled index" in warning for warning in result["warnings"]))
        self.assertTrue(result["data"]["expected"])

    def test_an_undeclared_home_region_warns_rather_than_guessing(self):
        self.write(skill.vault_profile.DEFAULT_PROFILE, PROFILE.replace(
            "| `home region` | Puget Lowland |\n", ""
        ))
        self.run_skill("compile", "--vault", str(self.vault))
        result = self.run_skill("report", "--vault", str(self.vault), "--month", "12")
        self.assertIsNone(result["data"]["region"])
        self.assertTrue(any("no home region" in warning for warning in result["warnings"]))
        # Without a region every window is reported together, and says so.
        self.assertEqual({record["common"] for record in result["data"]["expected"]}, {"Raccoon"})

    # ---- observe ----------------------------------------------------------

    def test_an_observation_is_schema_valid_and_links_to_its_card(self):
        result = self.run_skill(
            "observe", "--vault", str(self.vault), "--species", "Raccoon, Procyon lotor",
            "--date", "2026-03-04", "--place", "Back fence", "--count", "3",
            "--behavior", "Foraging under the feeder",
        )
        path = self.vault / result["data"]["path"]
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        metadata = vs.parse_frontmatter(vs.split_frontmatter(path.read_bytes())["frontmatter_text"])
        self.assertEqual(metadata["type"], "observation")
        self.assertEqual(metadata["domain"], "nature")
        self.assertEqual(metadata["subdomain"], "observations")
        self.assertEqual(metadata["date"], "2026-03-04")
        self.assertEqual(metadata["parent"], "[[Raccoon, Procyon lotor]]")
        self.assertEqual(metadata["related"], ["[[Puget Lowland]]"])
        self.assertEqual(metadata["capture_type"], "generated")
        self.assertIn("| Count | 3 |", text)
        self.assertIn("Foraging under the feeder", text)

    def test_an_observation_files_into_the_schemas_own_route(self):
        result = self.run_skill(
            "observe", "--vault", str(self.vault), "--species", "Raccoon, Procyon lotor", "--date", "2026-03-04"
        )
        self.assertTrue(result["data"]["path"].startswith("06 Nature/6.01 Observations/"))

    def test_the_filename_carries_the_date_so_the_backfill_can_read_it(self):
        result = self.run_skill(
            "observe", "--vault", str(self.vault), "--species", "Raccoon, Procyon lotor",
            "--date", "2026-03-04", "--place", "Back fence",
        )
        stem = Path(result["data"]["path"]).stem
        self.assertEqual(stem, "2026-03-04 Raccoon - Back fence")
        self.assertEqual(vs.parsed_prefix_date(stem), "2026-03-04")

    def test_created_is_stamped_when_the_schema_declares_it(self):
        result = self.run_skill(
            "observe", "--vault", str(self.vault), "--species", "Raccoon, Procyon lotor", "--date", "2019-01-01"
        )
        path = self.vault / result["data"]["path"]
        metadata = vs.parse_frontmatter(vs.split_frontmatter(path.read_bytes())["frontmatter_text"])
        # The note was written today; the sighting was in 2019. They are different facts.
        self.assertNotEqual(metadata["created"], metadata["date"])

    def test_a_dry_run_writes_nothing_and_returns_the_note(self):
        result = self.run_skill(
            "observe", "--vault", str(self.vault), "--species", "Raccoon, Procyon lotor", "--dry-run"
        )
        self.assertFalse((self.vault / result["data"]["path"]).exists())
        self.assertIn("type: observation", result["data"]["body"])

    def test_an_unknown_species_still_records_but_warns(self):
        result = self.run_skill(
            "observe", "--vault", str(self.vault), "--species", "Pine Marten, Martes martes", "--dry-run"
        )
        self.assertIsNone(result["data"]["speciesCard"])
        self.assertTrue(any("no species card" in warning for warning in result["warnings"]))

    def test_a_second_observation_never_overwrites_the_first(self):
        argv = ["observe", "--vault", str(self.vault), "--species", "Raccoon, Procyon lotor", "--date", "2026-03-04"]
        self.run_skill(*argv)
        with self.assertRaises(vs.UserError) as caught:
            skill.COMMANDS["observe"](skill.parse_args(argv))
        self.assertIn("already exists", str(caught.exception))

    def test_an_impossible_date_is_refused(self):
        with self.assertRaises(vs.UserError):
            skill.COMMANDS["observe"](skill.parse_args([
                "observe", "--vault", str(self.vault), "--species", "Raccoon, Procyon lotor",
                "--date", "2026-02-30",
            ]))

    def test_a_vault_without_the_observation_type_refuses_to_write(self):
        self.write(vs.DEFAULT_SCHEMA, SCHEMA.replace("- `observation` — Something seen in the field.\n", ""))
        with self.assertRaises(vs.UserError) as caught:
            skill.COMMANDS["observe"](skill.parse_args([
                "observe", "--vault", str(self.vault), "--species", "Raccoon, Procyon lotor",
            ]))
        self.assertIn("no 'observation' note type", str(caught.exception))

    def test_a_vault_without_the_nature_route_refuses_to_write(self):
        self.write(vs.DEFAULT_SCHEMA, SCHEMA.replace(
            "| `observations` | `1` | `Observations` | What was seen. |",
            "| `sightings` | `1` | `Sightings` | What was seen. |",
        ))
        with self.assertRaises(vs.UserError) as caught:
            skill.COMMANDS["observe"](skill.parse_args([
                "observe", "--vault", str(self.vault), "--species", "Raccoon, Procyon lotor",
            ]))
        self.assertIn("nature/observations", str(caught.exception))

    # ---- doctor -----------------------------------------------------------

    def test_doctor_reports_a_ready_vault(self):
        result = self.run_skill("doctor", "--vault", str(self.vault))
        self.assertEqual(result["data"]["missingSchemaRows"], [])
        self.assertTrue(result["data"]["createdProperty"])
        self.assertEqual(result["data"]["homeRegion"], "Puget Lowland")
        self.assertEqual(result["data"]["phenology"]["counts"]["species"], 3)

    def test_doctor_names_the_schema_rows_a_vault_is_missing(self):
        self.write(vs.DEFAULT_SCHEMA, SCHEMA.replace("- `observation` — Something seen in the field.\n", ""))
        result = self.run_skill("doctor", "--vault", str(self.vault))
        self.assertIn("note type `observation`", result["data"]["missingSchemaRows"])
        self.assertTrue(any("owner's edit" in warning for warning in result["warnings"]))

    def test_doctor_reports_uninstalled_templates_without_failing(self):
        result = self.run_skill("doctor", "--vault", str(self.vault))
        self.assertEqual({kind: entry["ok"] for kind, entry in result["data"]["templates"].items()},
                         {"animal": False, "plant": False, "fungus": False})
        self.assertTrue(any("template-install" in warning for warning in result["warnings"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""Tests for the `guide` mode: the vault compiling a skill that describes itself.

The output is read by a model at the start of a session and believed, so the
failure that matters is not a crash — it is a guide that stays confident while
the vault moves out from under it. Most of what follows is about that: that the
file is byte-idempotent so a real change is visible as a diff, that `--check`
notices both kinds of drift, that a folder full of machine artifacts is never
described as notes, and that every path the guide cites actually exists.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "vault-organizer.py"
SCHEMA_RELATIVE = "99 Meta/99.02 Schemas/0.00 Vault Schema.md"
GUIDE_RELATIVE = ".agents/skills/vault-guide/SKILL.md"
sys.dont_write_bytecode = True

SCHEMA = """# Vault Schema

## Approved properties

| Property | Required | Shape | Definition |
| --- | --- | --- | --- |
| `type` | yes | controlled scalar | Kind. |
| `status` | yes | controlled scalar | Lifecycle. |
| `domain` | yes | controlled scalar | Broad area. |
| `subdomain` | no | controlled scalar | Nested area. |
| `source_kind` | no | controlled scalar | Kind of source. |
| `date` | no | human-owned scalar | The day this is about. |

## Note types

- `note` — General note.
- `source` — Something read.

## Status values

- `active` — Active.

## Domains

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `craft` | `2` | `Craft` | Making things. |
| `archive` | `98` | `Archive` | Superseded material. |
| `meta` | `99` | `Meta` | System notes. |

## Subdomains

### craft

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `gardening` | `1` | `Gardening` | Plants. |
| `cooking` | `2` | `Cooking` | Food. |

### meta

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `schemas` | `2` | `Schemas` | Controlled vocabularies. |
| `workflows` | `6` | `Workflows` | Generated run directories. |
| `agent-rules` | `8` | `Agent Rules` | Model-facing operating constraints. |

## Project registry

| Approved value | Domain | Subdomain | Number | Definition |
| --- | --- | --- | --- | --- |
| `"[[Greenhouse]]"` | `craft` | `gardening` | `1` | Building the greenhouse. |

## Sources root

| Number | Label | Definition |
| --- | --- | --- |
| `10` | `Sources` | Everything read rather than written. |

## Source kinds

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `book` | `1` | `Book` | A book. |
| `article` | `2` | `Article` | An article. |

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

FOLDERS = [
    "00 Inbox",
    "02 Craft",
    "02 Craft/2.01 Gardening",
    "02 Craft/2.01 Gardening/2.01.01 Greenhouse",
    "02 Craft/2.02 Cooking",
    "10 Sources",
    "10 Sources/10.01 Book",
    "10 Sources/10.02 Article",
    "98 Archive",
    "99 Meta",
    "99 Meta/99.02 Schemas",
    "99 Meta/99.06 Workflows",
    "99 Meta/99.08 Agent Rules",
]


def run_script(*args):
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PI_FORGE_AGENT_DIR": "/nonexistent-agent-directory"}
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env)


class GuideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name).resolve() / "vault"
        for folder in FOLDERS:
            (self.vault / folder).mkdir(parents=True, exist_ok=True)
        self.schema_path = self.vault / SCHEMA_RELATIVE
        self.schema_path.write_text(SCHEMA, encoding="utf-8")
        self.note("02 Craft/2.01 Gardening/Beds.md", "note", "craft")
        self.note("02 Craft/2.02 Cooking/Stock.md", "note", "craft")

    def tearDown(self):
        self.tmp.cleanup()

    def note(self, relative, type_value=None, domain=None):
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if type_value is None:
            body = "---\ntags:\n  - legacy\n---\n\n# Old\n"
        else:
            body = "---\ntype: {0}\nstatus: active\ndomain: {1}\n---\n\n# Note\n".format(type_value, domain)
        path.write_text(body, encoding="utf-8")

    def config_note(self, name, title):
        (self.vault / "99 Meta/99.02 Schemas" / name).write_text(
            "# " + title + "\n\nBody.\n", encoding="utf-8"
        )

    def rule_note(self, name, title):
        (self.vault / "99 Meta/99.08 Agent Rules" / name).write_text(
            "---\ntype: note\nstatus: active\ndomain: meta\n---\n\n# " + title + "\n\nBody.\n",
            encoding="utf-8",
        )

    def guide(self, *extra, **kwargs):
        expect = kwargs.pop("expect", 0)
        result = run_script("guide", "--vault", str(self.vault), *extra)
        self.assertEqual(result.returncode, expect, result.stderr + result.stdout)
        return json.loads(result.stdout)

    def installed(self):
        return (self.vault / GUIDE_RELATIVE).read_text(encoding="utf-8")

    # --- the compiled file ------------------------------------------------

    def test_apply_installs_a_skill_the_agent_can_discover(self):
        payload = self.guide("--apply")
        path = self.vault / GUIDE_RELATIVE
        self.assertTrue(path.is_file())
        self.assertEqual(payload["data"]["guide"], str(path))
        text = path.read_text(encoding="utf-8")
        # The directory basename and the frontmatter name must agree or nothing
        # loads the skill at all.
        self.assertTrue(text.startswith("---\nname: vault-guide\n"))
        self.assertEqual(path.parent.name, "vault-guide")

    def test_trigger_cases_ship_beside_the_skill(self):
        self.guide("--apply")
        path = self.vault / ".agents/skills/vault-guide/tests/triggers.json"
        self.assertTrue(path.is_file())
        cases = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(cases["positive"])
        self.assertTrue(cases["negative"])

    def test_the_description_satisfies_the_skill_standard(self):
        self.guide("--apply")
        description = ""
        for line in self.installed().splitlines():
            if line.startswith("description: "):
                description = line[len("description: "):]
                break
        self.assertGreaterEqual(len(description), 40)
        self.assertLessEqual(len(description), 1024)
        # Routing metadata, not documentation: it has to say when to fire and
        # when the neighbouring vault skills own the job instead.
        self.assertIn("Use when", description)
        self.assertIn("Do not use", description)
        self.assertIn("vault-connections", description)
        self.assertIn("vault-organizer", description)

    def test_a_dry_run_stages_a_candidate_and_installs_nothing(self):
        payload = self.guide()
        self.assertTrue(payload["data"]["dryRun"])
        self.assertFalse((self.vault / GUIDE_RELATIVE).exists())
        candidate = Path(payload["data"]["candidate"])
        self.assertTrue(candidate.is_file())
        # Staged under cache/, which the vault's own .gitignore already excludes:
        # a candidate is regenerated every run and must not be versioned.
        self.assertIn("cache", candidate.parts)

    def test_the_map_carries_folders_values_and_the_sources_exception(self):
        self.guide("--apply")
        text = self.installed()
        self.assertIn("`02 Craft` `craft`", text)
        self.assertIn("2.01 Gardening `gardening`", text)
        self.assertIn("10.01 Book `book`", text)
        self.assertIn("`[[Greenhouse]]`", text)
        self.assertIn("00 Inbox", text)
        # Human-owned properties are marked, because a skill filling one in is a
        # silent overwrite of something only the owner may set.
        self.assertIn("`date`*", text)

    def test_every_path_the_guide_cites_exists_in_the_vault(self):
        self.config_note("0.04 Note Format.md", "Note Format")
        self.rule_note("Dashboard editing rules.md", "Dashboard editing rules")
        self.guide("--apply")
        cited = []
        for line in self.installed().splitlines():
            if line.startswith("| ") and line.endswith(" |") and ".md`" in line:
                cited.append(line.rsplit("`", 2)[1])
        self.assertTrue(cited)
        for relative in cited:
            self.assertTrue((self.vault / relative).is_file(), relative)

    def test_unnumbered_notes_beside_the_schema_are_not_treated_as_canon(self):
        # A vault accumulates drafts and superseded design notes in the schemas
        # folder. Indexing one as a convention is the failure this guards.
        self.config_note("0.01 Voice and Style.md", "Voice and Style")
        self.config_note("Old Storage Thoughts.md", "Old Storage Thoughts")
        self.guide("--apply")
        text = self.installed()
        self.assertIn("0.01 Voice and Style.md", text)
        self.assertNotIn("Old Storage Thoughts", text)

    def test_agent_rules_notes_are_indexed_by_title_not_copied(self):
        self.rule_note("Project corpus rules.md", "Project corpus rules")
        self.guide("--apply")
        text = self.installed()
        self.assertIn("99 Meta/99.08 Agent Rules/Project corpus rules.md", text)
        # Indexed, never paraphrased: the note stays the single source.
        self.assertNotIn("Body.", text)

    # --- what must never be described as notes ----------------------------

    def test_run_artifacts_are_named_as_output_not_counted_as_legacy_notes(self):
        for index in range(40):
            self.note("99 Meta/99.06 Workflows/Extractions/pkt-{0}.md".format(index))
        self.guide("--apply")
        text = self.installed()
        self.assertIn("99 Meta/99.06 Workflows` holds generated run directories", text)
        self.assertNotIn("Extractions` holds", text)
        self.assertNotIn("predate the schema", text)

    def test_a_pre_schema_pocket_is_reported_and_narrowed_to_the_child(self):
        for index in range(30):
            self.note("98 Archive/QE/Note {0}.md".format(index))
        self.guide("--apply")
        text = self.installed()
        self.assertIn("`98 Archive/QE` holds 30 notes that predate the schema", text)
        self.assertIn("do not normalize them", text)

    def test_a_handful_of_stragglers_is_not_reported_as_a_pocket(self):
        for index in range(3):
            self.note("02 Craft/2.02 Cooking/Loose {0}.md".format(index))
        self.guide("--apply")
        self.assertNotIn("predate the schema", self.installed())

    def test_routes_with_no_folder_are_listed_as_absences_not_errors(self):
        (self.vault / "02 Craft/2.01 Gardening/2.01.01 Greenhouse").rmdir()
        self.guide("--apply")
        text = self.installed()
        self.assertIn("`02 Craft/2.01 Gardening/2.01.01 Greenhouse`", text)
        self.assertIn("not an error to fix", text)

    # --- staying honest ---------------------------------------------------

    def test_regeneration_is_byte_identical_over_an_unchanged_vault(self):
        self.guide("--apply")
        first = self.installed()
        self.guide("--apply")
        self.assertEqual(first, self.installed())
        # No timestamp anywhere, which is what makes the above true and lets a
        # real diff mean something.
        self.assertNotIn("Generated at", first)

    def test_check_passes_on_a_fresh_guide(self):
        self.guide("--apply")
        payload = self.guide("--check")
        self.assertTrue(payload["data"]["current"])
        self.assertEqual(payload["warnings"], [])

    def test_check_fails_when_the_schema_note_changes(self):
        self.guide("--apply")
        self.schema_path.write_text(
            SCHEMA.replace("| `cooking` | `2` | `Cooking` | Food. |", "| `baking` | `2` | `Baking` | Bread. |"),
            encoding="utf-8",
        )
        payload = self.guide("--check", expect=1)
        self.assertFalse(payload["data"]["current"])
        self.assertIn("schema note changed", " ".join(payload["warnings"]))

    def test_check_fails_when_a_folder_appears(self):
        self.guide("--apply")
        (self.vault / "02 Craft" / "2.03 Sewing").mkdir()
        payload = self.guide("--check", expect=1)
        self.assertFalse(payload["data"]["current"])
        self.assertIn("folder tree changed", " ".join(payload["warnings"]))

    def test_check_fails_when_no_guide_is_installed(self):
        payload = self.guide("--check", expect=1)
        self.assertFalse(payload["data"]["current"])
        self.assertIn("no guide installed", " ".join(payload["warnings"]))

    def test_a_dry_run_names_what_changed_without_printing_the_body(self):
        self.guide("--apply")
        (self.vault / "02 Craft" / "2.03 Sewing").mkdir()
        payload = self.guide()
        self.assertTrue(payload["data"]["changed"])
        self.assertIn("tree fingerprint changed", payload["data"]["changes"])
        self.assertNotIn("body", payload["data"])
        self.assertIn("Folder map", payload["data"]["sections"])

    def test_print_is_what_puts_the_body_in_the_payload(self):
        payload = self.guide("--print")
        self.assertIn("name: vault-guide", payload["data"]["body"])

    # --- what the owner writes stays -------------------------------------

    def test_text_after_the_end_marker_survives_regeneration(self):
        self.guide("--apply")
        path = self.vault / GUIDE_RELATIVE
        path.write_text(self.installed() + "\n## Ellie's notes\n\nKeep this.\n", encoding="utf-8")
        self.guide("--apply")
        text = self.installed()
        self.assertIn("## Ellie's notes", text)
        self.assertIn("Keep this.", text)
        # And it stays put across a second pass rather than accumulating blank lines.
        self.guide("--apply")
        self.assertEqual(text, self.installed())

    def test_edits_inside_the_generated_block_are_overwritten(self):
        self.guide("--apply")
        path = self.vault / GUIDE_RELATIVE
        path.write_text(self.installed().replace("`02 Craft` `craft`", "`02 Crufts` `crufts`"), encoding="utf-8")
        self.guide("--apply")
        self.assertIn("`02 Craft` `craft`", self.installed())

    # --- flag discipline --------------------------------------------------

    def test_check_and_print_are_refused_outside_guide_mode(self):
        result = run_script("drift", "--vault", str(self.vault), "--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("belong to guide mode", result.stdout)

    def test_check_refuses_to_be_combined_with_apply(self):
        result = run_script("guide", "--vault", str(self.vault), "--check", "--apply")
        self.assertEqual(result.returncode, 1)
        self.assertIn("never writes", result.stdout)


if __name__ == "__main__":
    unittest.main()

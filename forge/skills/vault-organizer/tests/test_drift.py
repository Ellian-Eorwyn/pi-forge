#!/usr/bin/env python3
"""Tests for the `drift` mode: auditing, and the first code that writes the schema.

Filing a note creates its destination on demand, so a schema route naming a
folder that does not exist grows a second folder beside the one the notes are
in. This mode reports that, and — only for ids the user names — corrects the
schema side of it.

The dangerous half is the write. `--fix-schema` edits the vault's single source
of truth, so the tests below are mostly about what it must refuse: folder-side
corrections, ids nobody asked for, and any edit that leaves the note worse than
it found it.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "vault-organizer.py"
_shim_spec = importlib.util.spec_from_file_location(
    "obsidian_shim", Path(__file__).resolve().parents[3] / "lib" / "tests" / "obsidian_shim.py"
)
_obsidian_shim = importlib.util.module_from_spec(_shim_spec)
_shim_spec.loader.exec_module(_obsidian_shim)
ShimEnvironment = _obsidian_shim.ShimEnvironment
SCHEMA_RELATIVE = "99 Meta/99.02 Schemas/0.00 Vault Schema.md"
sys.dont_write_bytecode = True

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
| `contacts` | `1` | `Contacts` | Living people you might contact. |
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

CLEAN = [
    "00 Inbox",
    "02 Craft",
    "02 Craft/2.01 Gardening",
    "02 Craft/2.01 Gardening/2.01.01 Greenhouse",
    "02 Craft/2.02 Cooking",
    "08 Directory",
    "08 Directory/8.01 Contacts",
    "08 Directory/8.02 Organizations",
    "99 Meta",
    "99 Meta/99.02 Schemas",
    "99 Meta/99.05 Attachments",
]


def run_script(*args):
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PI_FORGE_AGENT_DIR": "/nonexistent-agent-directory"}
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env)


class DriftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name).resolve() / "vault"
        for folder in CLEAN:
            (self.vault / folder).mkdir(parents=True, exist_ok=True)
        self.schema_path = self.vault / SCHEMA_RELATIVE
        self.schema_path.write_text(SCHEMA, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def notes(self, folder, count):
        (self.vault / folder).mkdir(parents=True, exist_ok=True)
        for index in range(count):
            (self.vault / folder / f"Note {index}.md").write_text("# Note\n", encoding="utf-8")

    def schema_text(self):
        return self.schema_path.read_text(encoding="utf-8")

    def audit(self, *extra, expect=0):
        result = run_script("drift", "--vault", str(self.vault), *extra)
        self.assertEqual(result.returncode, expect, result.stderr + result.stdout)
        return json.loads(result.stdout)

    def moved_label(self):
        """The live drift the real vault had: a label sitting at the wrong number."""
        (self.vault / "08 Directory" / "8.02 Organizations").rmdir()
        self.notes("08 Directory/8.03 Organizations", 3)

    def finding(self, payload, kind):
        matches = [entry for entry in payload["data"]["findings"] if entry["kind"] == kind]
        self.assertTrue(matches, payload["data"]["findings"])
        return matches[0]

    # --- auditing ---------------------------------------------------------

    def test_a_clean_vault_reports_nothing(self):
        payload = self.audit()
        self.assertEqual(payload["data"]["counts"], {"high": 0, "medium": 0, "low": 0, "info": 0})
        self.assertEqual(payload["warnings"], [])

    def test_a_moved_label_is_reported_as_high(self):
        self.moved_label()
        payload = self.audit()
        self.assertEqual(payload["data"]["counts"]["high"], 1)
        finding = self.finding(payload, "label_moved")
        self.assertEqual(finding["path"], "08 Directory/8.03 Organizations")
        self.assertEqual(finding["fix_side"], "schema")

    def test_a_bare_run_leaves_the_schema_byte_identical(self):
        self.moved_label()
        self.audit()
        self.assertEqual(self.schema_text(), SCHEMA)

    def test_a_bare_run_is_a_dry_run(self):
        self.moved_label()
        payload = self.audit()
        self.assertTrue(payload["data"]["dryRun"])
        self.assertEqual(payload["data"]["applied"], [])

    def test_the_report_is_written_before_any_edit(self):
        self.moved_label()
        payload = self.audit()
        run_dir = Path(payload["data"]["runDirectory"])
        report = json.loads((run_dir / "drift_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["counts"]["high"], 1)
        self.assertTrue((run_dir / "drift_report.md").is_file())

    def test_the_mode_runs_with_no_model_endpoint_configured(self):
        self.moved_label()
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "drift", "--vault", str(self.vault)],
            capture_output=True,
            text=True,
            env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)["data"]["counts"]["high"], 1)

    # --- fixing the schema side -------------------------------------------

    def test_a_named_fix_rewrites_exactly_one_row(self):
        self.moved_label()
        identifier = self.finding(self.audit(), "label_moved")["id"]
        payload = self.audit("--fix-schema", identifier)
        self.assertEqual(len(payload["data"]["applied"]), 1)
        expected = SCHEMA.replace(
            "| `organizations` | `2` | `Organizations` |",
            "| `organizations` | `3` | `Organizations` |",
        )
        self.assertNotEqual(expected, SCHEMA)
        self.assertEqual(self.schema_text(), expected)

    def test_the_fix_clears_the_finding(self):
        self.moved_label()
        identifier = self.finding(self.audit(), "label_moved")["id"]
        payload = self.audit("--fix-schema", identifier)
        self.assertEqual(payload["data"]["counts"]["high"], 0)
        self.assertEqual(self.audit()["data"]["counts"]["high"], 0)

    def test_the_fix_backs_the_schema_up_first(self):
        self.moved_label()
        identifier = self.finding(self.audit(), "label_moved")["id"]
        payload = self.audit("--fix-schema", identifier)
        backup = Path(payload["data"]["runDirectory"], "backup", SCHEMA_RELATIVE)
        self.assertEqual(backup.read_text(encoding="utf-8"), SCHEMA)

    def test_no_notes_are_moved_by_a_fix(self):
        self.moved_label()
        identifier = self.finding(self.audit(), "label_moved")["id"]
        self.audit("--fix-schema", identifier)
        self.assertEqual(len(list((self.vault / "08 Directory" / "8.03 Organizations").glob("*.md"))), 3)
        self.assertFalse((self.vault / "08 Directory" / "8.02 Organizations").exists())

    # --- what the fix must refuse -----------------------------------------

    def test_a_folder_side_fix_is_refused(self):
        # gardening is 1 and cooking is 2; on disk they are swapped, so neither
        # row can take the other's number and only the folders can move.
        (self.vault / "02 Craft" / "2.01 Gardening" / "2.01.01 Greenhouse").rmdir()
        (self.vault / "02 Craft" / "2.01 Gardening").rmdir()
        (self.vault / "02 Craft" / "2.02 Cooking").rmdir()
        self.notes("02 Craft/2.01 Cooking", 1)
        self.notes("02 Craft/2.02 Gardening", 1)
        finding = [entry for entry in self.audit()["data"]["findings"] if entry["severity"] == "high"][0]
        self.assertEqual(finding["fix_side"], "folder")
        payload = self.audit("--fix-schema", finding["id"], expect=1)
        self.assertEqual(payload["status"], "error")
        self.assertIn("folder-side", payload["errors"][0]["message"])
        self.assertEqual(self.schema_text(), SCHEMA)

    def test_an_unknown_id_is_refused_and_lists_the_real_ones(self):
        self.moved_label()
        payload = self.audit("--fix-schema", "deadbeef", expect=1)
        self.assertIn("unknown finding id deadbeef", payload["errors"][0]["message"])
        self.assertEqual(self.schema_text(), SCHEMA)

    def test_a_fix_that_would_break_the_schema_rolls_back(self):
        # Each edit is legal alone; together they put two rows on number 3.
        (self.vault / "08 Directory" / "8.01 Contacts").rmdir()
        (self.vault / "08 Directory" / "8.02 Organizations").rmdir()
        self.notes("08 Directory/8.03 Contacts", 2)
        self.notes("08 Directory/8.03 Organizations", 3)
        identifiers = [entry["id"] for entry in self.audit()["data"]["findings"] if entry["fix_side"] == "schema"]
        self.assertEqual(len(identifiers), 2)
        payload = self.audit("--fix-schema", ",".join(identifiers), expect=1)
        self.assertIn("does not parse", payload["errors"][0]["message"])
        self.assertEqual(self.schema_text(), SCHEMA)

    def test_the_medium_archive_finding_is_not_schema_fixable(self):
        # Registering a new domain is an addition, not a row edit, so the user
        # makes it themselves rather than the tool inventing a definition.
        self.notes("98 Archive", 4)
        finding = self.finding(self.audit(), "undeclared_with_notes")
        self.assertEqual(finding["severity"], "medium")
        self.assertEqual(finding["fix_side"], "manual")
        self.audit("--fix-schema", finding["id"], expect=1)
        self.assertEqual(self.schema_text(), SCHEMA)

    def test_fix_schema_is_rejected_outside_drift_mode(self):
        result = run_script("vault", "--vault", str(self.vault), "--fix-schema", "abc123")
        self.assertEqual(result.returncode, 1)
        self.assertIn("drift mode", json.loads(result.stdout)["errors"][0]["message"])


class OrganizeBlockingTests(unittest.TestCase):
    """A dry run warns; an apply refuses until the drift is fixed or overridden."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name).resolve() / "vault"
        for folder in CLEAN:
            (self.vault / folder).mkdir(parents=True, exist_ok=True)
        (self.vault / SCHEMA_RELATIVE).write_text(SCHEMA, encoding="utf-8")
        (self.vault / "08 Directory" / "8.02 Organizations").rmdir()
        folder = self.vault / "08 Directory" / "8.03 Organizations"
        folder.mkdir()
        (folder / "Acme.md").write_text("# Acme\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def organize(self, *extra):
        # --limit 0 selects nothing, so the pipeline runs end to end without a
        # model: this is about the drift gate, not about classification.
        return run_script(
            "vault", "--vault", str(self.vault), "--limit", "0", "--no-embeddings", "--no-verify", *extra
        )

    def test_a_dry_run_proceeds_and_warns(self):
        result = self.organize()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["data"]["schema_drift"]["high"], 1)
        self.assertTrue(
            any("schema drift [high]" in warning for warning in payload["warnings"]), payload["warnings"]
        )

    def test_the_report_carries_a_schema_drift_section(self):
        result = self.organize()
        report = Path(json.loads(result.stdout)["data"]["run_directory"], "report.md").read_text(encoding="utf-8")
        self.assertIn("## Schema Drift", report)
        self.assertIn("8.03 Organizations", report)
        self.assertLess(report.index("## Schema Drift"), report.index("## Schema Suggestions"))

    def test_apply_is_refused_while_a_high_finding_stands(self):
        result = self.organize("--apply")
        self.assertEqual(result.returncode, 1, result.stdout)
        message = json.loads(result.stdout)["errors"][0]["message"]
        self.assertIn("schema drift", message)
        self.assertIn("--allow-schema-drift", message)

    def test_reserved_slots_do_not_become_warnings(self):
        # A reserved slot is correct behavior. Warning about it on every run
        # trains the reader to skip the section the real collisions appear in.
        (self.vault / "02 Craft" / "2.01 Gardening" / "2.01.01 Greenhouse").rmdir()
        (self.vault / "08 Directory" / "8.02 Organizations").mkdir()
        for note in (self.vault / "08 Directory" / "8.03 Organizations").glob("*.md"):
            note.rename(self.vault / "08 Directory" / "8.02 Organizations" / note.name)
        (self.vault / "08 Directory" / "8.03 Organizations").rmdir()
        payload = json.loads(self.organize().stdout)
        self.assertEqual(payload["data"]["schema_drift"], {"high": 0, "medium": 0, "low": 0, "info": 1})
        self.assertEqual(payload["warnings"], [])
        report = Path(payload["data"]["run_directory"], "report.md").read_text(encoding="utf-8")
        self.assertIn("2.01.01 Greenhouse", report)

    def test_apply_proceeds_with_the_override(self):
        result = self.organize("--apply", "--allow-schema-drift")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["data"]["dry_run"])
        self.assertTrue(any("schema drift [high]" in warning for warning in payload["warnings"]))


class PropertyDriftTests(unittest.TestCase):
    """The property-vocabulary half of drift, which only exists with the CLI.

    Comparing the schema's approved properties against what the vault actually
    holds needs an index of every note's frontmatter. Obsidian keeps one in
    memory; pi-forge would have to walk and parse the whole vault to build it. So
    this check is present when Obsidian is and simply absent when it is not —
    and the "absent" case has to leave every other finding untouched, which is
    what the second test here is for.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name).resolve() / "vault"
        for folder in CLEAN:
            (self.vault / folder).mkdir(parents=True, exist_ok=True)
        (self.vault / SCHEMA_RELATIVE).write_text(SCHEMA, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def shim(self, properties, **kwargs):
        env = ShimEnvironment(
            {"properties": json.dumps(properties)}, vault_path=self.vault, vault_name="vault", **kwargs
        )
        self.addCleanup(env.cleanup)
        return env

    def audit(self):
        result = run_script("drift", "--vault", str(self.vault))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads(result.stdout)

    def findings_of(self, payload, kind):
        return [finding for finding in payload["data"]["findings"] if finding["kind"] == kind]

    def test_unapproved_properties_are_reported_at_medium(self):
        self.shim([
            {"name": "type", "type": "text", "count": 12},
            {"name": "extraction_quality", "type": "text", "count": 16},
            {"name": "repository_slug", "type": "text", "count": 16},
        ])
        payload = self.audit()
        unapproved = self.findings_of(payload, "property_unapproved")
        self.assertEqual(sorted(finding["path"] for finding in unapproved), ["extraction_quality", "repository_slug"])
        self.assertTrue(all(finding["severity"] == "medium" for finding in unapproved))
        self.assertEqual(unapproved[0]["note_count"], 16)
        self.assertTrue(all(finding["fix_side"] == "manual" for finding in unapproved))

    def test_obsidian_builtins_are_a_smaller_finding(self):
        self.shim([
            {"name": "type", "type": "text", "count": 12},
            {"name": "tags", "type": "tags", "count": 102},
            {"name": "invented", "type": "text", "count": 3},
        ])
        payload = self.audit()
        builtin = self.findings_of(payload, "property_obsidian_builtin")
        self.assertEqual([finding["path"] for finding in builtin], ["tags"])
        self.assertEqual(builtin[0]["severity"], "low")
        self.assertEqual([finding["severity"] for finding in self.findings_of(payload, "property_unapproved")], ["medium"])

    def test_registered_but_unused_properties_are_not_reported(self):
        # Obsidian keeps a property registered after the last note using it is
        # cleaned up. A type with no notes behind it is not drift.
        self.shim([{"name": "type", "type": "text", "count": 12}, {"name": "ghost", "type": "text", "count": 0}])
        self.assertEqual(self.findings_of(self.audit(), "property_unapproved"), [])

    def test_shape_disagreement_is_reported(self):
        # The schema calls `subdomain` a scalar; Obsidian registering it as
        # multitext means some note writes it as a list.
        self.shim([
            {"name": "type", "type": "text", "count": 12},
            {"name": "subdomain", "type": "multitext", "count": 4},
        ])
        mismatch = self.findings_of(self.audit(), "property_type_mismatch")
        self.assertEqual([finding["path"] for finding in mismatch], ["subdomain"])
        self.assertEqual(mismatch[0]["severity"], "medium")
        self.assertIn("multitext", mismatch[0]["detail"])

    def test_approved_but_absent_properties_are_info_only(self):
        self.shim([{"name": "type", "type": "text", "count": 12}])
        unused = self.findings_of(self.audit(), "property_unused")
        self.assertEqual(sorted(finding["path"] for finding in unused), ["domain", "status", "subdomain"])
        self.assertTrue(all(finding["severity"] == "info" for finding in unused))

    def test_property_drift_never_blocks_an_apply(self):
        # Every property finding is medium or below by construction, so the
        # `high`-only gate in organize --apply cannot be tripped by vocabulary.
        self.shim([
            {"name": "type", "type": "text", "count": 12},
            {"name": "invented", "type": "text", "count": 40},
        ])
        payload = self.audit()
        severities = {finding["severity"] for finding in payload["data"]["findings"] if finding["kind"].startswith("property")}
        self.assertFalse(severities & {"high"})

    def test_without_the_cli_the_report_is_exactly_what_it_was(self):
        before = self.audit()
        env = self.shim([{"name": "type", "type": "text", "count": 12}, {"name": "invented", "type": "text", "count": 5}])
        with_cli = self.audit()
        env.set_env(FORGE_OBSIDIAN_CLI="off")
        after = self.audit()

        self.assertTrue(any(finding["kind"].startswith("property") for finding in with_cli["data"]["findings"]))
        self.assertEqual(
            [finding["id"] for finding in after["data"]["findings"]],
            [finding["id"] for finding in before["data"]["findings"]],
            "no CLI means no property findings and no change to the folder ones",
        )
        report = Path(after["data"]["runDirectory"], "drift_report.md").read_text(encoding="utf-8")
        self.assertIn("Property vocabulary was not checked", report)
        self.assertNotIn(
            "Property vocabulary was not checked",
            Path(with_cli["data"]["runDirectory"], "drift_report.md").read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

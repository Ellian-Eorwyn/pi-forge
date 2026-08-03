#!/usr/bin/env python3
"""The skill's half: what each command writes, refuses, and reports.

The membership rules themselves are proven in
``forge/lib/tests/test_vault_corpus.py``. What is proven here is that the CLI
honours them at the boundary -- that a dry run leaves no manifest behind, that a
corpus with a broken link refuses to freeze rather than freezing a wrong answer,
and that a pack stays inside the budget it was given.
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "lib" / "tests"))

import vault_corpus as vc  # noqa: E402
from test_vault_corpus import PROJECT_FOLDER, SCHEMA, SOURCES, note  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "vault_projects", ROOT / "skills" / "vault-projects" / "scripts" / "vault-projects.py"
)
skill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(skill)

HUB = """# Article 2

## Corpus

### Sources
- [[Suits - 2005 - The Grasshopper]] — core theory text
"""


class SkillTestCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.vault = Path(self._temporary.name)
        self.write("99 Meta/99.02 Schemas/0.00 Vault Schema.md", SCHEMA)
        self.write(
            f"{PROJECT_FOLDER}/Article 2.md",
            note({"type": "project", "status": "active", "domain": "academic",
                  "subdomain": "dissertation", "project": '"[[Article 2]]"'}, HUB),
        )
        self.write(
            f"{PROJECT_FOLDER}/Working Draft.md",
            note({"type": "note", "status": "active", "domain": "academic",
                  "project": '"[[Article 2]]"'}, "# Working Draft\n\nProse.\n"),
        )
        self.write(
            f"{SOURCES}/Suits - 2005 - The Grasshopper.md",
            note({"type": "source", "status": "complete", "domain": "academic",
                  "subdomain": "dissertation", "source_kind": "book",
                  "project": '"[[Article 2]]"'}, "# The Grasshopper\n\n" + "Games and utopia. " * 200),
        )

    def tearDown(self):
        self._temporary.cleanup()

    def write(self, relative, text):
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def run_command(self, *argv):
        return skill.COMMANDS[argv[0]](skill.parse_args([*argv, "--vault", str(self.vault)]))

    def manifest(self):
        path = self.vault / PROJECT_FOLDER / vc.MANIFEST_NAME
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


class EmitTests(SkillTestCase):
    def test_dry_run_writes_nothing_and_shows_the_manifest(self):
        result = self.run_command("emit", "--project", "Article 2")
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["data"]["dryRun"])
        self.assertIsNone(self.manifest())
        self.assertEqual(result["data"]["manifest"]["project"], "Article 2")

    def test_apply_writes_a_manifest_a_bare_agent_can_read(self):
        result = self.run_command("emit", "--project", "Article 2", "--apply")
        self.assertEqual(result["status"], "ok")
        manifest = self.manifest()
        paths = {record["path"] for record in manifest["members"]}
        self.assertIn(f"{SOURCES}/Suits - 2005 - The Grasshopper.md", paths)
        self.assertIn("work ONLY", manifest["readme"])
        self.assertEqual(manifest["rules_note"], vc.RULES_NOTE)
        self.assertEqual(result["artifacts"][0]["kind"], "corpus-manifest")

    def test_a_broken_link_refuses_to_freeze(self):
        self.write(
            f"{PROJECT_FOLDER}/Article 2.md",
            note({"type": "project", "status": "active", "domain": "academic",
                  "project": '"[[Article 2]]"'},
                 "# Article 2\n\n## Corpus\n\n### Sources\n- [[Missing Book]]\n"),
        )
        result = self.run_command("emit", "--project", "Article 2", "--apply")
        self.assertEqual(result["status"], "error")
        self.assertIsNone(self.manifest())

    def test_emit_is_idempotent(self):
        self.run_command("emit", "--project", "Article 2", "--apply")
        first = self.manifest()
        second_result = self.run_command("emit", "--project", "Article 2", "--apply")
        self.assertEqual(second_result["data"]["drift"]["state"], "fresh")
        self.assertEqual(
            [record["path"] for record in first["members"]],
            [record["path"] for record in self.manifest()["members"]],
        )


class DoctorTests(SkillTestCase):
    def test_clean_project_with_a_fresh_manifest_passes(self):
        self.run_command("emit", "--project", "Article 2", "--apply")
        result = self.run_command("doctor", "--project", "Article 2")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["projects"][0]["manifest"], "fresh")

    def test_stale_manifest_is_an_error_naming_the_fix(self):
        self.run_command("emit", "--project", "Article 2", "--apply")
        self.write(f"{PROJECT_FOLDER}/New Idea.md",
                   note({"type": "note", "status": "active", "domain": "academic"}, "# New Idea\n"))
        result = self.run_command("doctor", "--project", "Article 2")
        self.assertEqual(result["status"], "error")
        problems = result["data"]["projects"][0]["problems"]
        stale = next(problem for problem in problems if problem["code"] == "manifest_stale")
        self.assertIn("emit --apply", stale["message"])

    def test_doctor_covers_every_registered_project_by_default(self):
        result = self.run_command("doctor")
        names = {report["project"] for report in result["data"]["projects"]}
        self.assertEqual(names, {"Article 1", "Article 2"})


class ListTests(SkillTestCase):
    def test_list_reports_which_projects_lack_a_hub(self):
        result = self.run_command("list")
        rows = {row["project"]: row for row in result["data"]["projects"]}
        self.assertIsNotNone(rows["Article 2"]["hub"])
        self.assertIsNone(rows["Article 1"]["hub"])
        self.assertEqual(result["data"]["counts"]["withoutHub"], 1)


class PackTests(SkillTestCase):
    def test_pack_stays_within_budget_and_reports_what_it_dropped(self):
        result = self.run_command("pack", "--project", "Article 2", "--budget", "200")
        self.assertEqual(result["status"], "ok")
        self.assertLessEqual(result["data"]["estimatedTokens"], 200)
        self.assertTrue(result["data"]["skipped"])
        self.assertTrue(any("did not fit" in warning for warning in result["warnings"]))

    def test_pack_includes_out_of_folder_sources_and_leads_with_the_hub(self):
        result = self.run_command("pack", "--project", "Article 2")
        text = (self.vault / result["artifacts"][0]["path"]).read_text(encoding="utf-8")
        self.assertIn("Games and utopia.", text)
        self.assertLess(text.index("Article 2.md"), text.index("The Grasshopper.md"))

    def test_pack_is_written_outside_the_corpus_it_copies(self):
        result = self.run_command("pack", "--project", "Article 2")
        pack = result["artifacts"][0]["path"]
        self.assertFalse(pack.startswith(PROJECT_FOLDER))
        after = self.run_command("resolve", "--project", "Article 2")
        self.assertNotIn(pack, {record["path"] for record in after["data"]["members"]})


class DraftHubTests(SkillTestCase):
    def test_draft_seeds_sections_from_notes_carrying_the_project(self):
        (self.vault / PROJECT_FOLDER / "Article 2.md").unlink()
        result = self.run_command("draft-hub", "--project", "Article 2", "--print-draft")
        draft = result["data"]["draft"]
        self.assertIn("## Corpus", draft)
        self.assertIn("### Sources", draft)
        self.assertIn("- [[Suits - 2005 - The Grasshopper|The Grasshopper]] — ", draft)
        self.assertNotIn("Working Draft", draft)  # already in the folder, so implicitly a member
        self.assertEqual(result["data"]["counts"]["outsideFolder"], 1)

    def test_draft_frontmatter_quotes_the_project_once(self):
        (self.vault / PROJECT_FOLDER / "Article 2.md").unlink()
        draft = self.run_command("draft-hub", "--project", "Article 2", "--print-draft")["data"]["draft"]
        self.assertIn('project: "[[Article 2]]"', draft)
        self.assertNotIn('\\"', draft)

    def test_an_opaque_filename_is_drafted_with_its_heading_as_display_text(self):
        # Sources filed under whatever the download was called are common; a hub
        # line reading `[[15]]` is a correct link and a useless one.
        self.write(
            f"{SOURCES}/15.md",
            note({"type": "source", "status": "complete", "domain": "academic",
                  "source_kind": "book", "project": '"[[Article 2]]"'},
                 "# Simulationism: The Right to Dream\n\nProse.\n"),
        )
        (self.vault / PROJECT_FOLDER / "Article 2.md").unlink()
        draft = self.run_command("draft-hub", "--project", "Article 2", "--print-draft")["data"]["draft"]
        self.assertIn("[[15|Simulationism: The Right to Dream]]", draft)

    def test_draft_never_overwrites_an_existing_hub(self):
        original = (self.vault / PROJECT_FOLDER / "Article 2.md").read_text(encoding="utf-8")
        result = self.run_command("draft-hub", "--project", "Article 2")
        self.assertEqual((self.vault / PROJECT_FOLDER / "Article 2.md").read_text(encoding="utf-8"), original)
        self.assertTrue(any("already exists" in warning for warning in result["warnings"]))


if __name__ == "__main__":
    unittest.main()

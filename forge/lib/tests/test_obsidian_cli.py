#!/usr/bin/env python3

import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

LIB = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(LIB))
spec = importlib.util.spec_from_file_location("obsidian_cli", LIB / "obsidian_cli.py")
obsidian_cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(obsidian_cli)

shim_spec = importlib.util.spec_from_file_location("obsidian_shim", Path(__file__).parent / "obsidian_shim.py")
obsidian_shim = importlib.util.module_from_spec(shim_spec)
shim_spec.loader.exec_module(obsidian_shim)
ShimEnvironment = obsidian_shim.ShimEnvironment


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.env = ShimEnvironment()
        self.addCleanup(self.env.cleanup)

    def test_available_vault_reports_name_and_write_capability(self):
        session = obsidian_cli.probe(self.env.vault)
        self.assertTrue(session["available"])
        self.assertTrue(session["canWrite"])
        self.assertEqual(session["vaultName"], "TestVault")
        self.assertEqual(session["linkUpdates"], "always")
        self.assertIsNone(session["reason"])

    def test_missing_binary_is_unavailable_without_raising(self):
        os.environ["PATH"] = str(self.env.root / "empty")
        session = obsidian_cli.probe(self.env.vault)
        self.assertFalse(session["available"])
        self.assertIn("not found on PATH", session["reason"])

    def test_off_switch_disables_everything(self):
        os.environ["FORGE_OBSIDIAN_CLI"] = "off"
        session = obsidian_cli.probe(self.env.vault)
        self.assertFalse(session["available"])
        self.assertTrue(session["disabled"])
        self.assertEqual(self.env.calls(), [], "no subprocess when disabled")

    def test_unreadable_registry_is_unavailable(self):
        (self.env.config / "obsidian.json").write_text("{not json")
        session = obsidian_cli.probe(self.env.vault)
        self.assertFalse(session["available"])
        self.assertIn("registry", session["reason"])

    def test_unregistered_vault_is_unavailable(self):
        other = self.env.root / "vaults" / "Elsewhere"
        other.mkdir(parents=True)
        session = obsidian_cli.probe(other)
        self.assertFalse(session["available"])
        self.assertFalse(session["registered"])

    def test_ambiguous_basename_refuses_to_guess(self):
        twin = self.env.root / "other" / "TestVault"
        twin.mkdir(parents=True)
        (self.env.config / "obsidian.json").write_text(
            json.dumps({"cli": True, "vaults": {"a": {"path": str(self.env.vault)}, "b": {"path": str(twin)}}})
        )
        session = obsidian_cli.probe(self.env.vault)
        self.assertFalse(session["available"])
        self.assertIsNone(session["vaultName"])
        self.assertIn("shares this folder name", session["reason"])

    def test_cli_toggle_off_is_unavailable(self):
        env = ShimEnvironment(cli=False)
        self.addCleanup(env.cleanup)
        session = obsidian_cli.probe(env.vault)
        self.assertFalse(session["available"])
        self.assertIn("command line interface is turned off", session["reason"])

    def test_link_updates_unset_allows_reads_but_not_writes(self):
        env = ShimEnvironment(link_updates="unset")
        self.addCleanup(env.cleanup)
        session = obsidian_cli.probe(env.vault)
        self.assertTrue(session["available"])
        self.assertFalse(session["canWrite"])
        self.assertEqual(session["linkUpdates"], "unset")

    def test_old_obsidian_is_rejected(self):
        self.env.write_script({"version": "1.11.0 (installer 1.11.0)"})
        session = obsidian_cli.probe(self.env.vault)
        self.assertFalse(session["available"])
        self.assertIn("predates", session["reason"])


class RunTests(unittest.TestCase):
    def setUp(self):
        self.env = ShimEnvironment()
        self.addCleanup(self.env.cleanup)
        self.session = obsidian_cli.probe(self.env.vault)

    def test_every_call_targets_the_vault_by_name_and_never_by_file(self):
        obsidian_cli.run(self.session, "backlinks", path="Notes/A.md", format="json")
        for argv in self.env.calls():
            self.assertEqual(argv[0], "vault=TestVault", "vault= must come first: {0}".format(argv))
            self.assertFalse(any(token.startswith("file=") for token in argv), argv)

    def test_error_prefix_is_failure_despite_exit_zero(self):
        self.env.write_script({"read": 'Error: File "Nope.md" not found.'})
        result = obsidian_cli.run(self.session, "read", path="Nope.md")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], 'File "Nope.md" not found.')

    def test_vault_not_found_is_failure(self):
        self.env.write_script({"files": "Vault not found."})
        result = obsidian_cli.run(self.session, "files")
        self.assertFalse(result["ok"])

    def test_unknown_command_error_retries_exactly_once(self):
        self.env.write_script(
            {"files": ['Error: Command "files" not found. Did you mean: daily?', "Notes/A.md"]}
        )
        result = obsidian_cli.run(self.session, "files")
        self.assertTrue(result["ok"])
        self.assertTrue(result["retried"])
        self.assertEqual(sum(1 for argv in self.env.calls() if "files" in argv), 2)

    def test_other_errors_are_not_retried(self):
        self.env.write_script({"read": 'Error: File "Nope.md" not found.'})
        obsidian_cli.run(self.session, "read", path="Nope.md")
        self.assertEqual(sum(1 for argv in self.env.calls() if "read" in argv), 1)

    def test_timeout_on_a_write_is_indeterminate(self):
        self.env.write_script({"rename": {"sleep": 2, "output": "Renamed"}})
        result = obsidian_cli.run(self.session, "rename", allow_write=True, timeout=0.3, path="A.md", name="B")
        self.assertFalse(result["ok"])
        self.assertTrue(result["indeterminate"], "the rename may have happened; the caller must reconcile")

    def test_timeout_on_a_read_is_plain_failure(self):
        self.env.write_script({"files": {"sleep": 2, "output": "A.md"}})
        result = obsidian_cli.run(self.session, "files", timeout=0.3)
        self.assertFalse(result["ok"])
        self.assertFalse(result["indeterminate"])

    def test_denied_commands_never_run(self):
        for command in ["eval", "delete", "create", "property:set", "dev:screenshot", "command"]:
            result = obsidian_cli.run(self.session, command)
            self.assertFalse(result["ok"], command)
            self.assertIn("will run", result["reason"])
        self.assertEqual([argv for argv in self.env.calls() if "eval" in argv], [])

    def test_unknown_commands_fail_closed(self):
        result = obsidian_cli.run(self.session, "some:future:command")
        self.assertFalse(result["ok"])
        self.assertIn("not a known read-only", result["reason"])

    def test_writes_need_both_the_flag_and_the_capability(self):
        self.assertIn("allow_write", obsidian_cli.run(self.session, "move", path="A.md", to="B")["reason"])
        blocked = dict(self.session, canWrite=False, reason=None)
        self.assertIn("writes are not enabled", obsidian_cli.run(blocked, "move", allow_write=True, path="A.md", to="B")["reason"])

    def test_run_json_parses_and_rejects_non_json(self):
        self.env.write_script({"properties": '[{"name": "type", "count": 3}]'})
        result = obsidian_cli.run_json(self.session, "properties", format="json", counts=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"][0]["name"], "type")

        self.env.write_script({"properties": "not json at all"})
        self.assertFalse(obsidian_cli.run_json(self.session, "properties", format="json")["ok"])

    def test_boolean_params_become_bare_flags(self):
        obsidian_cli.run(self.session, "unresolved", format="json", verbose=True, counts=False)
        argv = self.env.calls()[-1]
        self.assertIn("verbose", argv)
        self.assertIn("format=json", argv)
        self.assertNotIn("counts", argv)


class LinkOnlyDiffTests(unittest.TestCase):
    def test_link_line_rewrites_are_allowed(self):
        before = "---\nrelated:\n  - \"[[Beta]]\"\n---\n\nSee [[Beta]] here.\n"
        after = "---\nrelated:\n  - \"[[Beta Three]]\"\n---\n\nSee [[Beta Three]] here.\n"
        ok, changed = obsidian_cli.link_only_diff(before, after)
        self.assertTrue(ok)
        self.assertEqual(changed, [3, 6])

    def test_prose_changes_are_rejected(self):
        before = "See [[Beta]] here.\nUntouched prose.\n"
        after = "See [[Beta Three]] here.\nRewritten prose.\n"
        ok, changed = obsidian_cli.link_only_diff(before, after)
        self.assertFalse(ok)
        self.assertEqual(changed, [1, 2])

    def test_line_count_changes_are_rejected(self):
        ok, _ = obsidian_cli.link_only_diff("a\n[[B]]\n", "a\n[[B]]\nextra\n")
        self.assertFalse(ok)

    def test_identical_text_has_no_changes(self):
        ok, changed = obsidian_cli.link_only_diff("same\n", "same\n")
        self.assertTrue(ok)
        self.assertEqual(changed, [])

    def test_markdown_link_rewrites_are_allowed(self):
        # Obsidian repoints Markdown links on a move as well as wikilinks, so a
        # line carrying one has to count as a link line or every such move would
        # be rolled back.
        ok, changed = obsidian_cli.link_only_diff(
            "See [Move](../Inbox/Move.md).\n", "See [Move](Move.md).\n"
        )
        self.assertTrue(ok)
        self.assertEqual(changed, [1])


class MarkdownLinkResolutionTests(unittest.TestCase):
    """Obsidian's shortest-path rewriting, measured against the filesystem.

    Obsidian resolves a Markdown link the way it resolves a wikilink, so after a
    move it will happily write a target that only it can follow. A vault that is
    meant to stay plain Markdown needs that named, not assumed away.
    """

    def setUp(self):
        self.env = ShimEnvironment()
        self.addCleanup(self.env.cleanup)
        self.env.write("Deep/Sub/Target.md", "# Target\n")
        self.env.write("Refs/Note.md", "placeholder\n")

    def check(self, text):
        return obsidian_cli.unresolved_markdown_links(self.env.vault, "Refs/Note.md", text)

    def test_a_target_that_resolves_from_the_note_is_fine(self):
        self.assertEqual(self.check("[T](../Deep/Sub/Target.md)"), set())

    def test_obsidians_shortest_path_form_does_not_resolve_on_disk(self):
        self.assertEqual(self.check("[T](Target.md)"), {"Target.md"})

    def test_external_targets_and_anchors_are_ignored(self):
        text = "[a](https://example.com) [b](#heading) [c](mailto:x@y.z) [d]()"
        self.assertEqual(self.check(text), set())

    def test_percent_encoding_is_decoded_before_resolving(self):
        self.env.write("Refs/With Space.md", "x\n")
        self.assertEqual(self.check("[s](With%20Space.md)"), set())

    def test_an_anchor_on_a_real_file_still_resolves(self):
        self.assertEqual(self.check("[T](../Deep/Sub/Target.md#heading)"), set())

    def test_wikilinks_are_not_markdown_links(self):
        self.assertEqual(self.check("[[Target]] and ![[Target]]"), set())


class ShimFidelityTests(unittest.TestCase):
    """The shim must behave like the real binary, or the tests above prove nothing."""

    def setUp(self):
        self.env = ShimEnvironment()
        self.addCleanup(self.env.cleanup)
        self.session = obsidian_cli.probe(self.env.vault)

    def test_rename_moves_the_file_and_rewrites_inbound_links(self):
        self.env.write("Notes/Beta.md", "# Beta\n")
        self.env.write("Notes/Alpha.md", '---\nrelated:\n  - "[[Beta]]"\n---\n\nSee [[Beta]].\n')
        result = obsidian_cli.run(self.session, "rename", allow_write=True, path="Notes/Beta.md", name="Beta Two")
        self.assertTrue(result["ok"], result)
        self.assertTrue((self.env.vault / "Notes" / "Beta Two.md").exists())
        alpha = (self.env.vault / "Notes" / "Alpha.md").read_text()
        self.assertIn('- "[[Beta Two]]"', alpha)
        self.assertIn("See [[Beta Two]].", alpha)

    def test_shim_always_exits_zero_on_failure(self):
        completed = subprocess.run(
            [str(self.env.bin / "obsidian"), "vault=TestVault", "rename", "path=Missing.md", "name=X"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertTrue(completed.stdout.startswith("Error: "))


class DoctorTests(unittest.TestCase):
    def test_available_vault_reports_reachable(self):
        env = ShimEnvironment({"files": "12"})
        self.addCleanup(env.cleanup)
        report = obsidian_cli.doctor(env.vault)
        self.assertTrue(report["available"])
        self.assertTrue(report["reachable"])
        self.assertEqual(report["warnings"], [])

    def test_link_updates_unset_is_an_actionable_warning(self):
        env = ShimEnvironment({"files": "12"}, link_updates="unset")
        self.addCleanup(env.cleanup)
        report = obsidian_cli.doctor(env.vault)
        self.assertTrue(report["available"])
        self.assertEqual(len(report["warnings"]), 1)
        self.assertIn("Automatically update internal links", report["warnings"][0])

    def test_unregistered_vault_warns_because_the_binary_is_right_there(self):
        env = ShimEnvironment()
        self.addCleanup(env.cleanup)
        other = env.root / "vaults" / "Elsewhere"
        other.mkdir(parents=True)
        report = obsidian_cli.doctor(other)
        self.assertFalse(report["available"])
        self.assertEqual(len(report["warnings"]), 1)
        self.assertIn("not registered", report["warnings"][0])

    def test_no_obsidian_at_all_says_nothing(self):
        env = ShimEnvironment()
        self.addCleanup(env.cleanup)
        os.environ["PATH"] = str(env.root / "empty")
        report = obsidian_cli.doctor(env.vault)
        self.assertFalse(report["available"])
        self.assertEqual(report["warnings"], [], "a vault without Obsidian is not a vault with a problem")


if __name__ == "__main__":
    unittest.main()

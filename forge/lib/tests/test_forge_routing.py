#!/usr/bin/env python3

import argparse
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

LIB = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(LIB))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, LIB / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


forge_llm = _load("forge_llm")
forge_routing = _load("forge_routing")


class StageResolutionTests(unittest.TestCase):
    def test_an_unmeasured_stage_runs_on_chat(self):
        # Not in the table means nobody measured it, which is not a reason to
        # move it anywhere.
        service = forge_routing.service_for("some-stage-nobody-measured", env={}, settings={}, routing={})
        self.assertEqual(service["routedTo"], "chat")
        self.assertEqual(service["url"], "http://llms:8004/v1/chat/completions")

    def test_the_table_routes_the_stages_the_report_cleared(self):
        for stage, expected in (
            ("clean-transcript-chunk-multi", "task"),
            ("connection-judgment", "task"),
        ):
            self.assertEqual(forge_routing.service_name_for(stage, routing={}), expected, stage)

    def test_cleanup_and_braindump_are_held_on_chat(self):
        # Both moved off `think`: single-speaker cleanup clears the meaning-first
        # gate on the non-thinking bulk tier (q38-none 8/8) with the verify pass as
        # the escalation net, and braindump-split is within measurement noise of
        # thinking once its eval gate is aligned to the skill, so the tie rule
        # takes the faster tier.
        for stage in ("clean-transcript-chunk-single", "split-braindump"):
            self.assertEqual(forge_routing.service_name_for(stage, routing={}), "chat", stage)
            self.assertIn(stage, forge_routing.STAGES_HELD_ON_CHAT)

    def test_classification_is_held_despite_clearing_its_case(self):
        # The one stage the report clears that the table deliberately does not
        # route: `vault-organizer` verifies and escalates on `think` already, so
        # sending classification there too would leave one profile reviewing its
        # own work — a trade `verifier-seeded` never measured.
        self.assertEqual(forge_routing.service_name_for("classify-note", routing={}), "chat")
        self.assertIn("classify-note", forge_routing.STAGES_HELD_ON_CHAT)

    def test_the_two_cleanup_directions_disagree_on_purpose(self):
        # The finding this whole module exists for: the same stage wants
        # different models depending on how many people are speaking. Multi-speaker
        # cleanup runs on the small tier; single-speaker cleanup runs on the
        # non-thinking bulk tier (the default), each for its own measured reason.
        self.assertEqual(forge_routing.service_name_for("clean-transcript-chunk-multi", routing={}), "task")
        self.assertEqual(forge_routing.service_name_for("clean-transcript-chunk-single", routing={}), "chat")

    def test_a_settings_override_beats_the_table(self):
        name = forge_routing.service_name_for("connection-judgment", routing={"connection-judgment": "think"})
        self.assertEqual(name, "think")

    def test_an_unknown_service_in_settings_is_ignored_rather_than_obeyed(self):
        # A typo must not route a stage into nothing.
        name = forge_routing.service_name_for("connection-judgment", routing={"connection-judgment": "thnik"})
        self.assertEqual(name, "task")

    def test_an_explicit_argument_beats_everything(self):
        name = forge_routing.service_name_for(
            "connection-judgment", override="chat", routing={"connection-judgment": "think"}
        )
        self.assertEqual(name, "chat")

    def test_overrides_are_read_from_the_agent_directory(self):
        # `split-braindump` runs on `chat` by default now, so overriding it to
        # `think` is what proves the settings override is actually read.
        with tempfile.TemporaryDirectory() as directory:
            agent = Path(directory) / "agent"
            agent.mkdir()
            (agent / "settings.json").write_text(
                json.dumps({"connectedServices": {"routing": {"split-braindump": "think"}}}), encoding="utf-8"
            )
            env = {"PI_FORGE_AGENT_DIR": str(agent)}
            self.assertEqual(forge_routing.service_name_for("split-braindump", routing={}), "chat")
            self.assertEqual(forge_routing.service_name_for("split-braindump", env=env), "think")


class FallbackTests(unittest.TestCase):
    def test_an_unconfigured_task_tier_lands_on_chat_and_says_so(self):
        # The task tier is off by default, so this is what most installs get.
        service = forge_routing.service_for("connection-judgment", env={}, settings={}, routing={})
        self.assertEqual(service["routedTo"], "task")
        self.assertEqual(service["fallback"], "chat")
        self.assertEqual(service["url"], "http://llms:8004/v1/chat/completions")

    def test_a_configured_task_tier_is_actually_used(self):
        settings = {"task": {"enabled": True, "baseUrl": "http://small:7/v1", "model": "small"}}
        service = forge_routing.service_for("connection-judgment", env={}, settings=settings, routing={})
        self.assertEqual(service["url"], "http://small:7/v1/chat/completions")
        self.assertNotIn("fallback", service)

    def test_the_journal_record_separates_where_it_was_sent_from_where_it_ran(self):
        # A stage that quietly ran somewhere other than where it was routed is
        # the failure this module exists to prevent, so both have to be legible
        # in the run directory months later.
        service = forge_routing.service_for("connection-judgment", env={}, settings={}, routing={})
        record = forge_routing.routing_record(service)
        self.assertEqual(record["stage"], "connection-judgment")
        self.assertEqual(record["routedTo"], "task")
        self.assertEqual(record["ranOn"], "chat")

    def test_a_stage_that_ran_where_it_was_sent_records_them_equal(self):
        settings = {"task": {"enabled": True, "baseUrl": "http://small:7/v1", "model": "small"}}
        record = forge_routing.routing_record(
            forge_routing.service_for("connection-judgment", env={}, settings=settings, routing={})
        )
        self.assertEqual(record["routedTo"], "task")
        self.assertEqual(record["ranOn"], "task")


class PinnedEndpointTests(unittest.TestCase):
    """`--base-url` has to keep meaning what it has always meant.

    Without this, pointing a command at one server sent its routed stages to
    whatever `connectedServices` named — so a single-endpoint machine would find
    classification reaching the built-in :8008 that nobody configured.
    """

    def arguments(self, **fields):
        return argparse.Namespace(
            **{"base_url": None, "model": None, "think_url": None, "think_model": None, **fields}
        )

    # No stage is table-routed to `think` any more (all the thinking-tier stages
    # moved to the bulk model), so these force a think route with a
    # `connectedServices.routing` override — a real configuration a user can set,
    # and the pin mechanism these tests cover applies to it exactly the same.
    THINK_ROUTE = {"a-think-routed-stage": "think"}

    def test_one_named_endpoint_holds_every_stage(self):
        args = self.arguments(base_url="http://only:1/v1", base_url_provided=True)
        service = forge_routing.service_for("a-think-routed-stage", args, env={}, settings={}, routing=self.THINK_ROUTE)
        self.assertEqual(service["url"], "http://only:1/v1/chat/completions")
        self.assertEqual(service["routedTo"], "think")
        self.assertTrue(service["pinned"])

    def test_naming_both_endpoints_lets_routing_apply(self):
        args = self.arguments(
            base_url="http://bulk:1/v1", base_url_provided=True, think_url="http://thinker:2/v1"
        )
        service = forge_routing.service_for("a-think-routed-stage", args, env={}, settings={}, routing=self.THINK_ROUTE)
        self.assertEqual(service["url"], "http://thinker:2/v1/chat/completions")
        self.assertNotIn("pinned", service)

    def test_routing_applies_when_nothing_was_named(self):
        args = self.arguments()
        service = forge_routing.service_for("a-think-routed-stage", args, env={}, settings={}, routing=self.THINK_ROUTE)
        self.assertEqual(service["url"], "http://llms:8008/v1/chat/completions")

    def test_an_explicit_override_still_wins_over_the_pin(self):
        args = self.arguments(base_url="http://only:1/v1", base_url_provided=True)
        service = forge_routing.service_for("a-think-routed-stage", args, override="think", env={}, settings={})
        self.assertEqual(service["url"], "http://llms:8008/v1/chat/completions")

    def test_a_disabled_tier_still_degrades_toward_chat_with_arguments_present(self):
        # The args path is a second implementation of the fallback, so it needs
        # its own coverage: the task tier is off by default, and a run that
        # passed arguments must land on chat exactly as one that did not.
        args = self.arguments()
        service = forge_routing.service_for("connection-judgment", args, env={}, settings={})
        self.assertEqual(service["routedTo"], "task")
        self.assertEqual(service["fallback"], "chat")
        self.assertEqual(service["url"], "http://llms:8004/v1/chat/completions")

    def test_a_chat_routed_stage_is_unaffected(self):
        args = self.arguments(base_url="http://only:1/v1", base_url_provided=True)
        service = forge_routing.service_for("some-unmeasured-stage", args, env={}, settings={})
        self.assertEqual(service["url"], "http://only:1/v1/chat/completions")
        self.assertNotIn("pinned", service)


class TableIntegrityTests(unittest.TestCase):
    def test_every_routed_stage_names_a_service_that_exists(self):
        for stage, name in forge_routing.STAGE_SERVICES.items():
            self.assertIn(name, forge_routing.RESOLVERS, stage)

    def test_no_stage_is_both_routed_and_held(self):
        overlap = set(forge_routing.STAGE_SERVICES) & set(forge_routing.STAGES_HELD_ON_CHAT)
        self.assertEqual(overlap, set())

    def test_nothing_is_routed_to_chat_in_the_table(self):
        # `chat` is the default. An explicit entry for it would read as a
        # decision when it is just the absence of one; the held list is where a
        # measured "no" belongs.
        self.assertNotIn("chat", forge_routing.STAGE_SERVICES.values())

    def test_every_table_key_is_a_label_some_call_site_passes(self):
        """A stage name nobody passes is a record that joins to nothing.

        Six entries in the held list once named the eval case rather than the
        `task=` string: `verify-packet` for what `forge_verify` passes as
        `verify`, `clean-document-chunk` for `document-ingest`'s `clean-chunk`,
        `ground-draft` for `vault-capture`'s `draft-note`. Nothing reads the
        held list at runtime, so nothing broke — but the reason a decision was
        made stopped pointing at the code it was about, and an override written
        against one of those names would have silently done nothing.

        `CAPABILITIES_MEASURED` is deliberately exempt: those measure what a
        tier is for, and no call site is expected to pass them.
        """
        import re

        forge = LIB.parent
        sources = [
            path.read_text(encoding="utf-8")
            for path in [*forge.glob("skills/*/scripts/*.py"), *forge.glob("skills/*/scripts/*.mjs"), *LIB.glob("*.py"), *LIB.glob("*.mjs")]
            if path.name not in {"forge_routing.py", "forge-routing.mjs"}
        ]
        def passed_somewhere(label):
            pattern = re.compile(rf'["\'`]{re.escape(label)}["\'`]')
            if any(pattern.search(source) for source in sources):
                return True
            # A corrective retry is built as `f"{stage}-repair"`, so the literal
            # never appears. Accept it when its base stage does and some call
            # site composes the suffix that way.
            base, _, suffix = label.rpartition("-")
            return bool(base) and suffix == "repair" and passed_somewhere(base) and any('-repair"' in source for source in sources)

        declared = {*forge_routing.STAGE_SERVICES, *forge_routing.STAGES_HELD_ON_CHAT}
        for stage in sorted(declared):
            self.assertTrue(
                passed_somewhere(stage),
                f"`{stage}` is in a routing table but no call site passes it as a stage label. "
                f"Either the table should name the label the code uses, or the entry belongs in "
                f"CAPABILITIES_MEASURED.",
            )

    def test_capabilities_are_not_also_stages(self):
        for name in forge_routing.CAPABILITIES_MEASURED:
            self.assertNotIn(name, forge_routing.STAGE_SERVICES, name)
            self.assertNotIn(name, forge_routing.STAGES_HELD_ON_CHAT, name)

    def test_the_javascript_twin_routes_identically(self):
        # Two tables in two languages is the arrangement; two tables that
        # disagree is a bug that would show up as one skill routing a stage
        # somewhere another skill does not.
        import re

        source = (LIB / "forge-routing.mjs").read_text(encoding="utf-8")
        block = re.search(r"STAGE_SERVICES = Object\.freeze\(\{(.*?)\}\);", source, re.DOTALL)
        self.assertIsNotNone(block, "STAGE_SERVICES moved or was renamed in forge-routing.mjs")
        twin = dict(re.findall(r'"([^"]+)":\s*"([^"]+)"', block.group(1)))
        self.assertEqual(twin, forge_routing.STAGE_SERVICES)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Tests for the shared personal-context parser and its privacy gates."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vault_profile as vp
import vault_voice as vv
from vault_schema import UserError

REGISTER = """---
type: system
status: active
domain: meta
subdomain: schemas
capture_type: manual
---

# Personal Context

## Cards

| Card | Tier | Scope | Applies | Triggers | Notes |
| --- | --- | --- | --- | --- | --- |
| `[[Core Identity]]` | `always` | `universal` |  |  | Who I am. |
| `[[Thinkers I Read]]` | `when-relevant` | `universal` |  | `Rorty`, `Madhyamaka` | Reading. |
| `[[People in My Life]]` | `when-relevant` | `owner-authored` | `personal` | `Gillian`, `Kodama` | Route-gated. |
| `[[Mental Health]]` | `when-relevant` | `owner-authored` | `personal/therapy` | `OCD` | Tightly gated. |
| `[[Old Passwords]]` | `on-request` | `owner-authored` |  | `password` | Never automatic. |
"""

CARDS = {
    "Core Identity": "# Core Identity\n\n## Context\n\n- Sociologist; they/them.\n- Lives in the Bay Area.\n",
    "Thinkers I Read": "# Thinkers I Read\n\n## Context\n\n- Rorty, for anti-foundationalism.\n",
    "People in My Life": "# People in My Life\n\n## Context\n\n- Gillian Eorwyn is my spouse.\n\n## Detail\n\n- Never injected.\n",
    "Mental Health": "# Mental Health\n\n## Context\n\n- Moral scrupulosity OCD; ADHD.\n",
    "Old Passwords": "# Old Passwords\n\n## Context\n\n- Not for prompts.\n",
}


def build_vault(root, register=REGISTER, cards=None):
    """A vault with the register filed where the schema routes it."""
    vault = Path(root)
    schemas = vault / "99 Meta" / "99.02 Schemas"
    schemas.mkdir(parents=True, exist_ok=True)
    (schemas / vp.PROFILE_BASENAME).write_text(register, encoding="utf-8")
    context = vault / "01 Personal" / "1.09 Context"
    context.mkdir(parents=True, exist_ok=True)
    for name, text in (CARDS if cards is None else cards).items():
        (context / f"{name}.md").write_text(text, encoding="utf-8")
    return vault


def loaded(root):
    vault = build_vault(root)
    profile, profile_hash, warnings = vp.compiled_profile_for(vault, vault / vp.DEFAULT_PROFILE)
    return vault, profile, profile_hash, warnings


class ParseTests(unittest.TestCase):
    def test_every_column_parses(self):
        cards = vp.parse_profile_note(REGISTER)["cards"]
        self.assertEqual([card["name"] for card in cards][:2], ["Core Identity", "Thinkers I Read"])
        people = next(card for card in cards if card["name"] == "People in My Life")
        self.assertEqual(people["tier"], vp.TIER_RELEVANT)
        self.assertEqual(people["scope"], vv.SCOPE_OWNER)
        self.assertEqual(people["routes"], frozenset({"personal"}))
        self.assertEqual(people["triggers"], ["Gillian", "Kodama"])

    def test_scope_defaults_to_owner_authored(self):
        register = REGISTER.replace("| `[[Core Identity]]` | `always` | `universal` |", "| `[[Core Identity]]` | `always` |  |")
        card = vp.parse_profile_note(register)["cards"][0]
        self.assertEqual(card["scope"], vv.SCOPE_OWNER)

    def test_an_unknown_tier_skips_only_its_own_row(self):
        register = REGISTER.replace("| `always` | `universal` |  |  | Who I am. |", "| `sometimes` | `universal` |  |  | Who I am. |")
        parsed = vp.parse_profile_note(register)
        self.assertNotIn("Core Identity", [card["name"] for card in parsed["cards"]])
        self.assertEqual(len(parsed["cards"]), 4)
        self.assertTrue(any("sometimes" in warning for warning in parsed["warnings"]))

    def test_an_unreadable_applies_value_refuses_the_card(self):
        register = REGISTER.replace("| `personal/therapy` | `OCD` |", "| `personal/therapy/deep/nested` | `OCD` |")
        parsed = vp.parse_profile_note(register)
        self.assertNotIn("Mental Health", [card["name"] for card in parsed["cards"]])
        self.assertTrue(any("Applies" in warning for warning in parsed["warnings"]))

    def test_a_duplicate_card_keeps_the_first_row(self):
        register = REGISTER + "| `[[Core Identity]]` | `always` | `universal` |  |  | Again. |\n"
        parsed = vp.parse_profile_note(register)
        self.assertEqual(sum(1 for card in parsed["cards"] if card["name"] == "Core Identity"), 1)
        self.assertTrue(any("duplicate" in warning for warning in parsed["warnings"]))

    def test_when_relevant_without_triggers_warns(self):
        register = REGISTER.replace("|  | `Rorty`, `Madhyamaka` | Reading. |", "|  |  | Reading. |")
        parsed = vp.parse_profile_note(register)
        self.assertTrue(any("never be selected" in warning for warning in parsed["warnings"]))

    def test_a_missing_cards_table_raises(self):
        with self.assertRaises(UserError):
            vp.parse_profile_note("# Personal Context\n\nNo table here.\n")


class CardTests(unittest.TestCase):
    def test_only_context_bullets_are_read(self):
        facts, warnings = vp.parse_card_note(CARDS["People in My Life"])
        self.assertEqual(facts, ["Gillian Eorwyn is my spouse."])
        self.assertEqual(warnings, [])

    def test_an_indented_bullet_is_dropped(self):
        facts, warnings = vp.parse_card_note("## Context\n\n- Top level.\n  - Nested detail.\n")
        self.assertEqual(facts, ["Top level."])
        self.assertTrue(any("indented" in warning for warning in warnings))

    def test_a_missing_context_section_is_a_warning_not_a_raise(self):
        facts, warnings = vp.parse_card_note("# Card\n\nJust prose.\n")
        self.assertEqual(facts, [])
        self.assertTrue(warnings)

    def test_an_over_budget_card_drops_whole_trailing_bullets(self):
        fact = "x" * 200
        facts, warnings = vp.parse_card_note("## Context\n\n" + "".join(f"- {fact}\n" for _ in range(6)))
        self.assertLess(len(facts), 6)
        self.assertTrue(all(item == fact for item in facts))
        self.assertLessEqual(sum(len(item) + 1 for item in facts), vp.MAX_CARD_CHARS)
        self.assertTrue(any("over" in warning for warning in warnings))


class GateTests(unittest.TestCase):
    """The privacy gates. These are the tests the design exists for."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault, self.profile, _hash, _warnings = loaded(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def names(self, site, material):
        return [card["name"] for card in vp.select_cards(self.profile, material, site)]

    def test_a_route_gated_card_is_refused_when_the_site_knows_no_route(self):
        site = vp.profile_site(vv.CONTEXT_OWNER, stage="classify")
        self.assertIn("Core Identity", self.names(site, "Gillian and Kodama and OCD"))
        self.assertNotIn("People in My Life", self.names(site, "Gillian and Kodama and OCD"))
        self.assertNotIn("Mental Health", self.names(site, "Gillian and Kodama and OCD"))

    def test_a_route_gated_card_is_refused_under_a_foreign_route(self):
        site = vp.profile_site(vv.CONTEXT_OWNER, routes=["work/data-centers"])
        self.assertNotIn("People in My Life", self.names(site, "Gillian said something"))

    def test_a_personal_card_reaches_a_therapy_note_by_ancestor_expansion(self):
        site = vp.profile_site(vv.CONTEXT_OWNER, routes=["personal/therapy"])
        selected = self.names(site, "Gillian and my OCD")
        self.assertIn("People in My Life", selected)
        self.assertIn("Mental Health", selected)

    def test_a_therapy_gated_card_stays_out_of_a_merely_personal_note(self):
        site = vp.profile_site(vv.CONTEXT_OWNER, routes=["personal"])
        selected = self.names(site, "Gillian and my OCD")
        self.assertIn("People in My Life", selected)
        self.assertNotIn("Mental Health", selected)

    def test_source_material_never_sees_an_owner_authored_card(self):
        site = vp.profile_site(vv.CONTEXT_SOURCE, routes=["personal/therapy"])
        selected = self.names(site, "Gillian and Kodama and my OCD and Rorty")
        self.assertEqual(selected, ["Core Identity", "Thinkers I Read"])

    def test_context_none_selects_nothing(self):
        site = vp.profile_site(vv.CONTEXT_NONE, routes=["personal/therapy"])
        self.assertEqual(self.names(site, "Gillian and my OCD"), [])

    def test_the_work_meeting_assertion(self):
        """No owner-authored card may reach a work meeting or a lecture."""
        material = "Gillian mentioned Kodama and my OCD came up, plus Rorty."
        for site in (
            vp.profile_site(vv.CONTEXT_OWNER, routes=["work/data-centers"], stage="summary"),
            vp.profile_site(vv.CONTEXT_SOURCE, routes=["academic"], stage="summary"),
            vp.profile_site(vv.CONTEXT_OWNER, stage="summary"),
        ):
            rendered = "\n".join(
                [vp.profile_prefix(self.profile, site), vp.profile_context(self.profile, site, material)]
            )
            self.assertNotIn("Gillian Eorwyn", rendered)
            self.assertNotIn("scrupulosity", rendered)


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault, self.profile, _hash, _warnings = loaded(self.tmp.name)
        self.site = vp.profile_site(vv.CONTEXT_OWNER, routes=["personal/therapy"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_always_tier_is_selected_with_no_material_at_all(self):
        selected = vp.select_cards(self.profile, "", self.site)
        self.assertEqual([card["name"] for card in selected], ["Core Identity"])

    def test_when_relevant_needs_a_literal_trigger(self):
        self.assertNotIn("Thinkers I Read", [card["name"] for card in vp.select_cards(self.profile, "nothing here", self.site)])
        self.assertIn("Thinkers I Read", [card["name"] for card in vp.select_cards(self.profile, "reading Rorty", self.site)])

    def test_a_trigger_matches_across_case_accents_and_spacing(self):
        selected = [card["name"] for card in vp.select_cards(self.profile, "studying madhya maka today", self.site)]
        self.assertIn("Thinkers I Read", selected)

    def test_a_fuzzy_near_miss_does_not_trigger(self):
        """Deliberate non-reuse of vault_lexicon.similarity: a false positive here
        is a privacy leak, not a missed spelling correction."""
        selected = [card["name"] for card in vp.select_cards(self.profile, "my friend Jillian visited", self.site)]
        self.assertNotIn("People in My Life", selected)

    def test_on_request_is_never_selected_automatically(self):
        selected = [card["name"] for card in vp.select_cards(self.profile, "password password", self.site)]
        self.assertNotIn("Old Passwords", selected)

    def test_the_cap_applies_to_triggered_cards_only(self):
        """The always-tier renders into the system prefix under its own budget, so
        it must not eat slots from the per-item cap. Counting both against one cap
        let two always cards starve selection down to one triggered card."""
        selected = vp.select_cards(self.profile, "Rorty Gillian OCD", self.site, limit=2)
        triggered = [card for card in selected if card["tier"] != vp.TIER_ALWAYS]
        always = [card for card in selected if card["tier"] == vp.TIER_ALWAYS]
        self.assertEqual(len(triggered), 2)
        self.assertEqual(len(always), 1)

    def test_every_triggered_card_survives_under_the_default_cap(self):
        selected = vp.select_cards(self.profile, "Rorty and Gillian and my OCD", self.site)
        names = [card["name"] for card in selected]
        self.assertIn("Thinkers I Read", names)
        self.assertIn("People in My Life", names)
        self.assertIn("Mental Health", names)

    def test_always_sorts_before_triggered_cards(self):
        selected = vp.select_cards(self.profile, "Rorty and Gillian", self.site)
        self.assertEqual(selected[0]["name"], "Core Identity")


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault, self.profile, _hash, _warnings = loaded(self.tmp.name)
        self.site = vp.profile_site(vv.CONTEXT_OWNER, routes=["personal/therapy"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_prefix_holds_only_always_tier_cards(self):
        prefix = vp.profile_prefix(self.profile, self.site)
        self.assertIn("Sociologist", prefix)
        self.assertNotIn("Gillian", prefix)

    def test_the_prefix_is_byte_stable_across_calls(self):
        first = vp.profile_prefix(self.profile, self.site)
        second = vp.profile_prefix(self.profile, vp.profile_site(vv.CONTEXT_OWNER, routes=["personal/therapy"]))
        self.assertEqual(first, second)

    def test_the_context_block_excludes_always_tier_cards(self):
        block = vp.profile_context(self.profile, self.site, "Gillian and Rorty")
        self.assertIn("Gillian", block)
        self.assertNotIn("Sociologist", block)

    def test_a_budget_drops_whole_cards(self):
        block = vp.profile_context(self.profile, self.site, "Gillian and Rorty", budget=40)
        self.assertNotIn("Gillian Eorwyn is my spouse.", block)

    def test_detail_sections_are_never_rendered(self):
        block = vp.profile_context(self.profile, self.site, "Gillian")
        self.assertNotIn("Never injected", block)

    def test_offers_carry_the_matched_triggers(self):
        selected = vp.select_cards(self.profile, "Rorty", self.site)
        offers = vp.profile_offers([card for card in selected if card["name"] == "Thinkers I Read"])
        self.assertEqual(offers[0]["because"], ["Rorty"])


class LoadTests(unittest.TestCase):
    def test_a_card_with_no_note_is_skipped_and_the_rest_compile(self):
        with tempfile.TemporaryDirectory() as root:
            cards = {name: text for name, text in CARDS.items() if name != "Mental Health"}
            vault = build_vault(root, cards=cards)
            profile, _hash, warnings = vp.compiled_profile_for(vault, vault / vp.DEFAULT_PROFILE)
            self.assertNotIn("Mental Health", [card["name"] for card in profile["cards"]])
            self.assertEqual(len(profile["cards"]), 4)
            self.assertTrue(any("Mental Health" in warning for warning in warnings))

    def test_an_ambiguous_card_is_refused_rather_than_guessed(self):
        with tempfile.TemporaryDirectory() as root:
            vault = build_vault(root)
            other = vault / "09 Wiki" / "9.01 Concepts"
            other.mkdir(parents=True)
            (other / "Core Identity.md").write_text("## Context\n\n- Impostor.\n", encoding="utf-8")
            profile, _hash, warnings = vp.compiled_profile_for(vault, vault / vp.DEFAULT_PROFILE)
            self.assertNotIn("Core Identity", [card["name"] for card in profile["cards"]])
            self.assertTrue(any("more than one" in warning for warning in warnings))

    def test_a_backup_copy_in_a_workflow_run_is_not_a_card(self):
        with tempfile.TemporaryDirectory() as root:
            vault = build_vault(root)
            run = vault / "99 Meta" / "99.06 Workflows" / "Transcripts" / "run-1"
            run.mkdir(parents=True)
            (vault / "99 Meta" / "99.06 Workflows" / ".forge-workspace").write_text("", encoding="utf-8")
            (run / "Core Identity.md").write_text("## Context\n\n- Stale backup.\n", encoding="utf-8")
            profile, _hash, warnings = vp.compiled_profile_for(vault, vault / vp.DEFAULT_PROFILE)
            self.assertIn("Core Identity", [card["name"] for card in profile["cards"]])

    def test_an_inbox_copy_is_not_a_card(self):
        with tempfile.TemporaryDirectory() as root:
            vault = build_vault(root)
            inbox = vault / "00 Inbox"
            inbox.mkdir(parents=True)
            (inbox / "Core Identity.md").write_text("## Context\n\n- Unfiled.\n", encoding="utf-8")
            profile, _hash, _warnings = vp.compiled_profile_for(vault, vault / vp.DEFAULT_PROFILE)
            self.assertIn("Core Identity", [card["name"] for card in profile["cards"]])


class DegradationTests(unittest.TestCase):
    """A malformed register costs the layer, never the run."""

    def test_no_register_compiles_to_nothing(self):
        profile, profile_hash, warnings = vp.compiled_profile_for("/nonexistent", None)
        self.assertIsNone(profile)
        self.assertEqual(profile_hash, "none")
        self.assertEqual(warnings, [])

    def test_a_malformed_register_warns_rather_than_raising(self):
        with tempfile.TemporaryDirectory() as root:
            vault = build_vault(root, register="# Personal Context\n\nThe table is gone.\n")
            profile, profile_hash, warnings = vp.compiled_profile_for(vault, vault / vp.DEFAULT_PROFILE)
            self.assertIsNone(profile)
            self.assertEqual(profile_hash, "none")
            self.assertTrue(warnings)

    def test_an_empty_cards_table_warns_rather_than_raising(self):
        register = REGISTER.split("| `[[Core Identity]]`")[0]
        with tempfile.TemporaryDirectory() as root:
            vault = build_vault(root, register=register)
            profile, _hash, warnings = vp.compiled_profile_for(vault, vault / vp.DEFAULT_PROFILE)
            self.assertIsNone(profile)
            self.assertTrue(warnings)

    def test_selection_on_a_missing_profile_is_empty(self):
        site = vp.profile_site(vv.CONTEXT_OWNER, routes=["personal"])
        self.assertEqual(vp.select_cards(None, "Gillian", site), [])
        self.assertEqual(vp.profile_prefix(None, site), "")
        self.assertEqual(vp.profile_context(None, site, "Gillian"), "")


class ResolutionTests(unittest.TestCase):
    def test_the_canonical_path_wins(self):
        with tempfile.TemporaryDirectory() as root:
            vault = build_vault(root)
            self.assertEqual(vp.resolve_profile_path(vault), (vault / vp.DEFAULT_PROFILE).resolve())

    def test_a_register_filed_elsewhere_is_found(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root)
            elsewhere = vault / "99 Meta" / "99.01 Vault Design"
            elsewhere.mkdir(parents=True)
            (elsewhere / vp.PROFILE_BASENAME).write_text(REGISTER, encoding="utf-8")
            self.assertEqual(vp.resolve_profile_path(vault), (elsewhere / vp.PROFILE_BASENAME).resolve())

    def test_two_candidates_outside_the_canonical_path_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root)
            for folder in ("99 Meta/99.01 Vault Design", "09 Wiki/9.01 Concepts"):
                target = vault / folder
                target.mkdir(parents=True)
                (target / vp.PROFILE_BASENAME).write_text(REGISTER, encoding="utf-8")
            with self.assertRaises(vp.AmbiguousProfileError):
                vp.resolve_profile_path(vault)

    def test_ambiguity_is_a_user_error_subclass(self):
        """Callers that only knew the old exception must keep catching this one."""
        self.assertTrue(issubclass(vp.AmbiguousProfileError, UserError))

    def test_an_explicitly_named_missing_register_is_an_error(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(UserError):
                vp.resolve_profile_path(root, raw_profile="nope.md")

    def test_disabled_resolves_to_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            vault = build_vault(root)
            self.assertIsNone(vp.resolve_profile_path(vault, disabled=True))


class DegradingResolutionTests(unittest.TestCase):
    """``resolve_profile_or_warn`` is what the skills call, and it draws the line
    between vault state a run can survive and a command that asked for the wrong
    thing."""

    def test_an_ambiguous_vault_costs_the_layer_not_the_run(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root)
            for folder in ("99 Meta/99.01 Vault Design", "09 Wiki/9.01 Concepts"):
                target = vault / folder
                target.mkdir(parents=True)
                (target / vp.PROFILE_BASENAME).write_text(REGISTER, encoding="utf-8")
            path, warnings = vp.resolve_profile_or_warn(vault)
            self.assertIsNone(path)
            self.assertEqual(len(warnings), 1)
            self.assertIn("more than one", warnings[0])

    def test_a_named_register_that_is_missing_still_raises(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(UserError) as caught:
                vp.resolve_profile_or_warn(root, raw_profile="typo.md")
            self.assertNotIsInstance(caught.exception, vp.AmbiguousProfileError)

    def test_a_healthy_vault_warns_about_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            vault = build_vault(root)
            path, warnings = vp.resolve_profile_or_warn(vault)
            self.assertEqual(path, (vault / vp.DEFAULT_PROFILE).resolve())
            self.assertEqual(warnings, [])

    def test_the_ambiguous_path_compiles_to_an_empty_layer(self):
        """The contract the call sites depend on: a None path is a clean no-layer
        result, so nothing downstream has to special-case the degraded run."""
        with tempfile.TemporaryDirectory() as root:
            profile, profile_hash, warnings = vp.compiled_profile_for(Path(root), None)
            self.assertIsNone(profile)
            self.assertEqual(profile_hash, "none")
            self.assertEqual(warnings, [])


class CompileTests(unittest.TestCase):
    def test_the_cache_is_reused_when_nothing_changed(self):
        with tempfile.TemporaryDirectory() as root:
            vault = build_vault(root)
            cache = vault / ".vault-transcripts" / "cache"
            first, first_hash, _warnings = vp.compiled_profile_for(vault, vault / vp.DEFAULT_PROFILE, cache_dir=cache)
            self.assertTrue((cache / "compiled-profile.json").is_file())
            second, second_hash, _warnings = vp.compiled_profile_for(vault, vault / vp.DEFAULT_PROFILE, cache_dir=cache)
            self.assertEqual(first_hash, second_hash)
            self.assertEqual(first, second)

    def test_editing_a_card_body_invalidates_the_cache(self):
        with tempfile.TemporaryDirectory() as root:
            vault = build_vault(root)
            cache = vault / ".vault-transcripts" / "cache"
            _first, first_hash, _warnings = vp.compiled_profile_for(vault, vault / vp.DEFAULT_PROFILE, cache_dir=cache)
            card = vault / "01 Personal" / "1.09 Context" / "Core Identity.md"
            card.write_text("# Core Identity\n\n## Context\n\n- Now says something else.\n", encoding="utf-8")
            profile, second_hash, _warnings = vp.compiled_profile_for(vault, vault / vp.DEFAULT_PROFILE, cache_dir=cache)
            self.assertNotEqual(first_hash, second_hash)
            self.assertEqual(profile["cards"][0]["facts"], ["Now says something else."])

    def test_editing_the_register_invalidates_the_cache(self):
        with tempfile.TemporaryDirectory() as root:
            vault = build_vault(root)
            cache = vault / ".vault-transcripts" / "cache"
            _first, first_hash, _warnings = vp.compiled_profile_for(vault, vault / vp.DEFAULT_PROFILE, cache_dir=cache)
            register = vault / vp.DEFAULT_PROFILE
            register.write_text(REGISTER.replace("`always`", "`on-request`", 1), encoding="utf-8")
            _profile, second_hash, _warnings = vp.compiled_profile_for(vault, vault / vp.DEFAULT_PROFILE, cache_dir=cache)
            self.assertNotEqual(first_hash, second_hash)


class ReportTests(unittest.TestCase):
    def test_the_digest_counts_by_tier(self):
        with tempfile.TemporaryDirectory() as root:
            _vault, profile, _hash, _warnings = loaded(root)
            digest = vp.profile_digest(profile)
            self.assertEqual(digest["cards"], 5)
            self.assertEqual(digest[vp.TIER_ALWAYS], 1)
            self.assertEqual(digest[vp.TIER_RELEVANT], 3)
            self.assertEqual(digest[vp.TIER_REQUEST], 1)
            self.assertEqual(digest["route_gated"], 2)

    def test_an_absent_profile_digests_to_zeroes(self):
        self.assertEqual(vp.profile_digest(None)["cards"], 0)

    def test_state_records_the_site_mode(self):
        site = vp.profile_site(vv.CONTEXT_OWNER, routes=["personal"])
        state = vp.profile_state("/vault/register.md", "a" * 64, site)
        self.assertEqual(state["profile_context_mode"], vv.CONTEXT_OWNER)
        self.assertEqual(state["profile_hash"], "a" * 16)
        self.assertEqual(state["profile_compiler_version"], vp.COMPILED_PROFILE_VERSION)


if __name__ == "__main__":
    unittest.main(verbosity=2)

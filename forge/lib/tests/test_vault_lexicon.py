#!/usr/bin/env python3
"""Tests for the shared speakers-and-terms lexicon."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vault_lexicon as vl
from vault_schema import UserError

NOTE = """---
type: system
status: active
domain: meta
subdomain: schemas
capture_type: manual
---

# Speakers and Terms

## Terms

| Term | Variants | Kind | Notes |
| --- | --- | --- | --- |
| `Bodhicitta` | `Buddhic chitta`, `Buddhicitta` | `term` | Awakening mind. |
| `CalNEXT` | `Cal Next`, `Cow Next` | `acronym` | |

## Speakers

| Person | Appears | Aliases | Cue |
| --- | --- | --- | --- |
| `[[Marge Anderson]]` | `always` | `Marge` | The other voice in the Slipstream sync. |
| `[[Alexi Miller]]` | `sometimes` | `Alexi` | NBI colleague on CalNEXT calls. |
| `[[Alan K Meier]]` | `never` | | Cited in lectures, never a speaker. |
"""

PERSON_NOTE = """---
type: person
status: active
domain: directory
subdomain: contacts
organization: "[[New Buildings Institute]]"
capture_type: imported
---
## Alexi Miller

**Director of Building Innovation**
**New Buildings Institute**

Dedicated his career to decarbonizing the built environment.
"""

SCHEMA_STUB = {
    "domains": {"directory": {"value": "directory", "number": "8", "label": "Directory"}},
    "subdomains": {
        "directory": {
            "contacts": {"value": "contacts", "domain": "directory", "number": "1", "label": "People — Contacts"}
        }
    },
}


class ParseTests(unittest.TestCase):
    def test_both_tables_parse(self):
        lexicon = vl.parse_lexicon_note(NOTE)
        self.assertEqual([entry["correct"] for entry in lexicon["terms"]], ["Bodhicitta", "CalNEXT"])
        self.assertEqual(lexicon["terms"][1]["category"], "acronym")
        self.assertEqual(lexicon["terms"][0]["variants"], ["Buddhic chitta", "Buddhicitta"])
        by_name = {entry["name"]: entry for entry in lexicon["speakers"]}
        self.assertEqual(by_name["Marge Anderson"]["appears"], "always")
        self.assertEqual(by_name["Marge Anderson"]["aliases"], ["Marge"])
        self.assertEqual(by_name["Alan K Meier"]["appears"], "never")
        self.assertEqual(by_name["Alexi Miller"]["link"], "[[Alexi Miller]]")

    def test_either_section_alone_is_enough(self):
        self.assertEqual(vl.parse_lexicon_note("## Terms\n\n| Term | Variants |\n| --- | --- |\n| `A` | `b` |\n")["speakers"], [])
        speakers_only = "## Speakers\n\n| Person | Appears |\n| --- | --- |\n| `Kim` | `always` |\n"
        self.assertEqual(vl.parse_lexicon_note(speakers_only)["terms"], [])

    def test_a_note_defining_nothing_is_an_error(self):
        with self.assertRaises(UserError):
            vl.parse_lexicon_note("# Speakers and Terms\n\nnothing here yet\n")

    def test_an_unknown_appears_value_is_refused(self):
        note = "## Speakers\n\n| Person | Appears |\n| --- | --- |\n| `Kim` | `sometimes-ish` |\n"
        with self.assertRaises(UserError) as caught:
            vl.parse_lexicon_note(note)
        self.assertIn("always, sometimes, never", str(caught.exception))

    def test_duplicates_are_refused_in_both_tables(self):
        terms = "## Terms\n\n| Term | Variants |\n| --- | --- |\n| `A` | `b` |\n| `a` | `c` |\n"
        with self.assertRaises(UserError):
            vl.parse_lexicon_note(terms)
        people = "## Speakers\n\n| Person | Appears |\n| --- | --- |\n| `Kim` | `always` |\n| `kim` | `never` |\n"
        with self.assertRaises(UserError):
            vl.parse_lexicon_note(people)


class CorrectionTests(unittest.TestCase):
    def setUp(self):
        self.terms = vl.parse_lexicon_note(NOTE)["terms"]

    def test_recorded_variants_are_replaced(self):
        text, rows = vl.apply_corrections("The Cal Next report covers Buddhic chitta.", self.terms)
        self.assertEqual(text, "The CalNEXT report covers Bodhicitta.")
        self.assertEqual({row["correct"] for row in rows}, {"CalNEXT", "Bodhicitta"})

    def test_a_longer_variant_wins_over_a_shorter_one(self):
        entries = [
            vl.normalize_entry({"correct": "Sé Chilbu", "variants": ["Se Chipu"]}),
            vl.normalize_entry({"correct": "Sé", "variants": ["Se"]}),
        ]
        self.assertEqual(vl.apply_corrections("Se Chipu wrote it.", entries)[0], "Sé Chilbu wrote it.")

    def test_whole_word_matching_leaves_substrings_alone(self):
        entries = [vl.normalize_entry({"correct": "Lojong", "variants": ["Lojang"]})]
        self.assertEqual(vl.apply_corrections("Lojanga is a different word.", entries)[0], "Lojanga is a different word.")

    def test_the_note_overrides_the_json_dictionary(self):
        base = [vl.normalize_entry({"correct": "CalNEXT", "variants": ["calnext"], "category": "term"})]
        merged = vl.merge_dictionaries(base, self.terms)
        entry = next(item for item in merged if item["correct"] == "CalNEXT")
        self.assertEqual(entry["category"], "acronym")
        self.assertNotIn("calnext", entry["variants"])


class NearMissTests(unittest.TestCase):
    """The threshold and the first-letter rule were calibrated against 44 real
    mistranscriptions; these pin the behaviour those runs settled on."""

    def setUp(self):
        self.candidates = vl.term_candidates(vl.parse_lexicon_note(NOTE))

    def offers(self, text):
        return {offer["term"]: offer["heardAs"] for offer in vl.near_miss_terms(text, self.candidates)}

    def test_a_novel_mangling_is_offered(self):
        self.assertEqual(self.offers("we talked about Cal Next funding")["CalNEXT"], "Cal Next")

    def test_a_term_split_across_words_is_offered(self):
        self.assertIn("Bodhicitta", self.offers("he explained Bodhi citta at length"))

    def test_a_correctly_spelled_term_is_not_offered(self):
        self.assertEqual(self.offers("Bodhicitta is the awakening mind"), {})

    def test_one_correct_term_never_draws_an_offer_to_become_another(self):
        # "Bodhicitta" and "Bodhisattva" are close enough to match each other.
        candidates = vl.term_candidates(
            {
                "terms": [
                    vl.normalize_entry({"correct": "Bodhicitta", "variants": []}),
                    vl.normalize_entry({"correct": "Bodhisattva", "variants": []}),
                ],
                "speakers": [],
            }
        )
        self.assertEqual(vl.near_miss_terms("Bodhicitta is the awakening mind", candidates), [])

    def test_unrelated_prose_draws_no_offers(self):
        self.assertEqual(self.offers("the gasket is cracked around the rim and it leaks"), {})

    def test_a_homophone_is_offered_with_the_words_it_was_heard_as(self):
        # "call next" really is how an engine renders "CalNEXT", so it is
        # offered even where the sense is wrong. The model is the second gate:
        # showing it what was heard is what lets it decline.
        self.assertEqual(self.offers("the call next week is cancelled")["CalNEXT"], "call next")

    def test_a_different_opening_sound_is_never_a_near_miss(self):
        candidates = vl.term_candidates({"terms": [vl.normalize_entry({"correct": "Lojong", "variants": []})], "speakers": []})
        # "jong" scores 0.80 against "Lojong" -- above the ratio, below the rule.
        self.assertEqual(vl.near_miss_terms("the jong was loud", candidates), [])

    def test_roster_names_are_offered_as_spellings(self):
        offers = self.offers("I spoke to Alexei Miller about it")
        self.assertEqual(offers.get("Alexi Miller"), "Alexei Miller")

    def test_a_never_speaker_still_gets_their_name_spelled_right(self):
        self.assertIn("Alan K Meier", self.offers("we should cite Alan K Meyer on standby power"))

    def test_a_first_name_said_alone_is_corrected(self):
        # People are named in passing far more often than in full.
        self.assertEqual(self.offers("I talked to Alexei about the numbers")["Alexi"], "Alexei")

    def test_a_name_part_does_not_match_an_ordinary_word(self):
        # "Marge" reaches 0.73 against "margin" -- over the term bar, under the
        # name bar that name candidates carry.
        self.assertEqual(self.offers("the margin was thin this quarter"), {})

    def test_a_correctly_spelled_name_draws_nothing(self):
        self.assertEqual(self.offers("I talked to Alexi Miller about it"), {})


class RosterTests(unittest.TestCase):
    def setUp(self):
        self.speakers = vl.parse_lexicon_note(NOTE)["speakers"]

    def names(self, text):
        return [entry["name"] for entry in vl.candidate_speakers(text, self.speakers)]

    def test_always_is_offered_with_no_mention_at_all(self):
        self.assertEqual(self.names("just me thinking out loud about nothing"), ["Marge Anderson"])

    def test_sometimes_needs_a_mention(self):
        self.assertNotIn("Alexi Miller", self.names("nothing relevant here"))
        self.assertIn("Alexi Miller", self.names("Alexi said he would send the numbers"))

    def test_a_mangled_name_still_finds_the_person(self):
        self.assertIn("Alexi Miller", self.names("Alexei sent the numbers over"))

    def test_never_is_excluded_even_when_named(self):
        self.assertNotIn("Alan K Meier", self.names("Alan K Meier wrote the standby power paper"))

    def test_a_common_word_near_a_name_does_not_summon_them(self):
        # "margin" reaches 0.73 against "Marge" -- under the stricter name bar.
        speakers = [entry for entry in self.speakers if entry["appears"] != "always"]
        self.assertEqual(vl.candidate_speakers("the margin was thin this quarter", speakers), [])

    def test_offers_drop_what_is_empty(self):
        offer = vl.speaker_offers(vl.candidate_speakers("nothing", self.speakers))[0]
        self.assertEqual(offer["name"], "Marge Anderson")
        self.assertTrue(offer["recurring"])
        self.assertNotIn("role", offer)

    def test_canonical_name_and_link_resolve_an_alias(self):
        lexicon = {"speakers": self.speakers}
        self.assertEqual(vl.canonical_name(lexicon, "Alexi"), "Alexi Miller")
        self.assertEqual(vl.canonical_link(lexicon, "Alexi"), "[[Alexi Miller]]")
        self.assertIsNone(vl.canonical_name(lexicon, "Someone Else"))


class CompileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp.name)
        schemas = self.vault / "99 Meta" / "99.02 Schemas"
        schemas.mkdir(parents=True)
        (schemas / vl.LEXICON_BASENAME).write_text(NOTE, encoding="utf-8")
        people = self.vault / "08 Directory" / "8.01 People — Contacts"
        people.mkdir(parents=True)
        (people / "Alexi Miller.md").write_text(PERSON_NOTE, encoding="utf-8")
        (people / "Kathy Kuntz.md").write_text(
            "---\ntype: person\ndomain: directory\nsubdomain: contacts\n---\n## Kathy Kuntz\n\n**Director**\n",
            encoding="utf-8",
        )
        self.addCleanup(self.temp.cleanup)

    def test_the_default_path_is_found(self):
        path = vl.resolve_lexicon_path(self.vault)
        self.assertEqual(path.name, vl.LEXICON_BASENAME)

    def test_directory_notes_supply_roles_and_join_the_roster(self):
        lexicon, _hash = vl.compiled_lexicon_for(self.vault, vl.resolve_lexicon_path(self.vault), schema=SCHEMA_STUB)
        by_name = {entry["name"]: entry for entry in lexicon["speakers"]}
        # The overlay row keeps its tier and gains the note's role.
        self.assertEqual(by_name["Alexi Miller"]["appears"], "sometimes")
        self.assertEqual(by_name["Alexi Miller"]["role"], "Director of Building Innovation, New Buildings Institute")
        # Someone with no overlay row joins at the default tier.
        self.assertEqual(by_name["Kathy Kuntz"]["appears"], "sometimes")
        self.assertEqual(by_name["Kathy Kuntz"]["role"], "Director")

    def test_a_role_never_carries_a_pipe_into_a_table(self):
        people = self.vault / "08 Directory" / "8.01 People — Contacts"
        (people / "Dion Abril.md").write_text(
            "---\ntype: person\ndomain: directory\nsubdomain: contacts\n---\n## Dion Abril\n\n"
            "**Executive Administrator | WSC**\n",
            encoding="utf-8",
        )
        lexicon, _hash = vl.compiled_lexicon_for(self.vault, vl.resolve_lexicon_path(self.vault), schema=SCHEMA_STUB)
        role = next(entry["role"] for entry in lexicon["speakers"] if entry["name"] == "Dion Abril")
        self.assertNotIn("|", role)

    def test_the_cache_is_reused_and_invalidated_by_an_edit(self):
        cache = self.vault / "cache"
        path = vl.resolve_lexicon_path(self.vault)
        _first, first_hash = vl.compiled_lexicon_for(self.vault, path, schema=SCHEMA_STUB, cache_dir=cache)
        cached = json.loads((cache / "compiled-lexicon.json").read_text(encoding="utf-8"))
        self.assertEqual(cached["lexicon_hash"], first_hash)
        _second, second_hash = vl.compiled_lexicon_for(self.vault, path, schema=SCHEMA_STUB, cache_dir=cache)
        self.assertEqual(first_hash, second_hash)
        path.write_text(NOTE.replace("Cow Next", "Kal Next"), encoding="utf-8")
        _third, third_hash = vl.compiled_lexicon_for(self.vault, path, schema=SCHEMA_STUB, cache_dir=cache)
        self.assertNotEqual(first_hash, third_hash)

    def test_a_new_person_note_invalidates_the_cache_too(self):
        cache = self.vault / "cache"
        path = vl.resolve_lexicon_path(self.vault)
        _first, first_hash = vl.compiled_lexicon_for(self.vault, path, schema=SCHEMA_STUB, cache_dir=cache)
        people = self.vault / "08 Directory" / "8.01 People — Contacts"
        (people / "New Person.md").write_text(
            "---\ntype: person\ndomain: directory\nsubdomain: contacts\n---\n## New Person\n\n**Analyst**\n",
            encoding="utf-8",
        )
        _second, second_hash = vl.compiled_lexicon_for(self.vault, path, schema=SCHEMA_STUB, cache_dir=cache)
        self.assertNotEqual(first_hash, second_hash)

    def test_load_lexicon_merges_the_json_dictionary_underneath(self):
        dictionary = self.vault / "dictionary.json"
        dictionary.write_text(
            json.dumps({"version": 1, "entries": [{"correct": "Lojong", "variants": ["Lojang"]}]}),
            encoding="utf-8",
        )
        lexicon, fingerprint = vl.load_lexicon(
            self.vault, vl.resolve_lexicon_path(self.vault), schema=SCHEMA_STUB, dictionary_path=dictionary
        )
        self.assertEqual({entry["correct"] for entry in lexicon["terms"]}, {"Bodhicitta", "CalNEXT", "Lojong"})
        self.assertTrue(fingerprint)

    def test_no_note_and_no_dictionary_is_no_lexicon(self):
        lexicon, fingerprint = vl.load_lexicon(self.vault, None, schema=None, dictionary_path=None)
        self.assertIsNone(lexicon)
        self.assertIsNone(fingerprint)

    def test_a_vault_with_only_person_notes_still_gets_a_roster(self):
        lexicon, _fingerprint = vl.load_lexicon(self.vault, None, schema=SCHEMA_STUB)
        self.assertEqual(lexicon["terms"], [])
        self.assertIn("Alexi Miller", {entry["name"] for entry in lexicon["speakers"]})

    def test_digest_counts_each_tier(self):
        lexicon, _hash = vl.compiled_lexicon_for(self.vault, vl.resolve_lexicon_path(self.vault), schema=SCHEMA_STUB)
        digest = vl.lexicon_digest(lexicon)
        self.assertEqual(digest["terms"], 2)
        self.assertEqual(digest["always"], 1)
        self.assertEqual(digest["never"], 1)
        self.assertEqual(digest["speakers"], len(lexicon["speakers"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)

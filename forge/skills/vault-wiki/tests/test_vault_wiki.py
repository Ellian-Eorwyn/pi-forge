#!/usr/bin/env python3

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "vault-wiki.py"
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("vault_wiki_skill", SCRIPT)
skill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(skill)

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
import run_state  # noqa: E402
import vault_wiki as vw  # noqa: E402
from vault_schema import UserError, sha256_bytes, split_frontmatter  # noqa: E402

SPECS = skill.kind_specs()
# An opt-in corpus check against a vault that actually holds wiki notes. It is
# the only test here that needs one, and the path is nobody's business but the
# person running it, so it comes from the environment rather than the repo.
_CORPUS = os.environ.get("PI_FORGE_WIKI_CORPUS", "")
REAL_VAULT = Path(_CORPUS).expanduser() if _CORPUS else None

SCHEMA_NOTE = """---
type: system
status: active
domain: meta
subdomain: schemas
---

# Vault Schema

## Approved properties

| Property | Required | Shape | Definition |
| --- | --- | --- | --- |
| `type` | yes | controlled scalar | Kind. |
| `status` | yes | controlled scalar | Lifecycle. |
| `domain` | yes | controlled scalar | Broad area. |
| `subdomain` | no | controlled scalar | Nested area. |
| `parent` | no | quoted wikilink | Nearest hub. |
| `related` | no | list of quoted wikilinks | Cross-cutting links. |
| `source_kind` | no | controlled scalar | Source format. |
| `capture_type` | no | controlled scalar | How it arrived. |

## Note types

- `note` — General note.
- `concept` — A named idea.
- `place` — A location.
- `event` — A happening.
- `work` — A named work.
- `person` — A named person.
- `source` — A source artifact.
- `template` — A reusable template.

## Status values

- `active` — Current.
- `raw` — Unprocessed.
- `complete` — Finished.

## Domains

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `personal` | `1` | `Personal` | Personal material. |
| `craft` | `2` | `Craft` | Making things. |
| `wiki` | `9` | `Wiki` | Cross-cutting entity notes. |
| `meta` | `99` | `Meta` | System notes. |

## Subdomains

### personal

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `journal` | `1` | `Journal` | Dated records. |

### wiki

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `concepts` | `1` | `Concepts` | Named ideas. |
| `practices` | `2` | `Practices` | Named methods. |
| `places` | `3` | `Places` | Locations. |
| `events` | `4` | `Events` | Happenings. |
| `terms` | `5` | `Terms` | Jargon. |
| `works` | `6` | `Works` | Named works. |
| `figures` | `7` | `Figures` | Named figures. |

### meta

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `schemas` | `2` | `Schemas` | Schema notes. |
| `templates` | `3` | `Templates` | Reusable note templates. |

## Project registry

| Approved value | Domain | Subdomain | Number | Definition |
| --- | --- | --- | --- | --- |
| `"[[Pi Forge]]"` | `craft` |  | `90` | Tooling. |

## Source kinds

- `book` — A book.
- `generated` — A generated research artifact.

## Capture types

- `manual` — Typed directly.
- `generated` — Produced by a tool.

## Legacy normalization map

| Legacy input | Canonical output |
| --- | --- |
| `type: daily` | `type: note` |

## Folder routing

Paths are derived from the registries above.
"""

FIGURE_NOTE = """---
type: person
status: active
domain: wiki
subdomain: figures
related:
  - "[[Situated Knowledge]]"
  - "[[Karen Barad]]"
capture_type: generated
---

# Donna Haraway

## Associated Concepts
- [[Situated Knowledge]]

## Colleague Thinkers
- [[Karen Barad]]
- [[Sandra Harding]]

## Notes
**mine:** read the 1988 essay first.
"""


def make_vault():
    # Resolved because macOS puts the temp dir behind a /var -> /private/var
    # symlink, and the script resolves its vault path the same way.
    root = Path(tempfile.mkdtemp()).resolve()
    (root / "99 Meta" / "99.02 Schemas").mkdir(parents=True)
    (root / "99 Meta" / "99.02 Schemas" / "0.00 Vault Schema.md").write_text(SCHEMA_NOTE, encoding="utf-8")
    (root / "09 Wiki" / "9.07 Figures").mkdir(parents=True)
    (root / "09 Wiki" / "9.07 Figures" / "Donna Haraway.md").write_text(FIGURE_NOTE, encoding="utf-8")
    (root / "09 Wiki" / "9.01 Concepts").mkdir(parents=True)
    return root


def install_args(vault, **overrides):
    return SimpleNamespace(**{"vault": str(vault), "schema": None, "dry_run": False, "force": False, **overrides})


class TemplateInstallTests(unittest.TestCase):
    def setUp(self):
        self.vault = make_vault()
        self.addCleanup(shutil.rmtree, self.vault, ignore_errors=True)
        self.folder = self.vault / "99 Meta" / "99.03 Templates"

    def test_installs_all_seven_and_then_no_ops(self):
        result = skill.command_template_install(install_args(self.vault))
        self.assertEqual(result["data"]["written"], 7)
        self.assertEqual(len(list(self.folder.glob("Wiki *.md"))), 7)
        again = skill.command_template_install(install_args(self.vault))
        self.assertEqual(again["data"]["written"], 0)
        self.assertEqual({entry["action"] for entry in again["data"]["operations"]}, {"unchanged"})

    def test_dry_run_writes_nothing(self):
        result = skill.command_template_install(install_args(self.vault, dry_run=True))
        self.assertEqual(result["data"]["written"], 7)
        self.assertFalse(self.folder.exists())

    def test_refuses_to_overwrite_a_modified_template(self):
        skill.command_template_install(install_args(self.vault))
        target = self.folder / "Wiki Figure.md"
        mine = target.read_text(encoding="utf-8") + "\nmy own note to self\n"
        target.write_text(mine, encoding="utf-8")
        result = skill.command_template_install(install_args(self.vault))
        self.assertEqual(result["data"]["written"], 0)
        self.assertIn("--force", " ".join(result["warnings"]))
        self.assertEqual(target.read_text(encoding="utf-8"), mine)

    def test_force_overwrites_a_modified_template(self):
        skill.command_template_install(install_args(self.vault))
        target = self.folder / "Wiki Figure.md"
        target.write_text("changed\n", encoding="utf-8")
        result = skill.command_template_install(install_args(self.vault, force=True))
        self.assertEqual(result["data"]["written"], 1)
        self.assertIn("{{title}}", target.read_text(encoding="utf-8"))

    def test_installed_templates_satisfy_both_callers(self):
        skill.command_template_install(install_args(self.vault))
        from vault_schema import compiled_schema_for, resolve_schema_path

        schema, _hash = compiled_schema_for(self.vault, resolve_schema_path(self.vault, None))
        self.assertTrue(vw.require_wiki_templates(self.vault, schema, vw.WIKI_KINDS, specs=SPECS))
        for kind in vw.WIKI_KINDS:
            legacy = vw.inspect_wiki_template(
                self.vault, schema, kind,
                required_fields=vw.WIKI_TEMPLATE_FIELDS,
                known_fields=SPECS[kind]["placeholders"],
            )
            self.assertTrue(legacy["ok"], (kind, legacy["errors"]))


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.spec = SPECS["figure"]
        self.item = {
            "id": "w-001",
            "title": "Donna Haraway",
            "missingSections": [],
            "relatedLinks": [],
        }
        self.sources = [
            {
                "sourceId": "s-1",
                "url": "https://en.wikipedia.org/wiki/Donna_Haraway",
                "label": "Wikipedia",
                "title": "Donna Haraway",
                "archivePath": "sources/s-1.txt",
            }
        ]
        self.text = 'Donna Haraway (born 1944) wrote "A Cyborg Manifesto" in 1985 about situated knowledges.'
        self.index = {"situated knowledge": {"title": "Situated Knowledge"}}

    def check(self, sections, citations=(), sources=None, allow_uncited=False):
        draft = {"id": "w-001", "sections": dict(sections), "citations": list(citations)}
        used = self.sources if sources is None else sources
        return skill.check_draft(
            draft, self.item, self.spec, used,
            [self.text] if used else [], "", self.index, allow_uncited,
        )

    def test_a_clean_draft_passes(self):
        problems = self.check(
            {"_lead": "Donna Haraway (born 1944) is a scholar.[^1]"},
            [{"label": "1", "sourceId": "s-1", "locator": ""}],
        )
        self.assertEqual(problems, [])

    def test_a_url_this_run_never_fetched_is_refused(self):
        problems = self.check({"_lead": "See https://example.com/invented for more."})
        self.assertTrue(any("never fetched" in message for message in problems))

    def test_a_footnote_without_a_citation_entry_is_refused(self):
        problems = self.check({"_lead": "A claim.[^7]"})
        self.assertTrue(any("[^7] has no citation entry" in message for message in problems))

    def test_a_citation_never_referenced_is_refused(self):
        problems = self.check({"_lead": "A claim."}, [{"label": "1", "sourceId": "s-1", "locator": ""}])
        self.assertTrue(any("never referenced" in message for message in problems))

    def test_a_citation_naming_an_unknown_source_is_refused(self):
        problems = self.check({"_lead": "A claim.[^1]"}, [{"label": "1", "sourceId": "s-99", "locator": ""}])
        self.assertTrue(any("unknown source s-99" in message for message in problems))

    def test_a_quote_absent_from_the_source_is_refused(self):
        problems = self.check({"_lead": 'She wrote "the vertigo of total systemic theory" in her essay.'})
        self.assertTrue(any("absent from every archived source" in message for message in problems))

    def test_a_quote_present_in_the_source_passes(self):
        problems = self.check({"_lead": 'She wrote "A Cyborg Manifesto" in 1985.'})
        self.assertEqual(problems, [])

    def test_an_invented_year_is_refused(self):
        problems = self.check({"_lead": "She was born in 1907."})
        self.assertTrue(any("states year 1907" in message for message in problems))

    def test_a_sourced_year_passes(self):
        self.assertEqual(self.check({"_lead": "She was born in 1944."}), [])

    def test_grounded_checks_are_skipped_without_sources(self):
        problems = self.check({"_lead": "She was born in 1907."}, sources=[], allow_uncited=True)
        self.assertFalse(any("states year" in message for message in problems))

    def test_over_budget_prose_is_refused(self):
        problems = self.check({"position": "x" * 400})
        self.assertTrue(any("over the 320 budget" in message for message in problems))

    def test_too_many_bullets_is_refused(self):
        problems = self.check({"key_ideas": "\n".join(f"- point {index}" for index in range(9))})
        self.assertTrue(any("over the 5 budget" in message for message in problems))

    def test_a_bullet_section_written_as_prose_is_refused(self):
        problems = self.check({"key_ideas": "One long paragraph with no bullets at all."})
        self.assertTrue(any("should be bullets" in message for message in problems))

    def test_a_prose_section_written_as_bullets_is_refused(self):
        problems = self.check({"position": "- a\n- b"})
        self.assertTrue(any("should be prose" in message for message in problems))

    def test_a_missing_definition_is_refused_when_the_note_has_none(self):
        self.item["missingSections"] = [vw.LEAD_SECTION]
        problems = self.check({"key_ideas": "- a\n- b\n- c"})
        self.assertTrue(any("did not supply one" in message for message in problems))


class NormalizationTests(unittest.TestCase):
    def test_bare_lines_become_bullets(self):
        text = skill.normalize_section("key_ideas", "first idea\nsecond idea", SPECS["figure"])
        self.assertEqual(text, "- first idea\n- second idea")

    def test_existing_bullets_are_left_alone(self):
        text = skill.normalize_section("key_ideas", "- a\n- b", SPECS["figure"])
        self.assertEqual(text, "- a\n- b")

    def test_a_single_paragraph_is_not_bulleted(self):
        text = skill.normalize_section("key_ideas", "one long paragraph", SPECS["figure"])
        self.assertEqual(text, "one long paragraph")

    def test_prose_sections_are_untouched(self):
        text = skill.normalize_section("position", "a\nb", SPECS["figure"])
        self.assertEqual(text, "a\nb")

    def test_markers_move_tight_against_the_sentence(self):
        self.assertEqual(skill.tidy_footnote_markers("Boulder [^1]."), "Boulder.[^1]")
        self.assertEqual(skill.tidy_footnote_markers("knowledges. [^1]"), "knowledges.[^1]")
        self.assertEqual(skill.tidy_footnote_markers("a claim [^2]"), "a claim[^2]")

    def test_repeated_markers_are_reduced_to_one_per_section(self):
        sections = {"key_ideas": "- a[^1]\n- b[^1]\n- c[^1]", "position": "d[^1] e[^1]"}
        skill.dedupe_footnote_markers(sections)
        self.assertEqual(sections["key_ideas"].count("[^1]"), 1)
        self.assertEqual(sections["position"].count("[^1]"), 1)

    def test_distinct_markers_all_survive(self):
        sections = {"key_ideas": "- a[^1]\n- b[^2]"}
        skill.dedupe_footnote_markers(sections)
        self.assertIn("[^1]", sections["key_ideas"])
        self.assertIn("[^2]", sections["key_ideas"])

    def test_unresolved_links_are_unwrapped_and_reported(self):
        sections = {"key_ideas": "- [[Cyborg]] and [[Situated Knowledge]]"}
        dropped = skill.drop_unresolved_links(
            sections, {"title": "Donna Haraway"}, {"situated knowledge": {}}
        )
        self.assertEqual(dropped, ["Cyborg"])
        self.assertIn("- Cyborg and [[Situated Knowledge]]", sections["key_ideas"])


class SourceRelevanceTests(unittest.TestCase):
    """Every case here is a page a real run actually fetched."""

    def test_subject_key_drops_the_gloss(self):
        self.assertEqual(skill.subject_key("Śūnyatā, Emptiness"), "śūnyatā")
        self.assertEqual(skill.subject_key("Bruno Latour"), "bruno latour")

    def test_a_page_titled_for_the_subject_is_about_it(self):
        self.assertEqual(skill.source_relevance("Bruno Latour", "Bruno Latour - Wikipedia"), "about")
        self.assertEqual(
            skill.source_relevance("Two Truths Doctrine, Saṃvṛti and Paramārtha", "Two Truths Doctrine"), "about"
        )

    def test_a_reworded_entry_title_still_counts_as_about(self):
        # The SEP names this entry differently from the vault note.
        self.assertEqual(
            skill.source_relevance("Two Truths Doctrine, Saṃvṛti and Paramārtha", "The Theory of Two Truths in India"),
            "about",
        )

    def test_dash_variants_do_not_defeat_the_match(self):
        # Wikipedia uses an en-dash where the note uses a hyphen.
        self.assertEqual(skill.source_relevance("Actor-Network Theory, ANT", "Actor–network theory"), "about")

    def test_a_page_that_merely_cites_the_subject_is_rejected(self):
        for subject, page in (
            ("Bruno Latour", "Phenomenological Approaches to Ethics and Information Technology"),
            ("Alison Jaggar", "Feminist Ethics"),
            ("Sheila Jasanoff", "The Social Dimensions of Scientific Knowledge"),
            ("God Trick", "God and Other Ultimates"),
        ):
            self.assertIsNone(skill.source_relevance(subject, page), (subject, page))

    def test_a_broader_entry_that_discusses_the_subject_covers_it(self):
        text = "situated knowledge is central here. Situated knowledge resists the view from nowhere. On situated knowledge, see below."
        self.assertEqual(
            skill.source_relevance("Situated Knowledge", "Feminist Epistemology and Philosophy of Science", text),
            "covers",
        )

    def test_a_passing_mention_is_not_enough_to_cover(self):
        self.assertIsNone(
            skill.source_relevance("Situated Knowledge", "Feminist Epistemology", "situated knowledge appears once")
        )

    def test_a_missing_title_without_text_is_rejected(self):
        self.assertIsNone(skill.source_relevance("Bruno Latour", ""))
        self.assertIsNone(skill.source_relevance("Bruno Latour", None))

    def test_a_single_word_subject_needs_a_substring_match(self):
        self.assertEqual(skill.source_relevance("Madhyamaka", "Madhyamaka - Wikipedia"), "about")
        self.assertIsNone(skill.source_relevance("Madhyamaka", "Buddhist Philosophy"))


class SectionCoercionTests(unittest.TestCase):
    def test_an_array_becomes_bullets(self):
        self.assertEqual(skill.coerce_section(["first", "second"]), "- first\n- second")

    def test_leading_markers_in_array_entries_are_not_doubled(self):
        self.assertEqual(skill.coerce_section(["- first", "* second"]), "- first\n- second")

    def test_empty_entries_are_dropped(self):
        self.assertEqual(skill.coerce_section(["first", "", "  "]), "- first")

    def test_a_string_is_passed_through(self):
        self.assertEqual(skill.coerce_section("  prose  "), "prose")

    def test_anything_else_is_empty(self):
        self.assertEqual(skill.coerce_section(None), "")
        self.assertEqual(skill.coerce_section(42), "")


class TitleMatchTests(unittest.TestCase):
    """Every case is a real page a resolver actually returned."""

    def test_an_exact_name_scores_highest(self):
        self.assertEqual(skill.title_match_score("madhyamaka", "Madhyamaka"), 1.0)

    def test_a_name_inside_a_longer_title_still_scores(self):
        self.assertGreater(skill.title_match_score("bruno latour", "Bruno Latour - Wikipedia"), 0.5)

    def test_a_reworded_entry_title_scores_on_word_overlap(self):
        self.assertGreaterEqual(
            skill.title_match_score("two truths doctrine", "The Theory of Two Truths in India"), skill.TITLE_OVERLAP
        )
        self.assertGreaterEqual(
            skill.title_match_score("two truths doctrine", "two truths in India, theory of"), skill.TITLE_OVERLAP
        )

    def test_a_short_title_inside_the_subject_does_not_match(self):
        # The SEP entry on `truth` is not the entry on the two truths doctrine,
        # even though "truth" is a substring of "two truths doctrine".
        self.assertEqual(skill.title_match_score("two truths doctrine", "truth"), 0.0)

    def test_weak_overlap_does_not_match(self):
        self.assertEqual(skill.title_match_score("god trick", "God and Other Ultimates"), 0.0)
        self.assertEqual(skill.title_match_score("alison jaggar", "Feminist Ethics"), 0.0)

    def test_best_candidate_picks_the_strongest_not_the_first(self):
        # An alphabetical index offers `truth` long before `two truths in India`.
        candidates = [("truth", "u/truth"), ("two truths in India, theory of", "u/twotruths")]
        best = skill.best_candidate("Two Truths Doctrine, Saṃvṛti and Paramārtha", candidates)
        self.assertEqual(best["url"], "u/twotruths")

    def test_best_candidate_returns_none_when_nothing_matches(self):
        self.assertIsNone(skill.best_candidate("God Trick", [("God and Other Ultimates", "u/god")]))


class ResolveSourceUrlTests(unittest.TestCase):
    """Which of the two resolution paths a source takes, and who does the matching.

    Index parsing and TLS handling used to be tested here. Both moved into
    web-research when the native resolvers did — one implementation now serves
    every caller, and Node reaches these hosts on machines where this
    interpreter's ``urllib`` cannot verify a certificate at all.
    """

    def setUp(self):
        self.calls = []

    def fake_candidates(self, result):
        def call(subject, provider, limit=6):
            self.calls.append((subject, provider, limit))
            return result

        return call

    def test_a_source_with_a_provider_is_resolved_through_the_registry(self):
        entry = {"id": "sep", "site": "plato.stanford.edu", "provider": "sep"}
        candidates = [
            # An alphabetical index offers `truth` long before the entry that is
            # actually about the subject; the matching stays on this side of the
            # subprocess precisely so that ordering does not decide it.
            ("Truth", "https://plato.stanford.edu/entries/truth/"),
            ("The Theory of Two Truths in India", "https://plato.stanford.edu/entries/twotruths-india/"),
        ]
        with mock.patch.object(skill, "reference_candidates", self.fake_candidates(candidates)):
            located = skill.resolve_source_url(Path("/nonexistent"), "Two Truths Doctrine, Saṃvṛti and Paramārtha", entry)
        self.assertEqual(located["url"], "https://plato.stanford.edu/entries/twotruths-india/")
        # The gloss after the comma is not part of the name to look up.
        self.assertEqual(self.calls, [("two truths doctrine", "sep", 6)])

    def test_an_unreachable_provider_resolves_to_nothing_rather_than_raising(self):
        entry = {"id": "iep", "site": "iep.utm.edu", "provider": "iep"}
        with mock.patch.object(skill, "reference_candidates", self.fake_candidates([])):
            self.assertIsNone(skill.resolve_source_url(Path("/nonexistent"), "Madhyamaka", entry))

    def test_a_source_without_a_provider_pins_searxng(self):
        # `site:` is SearXNG syntax. Routed freely it would be sent to providers
        # that treat it as a literal string to search for.
        entry = {"id": "britannica", "site": "britannica.com", "provider": None, "resolve": {"method": "search"}}
        seen = []

        def fake_run(command, arguments, output_dir, timeout=None):
            seen.append((command, arguments))
            return {"results": [{"url": "https://www.britannica.com/topic/anicca", "domain": "britannica.com", "title": "anicca"}]}

        with mock.patch.object(skill, "run_web_research", fake_run):
            located = skill.resolve_source_url(Path("/nonexistent"), "Impermanence", entry)
        self.assertEqual(located["url"], "https://www.britannica.com/topic/anicca")
        self.assertEqual(seen[0][0], "search")
        self.assertIn("--providers", seen[0][1])
        self.assertEqual(seen[0][1][seen[0][1].index("--providers") + 1], "searxng")

    def test_a_failure_reported_by_the_subprocess_is_kept_reportable(self):
        # A swallowed error is indistinguishable from "this subject has no entry",
        # which is the most misleading thing this pipeline could report.
        skill._HTTP_FAILURES.clear()
        completed = SimpleNamespace(
            stdout=json.dumps({"provider": "iep", "candidates": [], "failures": [{"host": "iep.utm.edu", "error": "request timed out after 30000ms"}]}),
            stderr="",
        )
        with mock.patch.object(skill, "web_research_script", lambda: Path("/tmp/web-research.mjs")):
            with mock.patch.object(skill.subprocess, "run", lambda *a, **k: completed):
                self.assertEqual(skill.reference_candidates("Madhyamaka", "iep"), [])
        self.assertEqual(skill.http_failures(), ["iep.utm.edu: request timed out after 30000ms"])
        skill._HTTP_FAILURES.clear()


class CitationCollapseTests(unittest.TestCase):
    def test_labels_for_the_same_source_and_locator_merge(self):
        draft = {
            "sections": {"_lead": "A.[^1]", "origin": "B.[^2]"},
            "citations": [
                {"label": "1", "sourceId": "s-1", "locator": ""},
                {"label": "2", "sourceId": "s-1", "locator": ""},
            ],
        }
        skill.collapse_duplicate_citations(draft)
        self.assertEqual([c["label"] for c in draft["citations"]], ["1"])
        self.assertEqual(draft["sections"]["origin"], "B.[^1]")

    def test_different_locators_stay_distinct(self):
        draft = {
            "sections": {"_lead": "A.[^1]", "origin": "B.[^2]"},
            "citations": [
                {"label": "1", "sourceId": "s-1", "locator": "§2"},
                {"label": "2", "sourceId": "s-1", "locator": "§4"},
            ],
        }
        skill.collapse_duplicate_citations(draft)
        self.assertEqual([c["label"] for c in draft["citations"]], ["1", "2"])

    def test_different_sources_stay_distinct(self):
        draft = {
            "sections": {"_lead": "A.[^1]", "origin": "B.[^2]"},
            "citations": [
                {"label": "1", "sourceId": "s-1", "locator": ""},
                {"label": "2", "sourceId": "s-2", "locator": ""},
            ],
        }
        skill.collapse_duplicate_citations(draft)
        self.assertEqual([c["label"] for c in draft["citations"]], ["1", "2"])


class CitationPruningTests(unittest.TestCase):
    def test_an_unreferenced_citation_is_dropped(self):
        draft = {
            "sections": {"_lead": "A claim.[^2]"},
            "citations": [
                {"label": "1", "sourceId": "s-1", "locator": ""},
                {"label": "2", "sourceId": "s-2", "locator": ""},
            ],
        }
        self.assertEqual(skill.prune_unused_citations(draft), ["1"])
        self.assertEqual([entry["label"] for entry in draft["citations"]], ["2"])

    def test_a_referenced_citation_survives(self):
        draft = {"sections": {"_lead": "A claim.[^1]"}, "citations": [{"label": "1", "sourceId": "s-1", "locator": ""}]}
        self.assertEqual(skill.prune_unused_citations(draft), [])
        self.assertEqual(len(draft["citations"]), 1)

    def test_a_marker_with_no_entry_is_not_repaired_here(self):
        draft = {"sections": {"_lead": "A claim.[^9]"}, "citations": []}
        self.assertEqual(skill.prune_unused_citations(draft), [])


class LinkSectionTests(unittest.TestCase):
    def test_link_sections_are_additive(self):
        body = split_frontmatter(FIGURE_NOTE.encode("utf-8"))["body"]
        rendered = skill.render_link_section(body, SPECS["figure"], "colleague_thinkers", ["Karen Barad"])
        # Sandra Harding is in the section but not in `related`; it must survive.
        self.assertIn("[[Sandra Harding]]", rendered)
        self.assertIn("[[Karen Barad]]", rendered)

    def test_related_is_split_by_target_type(self):
        body = split_frontmatter(FIGURE_NOTE.encode("utf-8"))["body"]
        item = {"relatedLinks": ["Situated Knowledge", "Karen Barad"], "title": "Donna Haraway"}
        index = {
            "situated knowledge": {"type": "concept", "title": "Situated Knowledge"},
            "karen barad": {"type": "person", "title": "Karen Barad"},
        }
        filled = skill.link_sections_for(item, SPECS["figure"], body, index)
        self.assertIn("[[Situated Knowledge]]", filled["associated_concepts"])
        self.assertIn("[[Karen Barad]]", filled["colleague_thinkers"])
        self.assertNotIn("[[Karen Barad]]", filled["associated_concepts"])


class LeadCalloutTests(unittest.TestCase):
    """The opening definition renders as an `[!abstract]` callout.

    A wiki note is skimmed before it is read, and the lead is the sentence that
    decides whether to keep reading.
    """

    def test_the_lead_is_wrapped_in_an_abstract_callout(self):
        rendered = skill.render_lead_callout("Agnotology is the study of produced ignorance.")
        self.assertEqual(rendered, "> [!abstract]\n> Agnotology is the study of produced ignorance.")

    def test_wrapping_is_idempotent(self):
        # The lead is rewritten on every expansion, so a second pass must not nest.
        once = skill.render_lead_callout("A definition.")
        self.assertEqual(skill.render_lead_callout(once), once)

    def test_a_multi_line_lead_keeps_every_line_quoted(self):
        rendered = skill.render_lead_callout("First line.\nSecond line.")
        self.assertEqual(rendered, "> [!abstract]\n> First line.\n> Second line.")

    def test_an_empty_lead_stays_empty(self):
        self.assertEqual(skill.render_lead_callout("   "), "")

    def test_unwrapping_recovers_the_prose(self):
        self.assertEqual(skill.unwrap_lead_callout("> [!abstract]\n> A definition."), "A definition.")
        self.assertEqual(skill.unwrap_lead_callout("A plain lead."), "A plain lead.")

    def test_build_filled_wraps_the_lead_and_leaves_other_sections_alone(self):
        draft = {"sections": {"_lead": "A definition.", "key_points": "- One point."}, "citations": []}
        item = {"relatedLinks": [], "title": "Something"}
        filled = skill.build_filled(draft, item, SPECS["concept"], "# Something\n", [], {})
        self.assertEqual(filled["_lead"], "> [!abstract]\n> A definition.")
        self.assertEqual(filled["key_points"], "- One point.")

    def test_every_shipped_template_leads_with_the_callout(self):
        for name in vw.WIKI_TEMPLATE_NAMES.values():
            path = Path(skill.__file__).resolve().parents[1] / "references" / "templates" / name
            self.assertIn("> [!abstract]\n> {{summary}}", path.read_text(encoding="utf-8"), name)


class ApplyRevertTests(unittest.TestCase):
    def setUp(self):
        self.vault = make_vault()
        self.addCleanup(shutil.rmtree, self.vault, ignore_errors=True)
        self.note = self.vault / "09 Wiki" / "9.07 Figures" / "Donna Haraway.md"
        self.before = self.note.read_bytes()
        self.spec = SPECS["figure"]
        body = split_frontmatter(self.before)["body"]
        self.merged = vw.merge_sections(
            body,
            self.spec,
            {
                "_lead": "Donna Haraway (born 1944) is an American scholar.[^1]",
                "key_ideas": "- Cyborg feminism.\n- Situated knowledges.",
                "sources": "- [Wikipedia](https://en.wikipedia.org/wiki/Donna_Haraway)",
                vw.FOOTNOTES_SECTION: '[^1]: Wikipedia, "Donna Haraway".',
            },
        )
        self.run_dir = self.vault / ".vault-wiki" / "runs" / "test-run"
        self.run_dir.mkdir(parents=True)
        self.manifest = [
            {
                "id": "w-001",
                "path": "09 Wiki/9.07 Figures/Donna Haraway.md",
                "title": "Donna Haraway",
                "kind": "figure",
                "action": "update",
                "sha256Before": sha256_bytes(self.before),
                "bodyAfter": self.merged,
                "sources": [{"sourceId": "s-1", "url": "https://en.wikipedia.org/wiki/Donna_Haraway"}],
                "uncited": False,
                "verdict": "ok",
                "reason": "",
            },
            {
                "id": "w-002",
                "path": "09 Wiki/9.07 Figures/Missing.md",
                "title": "Missing",
                "kind": "figure",
                "action": "update",
                "sha256Before": "0" * 64,
                "bodyAfter": "# Missing\n\nx\n",
                "sources": [],
                "uncited": True,
                "verdict": "flag",
                "reason": "unsupported",
            },
        ]
        run_state.atomic_write_json(self.run_dir / "proposals.json", self.manifest)
        state = run_state.create_run_state(
            skill.WORKFLOW, "expand", {"vault": str(self.vault)}, {}, phase="proposed"
        )
        state["proposalsSha256"] = run_state.configuration_fingerprint(self.manifest)
        run_state.initialize_run_state(self.run_dir, state)

    def args(self, **overrides):
        return SimpleNamespace(
            **{
                "vault": str(self.vault), "run": str(self.run_dir), "accept": None,
                "accept_batch": False, "reject": None, "dry_run": False, **overrides,
            }
        )

    def test_accept_batch_skips_flagged_and_uncited(self):
        result = skill.command_apply(self.args(accept_batch=True))
        self.assertEqual(result["data"]["accepted"], ["w-001"])
        self.assertEqual(result["data"]["results"]["updated"], 1)

    def test_backup_is_written_before_the_note(self):
        skill.command_apply(self.args(accept="w-001"))
        backup = self.run_dir / "backup" / "09 Wiki/9.07 Figures/Donna Haraway.md"
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_bytes(), self.before)

    def test_apply_preserves_frontmatter_and_owner_notes(self):
        skill.command_apply(self.args(accept="w-001"))
        after = self.note.read_text(encoding="utf-8")
        self.assertIn("capture_type: generated", after)
        self.assertIn('- "[[Situated Knowledge]]"', after)
        self.assertIn("**mine:** read the 1988 essay first.", after)
        self.assertIn("[[Sandra Harding]]", after)

    def test_dry_run_writes_nothing(self):
        result = skill.command_apply(self.args(accept="w-001", dry_run=True))
        self.assertEqual(result["data"]["results"]["updated"], 1)
        self.assertEqual(self.note.read_bytes(), self.before)

    def test_reapply_is_a_no_op(self):
        skill.command_apply(self.args(accept="w-001"))
        again = skill.command_apply(self.args(accept="w-001"))
        self.assertEqual(again["data"]["results"]["updated"], 0)
        self.assertEqual([entry["action"] for entry in again["data"]["operations"]], ["already-applied"])

    def test_refuses_an_uncited_proposal_by_name(self):
        with self.assertRaises(UserError) as caught:
            skill.command_apply(self.args(accept="w-002"))
        self.assertIn("uncited", str(caught.exception))

    def test_refuses_unknown_ids(self):
        with self.assertRaises(UserError) as caught:
            skill.command_apply(self.args(accept="w-404"))
        self.assertIn("unknown proposal ids", str(caught.exception))

    def test_refuses_an_id_both_accepted_and_rejected(self):
        with self.assertRaises(UserError):
            skill.command_apply(self.args(accept="w-001", reject="w-001"))

    def test_refuses_a_tampered_manifest(self):
        tampered = json.loads((self.run_dir / "proposals.json").read_text(encoding="utf-8"))
        tampered[0]["bodyAfter"] = "# Donna Haraway\n\nsomething else\n"
        run_state.atomic_write_json(self.run_dir / "proposals.json", tampered)
        with self.assertRaises(UserError) as caught:
            skill.command_apply(self.args(accept="w-001"))
        self.assertIn("has changed", str(caught.exception))

    def test_refuses_a_note_edited_since_review(self):
        self.note.write_bytes(self.before + b"\nedited by hand\n")
        result = skill.command_apply(self.args(accept="w-001"))
        self.assertEqual(result["data"]["results"]["skipped"], 1)
        self.assertIn("changed since review", " ".join(result["warnings"]))

    def test_needs_an_explicit_decision(self):
        with self.assertRaises(UserError):
            skill.command_apply(self.args())

    def test_revert_restores_the_original_bytes(self):
        skill.command_apply(self.args(accept="w-001"))
        self.assertNotEqual(self.note.read_bytes(), self.before)
        result = skill.command_revert(self.args())
        self.assertEqual(result["data"]["restored"], 1)
        self.assertEqual(self.note.read_bytes(), self.before)

    def test_revert_leaves_a_note_edited_after_apply_alone(self):
        skill.command_apply(self.args(accept="w-001"))
        mine = self.note.read_bytes() + b"\nmy later edit\n"
        self.note.write_bytes(mine)
        result = skill.command_revert(self.args())
        self.assertIn("edited after this run applied", " ".join(result["warnings"]))
        self.assertEqual(self.note.read_bytes(), mine)

    def test_revert_without_writes_is_harmless(self):
        result = skill.command_revert(self.args())
        self.assertEqual(result["data"]["restored"], 0)


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.vault = make_vault()
        self.addCleanup(shutil.rmtree, self.vault, ignore_errors=True)
        from vault_schema import compiled_schema_for, resolve_schema_path

        self.schema_path = resolve_schema_path(self.vault, None)
        self.schema, _hash = compiled_schema_for(self.vault, self.schema_path)

    def select(self, **overrides):
        options = {
            "kinds": ("figure",), "titles": [], "only_empty": False, "limit": None,
            **overrides,
        }
        return skill.select_notes(
            self.vault, self.schema, self.schema_path, options["kinds"],
            options["titles"], options["only_empty"], options["limit"], SPECS,
        )

    def test_finds_the_figure_note(self):
        items = self.select()
        self.assertEqual([item["title"] for item in items], ["Donna Haraway"])
        self.assertIn(vw.LEAD_SECTION, items[0]["missingSections"])

    def test_titles_restrict_the_selection(self):
        self.assertEqual(self.select(titles=["Nobody"]), [])
        self.assertEqual(len(self.select(titles=["Donna Haraway"])), 1)

    def test_only_empty_keeps_an_incomplete_note(self):
        self.assertEqual(len(self.select(only_empty=True)), 1)

    def test_limit_caps_the_selection(self):
        self.assertEqual(len(self.select(limit=1)), 1)

    def test_related_links_are_parsed(self):
        self.assertEqual(self.select()[0]["relatedLinks"], ["Situated Knowledge", "Karen Barad"])


class KindArgumentTests(unittest.TestCase):
    def test_defaults_and_all(self):
        self.assertEqual(skill.parse_kinds(None), vw.DEFAULT_WIKI_KINDS)
        self.assertEqual(skill.parse_kinds("all"), vw.WIKI_KINDS)

    def test_explicit_list_is_deduplicated_in_order(self):
        self.assertEqual(skill.parse_kinds("figure,concept,figure"), ("figure", "concept"))

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(UserError):
            skill.parse_kinds("figure,sandwich")


@unittest.skipUnless(
    REAL_VAULT is not None and REAL_VAULT.is_dir(),
    "set PI_FORGE_WIKI_CORPUS to a vault holding wiki notes to run the corpus check",
)
class RealCorpusTests(unittest.TestCase):
    """The strongest available check: every real wiki note survives a merge."""

    FILL = {
        "concept": {"_lead": "L.", "key_points": "- a\n- b", "sources": "- [x](https://x)", vw.FOOTNOTES_SECTION: "[^1]: x."},
        "figure": {"_lead": "L.", "key_ideas": "- a\n- b", "sources": "- [x](https://x)", vw.FOOTNOTES_SECTION: "[^1]: x."},
    }

    def test_every_wiki_note_merges_without_touching_unmanaged_content(self):
        checked = 0
        for folder, kind in (("9.01 Concepts", "concept"), ("9.07 Figures", "figure")):
            spec = SPECS[kind]
            for path in sorted((REAL_VAULT / "09 Wiki" / folder).glob("*.md")):
                split = split_frontmatter(path.read_bytes())
                if split["malformed"]:
                    continue
                body = split["body"]
                checked += 1
                self.assertEqual(vw.assemble(vw.parse_sections(body)), body, path.name)
                merged = vw.merge_sections(body, spec, self.FILL[kind])
                vw.assert_only_managed_changed(body, merged, spec)
                self.assertEqual(vw.merge_sections(merged, spec, self.FILL[kind]), merged, path.name)
        self.assertGreater(checked, 100, "expected the live vault to hold hundreds of wiki notes")


if __name__ == "__main__":
    unittest.main()

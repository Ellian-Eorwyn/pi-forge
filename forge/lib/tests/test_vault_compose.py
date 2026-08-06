#!/usr/bin/env python3
"""Tests for the source set, the grounding checks, and the note renderer.

The grounding half is the load-bearing part: it is the only thing standing
between "a note composed from several sources" and "a note that made things up",
so most of what follows asserts that a specific with no source is caught and a
specific with one is not.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vault_compose as vc
import vault_format as vf
from vault_schema import UserError, parse_schema_note

FORMAT = (Path(__file__).resolve().parents[1] / "vault-format" / "note-format.md").read_text(encoding="utf-8")

SCHEMA = """---
type: system
status: active
domain: meta
subdomain: schemas
capture_type: manual
---

# Vault Schema

## Core invariants

- Only properties listed under **Approved properties** may appear in frontmatter.

## Approved properties

| Property | Required | Shape | Definition |
| --- | --- | --- | --- |
| `type` | yes | controlled scalar | Kind. |
| `status` | yes | controlled scalar | Lifecycle. |
| `domain` | yes | controlled scalar | Broad area. |
| `subdomain` | no | controlled scalar | Nested area. |
| `related` | no | list of quoted wikilinks | Related links. |
| `source_kind` | conditional | controlled scalar | Source kind. |
| `capture_type` | no | controlled scalar | Capture type. |
| `processed_by` | no | list | Automated workflows that transformed this note. |

### Property constraints

- `source_kind` is required when `type: source` and forbidden for other types.

## Canonical frontmatter

```yaml
---
type: note
---
```

## Note types

- `note` — General note.
- `source` — External source.
- `journal` — Journal note.
- `task` — Something to do.
- `draft` — Something being written.

## Status values

- `raw` — Unprocessed.
- `active` — Active.

## Domains

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `personal` | `1` | `Personal` | Personal material. |
| `meta` | `99` | `Meta` | System notes. |

## Subdomains

### personal

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `home` | `1` | `Home` | Home matters. |

### meta

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `schemas` | `2` | `Schemas` | Schema notes. |

## Project registry

| Approved value | Domain | Subdomain | Number | Definition |
| --- | --- | --- | --- | --- |
| `"[[Pi Forge]]"` | `personal` | `home` | `1` | Local agent harness. |

## Source kinds

- `book` — Book.

## Capture types

- `manual` — Typed.
- `generated` — Made by a script, agent, or model.
- `voice` — Voice memo.

## Folder routing

### Derived names

```text
domain-folder(domain):
  <pad2(domain.number)> <domain.label>
```

### Derived destination paths

```text
domain only:
  domain-folder(domain)/
```

## Inbox processing contract

1. Read this schema.

### Content preservation

- Preserve body.

## Legacy normalization map

| Legacy input | Canonical output |
| --- | --- |
| `type: daily` | `type: journal` |
"""


def sources(*pairs):
    """A set from ``(label, text)`` pairs, or from full unit kwargs dicts."""
    units = []
    for entry in pairs:
        if isinstance(entry, dict):
            units.append(vc.source_unit(**entry))
        else:
            label, text = entry
            units.append(vc.source_unit(vc.KIND_TRANSCRIPT, label, text))
    return vc.source_set(units)


class SourceSetTests(unittest.TestCase):
    def test_ids_are_assigned_and_the_fingerprint_covers_content(self):
        first = sources(("a", "The gasket leaks."), ("b", "Order a replacement."))
        self.assertEqual([unit["id"] for unit in first["units"]], ["s-0001", "s-0002"])
        same = sources(("a", "The gasket leaks."), ("b", "Order a replacement."))
        self.assertEqual(first["fingerprint"], same["fingerprint"])
        changed = sources(("a", "The gasket leaks."), ("b", "Order two replacements."))
        self.assertNotEqual(first["fingerprint"], changed["fingerprint"])

    def test_relabelling_a_source_does_not_invalidate_a_run(self):
        """The fingerprint covers what a unit says, not what it is called."""
        first = sources(("a", "The gasket leaks."))
        relabelled = sources(("the recording", "The gasket leaks."))
        self.assertEqual(first["fingerprint"], relabelled["fingerprint"])

    def test_an_empty_unit_is_refused(self):
        with self.assertRaises(UserError):
            vc.source_unit(vc.KIND_CHAT, "empty", "   ")
        with self.assertRaises(UserError):
            vc.source_set([])

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(UserError):
            vc.source_unit("telepathy", "a hunch", "something I recall")

    def test_duplicate_ids_are_refused(self):
        with self.assertRaises(UserError):
            vc.source_set(
                [
                    vc.source_unit(vc.KIND_CHAT, "a", "one", unit_id="s-1"),
                    vc.source_unit(vc.KIND_CHAT, "b", "two", unit_id="s-1"),
                ]
            )

    def test_naming_an_unknown_id_is_refused(self):
        with self.assertRaises(UserError):
            vc.set_text(sources(("a", "one")), ["s-9999"])

    def test_a_source_set_round_trips(self):
        original = sources(("a", "The gasket leaks."))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sources.json"
            vc.dump_source_set(original, path)
            self.assertEqual(vc.load_source_set(path), original)


class GroundingTests(unittest.TestCase):
    SET = sources(
        ("the morning memo", "The gasket leaks and Gillian says the kettle is fine."),
        ("the afternoon memo", "Marcus recommended a supplier for the brackets."),
    )

    def test_a_name_in_any_source_is_grounded(self):
        found = vc.ungrounded_specifics(self.SET, "The gasket leaks, and Gillian says the kettle is fine.")
        self.assertEqual(found["names"], [])

    def test_a_name_in_no_source_is_caught(self):
        found = vc.ungrounded_specifics(self.SET, "The gasket leaks, and Priya found a supplier.")
        self.assertEqual(found["names"], ["Priya"])

    def test_a_name_from_an_uncited_source_is_caught(self):
        """This is the whole reason `cited_ids` exists: a section may not borrow a
        name from a source it never claimed to rest on, or a note whose sections
        are each plausible is collectively a collage."""
        body = "The gasket leaks, and Marcus recommended a supplier."
        self.assertEqual(vc.ungrounded_specifics(self.SET, body)["names"], [])
        narrowed = vc.ungrounded_specifics(self.SET, body, cited_ids=["s-0001"])
        self.assertEqual(narrowed["names"], ["Marcus"])

    def test_a_sentence_opening_capital_is_uncertain_rather_than_invented(self):
        found = vc.ungrounded_specifics(self.SET, "The gasket leaks. Priya found a supplier.")
        self.assertEqual(found["names"], [])
        self.assertEqual(found["uncertain_names"], ["Priya"])

    def test_a_derived_word_is_grounded_by_its_stem(self):
        found = vc.ungrounded_specifics(sources(("a", "I need to order a gasket.")), "Ordering a gasket this week.")
        self.assertEqual(found["names"], [])

    def test_a_diacritic_is_a_correction_not_an_invention(self):
        """A source saying "Jose" and a note writing "José" is the note being
        spelled properly. `vault-transcripts` learned this; capture never had it."""
        found = vc.ungrounded_specifics(sources(("a", "Jose fixed the gasket.")), "José fixed the gasket.")
        self.assertEqual(found["names"], [])

    def test_a_url_grounds_only_from_a_unit_that_carries_it(self):
        carrying = sources(
            {
                "kind": vc.KIND_WEB_CLAIM,
                "label": "a claim",
                "text": "Gaskets fail at the seam.",
                "url": "https://example.com/gaskets",
            }
        )
        body = "See https://example.com/gaskets for parts."
        self.assertEqual(vc.ungrounded_specifics(carrying, body)["links"], [])
        self.assertEqual(
            vc.ungrounded_specifics(sources(("a", "Gaskets fail at the seam.")), body)["links"],
            ["https://example.com/gaskets"],
        )

    def test_a_wikilink_grounds_only_from_a_unit_that_carries_it(self):
        """New with the source set: capture could never check these, because a
        braindump names no vault notes."""
        linked = sources(
            {
                "kind": vc.KIND_VAULT_NOTE,
                "label": "Pi Forge",
                "text": "The agent runs on local models.",
                "wikilink": "[[Pi Forge]]",
            }
        )
        self.assertEqual(vc.ungrounded_specifics(linked, "See [[Pi Forge]] for the stack.")["wikilinks"], [])
        found = vc.ungrounded_specifics(linked, "See [[Some Other Note]] for the stack.")
        self.assertEqual(found["wikilinks"], ["Some Other Note"])

    def test_a_piped_or_anchored_wikilink_resolves_to_its_target(self):
        linked = sources(
            {
                "kind": vc.KIND_VAULT_NOTE,
                "label": "Pi Forge",
                "text": "The agent runs on local models.",
                "wikilink": "[[Pi Forge]]",
            }
        )
        found = vc.ungrounded_specifics(linked, "See [[Pi Forge#Stack|the stack]].")
        self.assertEqual(found["wikilinks"], [])

    def test_spelled_out_numbers_cover_digits(self):
        self.assertEqual(vc.ungrounded_specifics(sources(("a", "I need three brackets.")), "Buy 3 brackets.")["numbers"], [])
        self.assertEqual(vc.ungrounded_specifics(sources(("a", "I need brackets.")), "Buy 7 brackets.")["numbers"], ["7"])


class DroppedUnitTests(unittest.TestCase):
    SET = sources(
        ("the coding tool memo", "A qualitative coding tool that applies a codebook consistently to interviews."),
        ("the groceries memo", "Order yogurt and some other grocery things for the week."),
    )

    def test_a_unit_that_reached_the_note_is_not_reported(self):
        body = (
            "A qualitative coding tool applying a codebook consistently to interview transcripts. "
            "Also: order yogurt and other groceries for the week."
        )
        self.assertEqual(vc.dropped_units(self.SET, body), [])

    def test_a_unit_that_was_silently_omitted_is_reported(self):
        """The failure fan-in actually produces: a day's log built from five of six
        recordings, with nothing about it looking wrong."""
        body = "A qualitative coding tool applying a codebook consistently to interview transcripts."
        dropped = vc.dropped_units(self.SET, body)
        self.assertEqual([entry["id"] for entry in dropped], ["s-0002"])
        self.assertEqual(dropped[0]["label"], "the groceries memo")

    def test_the_whole_set_can_score_well_while_a_member_is_dropped(self):
        """Which is exactly why `coverage_ratio` alone is not enough."""
        body = "A qualitative coding tool applying a codebook consistently to interview transcripts."
        self.assertGreater(vc.coverage_ratio(vc.set_text(self.SET), [body]), 0.4)
        self.assertTrue(vc.dropped_units(self.SET, body))


class RenderNoteTests(unittest.TestCase):
    def setUp(self):
        self.fmt = vf.parse_format(FORMAT)
        self.schema = parse_schema_note(SCHEMA)
        self.metadata = {"type": "note", "status": "raw", "domain": "personal", "capture_type": "generated"}

    def render(self, blocks, **kwargs):
        return vc.render_note(self.fmt, self.schema, self.metadata, blocks, **kwargs)

    def test_blocks_are_emitted_in_the_declared_order_whatever_order_they_arrive_in(self):
        text = self.render(
            {
                "provenance": {"title": "How this was made", "lines": ["Composed from two memos."]},
                "body": ["The gasket leaks."],
                "title": "Gasket",
                "summary": {"title": None, "lines": ["A leaking gasket."]},
            }
        )
        self.assertLess(text.index("# Gasket"), text.index("[!summary]"))
        self.assertLess(text.index("[!summary]"), text.index("The gasket leaks."))
        self.assertLess(text.index("The gasket leaks."), text.index("[!provenance]"))
        self.assertEqual(vc.check_grammar(self.fmt, text), [])

    def test_folding_comes_from_the_registry(self):
        text = self.render(
            {
                "title": "Gasket",
                "summary": {"title": None, "lines": ["A leaking gasket."]},
                "provenance": {"title": "Provenance", "lines": ["Composed."]},
            }
        )
        self.assertIn("> [!summary]\n", text)
        self.assertIn("> [!provenance]- Provenance", text)

    def test_an_owner_block_is_refused(self):
        with self.assertRaises(UserError) as caught:
            self.render({"title": "Gasket", "notes": ["mine"]})
        self.assertIn("owner-authored", str(caught.exception))

    def test_an_undeclared_block_is_refused(self):
        with self.assertRaises(UserError):
            self.render({"title": "Gasket", "epilogue": ["the end"]})

    def test_a_note_needs_a_title(self):
        with self.assertRaises(UserError):
            self.render({"summary": {"title": None, "lines": ["orphaned"]}})

    def test_body_sections_become_headings(self):
        text = self.render(
            {"title": "Day", "body": [{"heading": "Morning (~09:36)", "lines": ["Thought about coding tools."]}]}
        )
        self.assertIn("## Morning (~09:36)", text)

    def test_sources_and_footnotes_land_at_the_end(self):
        text = self.render(
            {"title": "Gasket", "body": ["Prose."], "sources": ["[[Pi Forge]]"]},
            footnotes=[("1", "A footnote.")],
        )
        self.assertLess(text.index("Prose."), text.index("## Sources"))
        self.assertLess(text.index("## Sources"), text.index("[^1]: A footnote."))
        self.assertTrue(text.rstrip().endswith("[^1]: A footnote."))

    def test_a_vault_with_no_declared_grammar_says_so(self):
        bare = {"callouts": {}, "blocks": [], "shapes": {}, "never": []}
        with self.assertRaises(UserError) as caught:
            vc.render_note(bare, self.schema, self.metadata, {"title": "Gasket"})
        self.assertIn("Block order", str(caught.exception))


class CheckGrammarTests(unittest.TestCase):
    def setUp(self):
        self.fmt = vf.parse_format(FORMAT)

    def test_two_level_one_headings_are_caught(self):
        findings = vc.check_grammar(self.fmt, "# One\n\nProse.\n\n# Two\n")
        self.assertIn("exactly one level-one heading", findings[0][1])

    def test_an_unregistered_callout_is_caught(self):
        findings = vc.check_grammar(self.fmt, "# Note\n\n> [!sparkle] Look\n> here\n")
        self.assertTrue(any("not in the vault's registry" in message for _, message in findings))

    def test_a_stock_callout_is_left_alone(self):
        findings = vc.check_grammar(self.fmt, "# Note\n\n> [!tip] Handy\n> advice\n")
        self.assertEqual(findings, [])

    def test_the_owner_section_is_caught(self):
        findings = vc.check_grammar(self.fmt, "# Note\n\nProse.\n\n## Notes\n\nMine.\n")
        self.assertTrue(any("never written, never read" in message for _, message in findings))

    def test_blocks_out_of_order_are_caught(self):
        out_of_order = "# Note\n\n> [!provenance]- How\n> made\n\n> [!summary]\n> the lead\n"
        findings = vc.check_grammar(self.fmt, out_of_order)
        self.assertTrue(any("out of the declared order" in message for _, message in findings))

    def test_inline_html_is_caught(self):
        findings = vc.check_grammar(self.fmt, '# Note\n\n<span style="color: red">no</span>\n')
        self.assertTrue(any("inline HTML" in message for _, message in findings))

    def test_a_clean_note_passes(self):
        clean = "# Note\n\n> [!summary]\n> The lead.\n\nProse about the thing.\n\n## Sources\n\n- [[Pi Forge]]\n"
        self.assertEqual(vc.check_grammar(self.fmt, clean), [])

    def test_syntax_inside_a_fence_is_not_a_violation(self):
        """A note that documents syntax is full of it. Checked raw, the vault's own
        format note reports two level-one headings and inline HTML -- in the note
        that forbids both."""
        documenting = (
            "# Note\n\nHow a note starts:\n\n```\n# Title\n> [!sparkle] not real\n```\n\n"
            "Never write `<span style=` in a note.\n"
        )
        self.assertEqual(vc.check_grammar(self.fmt, documenting), [])

    def test_the_vaults_own_policy_notes_pass(self):
        """The strongest available fixture: the note declaring the grammar has to
        satisfy it."""
        self.assertEqual(vc.check_grammar(self.fmt, FORMAT), [])


if __name__ == "__main__":
    unittest.main()

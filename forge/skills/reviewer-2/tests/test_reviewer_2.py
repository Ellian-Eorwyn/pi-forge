#!/usr/bin/env python3
"""Tests for the reviewer-2 skill.

The battery that matters most is the round trip: for every awkward article
shape, rendering the comments in and stripping them back out has to return the
original body byte for byte. That invariant is the skill's only real promise.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reviewer-2.py"
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "lib"))
spec = importlib.util.spec_from_file_location("reviewer_2", SCRIPT)
reviewer_2 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reviewer_2)
r2 = reviewer_2


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
- `draft` — Something being written.

## Status values

- `raw` — Unprocessed.
- `active` — Active.

## Domains

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `academic` | `5` | `Academic` | Scholarly material. |
| `meta` | `99` | `Meta` | System notes. |

## Subdomains

### academic

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `sociology` | `1` | `Sociology` | Sociology. |

### meta

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `schemas` | `2` | `Schemas` | Schema notes. |

## Project registry

| Approved value | Domain | Subdomain | Number | Definition |
| --- | --- | --- | --- | --- |
| `"[[Pi Forge]]"` | `academic` | `sociology` | `1` | Local agent harness. |

## Source kinds

- `book` — Book.

## Capture types

- `manual` — Typed.
- `generated` — Made by a script, agent, or model.

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

ARTICLE = """---
type: draft
status: active
domain: academic
---

# Boundary Work in Amateur Astronomy

## Introduction

Amateur astronomers occupy an unusual position in the sociology of science. They
produce data that professionals use, yet most accounts of scientific boundary
work treat them as an audience rather than as participants.

This article argues that the boundary between amateur and professional astronomy
is maintained conversationally, in the everyday talk of observing sessions,
rather than through formal credentialing.

## Methods

I attended eleven observing sessions over two years and recorded the talk at
each one. Participants were mostly retired men, which the analysis treats as
incidental.

## Findings

Most participants described professional astronomers as colleagues. The talk at
observing sessions constantly polices who counts as serious.
"""


def comments_payload(body_sha, path, **overrides):
    payload = {
        "schema_version": 1,
        "article": {"path": path, "body_sha256": body_sha},
        "research_runs": [],
        "comments": [
            {
                "id": "r-001",
                "anchor": "b-004",
                "category": "logic",
                "severity": "major",
                "critique": "The claim that the boundary is maintained conversationally is asserted rather than argued.",
                "fix": "State what evidence would count against the conversational account.",
            },
            {
                "id": "r-002",
                "anchor": "b-006",
                "category": "structure",
                "severity": "minor",
                "critique": "Treating the sample composition as incidental is a decision, not an observation.",
                "fix": "Say why gender and age are not analytically relevant here.",
                "insert_text": "Participants were predominantly retired men, a composition that reflects who has the leisure for night observing rather than who is drawn to it.",
            },
        ],
        "meta": {
            "assessment": "A promising argument that currently rests on assertion where it needs evidence.",
            "weaknesses": [{"rank": 1, "text": "The central mechanism is asserted.", "comment_ids": ["r-001"]}],
            "fix_plan": [
                {"step": 1, "text": "Make the conversational mechanism falsifiable.", "comment_ids": ["r-001"]},
                {"step": 2, "text": "Justify the sample framing.", "comment_ids": ["r-002"]},
            ],
        },
    }
    payload.update(overrides)
    return payload


ACADEMIC_WORK = {
    "work_id": "w-0001",
    "canonical_title": "Boundaries of Science",
    "normalized_title": "boundaries of science",
    "authors": [{"family": "Gieryn", "given": "Thomas F."}],
    "publication_year": 1999,
    "venue_name": "University of Chicago Press",
    "publisher": "University of Chicago Press",
    "abstract_best": "A study of how scientists demarcate science from non-science in public disputes.",
    "identifiers": {"doi": "10.7208/9780226824420"},
    "urls": ["https://example.org/boundaries"],
    "source_records": ["sr-1"],
    "dedupe_cluster_id": "dc-1",
}

DEEP_SOURCE_TEXT = (
    "Observing sessions are where the amateur-professional line gets drawn. "
    "One organizer described the vetting as constant and informal, and said that "
    "nobody is ever told they do not belong.\n"
)


def make_academic_run(root):
    directory = Path(root) / "academic-run"
    directory.mkdir(parents=True)
    (directory / "academic_run.json").write_text(json.dumps({"schemaVersion": 1, "query": "boundary work"}), encoding="utf-8")
    (directory / "works.jsonl").write_text(json.dumps(ACADEMIC_WORK) + "\n", encoding="utf-8")
    (directory / "ris_manifest.json").write_text(
        json.dumps({"records": [{"workId": "w-0001", "risKey": "doi:10.7208/9780226824420"}]}), encoding="utf-8"
    )
    return directory


def make_deep_run(root, flag_quote=False):
    directory = Path(root) / "deep-run"
    (directory / "downloads").mkdir(parents=True)
    (directory / "downloads" / "src-0001.txt").write_text(DEEP_SOURCE_TEXT, encoding="utf-8")
    (directory / "source_index.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "sources": [
                    {
                        "sourceId": "src-0001",
                        "title": "Field notes on observing sessions",
                        "sourceUrl": "https://example.org/notes",
                        "finalUrl": "https://example.org/notes",
                        "canonicalUrl": "https://example.org/notes",
                        "accessDate": "2026-05-02T10:00:00Z",
                        "outputPath": "downloads/src-0001.txt",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    evidence = {
        "evidenceId": "ev-0001",
        "sourceId": "src-0001",
        "text": "Vetting is constant and informal.",
        "directQuote": "the vetting as constant and informal",
    }
    if flag_quote:
        evidence["verification"] = {"verdict": "flag", "reason": "overstated"}
    (directory / "evidence_items.jsonl").write_text(json.dumps(evidence) + "\n", encoding="utf-8")
    return directory


# --------------------------------------------------------------------------- #
# Stub endpoints
# --------------------------------------------------------------------------- #


class StubChatHandler(BaseHTTPRequestHandler):
    responses = []
    requests = []

    def default_for(self, payload):
        if len(payload["messages"]) < 2:
            return "ready"
        user = json.loads(payload["messages"][1]["content"])
        if "items" in user:
            return {"verdicts": [{"id": item["id"], "verdict": "ok"} for item in user["items"]]}
        return {"claims": []}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        self.__class__.requests.append(payload)
        if self.__class__.responses:
            response = self.__class__.responses.pop(0)
        else:
            response = self.default_for(payload)
        content = response if isinstance(response, str) else json.dumps(response)
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        return


class StubServer:
    def __init__(self, responses=None):
        self.responses = list(responses or [])

    def __enter__(self):
        StubChatHandler.responses = list(self.responses)
        StubChatHandler.requests = []
        self.server = QuietServer(("127.0.0.1", 0), StubChatHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.url = f"http://{host}:{port}/v1/chat/completions"
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    @property
    def requests(self):
        return StubChatHandler.requests


def run_script(*args, environment=None):
    # Point the agent directory at nothing so endpoint resolution cannot pick up
    # the settings of whoever is running the tests, and never let a default run
    # reach a real thinking backend.
    base = environment if environment is not None else os.environ
    env = {**base, "PYTHONDONTWRITEBYTECODE": "1"}
    env.setdefault("PI_FORGE_AGENT_DIR", "/nonexistent-agent-directory")
    arguments = list(args)
    if arguments and arguments[0] == "render" and not {"--no-verify", "--think-url"} & set(arguments):
        arguments.append("--no-verify")
    return subprocess.run([sys.executable, str(SCRIPT), *arguments], capture_output=True, text=True, env=env)


def payload_of(result):
    return json.loads(result.stdout)


def build_vault(root, article=ARTICLE):
    vault = Path(root) / "vault"
    (vault / "00 Inbox").mkdir(parents=True)
    (vault / "99 Meta" / "99.02 Schemas").mkdir(parents=True)
    (vault / "99 Meta" / "99.02 Schemas" / "0.00 Vault Schema.md").write_text(SCHEMA, encoding="utf-8")
    (vault / "05 Academic").mkdir(parents=True)
    path = vault / "05 Academic" / "Boundary Work in Amateur Astronomy.md"
    path.write_text(article, encoding="utf-8")
    return vault, path


# --------------------------------------------------------------------------- #
# Indexing
# --------------------------------------------------------------------------- #


class IndexTests(unittest.TestCase):
    def test_block_kinds(self):
        body = (
            "# Heading\n\nA paragraph.\n\n- one\n- two\n\n> a quotation\n\n```\ncode\n```\n\n"
            "| a | b |\n| --- | --- |\n| 1 | 2 |\n\n---\n\n<div>html</div>\n"
        )
        kinds = [block["kind"] for block in r2.parse_blocks(body)]
        self.assertEqual(kinds, ["heading", "paragraph", "list", "quote", "fence", "table", "rule", "html"])

    def test_a_fence_swallows_everything_including_a_fake_marker(self):
        body = "Intro.\n\n```markdown\n> [!question]- R2 r-001 · Research gap · major\n> not a real comment\n```\n\nAfter.\n"
        blocks = r2.parse_blocks(body)
        self.assertEqual([block["kind"] for block in blocks], ["paragraph", "fence", "paragraph"])
        self.assertEqual(r2.reserved_comment_ids(blocks), [])

    def test_earlier_comments_are_recognized_and_reserved(self):
        body = "Intro.\n\n> [!warning]- R2 r-003 · Thin evidence · minor\n> an earlier objection\n\nAfter.\n"
        blocks = r2.parse_blocks(body)
        self.assertEqual(blocks[1]["kind"], "r2-comment")
        self.assertEqual(r2.reserved_comment_ids(blocks), ["r-003"])
        self.assertEqual(r2.next_comment_id(["r-003"]), "r-004")

    def test_a_setext_heading_stays_with_its_text(self):
        blocks = r2.parse_blocks("Title of a section\n---\n\nBody paragraph.\n")
        self.assertEqual(len(blocks), 2)
        self.assertIn("---", blocks[0]["text"])

    def test_frontmatter_is_not_part_of_the_body(self):
        parsed = r2.read_article_text(ARTICLE)
        self.assertTrue(parsed["body"].startswith("\n# Boundary Work"))
        self.assertNotIn("type: draft", parsed["body"])


# --------------------------------------------------------------------------- #
# The round trip
# --------------------------------------------------------------------------- #


class RoundTripTests(unittest.TestCase):
    """Render the comments in, strip them back out, demand the original bytes."""

    def render_and_strip(self, body, anchors=None, per_anchor=1):
        blocks = r2.parse_blocks(body)
        anchorable = [block for block in blocks if block["kind"] != "r2-comment"]
        chosen = [block for block in anchorable if anchors is None or block["id"] in anchors]
        comments = []
        counter = 10
        for block in chosen:
            for _ in range(per_anchor):
                counter += 1
                comments.append(
                    {
                        "id": f"r-{counter:03d}",
                        "anchor": block["id"],
                        "category": "gap",
                        "severity": "major",
                        "quoted_text": "",
                        "critique": "A critique.\n\nWith a second paragraph.",
                        "fix": "Do the thing.",
                        "insert_text": "Suggested prose (Gieryn, 1999).",
                        "citations": [],
                    }
                )
        tail = r2.build_tail(
            "20260727T000000Z",
            {"assessment": "Overall.", "weaknesses": [], "fix_plan": [{"number": 1, "text": "Step", "comment_ids": []}]},
            ["A reference line"],
            ["Source note: x"],
        )
        rendered = r2.render_body(body, blocks, comments, tail)
        restored, removed = r2.strip_body(
            rendered, ids={comment["id"] for comment in comments}, final_newline=body.endswith("\n")
        )
        return rendered, restored, removed, comments

    def assert_round_trip(self, body, **kwargs):
        rendered, restored, removed, comments = self.render_and_strip(body, **kwargs)
        self.assertEqual(restored.encode("utf-8"), body.encode("utf-8"))
        self.assertEqual(len(removed), len(comments))
        return rendered

    def test_plain_article(self):
        self.assert_round_trip("# Title\n\nOne paragraph.\n\nAnother paragraph.\n")

    def test_missing_final_newline(self):
        self.assert_round_trip("# Title\n\nOne paragraph.\n\nA last line with no newline")

    def test_crlf_line_endings(self):
        self.assert_round_trip("# Title\r\n\r\nOne paragraph.\r\n\r\nAnother.\r\n")

    def test_trailing_blank_lines(self):
        self.assert_round_trip("Only paragraph.\n\n\n")

    def test_article_that_already_carries_an_earlier_review(self):
        body = (
            "Intro paragraph.\n\n"
            "> [!question]- R2 r-001 · Research gap · major\n> an earlier objection\n\n"
            "Second paragraph.\n"
        )
        rendered = self.assert_round_trip(body)
        self.assertIn("r-001", rendered)
        cleaned, removed = r2.strip_body(rendered)
        self.assertIn("r-001", removed)
        self.assertNotIn("R2 r-", cleaned)

    def test_article_with_its_own_callouts_and_quotes(self):
        body = "Intro.\n\n> [!quote] Someone\n> An epigraph the author chose.\n\nAfter the quote.\n"
        self.assert_round_trip(body)

    def test_fenced_code_containing_the_comment_syntax(self):
        body = "Intro.\n\n```md\n> [!failure]- R2 r-999 · Logic · major\n> pasted from a review\n```\n\nAfter.\n"
        rendered = self.assert_round_trip(body)
        self.assertIn("r-999", rendered)

    def test_comment_on_the_first_and_last_blocks(self):
        body = "First paragraph.\n\nMiddle.\n\nLast paragraph.\n"
        self.assert_round_trip(body, anchors={"b-001", "b-003"})

    def test_several_comments_on_one_anchor(self):
        self.assert_round_trip("One paragraph.\n\nTwo paragraph.\n", anchors={"b-001"}, per_anchor=3)

    def test_headings_lists_and_blockquote_anchors(self):
        body = "# H\n\n- a list item\n- another\n\n> a blockquote\n\nEnd.\n"
        self.assert_round_trip(body)

    def test_unicode_survives(self):
        self.assert_round_trip("Le problème posé par Böhm — naïve, non?\n\nDeuxième paragraphe.\n")

    def test_every_callout_is_preceded_by_a_blank_line(self):
        # Without it Obsidian reads the callout as a lazy continuation of the
        # paragraph above and renders it as plain text.
        body = "# H\n\nA paragraph with no blank line before the next block.\n- a list\n\nLast line, no trailing blank"
        rendered = self.assert_round_trip(body)
        lines = rendered.splitlines()
        for position, line in enumerate(lines):
            if r2.MARKER_RE.match(line):
                self.assertGreater(position, 0)
                self.assertEqual(lines[position - 1].strip(), "")

    def test_marker_count_matches_the_comments(self):
        body = "Intro.\n\n> [!question]- R2 r-001 · Research gap · major\n> earlier\n\nAfter.\n"
        rendered, _restored, _removed, comments = self.render_and_strip(body)
        self.assertEqual(r2.count_markers(rendered), len(comments) + 1)


# --------------------------------------------------------------------------- #
# Citation register
# --------------------------------------------------------------------------- #


class RegisterTests(unittest.TestCase):
    def test_academic_works_resolve_by_doi_and_title(self):
        with tempfile.TemporaryDirectory() as root:
            directory = make_academic_run(root)
            register, runs = r2.load_register([str(directory)], [])
            self.assertEqual(runs, [str(directory.resolve())])
            by_doi = r2.resolve_citation(register, "doi:10.7208/9780226824420")
            by_title = r2.resolve_citation(register, "title:boundaries of science|1999")
            self.assertIsNotNone(by_doi)
            self.assertIs(by_doi, by_title)
            self.assertEqual(by_doi["evidence_level"], "abstract")
            self.assertEqual(by_doi["families"], ["Gieryn"])

    def test_a_doi_url_resolves_to_the_same_work(self):
        with tempfile.TemporaryDirectory() as root:
            register, _runs = r2.load_register([str(make_academic_run(root))], [])
            self.assertIsNotNone(r2.resolve_citation(register, "doi:https://doi.org/10.7208/9780226824420"))

    def test_deep_sources_carry_archived_text(self):
        with tempfile.TemporaryDirectory() as root:
            register, _runs = r2.load_register([str(make_deep_run(root))], [])
            entry = r2.resolve_citation(register, "source:src-0001")
            self.assertEqual(entry["evidence_level"], "full_text")
            self.assertEqual(entry["quotes"], ["the vetting as constant and informal"])

    def test_quotes_flagged_upstream_are_left_out(self):
        with tempfile.TemporaryDirectory() as root:
            warnings = []
            register, _runs = r2.load_register([str(make_deep_run(root, flag_quote=True))], warnings)
            self.assertEqual(r2.resolve_citation(register, "source:src-0001")["quotes"], [])
            self.assertTrue(any("flagged" in warning for warning in warnings))

    def test_a_directory_that_is_not_a_research_run_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(r2.UserError, "not a web-research run"):
                r2.load_register([root], [])

    def test_references_are_formatted_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as root:
            register, _runs = r2.load_register([str(make_academic_run(root))], [])
            entry = r2.resolve_citation(register, "doi:10.7208/9780226824420")
            comments = [{"citations": [{"entry": entry}]}, {"citations": [{"entry": entry}]}]
            references = r2.collect_references(comments)
            self.assertEqual(len(references), 1)
            self.assertIn("Gieryn, Thomas F.", references[0])
            self.assertIn("https://doi.org/10.7208/9780226824420", references[0])
            self.assertEqual(r2.citation_label(entry), "(Gieryn, 1999)")


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.parsed = r2.read_article_text(ARTICLE)
        self.blocks = r2.parse_blocks(self.parsed["body"])
        self.index = {
            "article": {"path": "05 Academic/Article.md", "body_sha256": self.parsed["body_sha256"]},
            "reserved_ids": [],
        }

    def validate(self, payload, register=None):
        return r2.validate_comments(payload, self.index, self.blocks, register or {})

    def base(self, **overrides):
        return comments_payload(self.parsed["body_sha256"], "05 Academic/Article.md", **overrides)

    def problems_for(self, payload, register=None):
        with self.assertRaises(r2.CommentsError) as caught:
            self.validate(payload, register)
        return " | ".join(caught.exception.problems)

    def test_a_clean_set_validates(self):
        comments, meta, warnings = self.validate(self.base())
        self.assertEqual([comment["id"] for comment in comments], ["r-001", "r-002"])
        self.assertEqual(len(meta["fix_plan"]), 2)
        self.assertEqual(warnings, [])

    def test_every_problem_is_reported_at_once(self):
        payload = self.base()
        payload["comments"][0]["anchor"] = "b-999"
        payload["comments"][1]["category"] = "vibes"
        problems = self.problems_for(payload)
        self.assertIn("b-999", problems)
        self.assertIn("vibes", problems)

    def test_a_repeated_id_is_refused(self):
        payload = self.base()
        payload["comments"][1]["id"] = "r-001"
        self.assertIn("repeats an id", self.problems_for(payload))

    def test_an_id_from_an_earlier_review_is_refused(self):
        self.index["reserved_ids"] = ["r-001"]
        self.assertIn("earlier review", self.problems_for(self.base()))

    def test_a_criticism_without_a_fix_is_refused(self):
        payload = self.base()
        payload["comments"][0]["fix"] = ""
        self.assertIn("without saying what to do", self.problems_for(payload))

    def test_a_strength_takes_no_severity_or_fix(self):
        payload = self.base()
        payload["comments"][0]["category"] = "strength"
        self.assertIn("takes no severity", self.problems_for(payload))

    def test_a_quotation_must_be_in_the_anchored_block(self):
        payload = self.base()
        payload["comments"][0]["quoted_text"] = "a phrase the article never uses"
        self.assertIn("quotes text that is not in", self.problems_for(payload))

    def test_a_hard_wrapped_quotation_still_matches(self):
        payload = self.base()
        payload["comments"][0]["quoted_text"] = "the boundary between amateur and professional astronomy is maintained"
        comments, _meta, _warnings = self.validate(payload)
        self.assertTrue(comments[0]["quoted_text"])

    def test_an_unresolvable_citation_is_refused(self):
        payload = self.base()
        payload["comments"][0]["citations"] = [{"key": "invented2020", "work": "doi:10.0000/invented"}]
        self.assertIn("not in any linked research run", self.problems_for(payload))

    def test_suggested_text_for_an_evidence_comment_needs_a_citation(self):
        payload = self.base()
        payload["comments"][0]["category"] = "evidence"
        payload["comments"][0]["insert_text"] = "Some prose with no source behind it."
        self.assertIn("needs the literature", self.problems_for(payload))

    def test_metadata_only_evidence_forces_the_hedge(self):
        with tempfile.TemporaryDirectory() as root:
            register, _runs = r2.load_register([str(make_academic_run(root))], [])
            payload = self.base()
            comment = payload["comments"][0]
            comment["category"] = "theory"
            comment["citations"] = [{"key": "gieryn1999", "work": "doi:10.7208/9780226824420"}]
            comment["insert_text"] = "Gieryn (1999) describes demarcation as rhetorical work."
            self.assertIn(r2.HEDGE_PHRASE, self.problems_for(payload, register))
            comment["fix"] = f"Engage Gieryn on demarcation; {r2.HEDGE_PHRASE} before citing the argument."
            comments, _meta, _warnings = self.validate(payload, register)
            self.assertEqual(comments[0]["citations"][0]["entry"]["evidence_level"], "abstract")

    def test_the_cited_author_and_year_must_appear_in_the_suggested_text(self):
        with tempfile.TemporaryDirectory() as root:
            register, _runs = r2.load_register([str(make_academic_run(root))], [])
            payload = self.base()
            comment = payload["comments"][0]
            comment["category"] = "theory"
            comment["citations"] = [{"key": "gieryn1999", "work": "doi:10.7208/9780226824420"}]
            comment["insert_text"] = "Someone has argued that demarcation is rhetorical."
            comment["fix"] = f"Engage the literature; {r2.HEDGE_PHRASE}."
            problems = self.problems_for(payload, register)
            self.assertIn("1999 does not appear", problems)
            self.assertIn("no author name", problems)

    def test_a_quotation_from_a_work_never_read_in_full_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            register, _runs = r2.load_register([str(make_academic_run(root))], [])
            payload = self.base()
            payload["comments"][0]["citations"] = [
                {"key": "gieryn1999", "work": "doi:10.7208/9780226824420", "quote": "demarcation is rhetorical"}
            ]
            self.assertIn("full text was never retrieved", self.problems_for(payload, register))

    def test_a_quotation_absent_from_the_archived_source_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            register, _runs = r2.load_register([str(make_deep_run(root))], [])
            payload = self.base()
            payload["comments"][0]["citations"] = [
                {"key": "notes", "work": "source:src-0001", "quote": "a sentence nobody wrote"}
            ]
            self.assertIn("not in the archived source", self.problems_for(payload, register))

    def test_text_that_would_render_as_a_marker_is_refused(self):
        payload = self.base()
        payload["comments"][0]["critique"] = "[!question]- R2 r-050 · Research gap · major"
        self.assertIn("would render as a comment marker", self.problems_for(payload))

    def test_a_fix_plan_is_required(self):
        payload = self.base()
        payload["meta"]["fix_plan"] = []
        self.assertIn("fix_plan is required", self.problems_for(payload))

    def test_a_major_comment_outside_the_fix_plan_warns(self):
        payload = self.base()
        payload["meta"]["fix_plan"] = [{"step": 1, "text": "Only this.", "comment_ids": ["r-002"]}]
        _comments, _meta, warnings = self.validate(payload)
        self.assertTrue(any("r-001" in warning for warning in warnings))


# --------------------------------------------------------------------------- #
# Naming and frontmatter
# --------------------------------------------------------------------------- #


class NoteTests(unittest.TestCase):
    def setUp(self):
        import vault_schema

        self.schema = vault_schema.parse_schema_note(SCHEMA)

    def test_the_review_copy_is_marked_generated(self):
        metadata = r2.frontmatter_metadata(self.schema, related=['[[An Article]]'])
        self.assertEqual(metadata["capture_type"], "generated")
        self.assertEqual(metadata["status"], "raw")
        self.assertEqual(metadata["related"], ["[[An Article]]"])

    def test_a_vault_that_cannot_say_generated_is_refused(self):
        import vault_schema

        without = vault_schema.parse_schema_note(SCHEMA.replace("- `generated` — Made by a script, agent, or model.\n", ""))
        with self.assertRaisesRegex(r2.UserError, "generated"):
            r2.frontmatter_metadata(without)

    def test_a_taken_name_gets_a_numbered_suffix(self):
        taken = {"an article - reviewer 2 - 2026-07-27"}
        name = r2.review_copy_name("An Article", "2026-07-27", taken)
        self.assertEqual(name, "An Article - Reviewer 2 - 2026-07-27 (2).md")

    def test_a_long_title_still_leaves_room_for_the_suffix(self):
        name = r2.review_copy_name("A" * 200, "2026-07-27", set())
        self.assertLessEqual(len(name) - 3, 120)
        self.assertIn("Reviewer 2 - 2026-07-27", name)

    def test_an_unsafe_title_is_repaired(self):
        name = r2.review_copy_name("Bracket [Work]: A Study", "2026-07-27", set())
        for character in "[]:|#^":
            self.assertNotIn(character, name)


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #


class CommandTests(unittest.TestCase):
    def index(self, vault, article):
        result = run_script("index", str(article), "--vault", str(vault))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return payload_of(result)["data"]

    def write_comments(self, root, payload):
        path = Path(root) / "comments.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_index_then_render_writes_a_review_copy(self):
        with tempfile.TemporaryDirectory() as root:
            vault, article = build_vault(root)
            data = self.index(vault, article)
            self.assertEqual(data["next_comment_id"], "r-001")
            self.assertGreater(data["anchorable"], 4)
            comments = self.write_comments(root, comments_payload(data["article"]["body_sha256"], data["article"]["path"]))
            result = run_script("render", "--run", data["run_directory"], "--comments", str(comments))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            body = payload_of(result)["data"]
            note = vault / body["note_path"]
            self.assertTrue(note.is_file())
            text = note.read_text(encoding="utf-8")
            self.assertIn("capture_type: generated", text)
            self.assertIn("R2 r-001 · Logic · major", text)
            self.assertIn("[!quote]+ Suggested text", text)
            self.assertIn("## Reviewer 2 · Meta Review", text)
            self.assertIn("### Fix plan", text)
            self.assertIn("## Provenance", text)
            self.assertIn("was not modified", text)
            # The article itself is untouched.
            self.assertEqual(article.read_text(encoding="utf-8"), ARTICLE)

    def test_the_written_copy_strips_back_to_the_article(self):
        with tempfile.TemporaryDirectory() as root:
            vault, article = build_vault(root)
            data = self.index(vault, article)
            comments = self.write_comments(root, comments_payload(data["article"]["body_sha256"], data["article"]["path"]))
            result = run_script("render", "--run", data["run_directory"], "--comments", str(comments))
            note = vault / payload_of(result)["data"]["note_path"]
            stripped = run_script("strip", str(note))
            self.assertEqual(stripped.returncode, 0, stripped.stdout + stripped.stderr)
            cleaned = payload_of(stripped)["data"]["markdown"]
            original_body = r2.read_article_text(ARTICLE)["body"]
            self.assertEqual(cleaned, original_body)

    def test_a_dry_run_writes_nothing_into_the_vault(self):
        with tempfile.TemporaryDirectory() as root:
            vault, article = build_vault(root)
            data = self.index(vault, article)
            comments = self.write_comments(root, comments_payload(data["article"]["body_sha256"], data["article"]["path"]))
            result = run_script("render", "--run", data["run_directory"], "--comments", str(comments), "--dry-run")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIsNone(payload_of(result)["data"]["note_path"])
            self.assertEqual(list((vault / "00 Inbox").glob("*.md")), [])
            self.assertTrue((Path(data["run_directory"]) / "review-copy.md").is_file())

    def test_an_edited_article_refuses_to_render(self):
        with tempfile.TemporaryDirectory() as root:
            vault, article = build_vault(root)
            data = self.index(vault, article)
            comments = self.write_comments(root, comments_payload(data["article"]["body_sha256"], data["article"]["path"]))
            article.write_text(ARTICLE + "\nA new paragraph.\n", encoding="utf-8")
            result = run_script("render", "--run", data["run_directory"], "--comments", str(comments))
            self.assertEqual(result.returncode, 2)
            self.assertIn("changed since it was indexed", result.stdout)
            self.assertEqual(list((vault / "00 Inbox").glob("*.md")), [])

    def test_rerunning_a_finished_render_writes_nothing_more(self):
        with tempfile.TemporaryDirectory() as root:
            vault, article = build_vault(root)
            data = self.index(vault, article)
            comments = self.write_comments(root, comments_payload(data["article"]["body_sha256"], data["article"]["path"]))
            first = run_script("render", "--run", data["run_directory"], "--comments", str(comments))
            second = run_script("render", "--run", data["run_directory"], "--comments", str(comments))
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertTrue(payload_of(second)["data"]["resumed"])
            self.assertEqual(len(list((vault / "00 Inbox").glob("*.md"))), 1)
            self.assertEqual(payload_of(first)["data"]["note_path"], payload_of(second)["data"]["note_path"])

    def test_a_collision_holds_rather_than_overwrites(self):
        with tempfile.TemporaryDirectory() as root:
            vault, article = build_vault(root)
            data = self.index(vault, article)
            comments = self.write_comments(root, comments_payload(data["article"]["body_sha256"], data["article"]["path"]))
            import datetime

            date = datetime.date.today().isoformat()
            taken = vault / "00 Inbox" / f"Boundary Work in Amateur Astronomy - Reviewer 2 - {date}.md"
            taken.write_text("someone else's note\n", encoding="utf-8")
            result = run_script("render", "--run", data["run_directory"], "--comments", str(comments))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            written = vault / payload_of(result)["data"]["note_path"]
            self.assertNotEqual(written, taken)
            self.assertEqual(taken.read_text(encoding="utf-8"), "someone else's note\n")

    def test_invalid_comments_report_every_problem(self):
        with tempfile.TemporaryDirectory() as root:
            vault, article = build_vault(root)
            data = self.index(vault, article)
            payload = comments_payload(data["article"]["body_sha256"], data["article"]["path"])
            payload["comments"][0]["anchor"] = "b-999"
            payload["comments"][1]["fix"] = ""
            comments = self.write_comments(root, payload)
            result = run_script("render", "--run", data["run_directory"], "--comments", str(comments))
            self.assertEqual(result.returncode, 2)
            errors = payload_of(result)["errors"]
            self.assertEqual({error["code"] for error in errors}, {"invalid_comments"})
            self.assertGreaterEqual(len(errors), 2)

    def test_status_reports_the_run(self):
        with tempfile.TemporaryDirectory() as root:
            vault, article = build_vault(root)
            data = self.index(vault, article)
            result = run_script("status", "--run", data["run_directory"])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            status = payload_of(result)["data"]
            self.assertEqual(status["phase"], "indexed")
            self.assertIsNone(status["review_copy"])

    def test_every_command_returns_the_envelope(self):
        with tempfile.TemporaryDirectory() as root:
            vault, article = build_vault(root)
            data = self.index(vault, article)
            for arguments in (
                ["index", str(article), "--vault", str(vault)],
                ["status", "--run", data["run_directory"]],
                ["strip", str(article)],
                ["index", "/nonexistent.md", "--vault", str(vault)],
                ["status", "--run", str(Path(root) / "nowhere")],
            ):
                result = run_script(*arguments)
                body = payload_of(result)
                self.assertEqual(set(body), {"status", "artifacts", "warnings", "errors", "data"})
                if body["status"] == "error":
                    self.assertTrue(body["errors"])


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #


class VerifyTests(unittest.TestCase):
    def prepare(self, root):
        vault, article = build_vault(root)
        result = run_script("index", str(article), "--vault", str(vault))
        data = payload_of(result)["data"]
        path = Path(root) / "comments.json"
        path.write_text(
            json.dumps(comments_payload(data["article"]["body_sha256"], data["article"]["path"])), encoding="utf-8"
        )
        return vault, data, path

    def test_the_verifier_sees_the_whole_passage(self):
        with tempfile.TemporaryDirectory() as root:
            vault, data, comments = self.prepare(root)
            with StubServer() as stub:
                result = run_script(
                    "render", "--run", data["run_directory"], "--comments", str(comments), "--think-url", stub.url
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                sent = json.loads(stub.requests[-1]["messages"][1]["content"])["items"]
            system = stub.requests[-1]["messages"][0]["content"]
            self.assertIn("Do not flag an item because you would have phrased", system)
            first = next(item for item in sent if item["id"] == "r-001")
            # The whole paragraph, wrapped exactly as the article wraps it, not
            # a summary of it: a reviewer given a paraphrase rubber-stamps.
            self.assertEqual(first["anchor_text"].count("\n"), 2)
            self.assertTrue(first["anchor_text"].startswith("This article argues"))
            self.assertTrue(first["anchor_text"].endswith("formal credentialing."))
            self.assertEqual(first["context_heading"], "Introduction")

    def test_a_flagged_comment_is_still_written_and_marked(self):
        with tempfile.TemporaryDirectory() as root:
            vault, data, comments = self.prepare(root)
            verdicts = {
                "verdicts": [
                    {"id": "r-001", "verdict": "flag", "reason": "the passage already names its counterevidence"},
                    {"id": "r-002", "verdict": "ok"},
                ]
            }
            with StubServer([verdicts]) as stub:
                result = run_script(
                    "render", "--run", data["run_directory"], "--comments", str(comments), "--think-url", stub.url
                )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            body = payload_of(result)["data"]
            self.assertEqual(body["flagged_ids"], ["r-001"])
            text = (vault / body["note_path"]).read_text(encoding="utf-8")
            self.assertIn("**Flagged in verification:** the passage already names its counterevidence", text)
            report = Path(body["report"]).read_text(encoding="utf-8")
            self.assertIn("## Flagged in verification", report)

    def test_a_flagged_comment_still_strips_cleanly(self):
        with tempfile.TemporaryDirectory() as root:
            vault, data, comments = self.prepare(root)
            verdicts = {"verdicts": [{"id": "r-001", "verdict": "flag", "reason": "overstated"}, {"id": "r-002", "verdict": "ok"}]}
            with StubServer([verdicts]) as stub:
                result = run_script(
                    "render", "--run", data["run_directory"], "--comments", str(comments), "--think-url", stub.url
                )
            note = vault / payload_of(result)["data"]["note_path"]
            stripped = payload_of(run_script("strip", str(note)))["data"]["markdown"]
            self.assertEqual(stripped, r2.read_article_text(ARTICLE)["body"])

    def test_a_malformed_verdict_gets_one_corrective_retry(self):
        with tempfile.TemporaryDirectory() as root:
            vault, data, comments = self.prepare(root)
            good = {"verdicts": [{"id": "r-001", "verdict": "ok"}, {"id": "r-002", "verdict": "ok"}]}
            with StubServer([{"verdicts": [{"id": "r-001", "verdict": "maybe"}]}, good]) as stub:
                result = run_script(
                    "render", "--run", data["run_directory"], "--comments", str(comments), "--think-url", stub.url
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(stub.requests), 2)

    def test_verdicts_are_journaled_so_a_rerun_reviews_nothing_again(self):
        with tempfile.TemporaryDirectory() as root:
            vault, data, comments = self.prepare(root)
            with StubServer() as stub:
                run_script("render", "--run", data["run_directory"], "--comments", str(comments), "--think-url", stub.url, "--dry-run")
                first = len(stub.requests)
                run_script("render", "--run", data["run_directory"], "--comments", str(comments), "--think-url", stub.url, "--dry-run")
                self.assertEqual(len(stub.requests), first)
            journal = Path(data["run_directory"]) / "verified.jsonl"
            self.assertEqual(len(journal.read_text(encoding="utf-8").strip().splitlines()), 2)

    def test_an_unreachable_verifier_writes_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            vault, data, comments = self.prepare(root)
            result = run_script(
                "render",
                "--run",
                data["run_directory"],
                "--comments",
                str(comments),
                "--think-url",
                "http://127.0.0.1:1/v1/chat/completions",
                "--request-timeout",
                "5",
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(payload_of(result)["errors"][0]["code"], "verify_error")
            self.assertEqual(list((vault / "00 Inbox").glob("*.md")), [])

    def test_no_verify_says_plainly_that_nothing_was_reviewed(self):
        with tempfile.TemporaryDirectory() as root:
            vault, data, comments = self.prepare(root)
            result = run_script("render", "--run", data["run_directory"], "--comments", str(comments), "--no-verify")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            body = payload_of(result)["data"]
            self.assertIn("skipped", body["verification"])
            text = (vault / body["note_path"]).read_text(encoding="utf-8")
            self.assertIn("not the same as approval", text)
            report = Path(body["report"]).read_text(encoding="utf-8")
            self.assertIn("Nothing was reviewed", report)


class InventoryTests(unittest.TestCase):
    def test_the_census_journals_and_resumes(self):
        with tempfile.TemporaryDirectory() as root:
            vault, article = build_vault(root)
            data = payload_of(run_script("index", str(article), "--vault", str(vault)))["data"]
            claims = {"claims": [{"text": "Amateurs produce data professionals use.", "cited": False, "note": "needs a source"}]}
            with StubServer([claims] * 6) as stub:
                first = run_script("inventory", "--run", data["run_directory"], "--base-url", stub.url)
                self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
                calls = len(stub.requests)
                self.assertGreater(calls, 0)
                body = payload_of(first)["data"]
                self.assertGreater(body["uncited_claims"], 0)
                second = run_script("inventory", "--run", data["run_directory"], "--base-url", stub.url)
                self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
                self.assertEqual(len(stub.requests), calls)
            self.assertEqual(list((vault / "00 Inbox").glob("*.md")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

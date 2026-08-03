#!/usr/bin/env python3
"""Membership rules for a project corpus, proven against a synthetic vault.

What these tests are really defending is the closed world. A corpus that quietly
grows is worse than no corpus at all: an agent told it may read only these files
would be reading others and reporting that it had not. So the cases that matter
most here are the negative ones -- a link inside an annotation, an embed of a
note, a second link under a Transcript heading, a basename two notes share --
where the tempting behaviour is to resolve something and the correct behaviour is
to resolve nothing and say why.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import vault_corpus as vc  # noqa: E402
from vault_schema import WORKSPACE_MARKER, UserError, parse_schema_note, sha256_text  # noqa: E402

SCHEMA = """# Vault Schema

## Approved properties

| Property | Required | Shape | Definition |
| --- | --- | --- | --- |
| `type` | yes | controlled scalar | Kind. |
| `status` | yes | controlled scalar | Lifecycle. |
| `domain` | yes | controlled scalar | Broad area. |
| `subdomain` | no | controlled scalar | Nested area. |
| `project` | no | quoted wikilink | Registered project. |
| `parent` | no | quoted wikilink | Parent note. |
| `source_kind` | no | controlled scalar | What a source is. |
| `capture_type` | no | controlled scalar | How it arrived. |
| `date` | no | scalar, human-owned | Subject date. |

## Note types

- `project` — A project hub.
- `source` — Something written elsewhere.
- `note` — General note.
- `person` — A person.

## Status values

- `active` — Active.
- `complete` — Complete.

## Domains

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `academic` | `5` | `Academic` | Scholarship. |
| `directory` | `8` | `Directory` | People. |
| `meta` | `99` | `Meta` | System notes. |

## Subdomains

### academic

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `dissertation` | `1` | `Dissertation` | The dissertation. |

### directory

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `contacts` | `1` | `Contacts` | Contacts. |

### meta

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `workflows` | `6` | `Workflows` | Run artifacts. |

## Project registry

| Approved value | Domain | Subdomain | Number | Definition |
| --- | --- | --- | --- | --- |
| `"[[Article 2]]"` | `academic` | `dissertation` | `2` | Utopian imagination. |
| `"[[Article 1]]"` | `academic` | `dissertation` | `1` | The first article. |

## Sources root

| Number | Label | Definition |
| --- | --- | --- |
| `10` | `Sources` | Source notes. |

## Source kinds

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `book` | `1` | `Book` | A book. |
| `transcript` | `3` | `Transcript` | A verbatim transcript. |

## Capture types

- `imported` — Brought in from outside.
- `voice` — Recorded.

## Legacy normalization map

| Legacy input | Canonical output |
| --- | --- |
| `type: paper` | `type: source` |

## Folder routing

Compiled from domain, subdomain, and project.
"""

PROJECT_FOLDER = "05 Academic/5.01 Dissertation/5.01.02 Article 2"
SOURCES = "10 Sources/10.01 Book/Academic/Dissertation"
TRANSCRIPTS = "10 Sources/10.03 Transcript/Academic/Dissertation"


def note(metadata, body):
    lines = ["---"]
    for key, value in metadata.items():
        lines.append(f"{key}: {value}")
    lines += ["---", ""]
    return "\n".join(lines) + body


class CorpusTestCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.vault = Path(self._temporary.name)
        self.schema = parse_schema_note(SCHEMA)
        self.schema_hash = sha256_text(SCHEMA)
        self.write("99 Meta/99.02 Schemas/0.00 Vault Schema.md", SCHEMA)
        self.build()

    def tearDown(self):
        self._temporary.cleanup()

    def write(self, relative, text):
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def build(self):
        self.write(
            f"{PROJECT_FOLDER}/Article 2.md",
            note(
                {"type": "project", "status": "active", "domain": "academic",
                 "subdomain": "dissertation", "project": '"[[Article 2]]"'},
                HUB_BODY,
            ),
        )
        self.write(
            f"{PROJECT_FOLDER}/Working Draft.md",
            note({"type": "note", "status": "active", "domain": "academic"}, "# Working Draft\n\nProse.\n"),
        )
        self.write(f"{PROJECT_FOLDER}/Private venting.md", "# Private venting\n\nNot for handoff.\n")
        self.write(
            f"{SOURCES}/Suits - 2005 - The Grasshopper.md",
            note(
                {"type": "source", "status": "complete", "domain": "academic",
                 "subdomain": "dissertation", "source_kind": "book"},
                "# The Grasshopper\n\nGames and utopia.\n",
            ),
        )
        self.write(
            "08 Directory/8.01 Contacts/Committee Chair.md",
            note({"type": "person", "status": "active", "domain": "directory", "subdomain": "contacts"},
                 "# Committee Chair\n"),
        )
        # A meeting note in the project folder whose Transcript section names the
        # verbatim source filed away in the sources tree: one document, two files.
        self.write(
            f"{PROJECT_FOLDER}/2026-02-09 - Meeting - Outline.md",
            note({"type": "note", "status": "active", "domain": "academic"},
                 "# Meeting\n\nWhat we decided.\n\n![[diagram.png]]\n\n# Transcript\n\n"
                 "[[2026-02-09 - Meeting - Outline - Transcript]]\n"),
        )
        self.write(
            f"{TRANSCRIPTS}/2026-02-09 - Meeting - Outline - Transcript.md",
            note({"type": "source", "status": "complete", "domain": "academic",
                  "subdomain": "dissertation", "source_kind": "transcript",
                  "parent": '"[[2026-02-09 - Meeting - Outline]]"'}, "# Transcript\n\nVerbatim.\n"),
        )
        self.write("99 Meta/99.05 Attachments/diagram.png", "not really a png")
        self.write("09 Wiki/9.01 Concepts/Old Draft.md", note({"type": "note", "status": "active",
                                                               "domain": "academic"}, "# Old Draft\n"))

    def resolve(self, project="Article 2", digest=False):
        return vc.resolve_corpus(self.vault, self.schema, self.schema_hash, project, digest=digest)

    def paths(self, resolution):
        return {record["path"] for record in resolution["members"]}

    def roles(self, resolution):
        return {record["path"]: record["role"] for record in resolution["members"]}


HUB_BODY = """# Article 2

> [!summary]
> Utopian imagination.

Prose that mentions [[Old Draft]] outside the corpus section, which must not
make it a member.

## Corpus

### Sources
- [[Suits - 2005 - The Grasshopper]] — core theory text; supersedes [[Old Draft]]

### People
- [[Committee Chair]]

### Excluded
- `Private venting.md` — personal

## Notes

Owner-authored.
"""


class MembershipTests(CorpusTestCase):
    def test_folder_files_are_members_without_being_listed(self):
        resolution = self.resolve()
        self.assertIn(f"{PROJECT_FOLDER}/Working Draft.md", self.paths(resolution))
        self.assertEqual(self.roles(resolution)[f"{PROJECT_FOLDER}/Working Draft.md"], vc.ROLE_FOLDER)

    def test_hub_links_pull_in_notes_from_elsewhere_in_the_vault(self):
        resolution = self.resolve()
        roles = self.roles(resolution)
        self.assertEqual(roles[f"{SOURCES}/Suits - 2005 - The Grasshopper.md"], "sources")
        self.assertEqual(roles["08 Directory/8.01 Contacts/Committee Chair.md"], "people")

    def test_annotation_is_carried_but_its_links_are_not_members(self):
        resolution = self.resolve()
        record = next(
            item for item in resolution["members"] if item["path"].endswith("The Grasshopper.md")
        )
        self.assertEqual(record["annotation"], "core theory text; supersedes [[Old Draft]]")
        self.assertNotIn("09 Wiki/9.01 Concepts/Old Draft.md", self.paths(resolution))

    def test_an_em_dash_inside_a_display_alias_does_not_split_the_bullet(self):
        # Aliasing a long filename to something readable is the normal way to keep
        # a hub browsable, and those titles contain em dashes. Splitting the
        # bullet on the first separator would cut the link in half and drop the
        # member without saying anything.
        self.write(
            f"{PROJECT_FOLDER}/Article 2.md",
            note({"type": "project", "status": "active", "domain": "academic",
                  "project": '"[[Article 2]]"'},
                 "# Article 2\n\n## Corpus\n\n### Sources\n"
                 "- [[Suits - 2005 - The Grasshopper|Suits — The Grasshopper]] — core theory text\n"),
        )
        resolution = self.resolve()
        record = next(
            (item for item in resolution["members"] if item["path"].endswith("The Grasshopper.md")), None
        )
        self.assertIsNotNone(record, "the aliased link should still resolve to a member")
        self.assertEqual(record["annotation"], "core theory text")

    def test_a_wikilink_inside_an_html_comment_is_not_reported_as_a_loose_link(self):
        # draft-hub leaves scaffolding comments that name the project, and a
        # comment is not content a reader sees or an agent may read.
        self.write(
            f"{PROJECT_FOLDER}/Article 2.md",
            note({"type": "project", "status": "active", "domain": "academic",
                  "project": '"[[Article 2]]"'},
                 "# Article 2\n\n## Corpus\n\n### Sources\n\n"
                 "<!-- nothing is filed under [[Article 2]] for sources yet -->\n"),
        )
        parsed = vc.parse_corpus_section(
            (self.vault / PROJECT_FOLDER / "Article 2.md").read_text(encoding="utf-8")
        )
        self.assertEqual(parsed["loose_links"], [])
        self.assertEqual(parsed["entries"], [])

    def test_prose_outside_the_corpus_section_never_adds_a_member(self):
        self.assertNotIn("09 Wiki/9.01 Concepts/Old Draft.md", self.paths(self.resolve()))

    def test_transcript_pair_closes_into_the_corpus(self):
        resolution = self.resolve()
        transcript = f"{TRANSCRIPTS}/2026-02-09 - Meeting - Outline - Transcript.md"
        self.assertIn(transcript, self.paths(resolution))
        self.assertEqual(self.roles(resolution)[transcript], vc.ROLE_TRANSCRIPT)

    def test_embedded_attachment_closes_but_an_embedded_note_does_not(self):
        self.write(
            f"{PROJECT_FOLDER}/Working Draft.md",
            note({"type": "note", "status": "active", "domain": "academic"},
                 "# Working Draft\n\n![[Old Draft]]\n"),
        )
        resolution = self.resolve()
        self.assertIn("99 Meta/99.05 Attachments/diagram.png", self.paths(resolution))
        self.assertEqual(
            self.roles(resolution)["99 Meta/99.05 Attachments/diagram.png"], vc.ROLE_ATTACHMENT
        )
        self.assertNotIn("09 Wiki/9.01 Concepts/Old Draft.md", self.paths(resolution))
        self.assertTrue(any(problem["code"] == "markdown_embed" for problem in resolution["problems"]))

    def test_closure_does_not_cascade(self):
        # The transcript names a further note; pulling it in would start walking
        # the link graph, which is the failure this design exists to prevent.
        self.write(
            f"{TRANSCRIPTS}/2026-02-09 - Meeting - Outline - Transcript.md",
            note({"type": "source", "status": "complete", "domain": "academic",
                  "source_kind": "transcript"},
                 "# Transcript\n\nVerbatim.\n\n# Transcript\n\n[[Old Draft]]\n"),
        )
        self.assertNotIn("09 Wiki/9.01 Concepts/Old Draft.md", self.paths(self.resolve()))

    def test_exclusion_removes_an_in_folder_file(self):
        resolution = self.resolve()
        self.assertNotIn(f"{PROJECT_FOLDER}/Private venting.md", self.paths(resolution))
        self.assertIn(f"{PROJECT_FOLDER}/Private venting.md", resolution["excluded"])

    def test_manifest_is_never_its_own_member(self):
        self.write(f"{PROJECT_FOLDER}/{vc.MANIFEST_NAME}", "{}")
        self.assertNotIn(f"{PROJECT_FOLDER}/{vc.MANIFEST_NAME}", self.paths(self.resolve()))

    def test_workspace_artifacts_are_never_members(self):
        # An extraction run dropped inside the folder still has its machine
        # artifacts kept out of scope, which is what the marker is for.
        self.write(f"{PROJECT_FOLDER}/Runs/{WORKSPACE_MARKER}", "marker")
        self.write(f"{PROJECT_FOLDER}/Runs/evidence.jsonl", '{"claim": 1}')
        self.assertNotIn(f"{PROJECT_FOLDER}/Runs/evidence.jsonl", self.paths(self.resolve()))


class ProblemTests(CorpusTestCase):
    def test_ambiguous_basename_is_refused_rather_than_guessed(self):
        self.write(
            "09 Wiki/9.01 Concepts/Committee Chair.md",
            note({"type": "note", "status": "active", "domain": "academic"}, "# Committee Chair\n"),
        )
        resolution = self.resolve()
        self.assertEqual(len(resolution["ambiguous"]), 1)
        self.assertNotIn("08 Directory/8.01 Contacts/Committee Chair.md", self.paths(resolution))
        self.assertTrue(vc.blocking_problems(resolution))

    def test_unresolved_link_is_reported_and_blocks(self):
        self.write(f"{PROJECT_FOLDER}/Article 2.md",
                   note({"type": "project", "status": "active", "domain": "academic",
                         "project": '"[[Article 2]]"'},
                        "# Article 2\n\n## Corpus\n\n### Sources\n- [[Nothing By This Name]]\n"))
        resolution = self.resolve()
        self.assertEqual([item["target"] for item in resolution["unresolved"]], ["Nothing By This Name"])
        self.assertTrue(vc.blocking_problems(resolution))

    def test_missing_corpus_section_blocks_with_a_named_reason(self):
        self.write(f"{PROJECT_FOLDER}/Article 2.md",
                   note({"type": "project", "status": "active", "domain": "academic",
                         "project": '"[[Article 2]]"'}, "# Article 2\n\n## Links\n\n- [[Committee Chair]]\n"))
        resolution = self.resolve()
        codes = {problem["code"] for problem in resolution["problems"]}
        self.assertIn("corpus_section_missing", codes)
        self.assertNotIn("08 Directory/8.01 Contacts/Committee Chair.md", self.paths(resolution))

    def test_missing_hub_blocks(self):
        (self.vault / PROJECT_FOLDER / "Article 2.md").unlink()
        resolution = self.resolve()
        self.assertIn("hub_missing", {problem["code"] for problem in resolution["problems"]})

    def test_dead_exclusion_is_an_error(self):
        self.write(f"{PROJECT_FOLDER}/Article 2.md",
                   note({"type": "project", "status": "active", "domain": "academic",
                         "project": '"[[Article 2]]"'},
                        "# Article 2\n\n## Corpus\n\n### Excluded\n- `Nonexistent.md` — gone\n"))
        self.assertIn("dead_exclusion", {problem["code"] for problem in self.resolve()["problems"]})

    def test_two_links_under_transcript_close_nothing(self):
        self.write(
            f"{PROJECT_FOLDER}/2026-02-09 - Meeting - Outline.md",
            note({"type": "note", "status": "active", "domain": "academic"},
                 "# Meeting\n\n# Transcript\n\n[[2026-02-09 - Meeting - Outline - Transcript]]\n[[Old Draft]]\n"),
        )
        resolution = self.resolve()
        self.assertNotIn(f"{TRANSCRIPTS}/2026-02-09 - Meeting - Outline - Transcript.md", self.paths(resolution))
        self.assertIn("transcript_ambiguous", {problem["code"] for problem in resolution["problems"]})

    def test_unregistered_project_names_the_registry(self):
        with self.assertRaises(UserError) as caught:
            self.resolve("Utopia")
        self.assertIn("Article 2", str(caught.exception))

    def test_extra_project_note_warns_without_becoming_a_second_hub(self):
        self.write(f"{PROJECT_FOLDER}/Project Timeline.md",
                   note({"type": "project", "status": "active", "domain": "academic"}, "# Timeline\n"))
        resolution = self.resolve()
        self.assertEqual(resolution["hub"], f"{PROJECT_FOLDER}/Article 2.md")
        self.assertIn("extra_project_note", {problem["code"] for problem in resolution["problems"]})


class ManifestTests(CorpusTestCase):
    def test_manifest_round_trips_and_reports_fresh(self):
        resolution = self.resolve(digest=True)
        project = resolution["project"]
        manifest = vc.build_manifest(resolution, generated="2026-08-03T00:00:00+00:00")
        vc.write_manifest(self.vault, project, manifest)
        again = vc.resolve_corpus(self.vault, self.schema, self.schema_hash, "Article 2", digest=True)
        drift = vc.compare_manifest(vc.read_manifest(self.vault, project), vc.build_manifest(again, None))
        self.assertEqual(drift["state"], "fresh")

    def test_absent_manifest_reports_absent(self):
        resolution = self.resolve(digest=True)
        drift = vc.compare_manifest(None, vc.build_manifest(resolution, None))
        self.assertEqual(drift["state"], "absent")

    def test_a_new_file_in_the_folder_makes_the_manifest_stale(self):
        resolution = self.resolve(digest=True)
        project = resolution["project"]
        vc.write_manifest(self.vault, project, vc.build_manifest(resolution, "2026-08-03T00:00:00+00:00"))
        self.write(f"{PROJECT_FOLDER}/New Idea.md",
                   note({"type": "note", "status": "active", "domain": "academic"}, "# New Idea\n"))
        again = vc.resolve_corpus(self.vault, self.schema, self.schema_hash, "Article 2", digest=True)
        drift = vc.compare_manifest(vc.read_manifest(self.vault, project), vc.build_manifest(again, None))
        self.assertEqual(drift["state"], "stale")
        self.assertIn(f"{PROJECT_FOLDER}/New Idea.md", drift["added"])

    def test_edited_member_is_content_drift_not_staleness(self):
        resolution = self.resolve(digest=True)
        project = resolution["project"]
        vc.write_manifest(self.vault, project, vc.build_manifest(resolution, "2026-08-03T00:00:00+00:00"))
        self.write(f"{PROJECT_FOLDER}/Working Draft.md",
                   note({"type": "note", "status": "active", "domain": "academic"},
                        "# Working Draft\n\nRewritten prose.\n"))
        again = vc.resolve_corpus(self.vault, self.schema, self.schema_hash, "Article 2", digest=True)
        drift = vc.compare_manifest(vc.read_manifest(self.vault, project), vc.build_manifest(again, None))
        self.assertEqual(drift["state"], "fresh")
        self.assertIn(f"{PROJECT_FOLDER}/Working Draft.md", drift["changed"])

    def test_renaming_a_member_trips_staleness(self):
        resolution = self.resolve(digest=True)
        project = resolution["project"]
        vc.write_manifest(self.vault, project, vc.build_manifest(resolution, "2026-08-03T00:00:00+00:00"))
        (self.vault / PROJECT_FOLDER / "Working Draft.md").rename(
            self.vault / PROJECT_FOLDER / "Working Draft v2.md"
        )
        again = vc.resolve_corpus(self.vault, self.schema, self.schema_hash, "Article 2", digest=True)
        drift = vc.compare_manifest(vc.read_manifest(self.vault, project), vc.build_manifest(again, None))
        self.assertEqual(drift["state"], "stale")
        self.assertIn(f"{PROJECT_FOLDER}/Working Draft.md", drift["removed"])


class InsertCorpusSectionTests(unittest.TestCase):
    SECTION = "## Corpus\n\n### Sources\n\n- [[A Source]] — \n"

    def test_the_section_lands_before_owner_authored_notes(self):
        body = (
            "---\ntype: project\n---\n\n# FORGE\n\nWhat the project is.\n\n"
            "## Links\n\n- [[Something]]\n\n## Notes\n\nMy own scratch, never written to.\n"
        )
        updated, where = vc.insert_corpus_section(body, self.SECTION)
        self.assertEqual(where, "before-notes")
        self.assertLess(updated.index("## Corpus"), updated.index("## Notes"))
        # everything the owner wrote survives, in order
        for fragment in ("# FORGE", "What the project is.", "## Links", "- [[Something]]",
                         "My own scratch, never written to."):
            self.assertIn(fragment, updated)
        self.assertEqual(updated.count("## Notes"), 1)

    def test_a_hub_without_a_notes_heading_gets_the_section_at_the_end(self):
        body = "---\ntype: project\n---\n\n# RAPID\n\nProse only.\n"
        updated, where = vc.insert_corpus_section(body, self.SECTION)
        self.assertEqual(where, "at-end")
        self.assertTrue(updated.rstrip().endswith("- [[A Source]] —"))
        self.assertIn("Prose only.", updated)

    def test_inserting_twice_is_refused_rather_than_duplicating_the_section(self):
        body = "---\ntype: project\n---\n\n# X\n\nProse.\n"
        once, _ = vc.insert_corpus_section(body, self.SECTION)
        with self.assertRaises(vc.UserError):
            vc.insert_corpus_section(once, self.SECTION)

    def test_nothing_outside_the_inserted_block_changes(self):
        body = (
            "---\ntype: project\n---\n\n# HoMEDUCS\n\nLine one.\n\n> [!summary]\n> A callout.\n\n"
            "## Notes\n\nOwner text.\n"
        )
        updated, _ = vc.insert_corpus_section(body, self.SECTION)
        removed = updated.replace(self.SECTION.rstrip("\n") + "\n", "")
        self.assertEqual(
            [line for line in removed.splitlines() if line.strip()],
            [line for line in body.splitlines() if line.strip()],
        )


if __name__ == "__main__":
    unittest.main()

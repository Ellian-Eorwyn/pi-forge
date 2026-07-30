#!/usr/bin/env python3

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "literature-library.py"
LIB = Path(__file__).resolve().parents[3] / "lib"
sys.dont_write_bytecode = True
sys.path.insert(0, str(LIB))

import citation_naming  # noqa: E402
import citation_parse  # noqa: E402

spec = importlib.util.spec_from_file_location("literature_library", SCRIPT)
literature_library = importlib.util.module_from_spec(spec)
spec.loader.exec_module(literature_library)

# PyMuPDF is an optional dependency everywhere else in this skill, so the tests
# that need a genuine PDF — one that opens, paginates, and yields text — skip
# rather than fail where it is absent. A fake fixture would not exercise the
# probe at all, which is the whole point of those cases.
try:
    import fitz  # noqa: F401

    HAVE_FITZ = True
except ImportError:
    HAVE_FITZ = False

NEEDS_FITZ = "PyMuPDF is not installed; real-PDF fixtures are unavailable"


# Derived from a real Consensus.app export. Every oddity here is one the real
# file has: `ER  - ` carries a trailing space, one title holds a literal U+FFFD
# that the exporter produced, one record has two authors, and both RIS author
# forms ("Family, Initial." and "Family, Given") appear.
FIXTURE = (
    "TY  - JOUR\n"
    "AU  - Goetze, T.\n"
    "TI  - Hermeneutical Dissent and the Species of Hermeneutical Injustice\n"
    "PY  - 2018\n"
    "DA  - 2018-02-01\n"
    "JO  - Hypatia\n"
    "VL  - 33\n"
    "SP  - 73\n"
    "EP  - 90\n"
    "DO  - 10.1111/hypa.12384\n"
    "UR  - https://consensus.app/papers/hermeneutical-dissent/a8cd0864/\n"
    "ER  - \n"
    "TY  - JOUR\n"
    "AU  - Carel, H.\n"
    "AU  - Kidd, I.\n"
    "TI  - Epistemic injustice in healthcare: a philosophial analysis\n"
    "PY  - 2014\n"
    "DA  - 2014-04-17\n"
    "JO  - Medicine, Health Care and Philosophy\n"
    "DO  - 10.1007/s11019-014-9560-2\n"
    "ER  - \n"
    "TY  - JOUR\n"
    "AU  - Crawford, Gordon\n"
    "TI  - Decolonising knowledge production on Africa: why it�s still necessary\n"
    "PY  - 2021\n"
    "DO  - 10.5871/jba/009s1.021\n"
    "ER  - \n"
    "TY  - JOUR\n"
    "AU  - Pohlhaus, Gaile\n"
    "TI  - Relational Knowing and Epistemic Injustice\n"
    "PY  - 2012\n"
    "AB  - This is a long abstract that the exporter wrapped\n"
    "      across a continuation line without any tag.\n"
    "DO  - 10.1111/j.1527-2001.2011.01222.x\n"
    "ER  - \n"
    "TY  - BOOK\n"
    "AU  - World Health Organization\n"
    "TI  - Global report on something\n"
    "PB  - WHO Press\n"
    "SN  - 9789241565349\n"
    "ER  - \n"
)


class RisParsingTests(unittest.TestCase):
    def test_parses_every_record_from_the_real_export_shape(self):
        parsed = citation_parse.parse_ris(FIXTURE)
        records = parsed["records"]
        self.assertEqual(len(records), 5)
        self.assertEqual(records[0]["canonical_title"], "Hermeneutical Dissent and the Species of Hermeneutical Injustice")
        self.assertEqual(records[0]["identifiers"]["doi"], "10.1111/hypa.12384")
        self.assertEqual(records[0]["publication_year"], 2018)
        self.assertEqual(records[0]["venue_name"], "Hypatia")
        self.assertEqual(records[0]["pages"], "73-90")
        self.assertEqual(records[0]["type"], "journal-article")

    def test_trailing_space_after_er_still_terminates_a_record(self):
        # Consensus writes "ER  - " with a trailing space; the Forge writer does
        # not. Both must terminate a record or every record after the first merges.
        self.assertEqual(len(citation_parse.parse_ris(FIXTURE)["records"]), 5)
        self.assertEqual(len(citation_parse.parse_ris(FIXTURE.replace("ER  - \n", "ER  -\n"))["records"]), 5)

    def test_repeated_author_tags_accumulate(self):
        record = citation_parse.parse_ris(FIXTURE)["records"][1]
        self.assertEqual([author["family"] for author in record["authors"]], ["Carel", "Kidd"])
        self.assertEqual(record["authors"][0]["given"], "H.")

    def test_both_ris_author_forms_parse(self):
        records = citation_parse.parse_ris(FIXTURE)["records"]
        self.assertEqual(records[0]["authors"][0]["family"], "Goetze")
        self.assertEqual(records[3]["authors"][0]["family"], "Pohlhaus")
        self.assertEqual(records[3]["authors"][0]["given"], "Gaile")

    def test_wrapped_value_is_joined_as_a_continuation(self):
        record = citation_parse.parse_ris(FIXTURE)["records"][3]
        self.assertEqual(
            record["abstract_best"],
            "This is a long abstract that the exporter wrapped across a continuation line without any tag.",
        )

    def test_replacement_character_is_preserved_by_default(self):
        record = citation_parse.parse_ris(FIXTURE)["records"][2]
        self.assertIn("�", record["canonical_title"])

    def test_replacement_character_repair_is_recorded(self):
        parsed = citation_parse.parse_text(FIXTURE, repair_replacements=True)
        self.assertNotIn("�", parsed["records"][2]["canonical_title"])
        self.assertIn("it’s", parsed["records"][2]["canonical_title"])
        self.assertEqual(len(parsed["normalizations"]), 1)
        self.assertIn("before", parsed["normalizations"][0])

    def test_isbn_is_distinguished_from_issn_by_shape(self):
        record = citation_parse.parse_ris(FIXTURE)["records"][4]
        self.assertEqual(record["identifiers"]["isbn"], "9789241565349")
        self.assertEqual(record["type"], "book")

    def test_unknown_tag_pattern_in_prose_is_not_read_as_a_tag(self):
        # A wrapped abstract line beginning "US - based" must stay part of the
        # abstract; treating it as a tag would silently truncate the value.
        text = "TY  - JOUR\nTI  - Title\nAB  - Findings from\nUS - based studies were mixed.\nER  - \n"
        record = citation_parse.parse_ris(text)["records"][0]
        self.assertEqual(record["abstract_best"], "Findings from US - based studies were mixed.")

    def test_doi_normalization_strips_every_common_prefix(self):
        for value in (
            "10.1111/hypa.12384",
            "https://doi.org/10.1111/hypa.12384",
            "http://dx.doi.org/10.1111/HYPA.12384",
            "doi: 10.1111/hypa.12384",
            "10.1111/hypa.12384.",
        ):
            self.assertEqual(citation_parse.normalize_doi(value), "10.1111/hypa.12384", value)
        self.assertIsNone(citation_parse.normalize_doi("not-a-doi"))
        self.assertIsNone(citation_parse.normalize_doi(""))


class EncodingTests(unittest.TestCase):
    def _records(self, raw):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "citations.ris"
            path.write_bytes(raw)
            return citation_parse.parse_file(path)

    def test_utf8_with_bom_yields_identical_records(self):
        plain = self._records(FIXTURE.encode("utf-8"))
        with_bom = self._records(b"\xef\xbb\xbf" + FIXTURE.encode("utf-8"))
        self.assertFalse(plain["had_bom"])
        self.assertTrue(with_bom["had_bom"])
        self.assertEqual(
            [record["canonical_title"] for record in plain["records"]],
            [record["canonical_title"] for record in with_bom["records"]],
        )

    def test_cp1252_source_decodes_without_replacement(self):
        text = FIXTURE.replace("Crawford, Gordon", "Gagn\xe9-Julien, Anne-Marie").replace("�", "'")
        parsed = self._records(text.encode("cp1252"))
        self.assertEqual(parsed["encoding_detected"], "cp1252")
        self.assertFalse(parsed["decode_errors_replaced"])
        self.assertEqual(parsed["records"][2]["authors"][0]["family"], "Gagn\xe9-Julien")

    def test_source_replacement_characters_are_counted_and_reported(self):
        parsed = self._records(FIXTURE.encode("utf-8"))
        self.assertEqual(parsed["replacement_char_count"], 1)
        self.assertTrue(any("lost before export" in warning for warning in parsed["warnings"]))


class WriterSymmetryTests(unittest.TestCase):
    # `buildRisRecord` in forge/skills/web-research/scripts/web-research.mjs is
    # the coupled writer. Every tag it emits must survive a round trip through
    # this reader, or a Forge-written RIS file cannot be read back by Forge.
    WRITER_TAGS = ("TY", "TI", "AU", "AB", "PY", "Y1", "JO", "T2", "PB", "DO", "UR", "KW", "SN", "N1", "ER")

    def test_every_tag_the_forge_writer_emits_is_read_back(self):
        text = (
            "TY  - JOUR\n"
            "TI  - A Written Title\n"
            "AU  - Writer, Ada\n"
            "AB  - An abstract.\n"
            "PY  - 2020\n"
            "Y1  - 2020-06-01\n"
            "JO  - Journal of Symmetry\n"
            "T2  - Journal of Symmetry\n"
            "PB  - A Publisher\n"
            "DO  - 10.1234/symmetry.1\n"
            "UR  - https://example.org/article\n"
            "KW  - symmetry\n"
            "KW  - round trip\n"
            "SN  - 1234-567X\n"
            "N1  - A note.\n"
            "ER  -\n"
        )
        record = citation_parse.parse_ris(text)["records"][0]
        self.assertEqual(record["canonical_title"], "A Written Title")
        self.assertEqual(record["authors"][0]["family"], "Writer")
        self.assertEqual(record["abstract_best"], "An abstract.")
        self.assertEqual(record["publication_year"], 2020)
        self.assertEqual(record["publication_date"], "2020-06-01")
        self.assertEqual(record["venue_name"], "Journal of Symmetry")
        self.assertEqual(record["publisher"], "A Publisher")
        self.assertEqual(record["identifiers"]["doi"], "10.1234/symmetry.1")
        self.assertEqual(record["urls"], ["https://example.org/article"])
        self.assertEqual(record["keywords"], ["symmetry", "round trip"])
        self.assertEqual(record["identifiers"]["issn"], "1234-567X")
        self.assertEqual(record["notes"], ["A note."])

    def test_the_writer_tag_list_is_covered_by_the_reader_vocabulary(self):
        for tag in self.WRITER_TAGS:
            self.assertIn(tag, citation_parse.KNOWN_TAGS, tag)

    def test_zotero_file_links_become_full_text_candidates(self):
        text = "TY  - JOUR\nTI  - T\nL1  - https://example.org/paper.pdf\nER  -\n"
        record = citation_parse.parse_ris(text)["records"][0]
        self.assertEqual(record["full_text_candidates"], [{"url": "https://example.org/paper.pdf", "source": "ris-file-link"}])


class MalformedInputTests(unittest.TestCase):
    def test_unterminated_final_record_is_recovered_with_a_warning(self):
        text = "TY  - JOUR\nTI  - First\nER  -\nTY  - JOUR\nTI  - Truncated\n"
        parsed = citation_parse.parse_ris(text)
        self.assertEqual(len(parsed["records"]), 2)
        self.assertEqual(parsed["records"][1]["canonical_title"], "Truncated")
        self.assertTrue(any("no ER terminator" in warning for warning in parsed["warnings"]))

    def test_new_record_before_terminator_is_an_error_with_a_line_number(self):
        text = "TY  - JOUR\nTI  - First\nTY  - JOUR\nTI  - Second\nER  -\n"
        with self.assertRaises(citation_parse.CitationParseError) as raised:
            citation_parse.parse_ris(text)
        self.assertIn("line 3", str(raised.exception))

    def test_continuation_before_any_tag_is_an_error_with_a_line_number(self):
        with self.assertRaises(citation_parse.CitationParseError) as raised:
            citation_parse.parse_ris("stray text before anything\nTY  - JOUR\nER  -\n")
        self.assertIn("line 1", str(raised.exception))

    def test_bibtex_fails_loudly_rather_than_half_parsing(self):
        with self.assertRaises(citation_parse.CitationParseError) as raised:
            citation_parse.parse_text("@article{key,\n  title = {A Title}\n}\n")
        self.assertIn("BibTeX", str(raised.exception))

    def test_unrecognized_format_is_rejected(self):
        with self.assertRaises(citation_parse.CitationParseError):
            citation_parse.parse_text("just some prose, not a citation file\n")


class NamingTests(unittest.TestCase):
    def _record(self, authors, year, title, date=None):
        return {
            "authors": [{"family": None, "given": None, "name": name} for name in authors],
            "publication_year": year,
            "publication_date": date,
            "canonical_title": title,
        }

    def test_surname_forms(self):
        cases = {
            "Goetze, T.": "Goetze",
            "Pohlhaus, Gaile": "Pohlhaus",
            "Gagn\xe9-Julien, Anne-Marie": "Gagn\xe9-Julien",
            "Ndlovu-Gatsheni, Sabelo J.": "Ndlovu-Gatsheni",
            "van der Heijden, M.": "van der Heijden",
            "M. van der Heijden": "van der Heijden",
            "de la Cruz, Maria": "de la Cruz",
            "Smith, John, Jr.": "Smith",
            "John Smith Jr.": "Smith",
        }
        for value, expected in cases.items():
            self.assertEqual(citation_naming.author_surname({"name": value})[0], expected, value)

    def test_family_field_is_preferred_over_parsing_a_display_name(self):
        surname, kind = citation_naming.author_surname({"family": "Ndlovu-Gatsheni", "given": "S.", "name": "S. Ndlovu-Gatsheni"})
        self.assertEqual((surname, kind), ("Ndlovu-Gatsheni", "person"))

    def test_corporate_authors_are_detected_and_flagged(self):
        surname, kind = citation_naming.author_surname({"name": "World Health Organization"})
        self.assertEqual((surname, kind), ("World Health Organization", "corporate"))
        naming = citation_naming.derive_stems([self._record(["World Health Organization"], 2021, "Global report")])
        self.assertTrue(any("corporate" in flag for flag in naming[0]["flags"]))

    def test_missing_author_and_year_use_the_documented_fallbacks(self):
        naming = citation_naming.derive_stems([self._record([], None, "Orphan paper")])
        self.assertEqual(naming[0]["stem"], "Unknown - n.d. - Orphan paper")

    def test_year_falls_back_to_the_date_field(self):
        naming = citation_naming.derive_stems([self._record(["Carel, H."], None, "Title", date="2014-04-17")])
        self.assertEqual(naming[0]["year"], "2014")

    def test_filename_unsafe_characters_are_removed_or_substituted(self):
        naming = citation_naming.derive_stems(
            [self._record(["X, Y"], 2020, 'Title: with [brackets] | pipes / slashes and "quotes" plus #hash ^caret')]
        )
        self.assertEqual(naming[0]["stem"], "X - 2020 - Title with (brackets) - pipes slashes and quotes plus hash caret")

    def test_long_titles_truncate_at_a_word_boundary_within_the_cap(self):
        title = (
            "The cognitive empire, politics of knowledge and African intellectual productions: "
            "reflections on struggles for epistemic freedom and resurgence of decolonisation in the twenty-first century"
        )
        naming = citation_naming.derive_stems([self._record(["Ndlovu-Gatsheni, S."], 2020, title)])
        built = naming[0]
        self.assertTrue(built["title_truncated"])
        self.assertLessEqual(len(built["stem"]), citation_naming.MAX_STEM)
        self.assertFalse(built["stem"].endswith("-"))
        self.assertNotIn("...", built["stem"])

    def test_colliding_filenames_are_lettered_citation_style(self):
        records = [self._record(["Carel, H."], 2014, "Same title exactly") for _ in range(3)]
        stems = [built["stem"] for built in citation_naming.derive_stems(records)]
        self.assertEqual(
            stems,
            [
                "Carel - 2014a - Same title exactly",
                "Carel - 2014b - Same title exactly",
                "Carel - 2014c - Same title exactly",
            ],
        )

    def test_distinct_titles_by_the_same_author_and_year_are_not_lettered(self):
        records = [self._record(["Carel, H."], 2014, "First paper"), self._record(["Carel, H."], 2014, "Second paper")]
        for built in citation_naming.derive_stems(records):
            self.assertIsNone(built["year_letter"])

    def test_a_stem_published_by_an_earlier_run_is_not_reused(self):
        naming = citation_naming.derive_stems(
            [self._record(["Carel, H."], 2014, "Same title exactly")],
            reserved={"Carel - 2014 - Same title exactly"},
        )
        self.assertEqual(naming[0]["stem"], "Carel - 2014a - Same title exactly")

    def test_every_derived_stem_is_already_filename_safe(self):
        from vault_schema import safe_title

        records = [
            self._record(["Goetze, T."], 2018, "Hermeneutical Dissent"),
            self._record(["World Health Organization"], 2021, "A" * 200),
            self._record([], None, "Title: with unsafe/chars"),
            self._record(["A" * 80 + ", B"], 2020, "Long corporate style author"),
        ]
        for built in citation_naming.derive_stems(records):
            # safe_title is idempotent, so this is the real invariant: the
            # assembled stem must already be what safe_title would produce.
            self.assertEqual(safe_title(built["stem"]), built["stem"])
            self.assertLessEqual(len(built["stem"]), citation_naming.MAX_STEM)


class CommandTests(unittest.TestCase):
    def _run(self, *arguments):
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            capture_output=True,
            text=True,
            check=False,
            env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "PYTHONDONTWRITEBYTECODE": "1"},
        )
        return completed

    def _fixture_run(self, directory, **kwargs):
        source = Path(directory) / "citations.ris"
        source.write_text(FIXTURE, encoding="utf-8")
        output = Path(directory) / "run"
        arguments = ["parse", str(source), "--output", str(output), "--contact-email", "someone@example.org"]
        arguments += kwargs.get("extra", [])
        return source, output, self._run(*arguments)

    def test_doctor_reports_capabilities_as_json(self):
        completed = self._run("doctor")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["workflow"], "literature-library")
        self.assertFalse(payload["formats"]["bibtex"])

    def test_parse_scaffolds_a_run_without_touching_the_network(self):
        with tempfile.TemporaryDirectory() as directory:
            _, output, completed = self._fixture_run(directory)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["status"], "parsed")
            self.assertEqual(payload["records"], 5)
            self.assertEqual(payload["withDoi"], 4)
            for artifact in ("run_state.json", "library_config.json", "library_index.jsonl", "library_plan.md"):
                self.assertTrue((output / artifact).is_file(), artifact)
            # Nothing may be published before acquisition runs.
            self.assertFalse((output / "pdf").exists())

    def test_plan_lists_every_derived_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            _, output, _ = self._fixture_run(directory)
            plan = (output / "library_plan.md").read_text(encoding="utf-8")
            self.assertIn("Goetze - 2018 - Hermeneutical Dissent and the Species of Hermeneutical Injustice.pdf", plan)
            self.assertIn("Nothing has been downloaded", plan)

    def test_duplicate_dois_are_merged_once(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "citations.ris"
            source.write_text(FIXTURE + FIXTURE.split("ER  - \n")[0] + "ER  - \n", encoding="utf-8")
            output = Path(directory) / "run"
            completed = self._run(
                "parse", str(source), "--output", str(output), "--contact-email", "someone@example.org"
            )
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["records"], 6)
            self.assertEqual(payload["unique"], 5)
            self.assertEqual(payload["duplicates"], 1)

    def test_rerunning_parse_resumes_instead_of_rebuilding(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output, _ = self._fixture_run(directory)
            again = self._run("parse", str(source), "--output", str(output), "--contact-email", "someone@example.org")
            self.assertEqual(json.loads(again.stdout)["status"], "resumed")

    def test_populated_output_without_run_state_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "citations.ris"
            source.write_text(FIXTURE, encoding="utf-8")
            output = Path(directory) / "run"
            output.mkdir()
            (output / "stray.txt").write_text("unrelated", encoding="utf-8")
            completed = self._run(
                "parse", str(source), "--output", str(output), "--contact-email", "someone@example.org"
            )
            self.assertEqual(completed.returncode, 1)
            self.assertIn("run_state.json", completed.stderr)

    def test_validate_is_valid_but_incomplete_before_acquisition(self):
        with tempfile.TemporaryDirectory() as directory:
            _, output, _ = self._fixture_run(directory)
            completed = self._run("validate", str(output), "--json", "--read-only")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["valid"])
            self.assertFalse(payload["complete"])
            self.assertEqual(payload["pending"], 5)

    def test_validate_reports_deferrals_as_warnings_and_still_completes(self):
        # A run whose remaining records need an institutional connection has
        # finished its work; refusing to import it would make the library
        # unusable from any residential connection.
        with tempfile.TemporaryDirectory() as directory:
            _, output, _ = self._fixture_run(directory)
            state = json.loads((output / "run_state.json").read_text(encoding="utf-8"))
            for index, item in enumerate(state["items"]):
                item["disposition"] = "deferred-institutional" if index else "acquired"
            (output / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
            payload = json.loads(self._run("validate", str(output), "--json", "--read-only").stdout)
            self.assertTrue(payload["valid"])
            self.assertTrue(payload["complete"])
            self.assertTrue(any("institutional" in warning for warning in payload["warnings"]))

    def test_status_reports_dispositions_and_no_drift_for_an_unchanged_source(self):
        with tempfile.TemporaryDirectory() as directory:
            _, output, _ = self._fixture_run(directory)
            payload = json.loads(self._run("status", str(output), "--json").stdout)
            self.assertEqual(payload["dispositions"], {"pending": 5})
            self.assertEqual(payload["inputDrift"]["changed"], [])

    def test_status_reports_drift_when_the_source_file_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output, _ = self._fixture_run(directory)
            source.write_text(FIXTURE + "TY  - JOUR\nTI  - Added later\nER  -\n", encoding="utf-8")
            payload = json.loads(self._run("status", str(output), "--json").stdout)
            self.assertEqual(len(payload["inputDrift"]["changed"]), 1)

    def test_missing_citation_file_fails_with_a_clear_message(self):
        with tempfile.TemporaryDirectory() as directory:
            completed = self._run(
                "parse",
                str(Path(directory) / "absent.ris"),
                "--output",
                str(Path(directory) / "run"),
                "--contact-email",
                "someone@example.org",
            )
            self.assertEqual(completed.returncode, 1)
            self.assertIn("not found", completed.stderr)


def _minimal_pdf():
    """A structurally valid one-page PDF.

    It has to genuinely open, not merely start with the magic number: the whole
    point of `verify_pdf` is that a file carrying a `%PDF-` header can still be
    unusable, so a fake fixture would test the wrong thing.
    """
    try:
        import fitz
    except ImportError:
        return b"%PDF-1.4\n% minimal fixture; PyMuPDF unavailable so structure is unchecked\n%%EOF\n"
    document = fitz.open()
    document.new_page()
    body = document.tobytes()
    document.close()
    return body


PDF_BODY = _minimal_pdf()
LANDING_HTML = b'<html><head><meta name="citation_pdf_url" content="/good.pdf"></head><body>x</body></html>'


class AcquirerFixtureHandler(BaseHTTPRequestHandler):
    hits = {}

    def log_message(self, *arguments):
        pass

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        AcquirerFixtureHandler.hits[self.path] = AcquirerFixtureHandler.hits.get(self.path, 0) + 1
        if self.path == "/good.pdf":
            self._send(200, "application/pdf", PDF_BODY)
        elif self.path == "/liar.pdf":
            # A paywall interstitial mislabeled as a PDF. Publishers really do this,
            # which is why only the magic number may be trusted.
            self._send(200, "application/pdf", b"<html><body>Purchase access</body></html>")
        elif self.path == "/blocked.pdf":
            self._send(403, "text/html", b"<html>denied</html>")
        elif self.path == "/landing":
            self._send(200, "text/html", LANDING_HTML)
        elif self.path == "/loop-a":
            self._redirect("/loop-b")
        elif self.path == "/loop-b":
            self._redirect("/loop-a")
        elif self.path == "/chain":
            self._redirect("/good.pdf")
        elif self.path == "/huge.pdf":
            self._send(200, "application/pdf", b"%PDF-1.4" + b"x" * 200_000)
        else:
            self._send(404, "text/plain", b"nope")

    def _send(self, status, content_type, body):
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("location", location)
        self.send_header("content-length", "0")
        self.end_headers()


class AcquirerTests(unittest.TestCase):
    """Exercises the Node acquirer against local fixtures.

    No test in this file may contact a real publisher: the fixtures cover every
    outcome the ladder distinguishes, and `allowPrivateHosts` is the only reason
    a loopback fixture is reachable at all.
    """

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), AcquirerFixtureHandler)
        cls.origin = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        AcquirerFixtureHandler.hits.clear()
        self._directory = tempfile.TemporaryDirectory()
        self.stage = Path(self._directory.name) / "stage"
        self.stage.mkdir()

    def tearDown(self):
        self._directory.cleanup()

    def _acquire(self, records, **overrides):
        payload = {
            "contactEmail": "test@example.org",
            "stageDirectory": str(self.stage),
            "allowPrivateHosts": True,
            "resolve": False,
            "hostDelayMs": 2000,
            "maxBytes": 100_000,
            "records": records,
            **overrides,
        }
        completed = subprocess.run(
            ["node", str(SCRIPT.parent / "acquire_pdf.mjs")],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertTrue(completed.stdout.strip(), completed.stderr)
        return json.loads(completed.stdout)

    def _record(self, identifier, path, access="open-access"):
        return {
            "id": identifier,
            "doi": None,
            "accessClass": access,
            "fullTextCandidates": [{"url": f"{self.origin}{path}", "source": "fixture"}],
        }

    def test_a_real_pdf_is_acquired_and_staged(self):
        result = self._acquire([self._record("r1", "/good.pdf")])
        entry = result["data"]["results"][0]
        self.assertEqual(entry["disposition"], "acquired")
        self.assertEqual(entry["stage"], "direct")
        self.assertEqual(entry["sha256"], hashlib.sha256(PDF_BODY).hexdigest())
        self.assertTrue(Path(entry["stagedPath"]).is_file())

    def test_html_mislabeled_as_pdf_is_never_accepted(self):
        entry = self._acquire([self._record("r1", "/liar.pdf")])["data"]["results"][0]
        self.assertNotEqual(entry["disposition"], "acquired")
        self.assertEqual([attempt["outcome"] for attempt in entry["attempts"]], ["landing-page"])

    def test_landing_page_citation_pdf_url_is_followed_once(self):
        entry = self._acquire([self._record("r1", "/landing")])["data"]["results"][0]
        self.assertEqual(entry["disposition"], "acquired")
        self.assertEqual(entry["stage"], "landing-scrape")

    def test_redirect_chain_is_followed(self):
        entry = self._acquire([self._record("r1", "/chain")])["data"]["results"][0]
        self.assertEqual(entry["disposition"], "acquired")

    def test_redirect_loop_is_detected_rather_than_hanging(self):
        entry = self._acquire([self._record("r1", "/loop-a")])["data"]["results"][0]
        self.assertEqual(entry["disposition"], "manual")
        self.assertIn("loop", entry["attempts"][0]["detail"])

    def test_oversize_body_is_refused_and_not_staged(self):
        entry = self._acquire([self._record("r1", "/huge.pdf")])["data"]["results"][0]
        self.assertNotEqual(entry["disposition"], "acquired")
        self.assertEqual(entry["attempts"][0]["outcome"], "too-large")
        self.assertEqual(list(self.stage.iterdir()), [])

    def test_missing_url_reports_not_found(self):
        entry = self._acquire([self._record("r1", "/absent.pdf")])["data"]["results"][0]
        self.assertEqual(entry["disposition"], "not-found")

    def test_circuit_breaker_stops_after_three_refusals(self):
        records = [self._record(f"b{index}", "/blocked.pdf") for index in range(5)]
        result = self._acquire(records)
        # The fixture must observe exactly three requests: the fourth and fifth
        # records are refused locally without contacting the host again.
        self.assertEqual(AcquirerFixtureHandler.hits["/blocked.pdf"], CIRCUIT_BREAKER_THRESHOLD := 3)
        self.assertEqual(result["data"]["trippedHosts"], ["127.0.0.1"])
        outcomes = [entry["attempts"][0]["outcome"] for entry in result["data"]["results"]]
        self.assertEqual(outcomes, ["blocked", "blocked", "blocked", "host-tripped", "host-tripped"])

    def test_institutional_record_is_deferred_without_campus_egress(self):
        result = self._acquire([self._record("r1", "/good.pdf", access="institutional")], campusEgress=False)
        entry = result["data"]["results"][0]
        self.assertEqual(entry["disposition"], "deferred-institutional")
        self.assertEqual(entry["nextAction"], "connect-vpn-and-resume")
        # Nothing may be requested for a deferred record.
        self.assertEqual(AcquirerFixtureHandler.hits, {})

    def test_institutional_record_never_uses_the_remote_browser(self):
        # A remote browser egresses from another host and cannot carry the
        # operator's institutional access, so asking for it must be refused.
        result = self._acquire(
            [self._record("r1", "/blocked.pdf", access="institutional")], campusEgress=True, browser=True
        )
        entry = result["data"]["results"][0]
        self.assertTrue(any("cannot carry institutional access" in warning for warning in entry["warnings"]))
        self.assertFalse(any(str(attempt.get("source", "")).startswith("browser") for attempt in entry["attempts"]))

    def test_private_hosts_are_refused_without_the_test_opt_in(self):
        # The production path never sets allowPrivateHosts, so this is what a
        # citation file pointing at an internal address would hit.
        result = self._acquire([self._record("r1", "/good.pdf")], allowPrivateHosts=False)
        entry = result["data"]["results"][0]
        self.assertNotEqual(entry["disposition"], "acquired")
        self.assertIn("refused", entry["attempts"][0]["detail"])

    def test_arxiv_doi_yields_a_derived_candidate(self):
        # Unpaywall does not index every 10.48550/arxiv.* DOI, so the identifier
        # in the DOI itself has to be enough. Verified against a local fixture by
        # checking the candidate list, not by contacting arXiv.
        result = self._acquire(
            [{"id": "r1", "doi": "10.48550/arxiv.2408.11441", "arxivId": "2408.11441", "accessClass": "open-access", "fullTextCandidates": []}],
            allowPrivateHosts=False,
        )
        entry = result["data"]["results"][0]
        self.assertTrue(any("arxiv.org/pdf/2408.11441" in (attempt.get("url") or "") for attempt in entry["attempts"]))

    def test_rate_limiter_floor_cannot_be_configured_away(self):
        started = time.monotonic()
        self._acquire(
            [self._record("r1", "/good.pdf"), self._record("r2", "/good.pdf")],
            hostDelayMs=0,
        )
        # Two requests to one host with the delay set to zero must still be
        # separated by the 2s floor.
        self.assertGreaterEqual(time.monotonic() - started, 2.0)


class ToolContractTests(unittest.TestCase):
    def _run(self, payload, *arguments):
        return subprocess.run(
            ["node", str(SCRIPT.parent / "acquire_pdf.mjs"), *arguments],
            input=payload,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_unknown_option_is_rejected(self):
        completed = self._run("{}", "--nope")
        self.assertEqual(json.loads(completed.stdout)["errors"][0]["code"], "unknown_option")

    def test_invalid_json_reports_the_documented_error_shape(self):
        result = json.loads(self._run("{not json").stdout)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["errors"][0]["code"], "invalid_json")

    def test_missing_records_is_rejected(self):
        result = json.loads(self._run(json.dumps({"contactEmail": "a@b.c", "stageDirectory": "/tmp"})).stdout)
        self.assertEqual(result["errors"][0]["code"], "missing_required_field")


class PublishTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.output = Path(self._directory.name)
        (self.output / "pdf").mkdir()

    def tearDown(self):
        self._directory.cleanup()

    def _stage(self, body=PDF_BODY):
        staged = self.output / "staged.bin"
        staged.write_bytes(body)
        return staged, hashlib.sha256(body).hexdigest()

    def test_publish_moves_and_journals_the_operation(self):
        staged, digest = self._stage()
        destination = self.output / "pdf" / "Author - 2020 - Title.pdf"
        outcome, problem = literature_library.publish_file(self.output, "item-1", staged, destination, digest)
        self.assertEqual((outcome, problem), ("published", None))
        self.assertTrue(destination.is_file())
        self.assertFalse(staged.exists())
        rows = [json.loads(line) for line in (self.output / "publish_ops.jsonl").read_text().splitlines()]
        self.assertEqual([row["status"] for row in rows], ["planned", "completed"])

    def test_republishing_identical_content_is_idempotent(self):
        staged, digest = self._stage()
        destination = self.output / "pdf" / "Author - 2020 - Title.pdf"
        literature_library.publish_file(self.output, "item-1", staged, destination, digest)
        again, digest_again = self._stage()
        outcome, _ = literature_library.publish_file(self.output, "item-1", again, destination, digest_again)
        self.assertEqual(outcome, "already-published")

    def test_a_mismatched_destination_is_blocked_and_never_overwritten(self):
        destination = self.output / "pdf" / "Author - 2020 - Title.pdf"
        destination.write_bytes(b"%PDF-1.4 someone else's file")
        staged, digest = self._stage()
        outcome, problem = literature_library.publish_file(self.output, "item-1", staged, destination, digest)
        self.assertEqual(outcome, "blocked")
        self.assertIn("different content", problem)
        self.assertEqual(destination.read_bytes(), b"%PDF-1.4 someone else's file")

    def test_reconcile_replays_an_interrupted_move(self):
        staged, digest = self._stage()
        destination = self.output / "pdf" / "Author - 2020 - Title.pdf"
        # Journal the operation but never perform it, which is what a crash
        # between the journal write and the rename leaves behind.
        run_state_module = literature_library.run_state
        run_state_module.append_jsonl_fsync(
            self.output / "publish_ops.jsonl",
            {"opId": "item-1:pdf", "status": "planned", "from": str(staged), "fromSha256": digest, "to": str(destination), "toSha256": digest},
        )
        blocked, _ = literature_library.reconcile_publish_ops(self.output)
        self.assertEqual(blocked, [])
        self.assertTrue(destination.is_file())

    def test_reconcile_blocks_a_destination_with_unexpected_content(self):
        staged, digest = self._stage()
        destination = self.output / "pdf" / "Author - 2020 - Title.pdf"
        destination.write_bytes(b"%PDF-1.4 unexpected")
        literature_library.run_state.append_jsonl_fsync(
            self.output / "publish_ops.jsonl",
            {"opId": "item-1:pdf", "status": "planned", "from": str(staged), "fromSha256": digest, "to": str(destination), "toSha256": digest},
        )
        blocked, _ = literature_library.reconcile_publish_ops(self.output)
        self.assertEqual(len(blocked), 1)
        self.assertEqual(destination.read_bytes(), b"%PDF-1.4 unexpected")

    def test_verify_pdf_rejects_non_pdf_content(self):
        path = self.output / "not.pdf"
        path.write_bytes(b"<html>nope</html>")
        usable, detail = literature_library.verify_pdf(path)
        self.assertFalse(usable)
        self.assertIn("%PDF", detail)

    def test_verify_pdf_accepts_a_real_pdf(self):
        path = self.output / "real.pdf"
        path.write_bytes(PDF_BODY)
        usable, _ = literature_library.verify_pdf(path)
        self.assertTrue(usable)


def _text_pdf(path, pages=2, line="Epistemic injustice is a wrong done to someone in their capacity as a knower. "):
    """A born-digital PDF with enough real text to route away from OCR."""
    import fitz

    document = fitz.open()
    for _ in range(pages):
        page = document.new_page()
        for row in range(30):
            page.insert_text((56, 72 + row * 18), line, fontsize=11)
    document.save(path)
    document.close()


def _image_only_pdf(path, source):
    """A rasterized PDF: real pages, zero extractable text. The OCR case."""
    import fitz

    original = fitz.open(source)
    scanned = fitz.open()
    for index in range(original.page_count):
        pixmap = original[index].get_pixmap(dpi=110)
        page = scanned.new_page(width=pixmap.width * 0.75, height=pixmap.height * 0.75)
        page.insert_image(page.rect, pixmap=pixmap)
    scanned.save(path)
    scanned.close()
    original.close()


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)

    def tearDown(self):
        self._directory.cleanup()

    @unittest.skipUnless(HAVE_FITZ, NEEDS_FITZ)
    def test_born_digital_pdf_does_not_escalate_to_ocr(self):
        path = self.root / "digital.pdf"
        _text_pdf(path)
        probe = literature_library.probe_pdf(path)
        self.assertFalse(probe["needsOcr"])
        self.assertGreater(probe["alnumPerPage"], literature_library.MIN_ALNUM_CHARS_PER_PAGE)
        self.assertEqual(probe["emptyRatio"], 0.0)

    @unittest.skipUnless(HAVE_FITZ, NEEDS_FITZ)
    def test_image_only_pdf_escalates_to_ocr(self):
        digital = self.root / "digital.pdf"
        _text_pdf(digital)
        scanned = self.root / "scanned.pdf"
        _image_only_pdf(scanned, digital)
        probe = literature_library.probe_pdf(scanned)
        self.assertTrue(probe["needsOcr"])
        self.assertIn("extractable text", probe["reason"])

    # Without PyMuPDF `probe_pdf` reports that it cannot tell rather than
    # routing to an OCR pass it also could not run, so the escalation this
    # asserts only exists when the library is installed.
    @unittest.skipUnless(HAVE_FITZ, NEEDS_FITZ)
    def test_unreadable_file_is_an_ocr_candidate_rather_than_a_crash(self):
        path = self.root / "broken.pdf"
        path.write_bytes(b"%PDF-1.4 truncated and invalid")
        probe = literature_library.probe_pdf(path)
        self.assertTrue(probe["needsOcr"])


class CoversheetTests(unittest.TestCase):
    def test_repository_coversheet_is_detected(self):
        body = "\n".join(
            [
                "# Title",
                "",
                "University of Bristol - Bristol Research Portal",
                "Version: Peer reviewed version",
                "Link to published version (if available): 10.1000/x",
                "Terms of Use for the portal are available online",
                "",
                "Do ill people suffer epistemic injustice?",
            ]
        )
        detected = literature_library.detect_coversheet(body)
        self.assertIsNotNone(detected)
        self.assertGreaterEqual(len(detected["markers"]), 3)

    def test_ordinary_article_text_is_not_flagged(self):
        body = "# Title\n\nIn this paper we argue that ill persons can experience epistemic injustice.\n"
        self.assertIsNone(literature_library.detect_coversheet(body))

    def test_detection_never_removes_text(self):
        # Flagging is the whole contract: a false positive must not be able to
        # delete the opening of an article.
        row = {"titleFull": "Real Title", "stem": "A - 2020 - Real Title", "authors": []}
        body = "# A---2020---Real-Title\n\nRepository\nVersion of record\nTerms of use\n\nActual first sentence."
        detected = literature_library.detect_coversheet(body)
        document = literature_library._markdown_document(row, body, "file-conversion-structural", {}, [], detected)
        self.assertIn("Actual first sentence.", document)
        self.assertIn("Repository", document)
        self.assertIn("left in place, not removed", document)


class MarkdownDocumentTests(unittest.TestCase):
    ROW = {
        "titleFull": "Epistemic injustice in healthcare: a philosophial analysis",
        "stem": "Carel - 2014 - Epistemic injustice in healthcare a philosophial analysis",
        "authors": [{"family": "Carel", "given": "H.", "name": "Carel, H."}],
        "publicationYear": 2014,
        "venueName": "Medicine, Health Care and Philosophy",
        "identifiers": {"doi": "10.1007/s11019-014-9560-2"},
        "accessClass": "open-access",
        "pdfSha256": "abc123",
    }

    def _build(self, body, warnings=(), coversheet=None):
        return literature_library._markdown_document(
            self.ROW, body, "file-conversion-structural", {"pages": 32}, list(warnings), coversheet
        )

    def test_the_mangled_heading_is_replaced_with_the_real_title(self):
        document = self._build("# Carel---2014---Epistemic-injustice-in-healthcare\n\nBody text.\n")
        self.assertNotIn("Carel---2014---", document)
        self.assertIn("# Epistemic injustice in healthcare: a philosophial analysis", document)
        self.assertIn("Body text.", document)

    def test_frontmatter_carries_the_bibliographic_record(self):
        document = self._build("# x\n\nBody.\n")
        self.assertIn('doi: "10.1007/s11019-014-9560-2"', document)
        self.assertIn('venue: "Medicine, Health Care and Philosophy"', document)
        self.assertIn('pdf_sha256: "abc123"', document)

    def test_numeric_fields_are_not_quoted(self):
        document = self._build("# x\n\nBody.\n")
        self.assertIn("year: 2014", document)
        self.assertIn("pdf_pages: 32", document)

    def test_a_title_with_a_colon_survives_yaml(self):
        document = self._build("# x\n\nBody.\n")
        header = document.split("---")[1]
        self.assertIn('title: "Epistemic injustice in healthcare: a philosophial analysis"', header)

    def test_conversion_warnings_become_needs_review_entries(self):
        document = self._build("# x\n\nBody.\n", warnings=["Tables are not reconstructed."])
        self.assertIn("needs_review:", document)
        self.assertIn("Tables are not reconstructed.", document)

    def test_a_body_without_a_heading_is_left_intact(self):
        document = self._build("First line of the article.\n\nSecond paragraph.\n")
        self.assertIn("First line of the article.", document)


@unittest.skipUnless(HAVE_FITZ, NEEDS_FITZ)
class ConvertCommandTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.run = Path(self._directory.name) / "run"
        (self.run / "pdf").mkdir(parents=True)
        self.pdf_name = "Author - 2020 - A born digital article.pdf"
        _text_pdf(self.run / "pdf" / self.pdf_name)
        digest = hashlib.sha256((self.run / "pdf" / self.pdf_name).read_bytes()).hexdigest()

        state = literature_library.run_state.create_run_state(
            workflow="literature-library",
            command="parse",
            input_config=[{"path": "fixture.ris", "sha256": "0" * 64}],
            options={},
            items=[{"id": "item-1", "stem": "Author - 2020 - A born digital article", "disposition": "acquired", "attempts": 1, "accessClass": "open-access", "doi": None}],
        )
        literature_library.run_state.initialize_run_state(self.run, state)
        literature_library.run_state.atomic_write_json(
            self.run / "library_config.json", {"workflow": "literature-library", "sourceLabel": "fixture", "contactEmail": "a@b.c", "input": {"path": "fixture.ris", "sha256": "0" * 64}}
        )
        literature_library.run_state.append_jsonl_fsync(
            self.run / "library_index.jsonl",
            {
                "id": "item-1",
                "stem": "Author - 2020 - A born digital article",
                "pdfFilename": self.pdf_name,
                "markdownFilename": "Author - 2020 - A born digital article.md",
                "titleFull": "A born digital article",
                "authors": [{"family": "Author", "given": "A.", "name": "Author, A."}],
                "publicationYear": 2020,
                "identifiers": {"doi": "10.1000/fixture"},
                "pdfSha256": digest,
                "disposition": "acquired",
            },
        )

    def tearDown(self):
        self._directory.cleanup()

    def _run(self, *arguments):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            capture_output=True,
            text=True,
            check=False,
            env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "PYTHONDONTWRITEBYTECODE": "1"},
        )

    def test_convert_publishes_markdown_under_the_bibliographic_name(self):
        completed = self._run("convert", str(self.run))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["converted"], 1)
        self.assertEqual(payload["viaStructural"], 1)
        self.assertEqual(payload["viaOcr"], 0)
        published = self.run / "markdown" / "Author - 2020 - A born digital article.md"
        self.assertTrue(published.is_file())
        text = published.read_text(encoding="utf-8")
        self.assertIn("# A born digital article", text)
        self.assertIn('doi: "10.1000/fixture"', text)

    def test_convert_is_idempotent(self):
        self._run("convert", str(self.run))
        payload = json.loads(self._run("convert", str(self.run)).stdout)
        self.assertEqual(payload["converted"], 0)

    def test_markdown_is_published_through_the_hash_bound_journal(self):
        self._run("convert", str(self.run))
        rows = [json.loads(line) for line in (self.run / "publish_ops.jsonl").read_text().splitlines()]
        markdown_ops = [row for row in rows if row["opId"].endswith(":md")]
        self.assertEqual(sorted(row["status"] for row in markdown_ops), ["completed", "planned"])

    def test_validate_catches_a_tampered_markdown_file(self):
        self._run("convert", str(self.run))
        published = self.run / "markdown" / "Author - 2020 - A born digital article.md"
        published.write_text("replaced", encoding="utf-8")
        payload = json.loads(self._run("validate", str(self.run), "--json", "--read-only").stdout)
        self.assertFalse(payload["valid"])
        self.assertTrue(any("no longer matches the hash" in error for error in payload["errors"]))


class EgressDetectionTests(unittest.TestCase):
    def test_detection_requires_two_independent_signals(self):
        result = literature_library.detect_campus_egress()
        self.assertIn("campusEgress", result)
        self.assertIsInstance(result["signals"], list)
        # Never true on one signal alone: a lingering search domain or a default
        # route on any unrelated tunnel would otherwise read as campus access.
        if len(result["signals"]) < 2:
            self.assertFalse(result["campusEgress"])

    def test_tailscale_is_not_mistaken_for_the_institution(self):
        # This machine runs Tailscale, which holds a default route and a
        # 100.64/10 CGNAT prefix. Neither may count as institutional egress.
        result = literature_library.detect_campus_egress()
        self.assertFalse(any(".ts.net" in signal for signal in result["signals"]))


if __name__ == "__main__":
    unittest.main()

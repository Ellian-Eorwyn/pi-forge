#!/usr/bin/env python3
"""Tests for the eval harness itself. No network, no model.

Everything here is about the machinery being trustworthy: a scorer that silently
mis-grades produces confident wrong numbers, which is worse than no suite.
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

EVALS = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(EVALS))
sys.path.insert(0, str(EVALS / "cases"))

# Imported by name, not by path: `judge.py` and `report.py` do `import harness`,
# and loading a second copy under another name would give the tests a different
# module object than the code under test uses.
harness = importlib.import_module("harness")


class ConfigurationTests(unittest.TestCase):
    def test_every_model_declares_a_context_ceiling(self):
        # A model without one silently inherits the 131072-token slot default,
        # and a case that overruns its real backend then fails at the server
        # where it should have been refused before the request.
        for model_id, entry in harness.models().items():
            with self.subTest(model=model_id):
                self.assertIn("url", entry)
                self.assertIn("model", entry)
                self.assertIsInstance(entry.get("contextTokens"), int, f"{model_id} has no contextTokens")
                self.assertGreater(entry["contextTokens"], 0)

    def test_resolve_model_ignores_local_connected_services(self):
        # Results have to be comparable between machines, so the eval never
        # reads the user's settings.json.
        service = harness.resolve_model("task-9b")
        self.assertEqual(service["url"], "http://llms:8007/v1/chat/completions")
        self.assertEqual(service["contextTokens"], 65792)
        self.assertEqual(service["chatTemplateKwargs"], {"enable_thinking": False})
        # Background scheduling would let an interactive turn preempt a call and
        # land it in the results as a model failure.
        self.assertFalse(service["scheduling"]["enabled"])

    def test_an_unknown_model_names_the_ones_that_exist(self):
        with self.assertRaises(harness.EvalError) as caught:
            harness.resolve_model("gpt-9")
        self.assertIn("chat-27b", str(caught.exception))

    def test_no_fixture_points_into_the_denied_trees(self):
        # The vault holds clinical records, therapy sessions, and live software
        # licence keys. None of it becomes a test artifact.
        for fixture_id, spec in harness.fixtures().items():
            for prefix in harness.DENIED_PREFIXES:
                self.assertFalse(
                    spec["path"].startswith(prefix),
                    f"{fixture_id} points at {spec['path']}, under the denied prefix {prefix!r}",
                )

    def test_every_fixture_is_pinned(self):
        for fixture_id, spec in harness.fixtures().items():
            with self.subTest(fixture=fixture_id):
                self.assertRegex(spec.get("sha256", ""), r"^[0-9a-f]{64}$")


class FingerprintTests(unittest.TestCase):
    """Written after a run labelled `task-9b` turned out to be a 4B, unrecoverably."""

    def test_llama_args_parse_into_flags_and_values(self):
        args = ["/path/llama-server", "--jinja", "--ctx-size", "32769", "--model", "/m/x.gguf", "--metrics"]
        self.assertEqual(
            harness._parse_llama_args(args),
            {"jinja": True, "ctx-size": "32769", "model": "/m/x.gguf", "metrics": True},
        )

    def test_quant_is_read_off_the_filename_when_metadata_lacks_it(self):
        self.assertEqual(harness._quant_from_filename("Qwen3.5-4B-UD-Q6_K_XL.gguf"), "Q6_K_XL")
        self.assertEqual(harness._quant_from_filename("Qwen3.5-9B-Q4_K_M.gguf"), "Q4_K_M")
        self.assertEqual(harness._quant_from_filename("model-f16.gguf"), "F16")
        self.assertIsNone(harness._quant_from_filename("mystery.gguf"))

    def test_a_swapped_model_is_caught_by_parameter_count(self):
        service = {"id": "task-9b", "url": "http://x/v1/chat/completions", "expectParams": 9_000_000_000}
        problems = harness.check_served(service, {"n_params": 4_205_751_296})
        self.assertTrue(problems)
        self.assertIn("not the ones this entry names", problems[0])

    def test_quantization_wobble_does_not_trip_the_check(self):
        # Same weights at a different quant move the count slightly; a different
        # model moves it a lot. The check has to tell those apart.
        service = {"id": "task-4b", "url": "http://x/v1/chat/completions", "expectParams": 4_205_751_296}
        self.assertEqual(harness.check_served(service, {"n_params": 4_180_000_000}), [])

    def test_a_swapped_model_is_caught_by_path(self):
        service = {"id": "task-9b", "url": "http://x/v1/chat/completions", "expectModelPath": "/m/nine.gguf"}
        problems = harness.check_served(service, {"modelPath": "/m/four.gguf"})
        self.assertTrue(problems)

    def test_an_entry_that_claims_nothing_is_not_second_guessed(self):
        service = {"id": "whatever", "url": "http://x/v1/chat/completions"}
        self.assertEqual(harness.check_served(service, {"n_params": 1}), [])

    def test_the_parameter_check_reads_the_shape_the_server_really_sends(self):
        # The bug this pins: `served_fingerprint` nests the server's metadata
        # under `meta`, `check_served` read `n_params` from the top level, and
        # the tests fed it a flat dict no server produces. So `expectParams` was
        # asserted by two entries and enforced on neither for as long as it
        # existed. Any future test of this must use the nested shape.
        service = {"id": "chat-27b", "url": "http://x/v1/chat/completions", "expectParams": 27_320_697_856}
        nested = {"meta": {"n_params": 4_205_751_296, "ftype": "Q6_K"}, "paramsB": 4.21}
        problems = harness.check_served(service, nested)
        self.assertTrue(problems, "a 4B behind a 27B entry has to be caught through meta.n_params")
        self.assertIn("not the ones this entry names", problems[0])
        self.assertEqual(harness.served_params(nested), 4_205_751_296)

    def test_a_requant_is_caught_by_quantization_where_the_count_cannot_be(self):
        # Same architecture at a different quant sits inside the 10% parameter
        # tolerance on purpose, so quant is the only thing that separates them.
        service = {
            "id": "chat-27b-q4",
            "url": "http://x/v1/chat/completions",
            "expectParams": 27_320_697_856,
            "expectQuant": "Q4",
        }
        served = {"meta": {"n_params": 27_320_697_856, "ftype": "Q6_K"}, "quant": "Q6_K"}
        self.assertEqual(harness.check_served({**service, "expectQuant": None}, served), [])
        problems = harness.check_served(service, served)
        self.assertTrue(problems)
        self.assertIn("different weights", problems[0])
        # And a substring match, so "Q4" need not guess the server's spelling.
        self.assertEqual(harness.check_served(service, {"meta": {"n_params": 27_320_697_856}, "quant": "Q4_K_XL"}), [])

    def test_an_unconfirmed_entry_warns_instead_of_refusing(self):
        # A claim the endpoint cannot answer is not a contradiction. Refusing
        # would block every cold task-tier run, since a sleeping router member
        # reports its argv but carries no meta until something loads it.
        service = {
            "id": "task-4b",
            "url": "http://x/v1/chat/completions",
            "expectParams": 4_205_751_296,
            "expectModelPath": "/m/four.gguf",
        }
        asleep = {"readFrom": "http://x/v1", "modelPath": "/m/four.gguf", "quant": "Q6_K_XL"}
        self.assertEqual(harness.check_served(service, asleep), [])
        self.assertIsNone(harness.attribution_warning(service, asleep), "the path claim did confirm it")

    def test_an_entry_nothing_could_check_says_so(self):
        service = {"id": "mystery", "url": "http://x/v1/chat/completions", "expectModelPath": "/m/x.gguf"}
        warning = harness.attribution_warning(service, {"readFrom": "http://x/v1", "meta": {"n_params": 1}})
        self.assertIn("expectModelPath", warning)
        naked = harness.attribution_warning({"id": "naked", "url": "u"}, {"readFrom": "http://x/v1"})
        self.assertIn("asserts no identity", naked)

    def test_an_unreachable_endpoint_is_reported_rather_than_passed(self):
        service = {"id": "gone", "url": "http://x/v1/chat/completions", "expectParams": 1}
        problems = harness.check_served(service, {"error": "connection refused"})
        self.assertIn("could not read what", problems[0])


class CaseContractTests(unittest.TestCase):
    def test_every_case_declares_its_shape(self):
        for case_id in harness.case_ids():
            with self.subTest(case=case_id):
                case = harness.load_case(case_id)
                self.assertTrue(hasattr(case, "items"), f"{case_id} has no items()")
                self.assertTrue(hasattr(case, "score"), f"{case_id} has no score()")
                self.assertIsInstance(getattr(case, "DIMENSION", None), str)
                self.assertIsInstance(getattr(case, "JUDGE", None), bool)

    def test_a_judged_case_supplies_context_for_the_bundle(self):
        # Without it the grader sees an output and nothing to read it against.
        for case_id in harness.case_ids():
            case = harness.load_case(case_id)
            if not case.JUDGE:
                continue
            with self.subTest(case=case_id):
                self.assertTrue(hasattr(case, "judge_context"), f"{case_id} is judged but has no judge_context()")

    def test_item_ids_are_unique_within_a_case(self):
        for case_id in harness.case_ids():
            with self.subTest(case=case_id):
                ids = [item["id"] for item in harness.load_case(case_id).items()]
                self.assertEqual(len(ids), len(set(ids)), f"{case_id} reuses an item id")

    def test_a_case_that_fits_a_model_fits_every_item_of_it(self):
        # The guarantee that used to be blanket, now conditioned on
        # applicability. If `applicable` says a model can be asked a case, every
        # item of that case must fit: a case half of which overruns would report
        # the overrun as model failure. What changed is that a case too large
        # for a model is skipped and said to be skipped, rather than being
        # forbidden from existing — a long-context rung only the big models can
        # run is a measurement, not a configuration error.
        for model_id in harness.models():
            service = harness.resolve_model(model_id)
            ceiling = service["contextTokens"]
            for case_id in harness.case_ids():
                case = harness.load_case(case_id)
                fits, _why = harness.applicable(case, service)
                if not fits:
                    continue
                for item in case.items():
                    with self.subTest(model=model_id, case=case_id, item=item["id"]):
                        total = harness.forge_llm.estimate_prompt_tokens(item["messages"]) + (
                            harness._output_budget(item, service) or 0
                        )
                        self.assertLess(
                            total, ceiling, f"{case_id}/{item['id']} needs {total} tokens on {model_id}, over {ceiling}"
                        )

    def test_every_case_fits_at_least_one_model(self):
        # A case no registered model can run measures nothing at all, and would
        # sit in the report as permanently "not run" without anyone noticing it
        # was never runnable.
        services = [harness.resolve_model(model_id) for model_id in harness.models()]
        for case_id in harness.case_ids():
            case = harness.load_case(case_id)
            with self.subTest(case=case_id):
                self.assertTrue(
                    any(harness.applicable(case, service)[0] for service in services),
                    f"{case_id} needs {harness.case_min_context(case):,} tokens, more than any model has",
                )

    def test_a_case_too_large_for_a_model_is_skipped_not_failed(self):
        case = harness.load_case(harness.case_ids()[0])
        tiny = {"id": "tiny", "contextTokens": 64, "outputHeadroom": 0}
        fits, why = harness.applicable(case, tiny)
        self.assertFalse(fits)
        self.assertIn("64", why)
        roomy = {"id": "roomy", "contextTokens": 10_000_000, "outputHeadroom": 0}
        self.assertEqual(harness.applicable(case, roomy), (True, None))

    def test_output_headroom_counts_against_applicability(self):
        # A reasoning backend is given room to think, and that room comes out of
        # the same ceiling. A case that fits without headroom can fail to fit
        # with it, and finding that out at run time wastes the run.
        case = harness.load_case(harness.case_ids()[0])
        needed = harness.case_min_context(case)
        exact = {"id": "exact", "contextTokens": needed + 100, "outputHeadroom": 0}
        self.assertTrue(harness.applicable(case, exact)[0])
        thinking = {"id": "thinking", "contextTokens": needed + 100, "outputHeadroom": 12000}
        self.assertFalse(harness.applicable(case, thinking)[0])

    def test_suites_nest_and_a_named_case_is_never_dropped(self):
        quick = harness.cases_for_suite("quick")
        standard = harness.cases_for_suite("standard")
        full = harness.cases_for_suite("full")
        self.assertLessEqual(set(quick), set(standard))
        self.assertLessEqual(set(standard), set(full))
        self.assertEqual(set(full), set(harness.case_ids()), "every case belongs to some tier")
        with self.assertRaises(harness.EvalError):
            harness.cases_for_suite("everything")

    def test_output_headroom_is_added_to_the_case_budget_not_written_into_it(self):
        item = {"max_tokens": 1024}
        self.assertEqual(harness._output_budget(item, {"outputHeadroom": 3000}), 4024)
        self.assertEqual(harness._output_budget(item, {}), 1024)
        # A case that sets no budget wants the server's default, and adding
        # headroom to nothing would impose a cap that was never asked for.
        self.assertIsNone(harness._output_budget({}, {"outputHeadroom": 3000}))


class ScoringTests(unittest.TestCase):
    """A scorer has to be wrong about wrong answers, not just right about right ones."""

    def test_a_malformed_reply_scores_as_a_failure_rather_than_raising(self):
        for case_id in harness.case_ids():
            case = harness.load_case(case_id)
            item = case.items()[0]
            for reply in ("", "not json at all", "{}", "[]"):
                with self.subTest(case=case_id, reply=reply):
                    scored = case.score(item, reply)
                    self.assertIsInstance(scored, dict)
                    self.assertFalse(scored["ok"], f"{case_id} passed a {reply!r} reply")

    def test_the_classifier_scorer_rejects_a_wrong_destination(self):
        case = harness.load_case("classify-notes")
        item = next(entry for entry in case.items() if entry["id"] == "classify-reification")
        right = json.dumps(
            {
                "metadata": {"type": "concept", "status": "active", "domain": "wiki", "subdomain": "concepts"},
                "needs_review": False,
                "review_reason": None,
            }
        )
        self.assertTrue(case.score(item, right)["ok"])

        wrong = json.dumps(
            {
                "metadata": {"type": "note", "status": "active", "domain": "craft", "subdomain": "cooking"},
                "needs_review": False,
                "review_reason": None,
            }
        )
        scored = case.score(item, wrong)
        self.assertFalse(scored["ok"])
        self.assertFalse(scored["gates"]["destinationMatches"])

    def test_the_verifier_scorer_separates_missing_a_defect_from_inventing_one(self):
        case = harness.load_case("verifier-seeded")
        item = case.items()[0]
        key = item["key"]

        perfect = {"verdicts": [{"id": i, "verdict": "flag" if d else "ok", "reason": "seeded" if d else ""} for i, d in key.items()]}
        scored = case.score(item, json.dumps(perfect))
        self.assertTrue(scored["ok"])
        self.assertEqual(scored["metrics"]["recall"], 1.0)
        self.assertEqual(scored["metrics"]["precision"], 1.0)

        # Approving everything is the failure that reads as success.
        rubber_stamp = {"verdicts": [{"id": i, "verdict": "ok"} for i in key]}
        scored = case.score(item, json.dumps(rubber_stamp))
        self.assertFalse(scored["ok"])
        self.assertEqual(scored["metrics"]["recall"], 0.0)
        self.assertFalse(scored["gates"]["noDefectMissed"])
        self.assertTrue(scored["gates"]["noFalseFlags"], "approving everything flags nothing")

        # So is flagging everything, differently: perfect recall, useless output.
        flag_all = {"verdicts": [{"id": i, "verdict": "flag", "reason": "unsure"} for i in key]}
        scored = case.score(item, json.dumps(flag_all))
        self.assertFalse(scored["ok"])
        self.assertEqual(scored["metrics"]["recall"], 1.0)
        self.assertLess(scored["metrics"]["precision"], 1.0)
        self.assertEqual(scored["metrics"]["falseFlags"], scored["metrics"]["soundItems"])

    def test_the_verifier_scorer_enforces_id_coverage(self):
        case = harness.load_case("verifier-seeded")
        item = case.items()[0]
        partial = {"verdicts": [{"id": "v-001", "verdict": "ok"}, {"id": "v-999", "verdict": "flag", "reason": "x"}]}
        scored = case.score(item, json.dumps(partial))
        self.assertFalse(scored["gates"]["idCoverage"])

    def test_the_connection_scorer_reads_both_directions(self):
        case = harness.load_case("connection-judgment")
        positive = next(entry for entry in case.items() if entry["expected"])
        negative = next(entry for entry in case.items() if not entry["expected"])
        yes = json.dumps({"connect": True, "strength": "strong", "kind": "generalization", "reason": "shared idea"})
        no = json.dumps({"connect": False, "reason": "only shared vocabulary"})
        self.assertTrue(case.score(positive, yes)["ok"])
        self.assertFalse(case.score(positive, no)["ok"])
        self.assertTrue(case.score(negative, no)["ok"])
        self.assertFalse(case.score(negative, yes)["ok"])

    def test_the_cleanup_scorer_catches_a_word_the_speaker_did_not_say(self):
        case = harness.load_case("transcript-cleanup-memo")
        item = case.items()[0]
        invented = "Kubernetes orchestration pipelines quarterly amortization stakeholder synergies deliverables."
        scored = case.score(item, json.dumps({"cleaned": invented, "chunk_summary": "x"}))
        self.assertFalse(scored["ok"])
        self.assertGreater(scored["metrics"]["inventedWords"], 0)

    def test_the_document_cleanup_word_check_counts_occurrences_not_presence(self):
        import doc_cleanup_ocr

        # A word the source uses once and the output uses twice has one added
        # occurrence; set semantics would miss it.
        self.assertEqual(doc_cleanup_ocr.added_words("the chiller unit", "the chiller unit chiller"), ["chiller"])
        self.assertEqual(doc_cleanup_ocr.added_words("the chiller unit", "the chiller"), [])
        self.assertEqual(doc_cleanup_ocr.added_words("certifi ed system", "certified system"), ["certified"])
        # Under three letters is not vocabulary, so page numbers and stray
        # letters do not register as fabrication.
        self.assertEqual(doc_cleanup_ocr.added_words("a b", "a b c 12"), [])

    def test_the_cleanup_reference_is_the_prose_and_not_the_note_around_it(self):
        import _cleanup

        note = (
            "> [!summary]\n> A summary another stage wrote.\n\n"
            "> [!reflection]- Context\n> - A reflection another stage wrote.\n\n"
            "Summary: a cue line.\nCue words: one, two\n\n"
            "The prose the cleanup stage actually produced.\n\n"
            "# Transcript\n\n[[Some - Transcript]]\n"
        )
        self.assertEqual(_cleanup.cleaned_prose(note), "The prose the cleanup stage actually produced.")

    def test_the_summary_case_is_not_handed_an_existing_summary_to_copy(self):
        import summary_transcript

        for fixture_id in summary_transcript.FIXTURES:
            with self.subTest(fixture=fixture_id):
                source = summary_transcript._cleaned_source(fixture_id)
                self.assertNotIn("[!summary]", source)
                self.assertNotIn("[!reflection]", source)

    def test_the_document_cleanup_prompt_is_read_from_the_skill(self):
        import doc_cleanup_ocr

        system = doc_cleanup_ocr.cleanup_system()
        self.assertIn("Structure is yours to fix; wording is not.", system)


class RepairTests(unittest.TestCase):
    def test_the_cleanup_repair_quotes_the_check_that_failed(self):
        case = harness.load_case("transcript-cleanup-memo")
        item = case.items()[0]
        scored = {"notes": ["words not in the chunk: synergies, deliverables"]}
        messages = case.repair(item, "{}", scored)
        # The skill's own branch: naming the violation is what changes the
        # answer, and the wording differs by which check broke.
        self.assertIn("Those words are yours, not the speaker's", messages[-1]["content"])
        self.assertEqual(messages[:-1], item["messages"])

        other = case.repair(item, "{}", {"notes": ["kept a transcript timestamp: '*00:12*'"]})
        self.assertIn("Stay inside the speaker's own words", other[-1]["content"])

    def test_no_repair_is_attempted_when_nothing_was_reported(self):
        case = harness.load_case("transcript-cleanup-memo")
        self.assertIsNone(case.repair(case.items()[0], "{}", {"notes": []}))


class TruncationTests(unittest.TestCase):
    def test_running_out_of_output_budget_is_not_reported_as_bad_json(self):
        # A model cut off mid-array was enumerating enthusiastically, which is
        # the opposite of the failure this case looks for.
        case = harness.load_case("enumeration-breadth")
        item = case.items()[0]
        record = {"finishReason": "length", "generatedTokens": 12288}
        scored = case.score(item, '[{"item_type": "claim", "text": "half an ar', record)
        self.assertFalse(scored["ok"])
        self.assertFalse(scored["gates"]["notTruncated"])
        self.assertIn("ran out of output budget", scored["notes"][0])
        self.assertTrue(scored["keepRaw"])

        # Genuine nonsense still reads as genuine nonsense.
        plain = case.score(item, "I could not do that", {"finishReason": "stop"})
        self.assertNotIn("notTruncated", plain["gates"])


class StabilityTests(unittest.TestCase):
    """8 of 12 cases moved between two runs of one model. A verdict has to know that."""

    def _attempt(self, item_id, ok, severity=None):
        return {"id": item_id, "ok": ok, "gates": {}, "metrics": {}, "telemetry": {}, "severity": severity}

    def test_repeats_are_counted_as_items_not_attempts(self):
        # Three attempts at two fixtures is two items, not six. Counting
        # attempts inflated the denominator and averaged instability away.
        items = [self._attempt("a", True) for _ in range(3)] + [self._attempt("b", True) for _ in range(3)]
        summary = harness.summarize_items(items)
        self.assertEqual(summary["items"], 2)
        self.assertEqual(summary["attempts"], 6)
        self.assertEqual(summary["passRate"], 1.0)

    def test_an_item_that_flips_is_unstable_and_does_not_count_as_passed(self):
        items = [self._attempt("a", True), self._attempt("a", True), self._attempt("a", False, "gated")]
        summary = harness.summarize_items(items)
        self.assertEqual(summary["stability"]["stableItems"], 0)
        self.assertEqual(summary["stability"]["unstableIds"], ["a"])
        # Two of three attempts passed, but the item did not: a coin flip is not
        # a two-thirds pass.
        self.assertEqual(summary["passed"], 0)

    def test_a_single_attempt_is_trivially_stable(self):
        summary = harness.summarize_items([self._attempt("a", True)])
        self.assertEqual(summary["stability"]["stableItems"], 1)
        self.assertEqual(summary["stability"]["unstableIds"], [])

    def test_the_worst_severity_across_attempts_wins(self):
        items = [self._attempt("a", True), self._attempt("a", False, "gated")]
        self.assertEqual(harness.stability_by_item(items)["a"]["severity"], "gated")


class VariantTests(unittest.TestCase):
    """A variant tests a change before it is made; the skill stays untouched."""

    def _items(self):
        return [
            {"id": "a", "messages": [{"role": "system", "content": "BASE RULES\nkeep this"}, {"role": "user", "content": "u"}],
             "max_tokens": 100, "response_format": {"type": "json_object"}}
        ]

    def test_a_patch_replaces_request_fields(self):
        variant = {"name": "v", "patch": {"response_format": {"type": "json_schema"}, "max_tokens": 500}}
        patched = harness.apply_variant(self._items(), variant, "any-case")
        self.assertEqual(patched[0]["response_format"], {"type": "json_schema"})
        self.assertEqual(patched[0]["max_tokens"], 500)

    def test_the_original_items_are_not_mutated(self):
        original = self._items()
        harness.apply_variant(original, {"name": "v", "patch": {"max_tokens": 9}}, "any-case")
        self.assertEqual(original[0]["max_tokens"], 100)

    def test_a_scoped_variant_leaves_other_cases_alone(self):
        variant = {"name": "v", "applies": ["only-this"], "patch": {"max_tokens": 9}}
        self.assertEqual(harness.apply_variant(self._items(), variant, "other")[0]["max_tokens"], 100)
        self.assertEqual(harness.apply_variant(self._items(), variant, "only-this")[0]["max_tokens"], 9)

    def test_a_suffix_appends_to_the_system_prompt_only(self):
        patched = harness.apply_variant(self._items(), {"name": "v", "patch": {"systemSuffix": "EXTRA"}}, "c")
        self.assertTrue(patched[0]["messages"][0]["content"].endswith("EXTRA"))
        self.assertIn("BASE RULES", patched[0]["messages"][0]["content"])
        self.assertEqual(patched[0]["messages"][1]["content"], "u")

    def test_stripping_text_that_is_not_there_is_refused(self):
        # A variant that believes it removed a clause and did not would be
        # measured as "the change had no effect" — the wrong conclusion entirely.
        with self.assertRaises(harness.EvalError) as caught:
            harness.apply_variant(self._items(), {"name": "v", "patch": {"systemStrip": "absent"}}, "c")
        self.assertIn("strips text that is not in", str(caught.exception))

    def test_stripping_removes_exactly_the_clause(self):
        patched = harness.apply_variant(self._items(), {"name": "v", "patch": {"systemStrip": "\nkeep this"}}, "c")
        self.assertEqual(patched[0]["messages"][0]["content"], "BASE RULES")

    def test_the_base_variant_is_a_no_op(self):
        self.assertIsNone(harness.load_variant("base"))
        self.assertIsNone(harness.load_variant(None))

    def test_an_unknown_variant_names_the_ones_that_exist(self):
        with self.assertRaises(harness.EvalError) as caught:
            harness.load_variant("no-such-variant")
        self.assertIn("enumeration-clause-removed", str(caught.exception))

    def test_the_calibration_variant_strips_a_clause_the_skill_really_has(self):
        # If this drifts from the skill, the calibration silently stops testing
        # anything, so it is pinned here rather than discovered during a run.
        variant = harness.load_variant("enumeration-clause-removed")
        case = harness.load_case("enumeration-breadth")
        patched = harness.apply_variant(case.items(), variant, "enumeration-breadth")
        before = case.items()[0]["messages"][0]["content"]
        after = patched[0]["messages"][0]["content"]
        self.assertIn("Do not stop at claims, findings, methods", before)
        self.assertNotIn("Do not stop at claims, findings, methods", after)
        self.assertLess(len(after), len(before))


class CompareTests(unittest.TestCase):
    """A bare count is not evidence when 8 of 12 cases move on their own."""

    def setUp(self):
        self.comparing = importlib.import_module("compare")

    def test_the_exact_test_matches_hand_computed_binomials(self):
        self.assertEqual(self.comparing.exact_mcnemar(0, 0), 1.0)
        self.assertAlmostEqual(self.comparing.exact_mcnemar(5, 0), 0.0625, places=4)
        self.assertAlmostEqual(self.comparing.exact_mcnemar(6, 0), 0.03125, places=5)
        self.assertAlmostEqual(self.comparing.exact_mcnemar(3, 3), 1.0, places=4)

    def test_a_lopsided_but_tiny_split_is_reported_as_underpowered(self):
        # Five improvements and no regressions looks decisive and is not.
        self.assertGreater(self.comparing.exact_mcnemar(5, 0), 0.05)

    def test_no_change_is_not_dressed_up_as_a_result(self):
        text = self.comparing.render(
            {"model": "m", "from": "base", "to": "v", "sharedItems": 8, "improved": [], "regressed": [],
             "unchanged": 8, "excludedUnstable": [], "discordant": 0, "p": 1.0, "underpowered": True}
        )
        self.assertIn("No item changed", text)

    def test_an_unstable_item_is_excluded_from_the_comparison(self):
        rows_stable = [{"id": "a", "ok": True, "gates": {}, "metrics": {}, "telemetry": {}}]
        rows_flaky = [
            {"id": "b", "ok": True, "gates": {}, "metrics": {}, "telemetry": {}},
            {"id": "b", "ok": False, "gates": {}, "metrics": {}, "telemetry": {}},
        ]
        documents = {"c": {"items": rows_stable + rows_flaky}}
        outcomes = self.comparing._outcomes(documents)
        self.assertTrue(outcomes[("c", "a")]["stable"])
        self.assertFalse(outcomes[("c", "b")]["stable"])


class UndecidedCaseTests(unittest.TestCase):
    """Which cases get repeated is computed, because a maintained list goes stale."""

    def _document(self, passed, items):
        rows = [{"id": str(n), "ok": n < passed, "gates": {}, "metrics": {}, "telemetry": {}, "severity": None} for n in range(items)]
        return {"items": rows, "summary": harness.summarize_items(rows)}

    def test_a_case_within_one_item_of_the_baseline_is_repeated(self):
        documents = {"close": self._document(5, 8)}
        baseline = {"close": self._document(6, 8)}
        self.assertEqual(harness.undecided_cases(documents, baseline), ["close"])

    def test_a_case_far_from_the_baseline_is_left_alone(self):
        documents = {"clear": self._document(8, 8)}
        baseline = {"clear": self._document(1, 8)}
        self.assertEqual(harness.undecided_cases(documents, baseline), [])

    def test_an_already_unstable_case_is_repeated_regardless(self):
        rows = [
            {"id": "a", "ok": True, "gates": {}, "metrics": {}, "telemetry": {}, "severity": None},
            {"id": "a", "ok": False, "gates": {}, "metrics": {}, "telemetry": {}, "severity": "gated"},
        ]
        documents = {"flaky": {"items": rows, "summary": harness.summarize_items(rows)}}
        self.assertEqual(harness.undecided_cases(documents, None), ["flaky"])

    def test_a_case_sitting_on_the_floor_is_repeated(self):
        # 5/8 is 0.625, within one item (0.125) of the 0.6 floor.
        self.assertEqual(harness.undecided_cases({"edge": self._document(5, 8)}, None), ["edge"])

    def test_small_cases_are_more_easily_undecided_because_one_item_is_worth_more(self):
        # One item of two is worth 0.5, so almost any two-item case is close to
        # something. That is the point: they should not be deciding anything.
        self.assertEqual(harness.undecided_cases({"tiny": self._document(1, 2)}, None), ["tiny"])

    def test_a_case_the_model_could_not_be_asked_is_not_undecided(self):
        # A skipped case carries an empty summary. This raised KeyError until
        # 2026-08-05, which killed `run --suite full --stabilize` on task-4b
        # after the first pass: lcr-80k needs ~82,000 tokens against its 65,538,
        # so the run skipped it correctly and then died choosing what to repeat.
        documents = {"fits": self._document(8, 8), "too-big": {"items": [], "summary": {}}}
        self.assertEqual(harness.undecided_cases(documents, None), [])

    def test_a_skipped_baseline_does_not_decide_a_case_that_ran(self):
        # The reverse pairing: this model answered the case and the baseline
        # could not. There is nothing to compare against, so the floor decides.
        documents = {"c": self._document(8, 8)}
        self.assertEqual(harness.undecided_cases(documents, {"c": {"items": [], "summary": {}}}), [])


class SeverityTests(unittest.TestCase):
    """`silent` is the number that should decide a handoff."""

    def setUp(self):
        self.reporting = importlib.import_module("report")

    def _document(self, case_id, judged, rows):
        items = [{"id": i, "ok": ok, "gates": {}, "metrics": {}, "telemetry": {}, "severity": sev} for i, ok, sev in rows]
        return {"case": case_id, "judged": judged, "items": items, "summary": harness.summarize_items(items)}

    def test_a_gated_failure_is_told_apart_from_one_nothing_caught(self):
        documents = {
            "cleanup": self._document("cleanup", True, [("caught", False, "gated"), ("fabricated", True, None)]),
        }
        graded = {
            "verdicts": [
                {"model": "m", "case": "cleanup", "item": "fabricated", "scores": {"faithfulness": 2}},
            ]
        }
        counts, detail, _ = self.reporting.severity_for(documents, graded, "m")
        self.assertEqual(counts["gated"], 1)
        self.assertEqual(counts["silent"], 1)
        self.assertIn("cleanup/fabricated (faithfulness 2)", detail["silent"])

    def test_ungraded_prose_is_unknown_rather_than_clean(self):
        documents = {"cleanup": self._document("cleanup", True, [("a", True, None)])}
        counts, detail, _ = self.reporting.severity_for(documents, None, "m")
        self.assertEqual(counts["unknown"], 1)
        self.assertEqual(counts["clean"], 0)
        self.assertEqual(detail["unknown"], ["cleanup/a"])

    def test_an_unjudged_case_is_clean_when_its_gates_pass(self):
        # There is no prose to be silently wrong about; the gates are the whole
        # story, so passing them is a clean bill.
        documents = {"classify": self._document("classify", False, [("a", True, None)])}
        counts, _, _ = self.reporting.severity_for(documents, None, "m")
        self.assertEqual(counts["clean"], 1)
        self.assertEqual(counts["unknown"], 0)

    def test_a_good_grade_clears_an_item(self):
        documents = {"cleanup": self._document("cleanup", True, [("a", True, None)])}
        graded = {"verdicts": [{"model": "m", "case": "cleanup", "item": "a", "scores": {"faithfulness": 5}}]}
        counts, _, _ = self.reporting.severity_for(documents, graded, "m")
        self.assertEqual(counts["clean"], 1)
        self.assertEqual(counts["silent"], 0)

    def test_another_models_grades_are_not_borrowed(self):
        documents = {"cleanup": self._document("cleanup", True, [("a", True, None)])}
        graded = {"verdicts": [{"model": "other", "case": "cleanup", "item": "a", "scores": {"faithfulness": 1}}]}
        counts, _, _ = self.reporting.severity_for(documents, graded, "m")
        self.assertEqual(counts["silent"], 0)
        self.assertEqual(counts["unknown"], 1)


class SummaryTests(unittest.TestCase):
    def test_summarize_items_rolls_up_gates_and_metrics(self):
        items = [
            {"id": "a", "ok": True, "gates": {"parsed": True, "clean": True}, "metrics": {"n": 2}, "telemetry": {"generatedTokens": 10, "elapsedMs": 5}},
            {"id": "b", "ok": False, "gates": {"parsed": True, "clean": False}, "metrics": {"n": 4}, "telemetry": {"generatedTokens": 20, "elapsedMs": 5}},
        ]
        summary = harness.summarize_items(items)
        self.assertEqual(summary["passed"], 1)
        self.assertEqual(summary["passRate"], 0.5)
        self.assertEqual(summary["gates"]["clean"], {"passed": 1, "of": 2})
        self.assertEqual(summary["metrics"]["n"]["mean"], 3.0)
        self.assertEqual(summary["generatedTokens"], 30)

    def test_thinking_is_summarized_from_what_happened_not_what_was_asked(self):
        # A flag can be set and not honoured — it was, on :8007 — so the record
        # of whether a model reasoned has to come from its own token counts.
        quiet = [
            {"id": str(n), "ok": True, "gates": {}, "metrics": {}, "telemetry": {"hiddenTokens": 0, "reasoned": False}}
            for n in range(9)
        ]
        # One long reply trips the visible-content estimator; the median must
        # not move, or every non-thinking run reads as thinking.
        quiet.append({"id": "9", "ok": True, "gates": {}, "metrics": {}, "telemetry": {"hiddenTokens": 1413, "reasoned": True}})
        observed = harness.summarize_items(quiet)["observedThinking"]
        self.assertEqual(observed["reasonedItems"], 1)
        self.assertEqual(observed["hiddenTokensMedian"], 0)
        self.assertEqual(observed["hiddenTokensMax"], 1413)

        loud = [
            {"id": str(n), "ok": True, "gates": {}, "metrics": {}, "telemetry": {"hiddenTokens": 2400, "reasoned": True}}
            for n in range(10)
        ]
        self.assertEqual(harness.summarize_items(loud)["observedThinking"]["hiddenTokensMedian"], 2400)

    def test_requested_settings_travel_with_the_result(self):
        service = harness.resolve_model("task-4b")
        requested = harness.requested_settings(service)
        self.assertEqual(requested["chatTemplateKwargs"], {"enable_thinking": False})
        self.assertEqual(requested["contextTokens"], 65538)
        self.assertEqual(requested["temperature"], 0)
        self.assertFalse(requested["backgroundScheduling"])

    def test_a_boolean_metric_is_not_averaged_as_a_number(self):
        items = [{"id": "a", "ok": True, "gates": {}, "metrics": {"flag": True}, "telemetry": {}}]
        self.assertNotIn("flag", harness.summarize_items(items)["metrics"])

    def test_the_label_shuffle_is_stable_for_a_seed(self):
        first = harness.stable_shuffle(["a", "b", "c"], "seed:case:item")
        second = harness.stable_shuffle(["a", "b", "c"], "seed:case:item")
        self.assertEqual(first, second)
        self.assertNotEqual(first, harness.stable_shuffle(["a", "b", "c"], "other"))


class JudgeTests(unittest.TestCase):
    """The bundle has to blind the grader and the merge has to unblind it again."""

    def setUp(self):
        self.judging = importlib.import_module("judge")
        self.workspace = tempfile.TemporaryDirectory()
        root = Path(self.workspace.name)
        self.original = (harness.RESULTS, self.judging.JUDGE_DIR, self.judging.KEY_PATH)
        harness.RESULTS = root / "results"
        self.judging.JUDGE_DIR = harness.RESULTS / "judge"
        self.judging.KEY_PATH = self.judging.JUDGE_DIR / "key.json"
        for model_id, cleaned in (("model-a", "the alpha text"), ("model-b", "the beta text")):
            harness.write_result(
                {
                    "case": "transcript-cleanup-memo",
                    "dimension": "faithful-cleanup",
                    "skill": "vault-transcripts",
                    "judged": True,
                    "model": model_id,
                    "modelLabel": model_id,
                    "endpoint": "http://example/v1/chat/completions",
                    "repeat": 1,
                    "elapsedMs": 1,
                    "items": [
                        {
                            "id": "transcript-context",
                            "attempt": 1,
                            "ok": True,
                            "gates": {},
                            "metrics": {},
                            "notes": [],
                            "output": cleaned,
                            "raw": None,
                            "telemetry": {},
                        }
                    ],
                    "summary": {"items": 1, "passed": 1, "passRate": 1.0, "gates": {}, "metrics": {}, "generatedTokens": 1, "hiddenTokens": 0, "elapsedMs": 1},
                }
            )

    def tearDown(self):
        harness.RESULTS, self.judging.JUDGE_DIR, self.judging.KEY_PATH = self.original
        self.workspace.cleanup()

    def test_the_bundle_never_names_the_model_beside_its_output(self):
        self.judging.build(["model-a", "model-b"], seed=1)
        bundle = (self.judging.JUDGE_DIR / "transcript-cleanup-memo.md").read_text(encoding="utf-8")
        self.assertIn("the alpha text", bundle)
        self.assertIn("the beta text", bundle)
        # A grader told which model wrote which grades the name.
        self.assertNotIn("model-a", bundle)
        self.assertNotIn("model-b", bundle)
        self.assertIn("model-a", self.judging.KEY_PATH.read_text(encoding="utf-8"))

    def test_scoring_unblinds_through_the_key(self):
        self.judging.build(["model-a", "model-b"], seed=1)
        key = harness.load_json(self.judging.KEY_PATH)["assignments"]["transcript-cleanup-memo/transcript-context"]
        label_for_a = next(label for label, model in key.items() if model == "model-a")
        verdicts = {
            "verdicts": [
                {
                    "case": "transcript-cleanup-memo",
                    "item": "transcript-context",
                    "label": label_for_a,
                    "scores": {"voice": 5, "faithfulness": 5, "coverage": 4, "usability": 5},
                }
            ]
        }
        path = Path(self.workspace.name) / "verdicts.json"
        path.write_text(json.dumps(verdicts), encoding="utf-8")
        merged = self.judging.score(path)
        self.assertEqual(merged["summary"]["model-a"]["voice"], 5)
        self.assertEqual(merged["summary"]["model-a"]["coverage"], 4)
        self.assertNotIn("model-b", merged["summary"])

    def test_a_verdict_with_an_unknown_label_is_reported_not_dropped(self):
        self.judging.build(["model-a", "model-b"], seed=1)
        verdicts = {"verdicts": [{"case": "transcript-cleanup-memo", "item": "transcript-context", "label": "Z", "scores": {"voice": 1}}]}
        path = Path(self.workspace.name) / "verdicts.json"
        path.write_text(json.dumps(verdicts), encoding="utf-8")
        merged = self.judging.score(path)
        self.assertTrue(merged["summary"]["_unmatched"])

    def test_building_without_results_for_every_model_says_so(self):
        with self.assertRaises(harness.EvalError) as caught:
            self.judging.build(["model-a", "model-missing"], seed=1)
        self.assertIn("Run the suite", str(caught.exception))


class RecommendationTests(unittest.TestCase):
    """The routing call is the output someone acts on, so its edge cases are pinned."""

    def setUp(self):
        self.reporting = importlib.import_module("report")

    def _document(self, model_id, passed, items=8, judged=False):
        return {
            "case": "example",
            "dimension": "example",
            "skill": "x",
            "judged": judged,
            "model": model_id,
            "modelLabel": model_id,
            "endpoint": "http://example",
            "repeat": 1,
            "elapsedMs": 1,
            "items": [],
            "summary": {
                "items": items,
                "passed": passed,
                "passRate": passed / items,
                "gates": {},
                "metrics": {},
                "generatedTokens": 0,
                "hiddenTokens": 0,
                "elapsedMs": 0,
            },
        }

    def _rendered(self, baseline_passed, candidate_passed, items=8):
        def document(model_id, passed):
            return {
                "case": "example",
                "dimension": "example",
                "skill": "x",
                "judged": False,
                "model": model_id,
                "modelLabel": model_id,
                "endpoint": "http://example",
                "repeat": 1,
                "elapsedMs": 1,
                "items": [],
                "summary": {
                    "items": items,
                    "passed": passed,
                    "passRate": passed / items,
                    "gates": {},
                    "metrics": {},
                    "generatedTokens": 0,
                    "hiddenTokens": 0,
                    "elapsedMs": 0,
                },
            }

        loaded = {"base": {"example": document("base", baseline_passed)}, "cand": {"example": document("cand", candidate_passed)}}
        return "\n".join(self.reporting._recommendation(loaded, ["base", "cand"], "base", None))

    def test_beating_a_failing_baseline_is_not_a_recommendation(self):
        # 2 of 8 against 0 of 8 is better. It is not usable.
        text = self._rendered(baseline_passed=0, candidate_passed=2)
        self.assertIn("neither model does this well enough", text)
        self.assertNotIn("Safe to route", text)

    def test_falling_behind_outranks_the_floor(self):
        # Both below the floor, but the candidate is far worse: the finding is
        # the gap, not that the case is hard.
        text = self._rendered(baseline_passed=4, candidate_passed=0)
        self.assertIn("Keep on the baseline", text)
        self.assertIn("below the baseline", text)

    def test_an_unstable_case_never_clears(self):
        # 8 of 12 cases moved between two runs of one model. A case whose items
        # flip is not evidence of anything, whatever its pass rate says.
        rows = [{"id": "a", "ok": True, "gates": {}, "metrics": {}, "telemetry": {}, "severity": None}] * 7
        rows = list(rows) + [
            {"id": "flaky", "ok": True, "gates": {}, "metrics": {}, "telemetry": {}, "severity": None},
            {"id": "flaky", "ok": False, "gates": {}, "metrics": {}, "telemetry": {}, "severity": "gated"},
        ]
        candidate = {"case": "example", "judged": False, "items": rows, "summary": harness.summarize_items(rows)}
        candidate.update({"model": "cand", "modelLabel": "cand", "endpoint": "x", "dimension": "d", "skill": "s", "elapsedMs": 1, "repeat": 1})
        loaded = {"base": {"example": self._document("base", 8)}, "cand": {"example": candidate}}
        text = "\n".join(self.reporting._recommendation(loaded, ["base", "cand"], "base", None))
        self.assertIn("flipped between attempts", text)
        self.assertNotIn("Safe to route", text)

    def test_a_case_with_too_few_fixtures_is_indicative_only(self):
        text = self._rendered(baseline_passed=3, candidate_passed=3, items=3)
        self.assertIn("Indicative only", text)
        self.assertNotIn("Safe to route", text)

    def test_a_silent_failure_blocks_a_case_its_gates_would_have_cleared(self):
        # Every deterministic check passed. A grader found it unfaithful anyway.
        # That is the failure nothing downstream sees, so it vetoes the handoff.
        rows = [
            {"id": f"i{n}", "ok": True, "gates": {}, "metrics": {}, "telemetry": {}, "severity": None} for n in range(8)
        ]
        candidate = {"case": "example", "judged": True, "items": rows, "summary": harness.summarize_items(rows)}
        candidate.update({"model": "cand", "modelLabel": "cand", "endpoint": "x", "dimension": "d", "skill": "s", "elapsedMs": 1, "repeat": 1})
        loaded = {"base": {"example": self._document("base", 8)}, "cand": {"example": candidate}}
        graded = {
            "summary": {"base": {"voice": 5}, "cand": {"voice": 5}},
            "verdicts": [{"model": "cand", "case": "example", "item": "i0", "scores": {"faithfulness": 2}}],
        }
        text = "\n".join(self.reporting._recommendation(loaded, ["base", "cand"], "base", graded))
        self.assertIn("Nothing downstream would catch that", text)
        self.assertIn("Keep on the baseline", text)

    def test_a_clean_case_clears(self):
        text = self._rendered(baseline_passed=8, candidate_passed=8)
        self.assertIn("Safe to route", text)

    def test_a_judged_case_never_clears_on_someone_elses_grades(self):
        # A `scored.json` left over from an earlier set of runs must not make a
        # model that was never graded look like its quality was checked.
        loaded = {
            "base": {"example": self._document("base", 8, judged=True)},
            "cand": {"example": self._document("cand", 8, judged=True)},
        }
        stale = {"summary": {"base": {"voice": 5}, "someone-else": {"voice": 5}}}
        text = "\n".join(self.reporting._recommendation(loaded, ["base", "cand"], "base", stale))
        self.assertIn("never graded", text)
        self.assertNotIn("Safe to route", text)


class FreezeTests(unittest.TestCase):
    def test_freeze_refuses_a_denied_path_before_reading_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            denied = root / "07 Administration" / "7.01 Health"
            denied.mkdir(parents=True)
            (denied / "Records.md").write_text("private", encoding="utf-8")
            original = harness.fixtures
            try:
                harness.fixtures = lambda: {
                    "denied": {"path": "07 Administration/7.01 Health/Records.md", "sha256": "0" * 64}
                }
                report = harness.freeze(vault=root, check=True)
            finally:
                harness.fixtures = original
            self.assertEqual(report[0][1], "refused")

    def test_excerpt_modes(self):
        text = "---\ntype: note\n---\nbody line one\nbody line two\n"
        self.assertEqual(harness.excerpt(text, {"mode": "head", "chars": 4}), "---\n")
        self.assertEqual(harness.excerpt(text, None), text)
        self.assertEqual(harness.excerpt(text, {"mode": "body"}), "body line one\nbody line two\n")
        self.assertEqual(harness.excerpt(text, {"mode": "lines", "start": 3, "end": 4}), "body line one")


class CaseSizeTests(unittest.TestCase):
    """Nine of twelve cases used to be under the bar a verdict needs."""

    def test_every_case_can_carry_a_verdict(self):
        reporting = importlib.import_module("report")
        for case_id in harness.case_ids():
            with self.subTest(case=case_id):
                case = harness.load_case(case_id)
                # `EXPECTED_ITEMS` is the count when every expectation file is
                # present. A case whose items depend on answer keys that are
                # deliberately not committed builds fewer of them here, and
                # checking the local filesystem would turn "these notes are
                # private" into "this case is undersized".
                count = getattr(case, "EXPECTED_ITEMS", None) or len(case.items())
                self.assertGreaterEqual(
                    count,
                    reporting.MIN_ITEMS_FOR_VERDICT,
                    f"{case_id} has {count} fixtures; the report will mark it indicative only",
                )

    def test_the_summary_case_measures_more_than_its_gate(self):
        # Both models scored 8/8 on the shape check. A case where everything
        # passes is measuring nothing, so it carries a metric that varies.
        import summary_transcript

        # Varied text, because a repeated sentence has no rare words in it and
        # coverage would be vacuously 1.0 for anything.
        source = " ".join(
            f"The {term} was discussed at length during the session and then set aside for later review."
            for term in (
                "dielectric immersion cooling", "coolant distribution unit", "rear door heat exchanger",
                "thermal design power", "refrigerant leakage rate", "retrofit feasibility",
                "curtailment signal", "interconnection queue", "hydronic loop", "economizer bypass",
            )
        )
        thorough = summary_transcript._term_coverage(
            source, "dielectric immersion cooling coolant distribution unit rear door heat exchanger thermal design power"
        )
        thin = summary_transcript._term_coverage(source, "some things were discussed and set aside")
        self.assertGreater(thorough, thin)
        self.assertLess(thin, 0.5)


class MeetingBriefTests(unittest.TestCase):
    """The reference keys are the case. If they drift from the transcripts, the
    numbers are confident and wrong, which is the failure mode this whole file
    exists to prevent."""

    @classmethod
    def setUpClass(cls):
        cls.meeting = importlib.import_module("_meeting")
        cls.case = harness.load_case("meeting-brief")

    def test_every_reference_quote_is_verbatim_in_its_transcript(self):
        for fixture_id in self.case.FIXTURES:
            key = self.meeting.load_key(fixture_id)
            if key is None:
                continue  # a private key, absent on this machine
            source = harness.frozen_text(fixture_id)
            flat = " ".join(source.split())
            for fact in key["facts"]:
                with self.subTest(fixture=fixture_id, fact=fact["id"]):
                    quote = fact["evidence"]
                    self.assertTrue(
                        quote in source or " ".join(quote.split()) in flat,
                        f"{fixture_id}/{fact['id']} quotes text that is not in the transcript",
                    )

    def test_every_reference_quote_supports_the_claim_it_is_attached_to(self):
        # Verbatim is not enough: a real quote pasted under the wrong fact would
        # pass the check above. One such quote was caught this way while the
        # keys were being written.
        for fixture_id in self.case.FIXTURES:
            key = self.meeting.load_key(fixture_id)
            if key is None:
                continue
            for fact in key["facts"]:
                with self.subTest(fixture=fixture_id, fact=fact["id"]):
                    shared = set(self.meeting._tokens(fact["canonical"])) & set(
                        self.meeting._tokens(fact["evidence"])
                    )
                    self.assertTrue(shared, f"{fixture_id}/{fact['id']} evidence shares no content word with the claim")

    def test_a_fact_is_matched_on_meaning_not_on_the_key_s_wording(self):
        # The bar this pins: the first version required every content word of
        # the reference phrasing, and scored a brief that had plainly reported
        # sixteen figures at 6 of 24.
        # Uses the real transcript, because the matcher's notion of a
        # distinctive word is word frequency *in the source*: on a synthetic
        # one-sentence source every word is equally rare and the test would
        # measure the tie-break rather than the behaviour.
        source = harness.frozen_text("meeting-brattle")
        fact = next(f for f in self.meeting.load_key("meeting-brattle")["facts"] if f["id"] == "f9")
        self.assertTrue(
            self.meeting.fact_matched(
                fact, "34 percent: Forecast share of homes with smart thermostats in 2030", source
            ),
            "a model that reported the figure in its own words has covered the fact",
        )
        self.assertFalse(
            self.meeting.fact_matched(fact, "the report discusses smart thermostat adoption", source),
            "naming the topic without the figure is not covering the fact",
        )

    def test_numbers_spoken_as_words_are_not_read_as_fabrications(self):
        # Speech-to-text spells numbers out and a brief writes digits. Without
        # this, a model that correctly reported "six or seven hundred bucks" was
        # flagged for inventing 600 and 700 — measured on the first live run.
        source = "you can pull in like six or seven hundred bucks depending on the events"
        self.assertEqual(self.meeting.invented_numbers("600 to 700 dollars per year", source), [])
        self.assertEqual(self.meeting.invented_numbers("4200 dollars per year", source), ["4200"])

    def test_the_prompt_is_still_built_from_the_skill_s_own_rules(self):
        for phrase in self.meeting._FIDELITY_RULES_USED:
            self.assertIn(phrase, self.meeting.BRIEF_SYSTEM)
        self.assertIn("Unassigned", self.meeting.BRIEF_SYSTEM)

    def test_a_missing_private_key_shrinks_the_case_rather_than_failing_it(self):
        self.assertIsNone(self.meeting.load_key("no-such-meeting"))
        built = {item["id"] for item in self.case.items()}
        self.assertTrue(built <= set(self.case.FIXTURES))
        self.assertTrue(built, "no keys at all is a broken checkout, not a valid state")


class AbstentionTests(unittest.TestCase):
    """A wrong answer costs what a right one earns; declining scores zero."""

    @classmethod
    def setUpClass(cls):
        cls.ab = importlib.import_module("_abstention")

    def _score(self, case_id, item_id, answer, flag, flag_value=True):
        case = harness.load_case(case_id)
        item = next(entry for entry in case.items() if entry["id"] == item_id)
        return case.score(item, json.dumps({"answer": answer, flag: flag_value}), {})

    def test_declining_ranks_above_a_confident_wrong_answer(self):
        declined = self._score("abstention-closed-book", "c10", "I don't know", "known", False)
        invented = self._score("abstention-closed-book", "c10", "Pacific Gas and Electric", "known", True)
        self.assertGreater(declined["metrics"]["omniscienceIndex"], invented["metrics"]["omniscienceIndex"])
        self.assertTrue(declined["ok"])
        self.assertFalse(invented["ok"], "asserting a nonexistent tariff's operator has to fail the gate")

    def test_declining_an_answerable_question_is_a_miss_but_not_a_confabulation(self):
        missed = self._score("abstention-closed-book", "c11", "I don't know", "known", False)
        self.assertEqual(missed["metrics"]["abstained"], 1.0)
        self.assertEqual(missed["metrics"]["omniscienceIndex"], 0.0)
        self.assertTrue(missed["ok"], "declining is not the failure the gate is for")

    def test_an_empty_reply_is_a_broken_contract_not_an_abstention(self):
        scored = self._score("abstention-closed-book", "c11", "", "known", False)
        self.assertFalse(scored["ok"])
        self.assertFalse(scored["gates"].get("answered", True))

    def test_a_definition_matches_however_it_is_worded(self):
        item = {"accept": [["consumption", "without"], "counterfactual"]}
        self.assertTrue(self.ab.answer_correct("Estimated energy consumption without demand response", item))
        self.assertTrue(self.ab.answer_correct("the counterfactual load", item))
        self.assertFalse(self.ab.answer_correct("a fixed tariff rate", item))


class LongContextTests(unittest.TestCase):
    def test_both_anchor_documents_survive_at_every_rung(self):
        lc = importlib.import_module("_longcontext")
        for rung, tokens in lc.RUNGS.items():
            with self.subTest(rung=rung):
                corpus = lc.corpus_text(tokens)
                for anchor in lc.ANCHORS:
                    self.assertIn(f"=== DOCUMENT: {anchor} ===", corpus)
                    # Whole, not truncated: an anchor cut short loses evidence
                    # and turns a harness limit into an apparent model failure.
                    self.assertIn(harness.frozen_text(anchor).strip()[-400:], corpus)

    def test_a_rung_too_small_for_its_evidence_is_refused(self):
        lc = importlib.import_module("_longcontext")
        with self.assertRaises(harness.EvalError):
            lc.corpus_text(1000)

    def test_the_answers_are_not_findable_in_the_padding(self):
        # The padding is same-project on purpose, so this is not automatic: if a
        # distractor started stating one of these, the question would stop
        # measuring cross-document reading and nobody would notice.
        lc = importlib.import_module("_longcontext")
        padding = " ".join(harness.frozen_text(name).lower() for name in lc.PADDING)
        # Only the anchors whose questions depend on being unique. `q5` is
        # knowingly weaker: two of its four valid answers also appear in the
        # market study, so it can be reached without reading both quarters. It
        # is kept because it still requires the Q6-yes/Q8-no property, and the
        # weakness is recorded in the key rather than hidden by loosening this.
        for anchor in ("18/771,285", "gem containers", "0.025 k/w", "ocp summit"):
            with self.subTest(anchor=anchor):
                self.assertNotIn(anchor, padding, f"{anchor!r} leaked into the distractor documents")

    def test_no_rung_crosses_the_context_compression_threshold(self):
        # Above it the stack compresses, and because the anchors sit at the two
        # ends of the corpus, compression eats the padding between them and
        # leaves the evidence adjacent. A rung that crosses this measures an
        # easier task than its label while looking like a clean pass — a 110k
        # rung scored 10/10 that way before the rungs were resized.
        lc = importlib.import_module("_longcontext")
        for case_id in (name for name in harness.case_ids() if name.startswith("lcr-")):
            case = harness.load_case(case_id)
            for model_id in harness.models():
                service = harness.resolve_model(model_id)
                if not harness.applicable(case, service)[0]:
                    continue
                item = case.items()[0]
                total = harness.forge_llm.estimate_prompt_tokens(item["messages"]) + (
                    harness._output_budget(item, service) or 0
                )
                with self.subTest(case=case_id, model=model_id):
                    self.assertLess(total, lc.COMPRESSION_THRESHOLD, f"{case_id} on {model_id} would be compressed")

    def test_the_rungs_differ_only_in_distance(self):
        lc = importlib.import_module("_longcontext")
        sizes = [len(lc.corpus_text(tokens)) for tokens in lc.RUNGS.values()]
        self.assertEqual(sizes, sorted(sizes), "rungs must grow")
        self.assertLess(sizes[0], sizes[-1])


class ArchiveTests(unittest.TestCase):
    """The copy that survives the vault being reorganised."""

    @classmethod
    def setUpClass(cls):
        cls.archiving = importlib.import_module("archive")

    def test_the_archive_refuses_to_live_where_it_could_be_committed(self):
        with self.assertRaises(harness.EvalError) as caught:
            self.archiving.archive_root(harness.EVALS_ROOT / "sources")
        self.assertIn("repository", str(caught.exception))

    def test_the_archive_refuses_to_live_inside_the_vault(self):
        with self.assertRaises(harness.EvalError) as caught:
            self.archiving.archive_root(harness.DEFAULT_VAULT / "backup")
        self.assertIn("vault", str(caught.exception))

    def test_a_denied_note_is_never_pulled_into_the_archive(self):
        # The deny-list has to hold on this path too. It would be easy for a
        # backup to become the way material re-enters the suite after the vault
        # rule started refusing it.
        spec = {"path": harness.DENIED_PREFIXES[0] + "/whatever.md", "sha256": "0" * 64}
        raw, why = self.archiving._from_vault(spec, harness.DEFAULT_VAULT)
        self.assertIsNone(raw)
        self.assertIn("denied", why)

    def test_an_excerpted_fixture_is_never_recovered_from_its_frozen_copy(self):
        # A frozen excerpt is a subset of the source. Archiving it as the source
        # would look fine and make re-pinning silently wrong later.
        excerpted = {"excerpt": {"mode": "head", "chars": 100}, "sha256": "0" * 64}
        raw, why = self.archiving._from_frozen("report-calnext", excerpted)
        self.assertIsNone(raw)
        self.assertIn("excerpt", why)

    def test_a_recovered_copy_must_still_match_the_pinned_hash(self):
        # A real, frozen, full-mode fixture, so the only thing that can fail is
        # the hash comparison this test is about.
        fixture_id = next(
            name for name, spec in harness.fixtures().items()
            if not spec.get("excerpt") and (harness.FROZEN / f"{name}.md").exists()
        )
        raw, why = self.archiving._from_frozen(fixture_id, {"excerpt": None, "sha256": "1" * 64})
        self.assertIsNone(raw)
        self.assertIn("hash", why)
        # And the same fixture with its real hash comes back fine.
        raw, why = self.archiving._from_frozen(fixture_id, harness.fixtures()[fixture_id])
        self.assertIsNotNone(raw, why)

    def _both_sides(self, tmp, vault_text, archive_text):
        """A vault and an archive that each hold a copy, pinned to ``vault_text``."""
        spec = {"path": "99 Meta/99.02 Schemas/0.00 Vault Schema.md", "sha256": harness.sha256_text(vault_text)}
        vault = Path(tmp) / "vault"
        (vault / spec["path"]).parent.mkdir(parents=True, exist_ok=True)
        (vault / spec["path"]).write_text(vault_text, encoding="utf-8")
        root = Path(tmp) / "archive"
        (root / "sources").mkdir(parents=True, exist_ok=True)
        (root / "sources" / "fixture.md").write_text(archive_text, encoding="utf-8")
        return spec, vault, root

    def test_the_vault_wins_when_it_still_has_the_fixture(self):
        # The archive must not mask a deliberate edit: drift is a finding, and
        # a backup that quietly supplies the old bytes would hide it.
        #
        # Both sides are built here rather than read off the live vault. Reading
        # it made the assertion depend on whether the owner had edited a note
        # since the fixture was pinned -- which they had, so this failed on the
        # developer machine and could not run in CI, where there is no vault at
        # all. The property under test is the precedence rule, and the rule is
        # observable without anyone's notes.
        text = "# Vault Schema\n\n| `wiki` | 9 | Wiki | Wiki cards. |\n"
        with tempfile.TemporaryDirectory() as tmp:
            spec, vault, root = self._both_sides(tmp, text, "# Stale archived copy\n")
            raw, origin = self.archiving.resolve("fixture", spec, vault, root=root)
            self.assertEqual(origin, "vault")
            self.assertEqual(raw, text)

    def test_the_archive_supplies_the_fixture_once_the_vault_copy_has_drifted(self):
        # The other half of the same rule, and the state the real vault is in:
        # the note has been edited since it was pinned, so the vault can no
        # longer supply those bytes and the archive is what keeps the case
        # runnable.
        text = "# Vault Schema\n\n| `wiki` | 9 | Wiki | Wiki cards. |\n"
        with tempfile.TemporaryDirectory() as tmp:
            spec, vault, root = self._both_sides(tmp, text, text)
            (vault / spec["path"]).write_text("# Vault Schema\n\nEdited since it was pinned.\n", encoding="utf-8")
            raw, origin = self.archiving.resolve("fixture", spec, vault, root=root)
            self.assertEqual(origin, "archive")
            self.assertEqual(raw, text)

    def test_an_orphan_never_blocks_a_run(self):
        # An orphaned frozen file is information. An earlier version had `run`
        # refuse on any freeze status that was not "ok", so a leftover file
        # blocked every run until it was deleted.
        self.assertNotIn("orphan", harness.BLOCKING_FREEZE_STATUSES)
        self.assertIn("drifted", harness.BLOCKING_FREEZE_STATUSES)
        self.assertIn("missing", harness.BLOCKING_FREEZE_STATUSES)


class RoutingTableTests(unittest.TestCase):
    """The committed routing table against the evidence that justifies it.

    `forge/lib/forge_routing.py` decides where production sends each stage, and
    the report decides what the measurements support. Nothing kept the two
    honest with each other, so a stage could stay routed to a model long after
    the run that justified it stopped saying so. This is the join.
    """

    def setUp(self):
        sys.path.insert(0, str(EVALS.parent / "lib"))
        self.routing = importlib.import_module("forge_routing")
        self.reporting = importlib.import_module("report")

    def _supported(self):
        """What the results on disk currently support, as case -> model tier."""
        models = [model_id for model_id in sorted(harness.models()) if harness.read_results(model_id)]
        if not models:
            self.skipTest("no results on disk to check the routing table against")
        table = self.reporting.routing_table(models, baseline="chat-27b")
        return {case: entry["tier"] for case, entry in (table.get("cases") or {}).items()}

    def test_every_routed_stage_names_the_case_that_measured_it(self):
        # A stage in the table with no case behind it is a routing decision with
        # no evidence, which is the thing this file exists to prevent.
        for stage in self.routing.STAGE_SERVICES:
            self.assertIn(stage, self.routing.STAGE_EVAL_CASES, stage)

    def test_every_named_case_exists_in_the_suite(self):
        available = set(harness.case_ids()) if hasattr(harness, "case_ids") else None
        if available is None:
            self.skipTest("harness does not enumerate cases")
        for stage, case_id in self.routing.STAGE_EVAL_CASES.items():
            self.assertIn(case_id, available, f"{stage} names a case that does not exist")

    def test_no_stage_is_routed_somewhere_the_report_does_not_support(self):
        supported = self._supported()
        for stage, service in self.routing.STAGE_SERVICES.items():
            case_id = self.routing.STAGE_EVAL_CASES[stage]
            if case_id not in supported:
                # Nothing cleared that case. Routing away from the default on a
                # case no model cleared is exactly the move to refuse.
                self.assertEqual(
                    service,
                    self.routing.DEFAULT_SERVICE,
                    f"{stage} is routed to `{service}` but no model cleared `{case_id}`",
                )
                continue
            self.assertEqual(
                self.routing.SERVICE_TIERS[service],
                supported[case_id],
                f"{stage} is routed to `{service}` but the report puts `{case_id}` on the "
                f"`{supported[case_id]}` tier. Re-run the report, or change the table.",
            )


if __name__ == "__main__":
    unittest.main()

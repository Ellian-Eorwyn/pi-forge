#!/usr/bin/env python3

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

LIB = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(LIB))
spec = importlib.util.spec_from_file_location("forge_verify", LIB / "forge_verify.py")
forge_verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(forge_verify)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_forge_llm import FakeChatServer, forge_llm, service  # noqa: E402


def verdicts(*pairs):
    return json.dumps({"verdicts": [{"id": i, "verdict": v, "reason": r} for i, v, r in pairs]})


def items(count, start=1):
    return [{"id": f"n{index}", "title": f"Note {index}"} for index in range(start, start + count)]


class PacketTests(unittest.TestCase):
    def test_packets_respect_the_item_count(self):
        packets = forge_verify.build_packets(items(45), packet_size=20)
        self.assertEqual([len(packet) for packet in packets], [20, 20, 5])

    def test_packets_respect_a_character_budget(self):
        packets = forge_verify.build_packets(items(10), packet_size=20, budget_characters=80)
        self.assertGreater(len(packets), 1)
        self.assertTrue(all(packet for packet in packets))

    def test_every_item_appears_exactly_once(self):
        packets = forge_verify.build_packets(items(37), packet_size=8)
        flattened = [item["id"] for packet in packets for item in packet]
        self.assertEqual(flattened, [item["id"] for item in items(37)])


class VerifyTests(unittest.TestCase):
    def test_all_items_are_reviewed_in_batches(self):
        first = verdicts(*[(f"n{index}", "ok", "") for index in range(1, 21)])
        second = verdicts(*[(f"n{index}", "ok", "") for index in range(21, 26)])
        with FakeChatServer(responses=[first, second]) as server:
            result = forge_verify.verify_packets(
                service(server.url, name="think"), "Check these.", items(25), packet_size=20, background=False
            )
        self.assertEqual(len(result), 25)
        self.assertTrue(all(entry["verdict"] == "ok" for entry in result.values()))
        # 25 items reviewed for 2 thinking calls, not 25.
        self.assertEqual(len(server.requests), 2)

    def test_a_flag_carries_its_reason(self):
        response = verdicts(("n1", "ok", ""), ("n2", "flag", "filed under the wrong domain"))
        with FakeChatServer(responses=[response]) as server:
            result = forge_verify.verify_packets(service(server.url, name="think"), "Check.", items(2), background=False)
        self.assertEqual(result["n2"]["verdict"], "flag")
        self.assertIn("wrong domain", result["n2"]["reason"])

    def test_incomplete_coverage_is_retried_then_accepted(self):
        partial = verdicts(("n1", "ok", ""))
        complete = verdicts(("n1", "ok", ""), ("n2", "ok", ""))
        with FakeChatServer(responses=[partial, complete]) as server:
            result = forge_verify.verify_packets(service(server.url, name="think"), "Check.", items(2), background=False)
        self.assertEqual(len(result), 2)
        self.assertEqual(len(server.requests), 2)
        self.assertIn("missing verdicts", server.requests[1]["messages"][-1]["content"])

    def test_a_verdict_for_an_item_that_was_not_sent_is_rejected(self):
        bogus = verdicts(("n1", "ok", ""), ("n99", "flag", "invented"))
        good = verdicts(("n1", "ok", ""))
        with FakeChatServer(responses=[bogus, good]) as server:
            result = forge_verify.verify_packets(service(server.url, name="think"), "Check.", items(1), background=False)
        self.assertEqual(list(result), ["n1"])

    def test_a_flag_without_a_reason_is_rejected(self):
        unreasoned = verdicts(("n1", "flag", ""))
        reasoned = verdicts(("n1", "flag", "actually wrong because"))
        with FakeChatServer(responses=[unreasoned, reasoned]) as server:
            result = forge_verify.verify_packets(service(server.url, name="think"), "Check.", items(1), background=False)
        self.assertEqual(result["n1"]["reason"], "actually wrong because")

    def test_a_resumed_run_does_not_re_review_journaled_items(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "verified.jsonl"
            first = verdicts(*[(f"n{index}", "ok", "") for index in range(1, 3)])
            with FakeChatServer(responses=[first]) as server:
                forge_verify.verify_packets(
                    service(server.url, name="think"), "Check.", items(2), journal_path=journal, background=False
                )
                self.assertEqual(len(server.requests), 1)

            # Same two items plus a third: only the third costs a call.
            third = verdicts(("n3", "ok", ""))
            with FakeChatServer(responses=[third]) as server:
                result = forge_verify.verify_packets(
                    service(server.url, name="think"), "Check.", items(3), journal_path=journal, background=False
                )
            self.assertEqual(len(result), 3)
            self.assertEqual(len(server.requests), 1)
            self.assertEqual([item["id"] for item in server.requests[0] and json.loads(server.requests[0]["messages"][-1]["content"])["items"]], ["n3"])

    def test_an_unreachable_verifier_raises_rather_than_passing_work(self):
        with self.assertRaises(forge_verify.VerificationError):
            forge_verify.verify_packets(
                service("http://127.0.0.1:9/v1/chat/completions", name="think"),
                "Check.",
                items(1),
                background=False,
                timeout=1.0,
            )


class EscalationTests(unittest.TestCase):
    def test_flagged_items_are_redone_and_journaled(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "escalated.jsonl"
            results = forge_verify.escalate(
                [({"id": "n1"}, "wrong domain")], lambda item, reason: {"domain": "corrected"}, journal_path=journal
            )
            self.assertTrue(results["n1"]["ok"])
            self.assertEqual(results["n1"]["value"], {"domain": "corrected"})
            rows = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[0]["id"], "n1")
            self.assertTrue(rows[0]["escalated"])
            self.assertEqual(rows[0]["reason"], "wrong domain")

    def test_an_item_already_escalated_is_not_redone_on_resume(self):
        # A flag verdict stays in the journal, so without this guard every
        # resumed run pays another reasoning-model call for the same item.
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "escalated.jsonl"
            calls = []

            def redo(item, reason):
                calls.append(item["id"])
                return {"domain": "corrected"}

            first = forge_verify.escalate([({"id": "n1"}, "wrong domain")], redo, journal_path=journal)
            second = forge_verify.escalate([({"id": "n1"}, "wrong domain")], redo, journal_path=journal)

            self.assertEqual(calls, ["n1"])
            self.assertNotIn("resumed", first["n1"])
            self.assertTrue(second["n1"]["ok"])
            self.assertTrue(second["n1"]["resumed"])
            self.assertNotIn("value", second["n1"])

    def test_a_resumed_failed_escalation_stays_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "escalated.jsonl"

            def explode(_item, _reason):
                raise RuntimeError("model unavailable")

            forge_verify.escalate([({"id": "n1"}, "wrong")], explode, journal_path=journal)
            resumed = forge_verify.escalate([({"id": "n1"}, "wrong")], explode, journal_path=journal)
            self.assertFalse(resumed["n1"]["ok"])
            self.assertTrue(resumed["n1"]["resumed"])
            self.assertIn("model unavailable", resumed["n1"]["detail"])

    def test_a_failed_escalation_becomes_a_human_review_item(self):
        def explode(_item, _reason):
            raise RuntimeError("model unavailable")

        results = forge_verify.escalate([({"id": "n1"}, "wrong")], explode)
        self.assertFalse(results["n1"]["ok"])
        self.assertIn("model unavailable", results["n1"]["detail"])

    def test_summary_counts_what_the_report_needs(self):
        reviewed = {"n1": {"verdict": "ok", "reason": ""}, "n2": {"verdict": "flag", "reason": "wrong"}}
        escalations = {"n2": {"ok": True, "value": {}}}
        summary = forge_verify.summarize(reviewed, escalations)
        self.assertEqual(summary["verified"], 2)
        self.assertEqual(summary["ok"], 1)
        self.assertEqual(summary["flagged"], 1)
        self.assertEqual(summary["escalated"], 1)
        self.assertEqual(summary["needsReview"], 0)
        self.assertEqual(summary["flaggedIds"], ["n2"])


class IndependenceTests(unittest.TestCase):
    """Stage routing made self-review reachable, so a verdict has to say whether
    the reviewer was the model that wrote the thing."""

    def setUp(self):
        self.service = {
            "name": "think", "enabled": True, "url": "http://llms:8008/v1/chat/completions",
            "model": "code", "scheduling": forge_llm.DEFAULT_SERVICES["think"]["scheduling"],
        }

    def test_two_names_for_one_backend_are_not_independent(self):
        # The comparison is on endpoint and model, not on the service name:
        # names are a routing concept and the backend decides independence.
        producer = {**self.service, "name": "task"}
        self.assertTrue(forge_verify.same_backend(self.service, producer))

    def test_a_different_endpoint_is_independent(self):
        producer = {**self.service, "name": "chat", "url": "http://llms:8004/v1/chat/completions", "model": "chat"}
        self.assertFalse(forge_verify.same_backend(self.service, producer))

    def test_an_unknown_producer_is_treated_as_independent(self):
        # Absence of information is not evidence of self-review; a caller that
        # says nothing gets the behaviour it had before this existed.
        self.assertFalse(forge_verify.same_backend(self.service, None))

    def test_a_clean_verdict_from_the_author_is_not_evidence(self):
        verdicts = {
            "a": {"verdict": "ok", "reason": "", "independent": False},
            "b": {"verdict": "ok", "reason": "", "independent": True},
        }
        warning = forge_verify.independence_warning(verdicts)
        self.assertIn("1 item(s)", warning)
        self.assertIn("not independent evidence", warning)

    def test_a_flag_from_the_author_still_counts(self):
        # A reasoning pass over its own output can still catch a contract
        # violation, and anything it flags is escalated as before. Only "ok"
        # loses its standing.
        verdicts = {"a": {"verdict": "flag", "reason": "wrong", "independent": False}}
        self.assertIsNone(forge_verify.independence_warning(verdicts))

    def test_an_ordinary_run_says_nothing(self):
        verdicts = {"a": {"verdict": "ok", "reason": "", "independent": True}}
        self.assertIsNone(forge_verify.independence_warning(verdicts))

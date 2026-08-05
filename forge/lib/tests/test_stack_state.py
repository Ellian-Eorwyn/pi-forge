#!/usr/bin/env python3
"""Tests for the llm-stack state API client.

The fixture is a real snapshot captured from the deployment, not a hand-written
one, because the shapes that matter here are the awkward ones a made-up payload
would smooth over: backends whose ``unit`` is null because the model router
holds them, a proxy that appears in no ``base_url``, and a reranker the router
spells differently from the backend list.

Everything runs against a local stub. Nothing here may depend on a host being
up — that is the whole point of the module being optional.
"""

import importlib.util
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LIB = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "stack-snapshot.json"
sys.dont_write_bytecode = True
sys.path.insert(0, str(LIB))
spec = importlib.util.spec_from_file_location("stack_state", LIB / "stack_state.py")
stack_state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stack_state)

SNAPSHOT = json.loads(FIXTURE.read_text(encoding="utf-8"))

CHAT_URL = "http://llms:8004/v1/chat/completions"
THINK_URL = "http://llms:8008/v1/chat/completions"
EMBED_URL = "http://llms:8005/v1/embeddings"
TASK_URL = "http://llms:8007/v1"
BACKEND_URL = "http://llms:8010/v1"
QWEN = "/mnt/LLMs/llamacpp/llm-stack-git/models/Qwen3.6-27B-Q6_K.gguf"


class FakeStackServer:
    """A stub state API that can misbehave in each way a real one might."""

    def __init__(self, payload=SNAPSHOT, status=200, body=None):
        self.requests = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                server.requests.append({"path": self.path, "auth": self.headers.get("Authorization")})
                if status != 200:
                    self.send_response(status)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if body is not None:
                    encoded = body.encode("utf-8")
                elif self.path.endswith("/health"):
                    encoded = json.dumps({"ok": True, "api_version": payload.get("api_version", "1.0")}).encode("utf-8")
                else:
                    encoded = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"

    def __enter__(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        stack_state.clear_cache()
        return self

    def __exit__(self, *_exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        stack_state.clear_cache()


def env_for(url, **extra):
    """A clean environment, so a developer's own settings cannot leak in."""
    return {"FORGE_STACK_STATE_URL": url, **extra}


class ResolutionTests(unittest.TestCase):
    """Turning a configured forge URL into the backend actually behind it."""

    def test_proxy_ports_resolve_through_the_config_port_map(self):
        # :8004 and :8008 appear in no base_url and no probe target. Only the
        # config["Ports"] block connects them to the backend they front, and
        # this is the case the whole module exists for.
        for url, role in ((CHAT_URL, "NOTHINK_PORT"), (THINK_URL, "CODE_PORT")):
            located = stack_state.backend_for_url(SNAPSHOT, url)
            self.assertIsNotNone(located, url)
            self.assertEqual(located["backend"]["name"], "chat-primary")
            self.assertEqual(located["role"], role)

    def test_backend_ports_resolve_without_the_port_map(self):
        # These match a backend's own base_url, so they must resolve even if the
        # config block is missing entirely.
        stripped = {**SNAPSHOT, "config": {}}
        for url, name in ((EMBED_URL, "embed"), (TASK_URL, "task"), (BACKEND_URL, "chat-primary")):
            located = stack_state.backend_for_url(stripped, url)
            self.assertIsNotNone(located, url)
            self.assertEqual(located["backend"]["name"], name)

    def test_unknown_port_resolves_to_nothing(self):
        self.assertIsNone(stack_state.backend_for_url(SNAPSHOT, "http://llms:9999/v1"))

    def test_router_held_backend_finds_its_service_row_by_name(self):
        # These backends have `unit: null` because the router loads them on
        # demand, so a lookup keyed only on `unit` would lose the service row
        # that carries the reason worth reporting.
        located = stack_state.backend_for_url(SNAPSHOT, EMBED_URL)
        self.assertIsNone(located["backend"]["unit"])
        self.assertEqual(located["unitService"]["name"], "embed")

    def test_a_malformed_snapshot_is_not_an_error(self):
        for bad in (None, {}, {"backends": "nonsense"}, {"backends": []}):
            self.assertIsNone(stack_state.backend_for_url(bad, CHAT_URL))
            self.assertIsNone(stack_state.capacity_for_url(bad, CHAT_URL))
            self.assertIsNone(stack_state.identity_for_url(bad, CHAT_URL))
            self.assertIsNone(stack_state.explain_unreachable(bad, CHAT_URL))


class CapacityTests(unittest.TestCase):
    def test_capacity_is_per_slot_not_the_pool(self):
        capacity = stack_state.capacity_for_url(SNAPSHOT, CHAT_URL)
        self.assertEqual(capacity["contextTokens"], 131072)
        self.assertEqual(capacity["contextTotal"], 262144)
        self.assertEqual(capacity["totalSlots"], 2)

    def test_capacity_follows_a_reconfigured_backend(self):
        moved = json.loads(json.dumps(SNAPSHOT))
        for backend in moved["backends"]:
            if backend["name"] == "chat-primary":
                backend["props"]["n_ctx_per_slot"] = 255998
                backend["props"]["total_slots"] = 1
        capacity = stack_state.capacity_for_url(moved, CHAT_URL)
        self.assertEqual(capacity["contextTokens"], 255998)
        self.assertEqual(capacity["totalSlots"], 1)

    def test_an_inactive_backend_reports_no_capacity(self):
        self.assertIsNone(stack_state.capacity_for_url(SNAPSHOT, EMBED_URL))

    def test_identity_names_the_weights_and_the_build(self):
        identity = stack_state.identity_for_url(SNAPSHOT, CHAT_URL)
        self.assertEqual(identity["modelPath"], QWEN)
        self.assertEqual(identity["quant"], "Q6_K")
        self.assertEqual(identity["buildInfo"], "b10083-846e991ec")
        self.assertEqual(identity["unit"], "chat-backend-dense")

    def test_both_proxy_profiles_name_the_same_weights(self):
        # :8004 and :8008 are two request shapes in front of one llama-server.
        self.assertEqual(stack_state.identity_for_url(SNAPSHOT, CHAT_URL), stack_state.identity_for_url(SNAPSHOT, THINK_URL))


class ExplanationTests(unittest.TestCase):
    """Each shape of failure the stack can describe."""

    def snapshot_with(self, mutate):
        copy = json.loads(json.dumps(SNAPSHOT))
        mutate(copy)
        return copy

    def test_a_stopped_proxy_is_named_with_its_reason(self):
        def stop_proxy(snapshot):
            for row in snapshot["services"]:
                if row["name"] == "chat-proxy":
                    row["state"] = "stopped"
                    row["reason"] = "stopped on purpose"

        why = stack_state.explain_unreachable(self.snapshot_with(stop_proxy), CHAT_URL)
        self.assertIn("port 8004", why)
        self.assertIn("stopped on purpose", why)

    def test_a_live_proxy_with_no_live_backend_says_so(self):
        # The worst case to diagnose without this: the connection is accepted
        # and every request fails, which reads as the model misbehaving.
        def strand_proxy(snapshot):
            for row in snapshot["services"]:
                if row["name"] == "chat-proxy":
                    row["upstreams"] = [
                        {"any_of": ["chat-backend-dense"], "ok": False, "states": {"chat-backend-dense": "stopped"}}
                    ]

        why = stack_state.explain_unreachable(self.snapshot_with(strand_proxy), CHAT_URL)
        self.assertIn("no live backend", why)
        self.assertIn("chat-backend-dense is stopped", why)

    def test_a_router_held_model_says_it_loads_on_demand(self):
        why = stack_state.explain_unreachable(SNAPSHOT, EMBED_URL)
        self.assertIn("model router", why)
        self.assertIn("not resident", why)

    def test_a_loading_model_says_to_retry(self):
        # Observed live: probing :8005 cold moved the router to `loading` and
        # the call timed out. That is the one state where the same call again
        # in a moment is the right response, so it must not read as a fault.
        def loading(snapshot):
            for row in snapshot["router"]["models"]:
                if row["id"] == "embed":
                    row["state"] = "loading"

        why = stack_state.explain_unreachable(self.snapshot_with(loading), EMBED_URL)
        self.assertIn("loading", why)
        self.assertIn("retrying shortly", why)

    def test_the_reranker_is_matched_despite_its_router_spelling(self):
        # The backend list calls it `rerank`; the router calls it `rank`.
        why = stack_state.explain_unreachable(SNAPSHOT, "http://llms:8006/v1")
        self.assertIn("model router", why)

    def test_a_sleeping_backend_is_named(self):
        def sleep(snapshot):
            for backend in snapshot["backends"]:
                if backend["name"] == "chat-primary":
                    backend["props"]["is_sleeping"] = True

        self.assertIn("sleeping", stack_state.explain_unreachable(self.snapshot_with(sleep), CHAT_URL))

    def test_a_healthy_stack_explains_nothing(self):
        # The endpoint may still be failing for a reason the stack cannot see.
        # Inventing one would be worse than the transport error the caller has.
        self.assertIsNone(stack_state.explain_unreachable(SNAPSHOT, CHAT_URL))


class AlertTests(unittest.TestCase):
    def test_warnings_are_passed_through_as_the_stack_wrote_them(self):
        warnings = stack_state.health_warnings(SNAPSHOT)
        self.assertTrue(any("swap" in text.lower() for text in warnings))

    def test_info_alerts_are_excluded(self):
        # `api_unauthenticated` is an info-level notice about the API itself.
        # Repeating it on every batch report would train the reader to skip
        # warnings entirely.
        codes = [row["code"] for row in stack_state.health_alerts(SNAPSHOT)]
        self.assertNotIn("api_unauthenticated", codes)
        self.assertIn("api_unauthenticated", [row["code"] for row in SNAPSHOT["alerts"]])


class TransportTests(unittest.TestCase):
    """Every way the read can fail has to look the same: no extra information."""

    def test_a_healthy_stub_is_read(self):
        with FakeStackServer() as server:
            snapshot = stack_state.read_snapshot(env=env_for(server.url))
            self.assertEqual(snapshot["api_version"], "1.0")
            self.assertTrue(stack_state.health(env=env_for(server.url)))

    def test_a_bearer_token_is_sent_when_configured(self):
        with FakeStackServer() as server:
            stack_state.read_snapshot(env=env_for(server.url, FORGE_STACK_STATE_TOKEN="hunter2"))
            self.assertEqual(server.requests[0]["auth"], "Bearer hunter2")

    def test_a_server_error_reads_as_absent(self):
        with FakeStackServer(status=500) as server:
            self.assertIsNone(stack_state.read_snapshot(env=env_for(server.url)))

    def test_malformed_json_reads_as_absent(self):
        with FakeStackServer(body="{not json") as server:
            self.assertIsNone(stack_state.read_snapshot(env=env_for(server.url)))

    def test_a_future_major_version_is_refused(self):
        # A wrong reading is worse than no reading: a bogus n_ctx_per_slot would
        # be written into settings as though it had been measured.
        with FakeStackServer(payload={**SNAPSHOT, "api_version": "2.0"}) as server:
            self.assertIsNone(stack_state.read_snapshot(env=env_for(server.url)))

    def test_a_later_minor_version_is_still_read(self):
        with FakeStackServer(payload={**SNAPSHOT, "api_version": "1.7"}) as server:
            self.assertIsNotNone(stack_state.read_snapshot(env=env_for(server.url)))

    def test_an_unreachable_host_reads_as_absent(self):
        stack_state.clear_cache()
        self.assertIsNone(stack_state.read_snapshot(env=env_for("http://127.0.0.1:1"), timeout=1.0))

    def test_the_snapshot_is_cached(self):
        with FakeStackServer() as server:
            for _ in range(3):
                stack_state.read_snapshot(env=env_for(server.url))
            self.assertEqual(len(server.requests), 1)

    def test_failure_is_cached_too(self):
        # A doctor pass over chat, think, and embeddings against a stack that is
        # down should wait one timeout, not three.
        with FakeStackServer(status=500) as server:
            for _ in range(3):
                stack_state.read_snapshot(env=env_for(server.url))
            self.assertEqual(len(server.requests), 1)


class ConfigurationTests(unittest.TestCase):
    def test_the_skip_switch_disables_every_read(self):
        with FakeStackServer() as server:
            env = env_for(server.url, PI_FORGE_SKIP_STACK_DISCOVERY="1")
            self.assertIsNone(stack_state.read_snapshot(env=env))
            self.assertFalse(stack_state.health(env=env))
            self.assertEqual(server.requests, [])

    def test_an_empty_url_turns_the_integration_off(self):
        self.assertFalse(stack_state.resolve_stack_state(env={"FORGE_STACK_STATE_URL": ""})["enabled"])

    def test_settings_supply_the_url_when_the_environment_does_not(self):
        resolved = stack_state.resolve_stack_state(env={}, settings={"stackState": {"baseUrl": "http://box:9000/"}})
        self.assertEqual(resolved["baseUrl"], "http://box:9000")

    def test_the_environment_wins_over_settings(self):
        resolved = stack_state.resolve_stack_state(
            env={"FORGE_STACK_STATE_URL": "http://env:1"}, settings={"stackState": {"baseUrl": "http://settings:2"}}
        )
        self.assertEqual(resolved["baseUrl"], "http://env:1")

    def test_settings_can_disable_it(self):
        self.assertFalse(stack_state.resolve_stack_state(env={}, settings={"stackState": {"enabled": False}})["enabled"])

    def test_a_persisted_api_key_is_used_as_the_token(self):
        resolved = stack_state.resolve_stack_state(env={}, settings={"apiKeys": {"stack-state": "from-settings"}})
        self.assertEqual(resolved["token"], "from-settings")

    def test_the_default_is_the_deployment(self):
        self.assertEqual(stack_state.resolve_stack_state(env={})["baseUrl"], "http://llms:8078")


if __name__ == "__main__":
    unittest.main()

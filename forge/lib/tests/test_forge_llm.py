#!/usr/bin/env python3

import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LIB = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(LIB))
spec = importlib.util.spec_from_file_location("forge_llm", LIB / "forge_llm.py")
forge_llm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(forge_llm)


class FakeChatServer:
    """Minimal OpenAI-compatible endpoint that records what it was sent."""

    def __init__(self, responses=None, predicted_n=2):
        self.requests = []
        self.responses = list(responses or [])
        self.predicted_n = predicted_n
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                body = json.dumps({"object": "list", "data": [{"id": "chat"}]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
                server.requests.append(payload)
                content = server.responses.pop(0) if server.responses else '{"ok": true}'
                if isinstance(content, int):
                    self.send_response(content)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = json.dumps(
                    {
                        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 11, "prompt_tokens_details": {"cached_tokens": 7}},
                        "timings": {"predicted_n": server.predicted_n, "prompt_ms": 1.0, "predicted_ms": 2.0},
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1/chat/completions"

    def __enter__(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_args):
        self.httpd.shutdown()
        self.httpd.server_close()


def service(url, name="chat", scheduling_enabled=False):
    return {
        "name": name,
        "enabled": True,
        "url": url,
        "model": "chat",
        "scheduling": {
            "enabled": scheduling_enabled,
            "interactiveSlot": 0,
            "backgroundSlot": 1,
            "idleGraceMs": 0,
            "yieldMs": 0,
            "backgroundOutputTokens": 4096,
        },
    }


class ResolutionTests(unittest.TestCase):
    def test_defaults_split_bulk_and_judgment_across_backends(self):
        chat = forge_llm.resolve_service("chat", env={}, settings={})
        think = forge_llm.resolve_service("think", env={}, settings={})
        self.assertEqual(chat["url"], "http://llms:8004/v1/chat/completions")
        self.assertEqual(chat["model"], "chat")
        self.assertEqual(think["url"], "http://llms:8008/v1/chat/completions")
        self.assertEqual(think["model"], "code")

    def test_precedence_is_explicit_then_environment_then_settings(self):
        settings = {"chat": {"baseUrl": "http://settings:1/v1/chat/completions", "model": "from-settings"}}
        from_settings = forge_llm.resolve_service("chat", env={}, settings=settings)
        self.assertEqual(from_settings["model"], "from-settings")

        environment = {"FORGE_CHAT_URL": "http://env:2/v1", "FORGE_BASE_MODEL": "from-env"}
        from_environment = forge_llm.resolve_service("chat", env=environment, settings=settings)
        self.assertEqual(from_environment["url"], "http://env:2/v1/chat/completions")
        self.assertEqual(from_environment["model"], "from-env")

        explicit = forge_llm.resolve_service(
            "chat", base_url="http://explicit:3/v1/chat/completions", model="from-argument", env=environment, settings=settings
        )
        self.assertEqual(explicit["url"], "http://explicit:3/v1/chat/completions")
        self.assertEqual(explicit["model"], "from-argument")

    def test_bare_v1_base_is_completed(self):
        self.assertEqual(
            forge_llm.normalize_base_url("http://llms:8004/v1"), "http://llms:8004/v1/chat/completions"
        )

    def test_think_falls_back_to_chat_when_disabled(self):
        settings = {
            "chat": {"baseUrl": "http://only:1/v1/chat/completions", "model": "solo"},
            "think": {"enabled": False},
        }
        resolved = forge_llm.resolve_think_or_chat(env={}, settings=settings)
        self.assertEqual(resolved["url"], "http://only:1/v1/chat/completions")
        self.assertEqual(resolved["fallback"], "chat")

    def test_settings_are_read_from_the_agent_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = Path(directory) / "agent"
            agent.mkdir()
            (agent / "settings.json").write_text(
                json.dumps({"connectedServices": {"chat": {"baseUrl": "http://persisted:4/v1", "model": "persisted"}}}),
                encoding="utf-8",
            )
            resolved = forge_llm.resolve_service("chat", env={"PI_FORGE_AGENT_DIR": str(agent)})
            self.assertEqual(resolved["url"], "http://persisted:4/v1/chat/completions")
            self.assertEqual(resolved["model"], "persisted")


class ResponseParsingTests(unittest.TestCase):
    def test_stray_think_block_and_fences_are_stripped(self):
        self.assertEqual(forge_llm.parse_json_content('<think>\nhm\n</think>\n{"a": 1}'), {"a": 1})
        self.assertEqual(forge_llm.parse_json_content('```json\n{"a": 2}\n```'), {"a": 2})
        self.assertEqual(forge_llm.parse_json_content('Here you go: {"a": 3} — done'), {"a": 3})

    def test_hidden_reasoning_is_measured_from_token_counts(self):
        # The server strips the think block, so only the token count betrays it.
        self.assertEqual(forge_llm.hidden_token_count(2, "ready"), 0)
        self.assertGreater(forge_llm.hidden_token_count(419, "ready"), 400)

    def test_call_records_hidden_reasoning(self):
        with FakeChatServer(responses=['{"ok": true}'], predicted_n=400) as server:
            value, record = forge_llm.call_json(service(server.url), [{"role": "user", "content": "hi"}])
            self.assertEqual(value, {"ok": True})
            self.assertTrue(record["reasoned"])
            self.assertEqual(record["cachedTokens"], 7)
            self.assertEqual(record["service"], "chat")

    def test_call_reports_a_non_thinking_backend_as_such(self):
        with FakeChatServer(responses=['{"ok": true}'], predicted_n=4) as server:
            _, record = forge_llm.call_json(service(server.url), [{"role": "user", "content": "hi"}])
            self.assertFalse(record["reasoned"])

    def test_transient_failures_are_retried(self):
        with FakeChatServer(responses=[503, '{"ok": true}'], predicted_n=2) as server:
            value, _ = forge_llm.call_json_with_retry(service(server.url), [{"role": "user", "content": "hi"}])
            self.assertEqual(value, {"ok": True})
            self.assertEqual(len(server.requests), 2)

    def test_cache_prompt_is_requested_by_default(self):
        with FakeChatServer(responses=['{"ok": true}']) as server:
            forge_llm.call_json(service(server.url), [{"role": "user", "content": "hi"}])
            self.assertTrue(server.requests[0]["cache_prompt"])
            self.assertNotIn("id_slot", server.requests[0])

    def test_background_calls_pin_the_background_slot(self):
        with tempfile.TemporaryDirectory() as directory, FakeChatServer(responses=['{"ok": true}']) as server:
            forge_llm.call_json(
                service(server.url, name="think", scheduling_enabled=True),
                [{"role": "user", "content": "hi"}],
                background=True,
                env={"PI_FORGE_AGENT_DIR": directory},
            )
            self.assertEqual(server.requests[0]["id_slot"], 1)

    def test_background_leases_are_released(self):
        with tempfile.TemporaryDirectory() as directory, FakeChatServer(responses=['{"ok": true}']) as server:
            forge_llm.call_json(
                service(server.url, name="think", scheduling_enabled=True),
                [{"role": "user", "content": "hi"}],
                background=True,
                env={"PI_FORGE_AGENT_DIR": directory},
            )
            leases = list((Path(directory) / "inference-leases").glob("*.json"))
            self.assertEqual(leases, [])


class DoctorTests(unittest.TestCase):
    def test_doctor_flags_a_bulk_endpoint_that_still_thinks(self):
        with FakeChatServer(responses=["ready"], predicted_n=400) as server:
            report = forge_llm.service_doctor(service(server.url), expect_non_thinking=True)
            self.assertTrue(report["reachable"])
            self.assertTrue(report["thinking"])
            self.assertIn("non-thinking server", report["warning"])

    def test_doctor_passes_a_genuine_non_thinking_endpoint(self):
        with FakeChatServer(responses=["ready"], predicted_n=2) as server:
            report = forge_llm.service_doctor(service(server.url), expect_non_thinking=True)
            self.assertFalse(report["thinking"])
            self.assertNotIn("warning", report)

    def test_doctor_reports_a_model_name_the_server_does_not_serve(self):
        with FakeChatServer(responses=["ready"]) as server:
            configured = service(server.url)
            configured["model"] = "not-served"
            report = forge_llm.service_doctor(configured)
            self.assertTrue(report["modelMismatch"])

    def test_doctor_reports_an_unreachable_endpoint(self):
        report = forge_llm.service_doctor(service("http://127.0.0.1:9/v1/chat/completions"), timeout=1.0)
        self.assertFalse(report["reachable"])


if __name__ == "__main__":
    unittest.main()

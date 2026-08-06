#!/usr/bin/env python3

import argparse
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
# The same module object `forge_llm` imported, so clearing its cache in a test
# actually clears the one the code under test reads.
stack_state = sys.modules["stack_state"]


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
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/v1/chat/completions"

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

    def test_a_service_carries_its_own_context_ceiling(self):
        default = forge_llm.resolve_service("chat", env={}, settings={})
        self.assertEqual(default["contextTokens"], forge_llm.SLOT_CONTEXT_TOKENS)

        settings = {"chat": {"contextTokens": 32768}}
        from_settings = forge_llm.resolve_service("chat", env={}, settings=settings)
        self.assertEqual(from_settings["contextTokens"], 32768)

        environment = {"FORGE_BASE_CHAT_CONTEXT_TOKENS": "8192"}
        from_environment = forge_llm.resolve_service("chat", env=environment, settings=settings)
        self.assertEqual(from_environment["contextTokens"], 8192)

    def test_a_nonsense_context_ceiling_falls_back_rather_than_disabling_the_check(self):
        # Zero, a negative, and a non-number all mean "unset". Honouring any of
        # them would leave the preflight comparing against a ceiling nothing can
        # fit, or against no ceiling at all.
        for value in (0, -1, "many", None):
            resolved = forge_llm.resolve_service("chat", env={}, settings={"chat": {"contextTokens": value}})
            self.assertEqual(resolved["contextTokens"], forge_llm.SLOT_CONTEXT_TOKENS)

    def test_chat_template_kwargs_resolve_from_settings_and_environment(self):
        self.assertIsNone(forge_llm.resolve_service("chat", env={}, settings={})["chatTemplateKwargs"])

        settings = {"chat": {"chatTemplateKwargs": {"enable_thinking": False}}}
        from_settings = forge_llm.resolve_service("chat", env={}, settings=settings)
        self.assertEqual(from_settings["chatTemplateKwargs"], {"enable_thinking": False})

        environment = {"FORGE_BASE_CHAT_TEMPLATE_KWARGS": '{"enable_thinking": true}'}
        from_environment = forge_llm.resolve_service("chat", env=environment, settings=settings)
        self.assertEqual(from_environment["chatTemplateKwargs"], {"enable_thinking": True})

    def test_unusable_chat_template_kwargs_are_treated_as_unset(self):
        # Forwarding a malformed value would make the backend reject the whole
        # request rather than ignore one field.
        for value in ("not json", "[1,2]", "{}", "", 7):
            resolved = forge_llm.resolve_service("chat", env={}, settings={"chat": {"chatTemplateKwargs": value}})
            self.assertIsNone(resolved["chatTemplateKwargs"])

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


class TaskServiceTests(unittest.TestCase):
    """The small tier. Off unless configured, and it degrades toward the 27B."""

    def test_the_default_carries_the_smaller_ceiling_and_disables_thinking(self):
        task = forge_llm.resolve_service("task", env={}, settings={})
        self.assertEqual(task["url"], "http://llms:8007/v1/chat/completions")
        self.assertEqual(task["contextTokens"], 65538)
        self.assertEqual(task["chatTemplateKwargs"], {"enable_thinking": False})

    def test_it_is_off_until_someone_turns_it_on(self):
        # A separate backend behind a swapping router. An install that never
        # asked for it should not start paying model swaps.
        self.assertFalse(forge_llm.resolve_service("task", env={}, settings={})["enabled"])
        enabled = forge_llm.resolve_service("task", env={}, settings={"task": {"enabled": True}})
        self.assertTrue(enabled["enabled"])

    def test_an_unconfigured_task_tier_falls_back_up_to_chat(self):
        resolved = forge_llm.resolve_task_or_chat(env={}, settings={})
        self.assertEqual(resolved["url"], "http://llms:8004/v1/chat/completions")
        self.assertEqual(resolved["fallback"], "chat")
        self.assertEqual(resolved["name"], "task")

    def test_a_configured_task_tier_is_used(self):
        settings = {"task": {"enabled": True, "baseUrl": "http://small:7/v1", "model": "small"}}
        resolved = forge_llm.resolve_task_or_chat(env={}, settings=settings)
        self.assertEqual(resolved["url"], "http://small:7/v1/chat/completions")
        self.assertNotIn("fallback", resolved)

    def test_its_endpoint_resolves_through_every_layer(self):
        settings = {"task": {"enabled": True, "baseUrl": "http://settings:1/v1", "model": "from-settings"}}
        self.assertEqual(forge_llm.resolve_service("task", env={}, settings=settings)["model"], "from-settings")

        environment = {"FORGE_TASK_URL": "http://env:2/v1", "FORGE_TASK_MODEL": "from-env"}
        from_environment = forge_llm.resolve_service("task", env=environment, settings=settings)
        self.assertEqual(from_environment["url"], "http://env:2/v1/chat/completions")
        self.assertEqual(from_environment["model"], "from-env")

        explicit = forge_llm.resolve_service(
            "task", base_url="http://explicit:3/v1", model="from-argument", env=environment, settings=settings
        )
        self.assertEqual(explicit["url"], "http://explicit:3/v1/chat/completions")
        self.assertEqual(explicit["model"], "from-argument")

    def test_its_ceiling_and_template_kwargs_are_overridable(self):
        environment = {"FORGE_TASK_CONTEXT_TOKENS": "32768", "FORGE_TASK_TEMPLATE_KWARGS": '{"enable_thinking": true}'}
        resolved = forge_llm.resolve_service("task", env=environment, settings={})
        self.assertEqual(resolved["contextTokens"], 32768)
        self.assertEqual(resolved["chatTemplateKwargs"], {"enable_thinking": True})


class ServiceFromArgumentsTests(unittest.TestCase):
    """Rebuilding a service mid-run must not lose what resolution established.

    Four skills rebuilt one by hand from ``args.base_url`` and ``args.model``,
    which silently dropped the two fields that make a non-default backend usable.
    """

    def arguments(self, **fields):
        return argparse.Namespace(**{"base_url": None, "model": None, "think_url": None, "think_model": None, **fields})

    def test_a_rebuilt_service_keeps_its_context_ceiling_and_template_kwargs(self):
        settings = {"chat": {"contextTokens": 65538, "chatTemplateKwargs": {"enable_thinking": False}}}
        service = forge_llm.service_from_args(self.arguments(), "chat", env={}, settings=settings)
        self.assertEqual(service["contextTokens"], 65538)
        self.assertEqual(service["chatTemplateKwargs"], {"enable_thinking": False})

    def test_a_prompt_over_the_rebuilt_ceiling_is_refused_before_it_is_sent(self):
        # The whole point of carrying contextTokens. A 100k prompt against a
        # 65,538-token backend used to sail through the preflight against the
        # 131,072 default and come back as an HTTP error that read like the
        # model failing the task.
        settings = {"chat": {"contextTokens": 65538}}
        service = forge_llm.service_from_args(self.arguments(), "chat", env={}, settings=settings)
        messages = [{"role": "user", "content": "x" * (100_000 * 3)}]
        with self.assertRaises(forge_llm.ContextBudgetError):
            forge_llm.call(service, messages, env={})

    def test_the_resolution_is_cached_on_the_arguments(self):
        args = self.arguments()
        first = forge_llm.service_from_args(args, "chat", env={}, settings={})
        second = forge_llm.service_from_args(args, "chat", env={}, settings={"chat": {"model": "ignored"}})
        self.assertIs(first, second)

    def test_the_endpoint_a_command_already_resolved_wins(self):
        args = self.arguments(base_url="http://resolved:9/v1", model="resolved")
        service = forge_llm.service_from_args(args, "chat", env={}, settings={"chat": {"model": "from-settings"}})
        self.assertEqual(service["url"], "http://resolved:9/v1/chat/completions")
        self.assertEqual(service["model"], "resolved")

    def test_think_reads_its_own_argument_names(self):
        args = self.arguments(think_url="http://thinker:8/v1", think_model="thinker")
        service = forge_llm.service_from_args(args, "think", env={}, settings={})
        self.assertEqual(service["url"], "http://thinker:8/v1/chat/completions")
        self.assertEqual(service["model"], "thinker")


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

    def test_chat_template_kwargs_are_forwarded_verbatim(self):
        with FakeChatServer(responses=['{"ok": true}']) as server:
            configured = service(server.url)
            configured["chatTemplateKwargs"] = {"enable_thinking": False}
            forge_llm.call_json(configured, [{"role": "user", "content": "hi"}])
            self.assertEqual(server.requests[0]["chat_template_kwargs"], {"enable_thinking": False})

    def test_no_chat_template_kwargs_key_when_none_is_configured(self):
        # A backend that does not understand the field must not be sent it.
        with FakeChatServer(responses=['{"ok": true}']) as server:
            forge_llm.call_json(service(server.url), [{"role": "user", "content": "hi"}])
            self.assertNotIn("chat_template_kwargs", server.requests[0])

    def test_a_smaller_service_ceiling_refuses_a_prompt_the_default_would_allow(self):
        with FakeChatServer(responses=['{"ok": true}']) as server:
            # Comfortably inside a 131072-token slot, well over a 4096-token one.
            messages = [{"role": "user", "content": "x" * 40000}]
            forge_llm.call_json(service(server.url), messages)
            self.assertEqual(len(server.requests), 1)

            smaller = service(server.url)
            smaller["contextTokens"] = 4096
            with self.assertRaises(forge_llm.ContextBudgetError) as caught:
                forge_llm.call_json(smaller, messages)
            self.assertIn("4096-token limit on service 'chat'", str(caught.exception))
            # Refused before a socket was opened, so the count has not moved.
            self.assertEqual(len(server.requests), 1)

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

    def test_doctor_names_an_endpoint_that_answers_with_no_visible_content(self):
        # Measured against the task backend: with `--reasoning-format deepseek`
        # the reply arrives in `reasoning_content` and `content` is empty, so
        # every JSON-expecting skill fails on a response the server called a
        # success. The doctor has to say which knob fixes it.
        with FakeChatServer(responses=[""], predicted_n=64) as server:
            report = forge_llm.service_doctor(service(server.url), expect_non_thinking=True)
            self.assertTrue(report["reachable"])
            self.assertTrue(report["emptyContent"])
            self.assertIn("enable_thinking", report["warning"])
            self.assertIn("no visible content", report["detail"])

    def test_doctor_reports_the_effective_context_ceiling(self):
        with FakeChatServer(responses=["ready"], predicted_n=2) as server:
            configured = service(server.url)
            configured["contextTokens"] = 32768
            report = forge_llm.service_doctor(configured)
            self.assertEqual(report["contextTokens"], 32768)

    def test_doctor_reports_an_unreachable_endpoint(self):
        report = forge_llm.service_doctor(service("http://127.0.0.1:9/v1/chat/completions"), timeout=1.0)
        self.assertFalse(report["reachable"])


class FakeStackServer:
    """A state API whose primary backend claims to live on ``chat_port``.

    Rewriting the port is what lets a doctor test resolve a throwaway stub on
    127.0.0.1 to a backend in the captured fixture.
    """

    def __init__(self, chat_port, context_tokens=131072, total_slots=2, active=True):
        snapshot = json.loads((Path(__file__).resolve().parent / "fixtures" / "stack-snapshot.json").read_text(encoding="utf-8"))
        for backend in snapshot["backends"]:
            if backend["name"] == "chat-primary":
                backend["base_url"] = f"http://127.0.0.1:{chat_port}"
                backend["active"] = active
                backend["props"]["n_ctx_per_slot"] = context_tokens
                backend["props"]["total_slots"] = total_slots
        # The fixture's own port map would otherwise claim this port for a
        # different role and defeat the rewrite above.
        snapshot["config"] = {}
        body = json.dumps(snapshot).encode("utf-8")

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.env = {"FORGE_STACK_STATE_URL": f"http://127.0.0.1:{self.httpd.server_port}"}

    def __enter__(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        stack_state.clear_cache()
        return self

    def __exit__(self, *_exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        stack_state.clear_cache()


class StackAwareDoctorTests(unittest.TestCase):
    """What the doctor gains where the deployment publishes a state API.

    All of it is additive. The final test is the one that matters most: on an
    install with no such API, the report is exactly what it always was.
    """

    def test_doctor_names_the_weights_behind_the_endpoint(self):
        # The model id proves nothing — llama.cpp answers to whatever name it is
        # sent regardless of what is loaded — so the doctor reports the launched
        # path, its quantization, and the llama.cpp build instead.
        with FakeChatServer(responses=["ready"]) as chat, FakeStackServer(chat.port) as stack:
            report = forge_llm.service_doctor(service(chat.url), env=stack.env)
            self.assertEqual(report["backend"]["quant"], "Q6_K")
            self.assertEqual(report["backend"]["buildInfo"], "b10083-846e991ec")
            self.assertTrue(report["backend"]["modelPath"].endswith(".gguf"))

    def test_doctor_explains_why_an_endpoint_is_down(self):
        with FakeChatServer(responses=["ready"]) as chat:
            port = chat.port
        # `chat` is closed here, so the doctor's probe genuinely fails while the
        # stack still describes the backend that used to be on that port.
        with FakeStackServer(port, active=False) as stack:
            report = forge_llm.service_doctor(service(f"http://127.0.0.1:{port}/v1/chat/completions"), timeout=1.0, env=stack.env)
            self.assertFalse(report["reachable"])
            self.assertIn("stackDetail", report)
            self.assertIn(report["stackDetail"], report["detail"])

    def test_doctor_warns_when_the_configured_context_does_not_match_the_slot(self):
        # Configured too high, a skill sends a prompt this client believes fits
        # and reads the server's rejection as the model failing the task.
        with FakeChatServer(responses=["ready"]) as chat, FakeStackServer(chat.port, context_tokens=65536) as stack:
            report = forge_llm.service_doctor(service(chat.url), env=stack.env)
            self.assertTrue(report["contextMismatch"])
            self.assertEqual(report["servedContextTokens"], 65536)
            self.assertIn("65536", report["contextWarning"])
            self.assertIn(report["contextWarning"], forge_llm.doctor_warnings(report))

    def test_doctor_warns_when_the_pinned_slot_does_not_exist(self):
        # Nothing else in forge checks this: the slot number is only ever sent,
        # never validated, so a backend moving to `--parallel 1` would leave
        # every background call naming slot 1 forever.
        with FakeChatServer(responses=["ready"]) as chat, FakeStackServer(chat.port, total_slots=1) as stack:
            report = forge_llm.service_doctor(service(chat.url, scheduling_enabled=True), env=stack.env)
            self.assertIn("slot 1", report["slotWarning"])
            self.assertIn("1 slot", report["slotWarning"])

    def test_a_matching_deployment_produces_no_warnings(self):
        with FakeChatServer(responses=["ready"]) as chat, FakeStackServer(chat.port) as stack:
            report = forge_llm.service_doctor(service(chat.url, scheduling_enabled=True), env=stack.env)
            self.assertEqual(forge_llm.doctor_warnings(report), [])

    def test_the_report_is_unchanged_without_a_stack(self):
        # The degradation guarantee: every install that is not this deployment.
        with FakeChatServer(responses=["ready", "ready"]) as chat:
            without = forge_llm.service_doctor(service(chat.url), env={"PI_FORGE_SKIP_STACK_DISCOVERY": "1"})
            unreachable = forge_llm.service_doctor(service(chat.url), env={"FORGE_STACK_STATE_URL": "http://127.0.0.1:1"})
        for report in (without, unreachable):
            report.pop("elapsedMs", None)
            self.assertNotIn("backend", report)
            self.assertNotIn("stackDetail", report)
            self.assertNotIn("contextWarning", report)
            self.assertNotIn("slotWarning", report)
        self.assertEqual(without, unreachable)


class StackConditionTests(unittest.TestCase):
    """Backend provenance on the call records skills journal into a run."""

    def test_the_first_record_carries_the_backend_and_the_stack_warnings(self):
        forge_llm.reset_stack_conditions()
        with FakeChatServer(responses=["ok", "ok"]) as chat, FakeStackServer(chat.port) as stack:
            _, first = forge_llm.call(service(chat.url), [{"role": "user", "content": "hi"}], env=stack.env)
            _, second = forge_llm.call(service(chat.url), [{"role": "user", "content": "hi"}], env=stack.env)
        self.assertTrue(first["backend"]["modelPath"].endswith(".gguf"))
        self.assertTrue(any("swap" in text.lower() for text in first["stackWarnings"]))
        # A 500-item batch should say which weights it ran against once, not
        # five hundred times.
        self.assertNotIn("backend", second)
        self.assertNotIn("stackWarnings", second)
        forge_llm.reset_stack_conditions()

    def test_records_are_unchanged_without_a_stack(self):
        forge_llm.reset_stack_conditions()
        with FakeChatServer(responses=["ok"]) as chat:
            _, record = forge_llm.call(
                service(chat.url), [{"role": "user", "content": "hi"}], env={"PI_FORGE_SKIP_STACK_DISCOVERY": "1"}
            )
        self.assertNotIn("backend", record)
        self.assertNotIn("stackWarnings", record)
        forge_llm.reset_stack_conditions()


if __name__ == "__main__":
    unittest.main()

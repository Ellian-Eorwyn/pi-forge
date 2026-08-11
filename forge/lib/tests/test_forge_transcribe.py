#!/usr/bin/env python3
"""Tests for the llm-stack transcription client.

Everything runs against a local stub. Nothing here may reach the real service:
the suite has to pass on a machine that has never heard of ``llms``.

The payload shapes are the ones the live service actually produced on
2026-08-10, including the two that a hand-written fixture would smooth over: an
``hf-asr`` entry registered with an empty ``model``, and a ``202`` job envelope
that carries no ``text`` at all.
"""

import importlib.util
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LIB = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(LIB))
spec = importlib.util.spec_from_file_location("forge_transcribe", LIB / "forge_transcribe.py")
forge_transcribe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(forge_transcribe)

ENGINES = {
    "ok": True,
    "active_engine": "parakeet-v3",
    "device": "cuda",
    "resident": None,
    "idle_unload_seconds": 300.0,
    "router": {"models": ["embed", "ocr", "rank", "task"], "reachable": True, "yield_mode": "asr"},
    "engines": [
        {"id": "parakeet-v3", "model": "preset:nvidia/parakeet-tdt-0.6b-v3", "runtime": "nemo"},
        {"id": "faster-whisper", "model": "preset:large-v3", "runtime": "faster-whisper"},
        {"id": "hf-asr", "model": "", "runtime": "hf"},
    ],
}

TRANSCRIPT = {
    "ok": True,
    "request_id": "a3f9c1d2",
    "text": "This is a test recording.",
    "language": "en",
    "duration": 12.8,
    "segments": [
        {"id": 0, "start": 0.0, "end": 3.76, "text": "This is a test recording."},
        {"id": 1, "start": 3.92, "end": 8.72, "text": "  "},
    ],
    "engine": "parakeet-v3",
    "model": "preset:nvidia/parakeet-tdt-0.6b-v3",
    "device": "cuda",
    "capabilities": {"word_timestamps": True, "diarization": False, "translate": False},
    "timings": {"queued_ms": 2, "load_ms": 24500, "decode_ms": 550, "total_ms": 25052},
}


class FakeTranscriptionServer:
    """A stub sidecar that can misbehave in each way a real one might."""

    def __init__(self, *, transcribe_status=200, transcribe_body=None, job_states=None):
        self.requests = []
        server = self
        self._job_states = list(job_states or [])

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _respond(self, status, payload):
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self):
                server.requests.append({"method": "GET", "path": self.path, "auth": self.headers.get("Authorization")})
                if self.path.endswith("/health"):
                    self._respond(200, {"ok": True, "status": "ok", "service": "transcript-backend"})
                    return
                if self.path.endswith("/engines"):
                    self._respond(200, ENGINES)
                    return
                if self.path.startswith("/jobs/"):
                    state = server._job_states.pop(0) if server._job_states else {"status": "done", "result": TRANSCRIPT}
                    self._respond(200, state)
                    return
                self._respond(404, {"ok": False, "error": {"type": "bad_request", "message": "no such route"}})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                server.requests.append(
                    {
                        "method": "POST",
                        "path": self.path,
                        "auth": self.headers.get("Authorization"),
                        "contentType": self.headers.get("Content-Type"),
                        "declaredLength": length,
                        "actualLength": len(body),
                        "body": body.decode("utf-8", errors="replace"),
                    }
                )
                self._respond(transcribe_status, transcribe_body if transcribe_body is not None else TRANSCRIPT)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        host, port = self._server.server_address[:2]
        self.base_url = f"http://{host}:{port}"
        return self

    def __exit__(self, *_args):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def service(base_url, **overrides):
    resolved = {
        "name": "transcription",
        "enabled": True,
        "baseUrl": base_url,
        "engine": "parakeet-v3",
        "token": None,
        "timeoutSeconds": 30.0,
    }
    resolved.update(overrides)
    return resolved


def audio_fixture(directory, name="clip.wav", payload=b"RIFF....synthetic audio bytes"):
    path = Path(directory) / name
    path.write_bytes(payload)
    return path


class ResolutionTests(unittest.TestCase):
    def test_defaults_when_nothing_is_configured(self):
        resolved = forge_transcribe.resolve_transcription(env={}, settings={})
        self.assertEqual(resolved["baseUrl"], forge_transcribe.DEFAULT_TRANSCRIPTION_URL)
        self.assertEqual(resolved["engine"], "parakeet-v3")
        self.assertTrue(resolved["enabled"])
        self.assertIsNone(resolved["token"])

    def test_precedence_is_explicit_then_environment_then_settings(self):
        settings = {"transcription": {"baseUrl": "http://persisted:8014", "engine": "canary-qwen"}}
        env = {"FORGE_TRANSCRIPTION_URL": "http://env:8014", "FORGE_TRANSCRIPTION_ENGINE": "faster-whisper"}
        self.assertEqual(
            forge_transcribe.resolve_transcription(env={}, settings=settings)["baseUrl"], "http://persisted:8014"
        )
        self.assertEqual(forge_transcribe.resolve_transcription(env=env, settings=settings)["baseUrl"], "http://env:8014")
        self.assertEqual(
            forge_transcribe.resolve_transcription(base_url="http://explicit:8014", env=env, settings=settings)["baseUrl"],
            "http://explicit:8014",
        )
        self.assertEqual(forge_transcribe.resolve_transcription(env=env, settings=settings)["engine"], "faster-whisper")

    def test_empty_url_turns_the_integration_off(self):
        resolved = forge_transcribe.resolve_transcription(env={"FORGE_TRANSCRIPTION_URL": ""}, settings={})
        self.assertFalse(resolved["enabled"])

    def test_empty_token_env_clears_a_persisted_token(self):
        settings = {"transcription": {"token": "persisted-secret"}}
        self.assertEqual(forge_transcribe.resolve_transcription(env={}, settings=settings)["token"], "persisted-secret")
        cleared = forge_transcribe.resolve_transcription(env={"FORGE_TRANSCRIPTION_TOKEN": ""}, settings=settings)
        self.assertIsNone(cleared["token"])


class EngineStatusTests(unittest.TestCase):
    def test_a_configured_engine_is_usable(self):
        usable, reason = forge_transcribe.engine_status(ENGINES, "parakeet-v3")
        self.assertTrue(usable)
        self.assertIsNone(reason)

    def test_an_engine_with_no_model_is_refused_before_an_upload(self):
        # Registered but unusable, and it fails as `model_load_failed` at load
        # time rather than as `engine_unavailable` up front -- so the engine list
        # is the only place to catch it cheaply.
        usable, reason = forge_transcribe.engine_status(ENGINES, "hf-asr")
        self.assertFalse(usable)
        self.assertIn("no model configured", reason)

    def test_an_unregistered_engine_names_what_is_registered(self):
        usable, reason = forge_transcribe.engine_status(ENGINES, "whisper-large")
        self.assertFalse(usable)
        self.assertIn("parakeet-v3", reason)


class HealthTests(unittest.TestCase):
    def test_health_and_engines_read_the_service(self):
        with FakeTranscriptionServer() as stub:
            resolved = service(stub.base_url)
            self.assertEqual(forge_transcribe.health(resolved)["status"], "ok")
            self.assertEqual(forge_transcribe.engines(resolved)["active_engine"], "parakeet-v3")

    def test_an_unreachable_service_reads_as_absence_not_an_error(self):
        resolved = service("http://127.0.0.1:1")
        self.assertIsNone(forge_transcribe.health(resolved))
        self.assertIsNone(forge_transcribe.engines(resolved))


class TranscribeTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.audio = audio_fixture(self._directory.name)
        # The real interval is tuned for a GPU, not for a test suite.
        interval = forge_transcribe.POLL_INTERVAL
        forge_transcribe.POLL_INTERVAL = 0.01
        self.addCleanup(setattr, forge_transcribe, "POLL_INTERVAL", interval)

    def test_the_synchronous_path_returns_the_envelope(self):
        with FakeTranscriptionServer() as stub:
            result = forge_transcribe.transcribe(service(stub.base_url), self.audio)
            self.assertEqual(result["text"], "This is a test recording.")
            self.assertEqual(result["engine"], "parakeet-v3")

    def test_the_request_pins_the_engine_and_asks_for_word_timestamps(self):
        # NeMo returns no timeline at all unless word timestamps are requested,
        # so this is the difference between real utterance boundaries and the
        # decoder's own 60-second windows.
        with FakeTranscriptionServer() as stub:
            forge_transcribe.transcribe(service(stub.base_url), self.audio)
            post = next(item for item in stub.requests if item["method"] == "POST")
            self.assertIn("multipart/form-data; boundary=", post["contentType"])
            self.assertIn('name="engine"\r\n\r\nparakeet-v3', post["body"])
            self.assertIn('name="word_timestamps"\r\n\r\ntrue', post["body"])
            self.assertIn('name="file"; filename="clip.wav"', post["body"])
            # The body is streamed from disk, so a wrong Content-Length would
            # truncate the upload rather than fail loudly.
            self.assertEqual(post["declaredLength"], post["actualLength"])

    def test_word_timestamps_can_be_turned_off(self):
        with FakeTranscriptionServer() as stub:
            forge_transcribe.transcribe(service(stub.base_url), self.audio, word_timestamps=False)
            post = next(item for item in stub.requests if item["method"] == "POST")
            self.assertIn('name="word_timestamps"\r\n\r\nfalse', post["body"])

    def test_a_token_is_sent_only_when_one_is_configured(self):
        with FakeTranscriptionServer() as stub:
            forge_transcribe.transcribe(service(stub.base_url), self.audio)
            self.assertIsNone(next(item for item in stub.requests if item["method"] == "POST")["auth"])
        with FakeTranscriptionServer() as stub:
            forge_transcribe.transcribe(service(stub.base_url, token="secret"), self.audio)
            self.assertEqual(
                next(item for item in stub.requests if item["method"] == "POST")["auth"], "Bearer secret"
            )

    def test_long_audio_is_polled_to_completion_rather_than_returned_as_a_job(self):
        # The failure this exists to prevent: storing the 202 envelope as if it
        # were a transcript, and finding out only when something reads `text`.
        envelope = {"ok": True, "job_id": "55cb265a", "status": "queued", "poll": "/jobs/55cb265a", "estimated_seconds": 3}
        states = [{"status": "running"}, {"status": "done", "result": TRANSCRIPT}]
        with FakeTranscriptionServer(transcribe_status=202, transcribe_body=envelope, job_states=states) as stub:
            waits = []
            result = forge_transcribe.transcribe(service(stub.base_url), self.audio, on_wait=waits.append)
            self.assertEqual(result["text"], "This is a test recording.")
            self.assertEqual([item["status"] for item in waits], ["queued", "running"])
            self.assertEqual(sum(1 for item in stub.requests if item["path"].startswith("/jobs/")), 2)

    def test_a_failed_job_raises_with_its_type(self):
        envelope = {"ok": True, "job_id": "55cb265a", "status": "queued", "poll": "/jobs/55cb265a"}
        states = [{"status": "error", "error": {"type": "decode_failed", "message": "the decode itself failed"}}]
        with FakeTranscriptionServer(transcribe_status=202, transcribe_body=envelope, job_states=states) as stub:
            with self.assertRaises(forge_transcribe.TranscribeError) as caught:
                forge_transcribe.transcribe(service(stub.base_url), self.audio)
            self.assertEqual(caught.exception.error_type, "decode_failed")

    def test_an_uninstalled_runtime_is_permanent_and_carries_its_hint(self):
        body = {
            "ok": False,
            "error": {
                "type": "engine_unavailable",
                "message": "the nemo runtime is not installed",
                "hint": "bash scripts/install-transcribe.sh --engines nemo",
            },
        }
        with FakeTranscriptionServer(transcribe_status=503, transcribe_body=body) as stub:
            with self.assertRaises(forge_transcribe.TranscribeError) as caught:
                forge_transcribe.transcribe(service(stub.base_url), self.audio)
        self.assertEqual(caught.exception.error_type, "engine_unavailable")
        self.assertFalse(caught.exception.transient, "retrying never installs a runtime")
        self.assertIn("install-transcribe", caught.exception.hint)

    def test_an_unreadable_container_is_permanent(self):
        # Measured against the live service: the host decodes without ffmpeg
        # bindings, so an .m4a answers `decode_failed` — and the bytes do not
        # change between attempts, so retrying it three times bought nothing.
        body = {"ok": False, "error": {"type": "decode_failed", "message": "No module named 'torchaudio.io'"}}
        with FakeTranscriptionServer(transcribe_status=500, transcribe_body=body) as stub:
            with self.assertRaises(forge_transcribe.TranscribeError) as caught:
                forge_transcribe.transcribe(service(stub.base_url), self.audio)
        self.assertFalse(caught.exception.transient)

    def test_a_model_load_failure_is_worth_retrying(self):
        body = {"ok": False, "error": {"type": "model_load_failed", "message": "CUDA out of memory"}}
        with FakeTranscriptionServer(transcribe_status=503, transcribe_body=body) as stub:
            with self.assertRaises(forge_transcribe.TranscribeError) as caught:
                forge_transcribe.transcribe(service(stub.base_url), self.audio)
        self.assertTrue(caught.exception.transient)

    def test_oversized_audio_is_refused_before_it_is_uploaded(self):
        with FakeTranscriptionServer() as stub:
            original = forge_transcribe.UPLOAD_CAP_BYTES
            forge_transcribe.UPLOAD_CAP_BYTES = 4
            try:
                with self.assertRaises(forge_transcribe.TranscribeError) as caught:
                    forge_transcribe.transcribe(service(stub.base_url), self.audio)
            finally:
                forge_transcribe.UPLOAD_CAP_BYTES = original
            self.assertEqual(caught.exception.error_type, "too_large")
            self.assertEqual([item for item in stub.requests if item["method"] == "POST"], [])


class ResultShapeTests(unittest.TestCase):
    def test_segments_drop_the_empty_ones(self):
        segments = forge_transcribe.segments_from_result(TRANSCRIPT)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0], {"start": 0.0, "end": 3.76, "text": "This is a test recording."})

    def test_a_result_with_no_segments_falls_back_to_its_text(self):
        segments = forge_transcribe.segments_from_result({"text": "only flat text", "segments": []})
        self.assertEqual(segments, [{"start": 0.0, "end": 0.0, "text": "only flat text"}])

    def test_load_seconds_reports_a_cold_start(self):
        # Non-zero means the weights were not resident: they had yielded the GPU
        # to the model router, which is the difference between a run that looked
        # slow and a run that was slow.
        self.assertAlmostEqual(forge_transcribe.load_seconds(TRANSCRIPT), 24.5)
        self.assertEqual(forge_transcribe.load_seconds({"timings": {"load_ms": 0}}), 0.0)
        self.assertEqual(forge_transcribe.load_seconds({}), 0.0)


if __name__ == "__main__":
    unittest.main()

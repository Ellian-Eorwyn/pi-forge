#!/usr/bin/env python3
"""Resilience tests for the shared embeddings client.

The endpoint is a model loaded on demand behind the stack's router, on a GPU it
shares with the always-on chat/think weights, so it 500s on a cold-load race or a
moment's contention. These tests pin the retry, per-text fallback, and warm-up that
keep a run's near-dupe pass alive through that, without hitting the network.
"""

import importlib.util
import io
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

LIB = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(LIB))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, LIB / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_load("stack_state")
fe = _load("forge_embeddings")


def _http(code, body=b""):
    return urllib.error.HTTPError("http://x/embeddings", code, "err", {}, io.BytesIO(body))


class _NoSleep(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(fe.time, "sleep", lambda *_: None)
        patch.start()
        self.addCleanup(patch.stop)


class PostBatchRetryTests(_NoSleep):
    def test_transient_500_is_retried_then_succeeds(self):
        calls = []

        def stub(url, model, batch, timeout):
            calls.append(batch)
            if len(calls) == 1:
                raise _http(500, b"internal error")
            return [[0.1, 0.2] for _ in batch]

        with mock.patch.object(fe, "_do_request", stub):
            vectors = fe._post_batch("u", "embed", ["a"], timeout=5)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(vectors), 1)

    def test_gives_up_after_exhausting_retries(self):
        with mock.patch.object(fe, "_do_request", lambda *a: (_ for _ in ()).throw(_http(500, b"down"))):
            with self.assertRaises(urllib.error.HTTPError):
                fe._post_batch("u", "embed", ["a"], timeout=5, retries=2)

    def test_400_bad_model_name_is_not_retried(self):
        calls = []

        def stub(*_a):
            calls.append(1)
            raise _http(400, b"model 'Qwen3-Embedding-4B' not found")

        with mock.patch.object(fe, "_do_request", stub):
            with self.assertRaises(urllib.error.HTTPError):
                fe._post_batch("u", "embed", ["a"], timeout=5, retries=3)
        self.assertEqual(len(calls), 1)  # tried once, never retried

    def test_400_router_still_loading_is_retried(self):
        calls = []

        def stub(url, model, batch, timeout):
            calls.append(1)
            if len(calls) == 1:
                raise _http(400, b"the model router loads embed on demand; not resident, sleeping")
            return [[1.0] for _ in batch]

        with mock.patch.object(fe, "_do_request", stub):
            vectors = fe._post_batch("u", "embed", ["a"], timeout=5)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(vectors), 1)


class EmbedTextsTests(_NoSleep):
    def test_per_text_fallback_when_a_batch_fails(self):
        # A multi-item request fails; single-item requests succeed. The call must
        # fall back to per-text and still return one vector per input.
        def stub(url, model, batch, timeout):
            if batch == ["ping"]:
                return [[0.0]]
            if len(batch) > 1:
                raise _http(500, b"batch too heavy")
            return [[float(len(batch[0]))]]

        with mock.patch.object(fe, "_do_request", stub):
            result = fe.embed_texts(["aa", "bbb", "cccc"], batch_size=8, retries=1)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["vectors"]), 3)

    def test_over_width_text_is_truncated_and_kept(self):
        # A text past one ubatch 500s at full width but embeds once truncated to
        # SAFE_INPUT_CHARS — the note stays in the run rather than failing it.
        long_text = "x" * (fe.SAFE_INPUT_CHARS + 500)

        def stub(url, model, batch, timeout):
            if batch == ["ping"]:
                return [[0.0]]
            if len(batch[0]) > fe.SAFE_INPUT_CHARS:
                raise _http(500, b"input exceeds one ubatch")
            return [[float(len(batch[0]))]]

        with mock.patch.object(fe, "_do_request", stub):
            result = fe.embed_texts([long_text], batch_size=8, retries=1)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["vectors"]), 1)

    def test_short_text_that_always_fails_is_not_truncated_further(self):
        # A text already within the safe width has no shorter slice to fall back to,
        # so a persistent failure surfaces rather than looping.
        calls = []

        def stub(url, model, batch, timeout):
            if batch == ["ping"]:
                return [[0.0]]
            calls.append(batch[0])
            raise _http(500, b"down")

        with mock.patch.object(fe, "_do_request", stub):
            result = fe.embed_texts(["short"], batch_size=8, retries=0)
        self.assertFalse(result["ok"])
        # tried "short" once in the batch and once in _embed_one; never truncated
        self.assertTrue(all(c == "short" for c in calls))

    def test_success_returns_vectors_aligned_to_inputs(self):
        with mock.patch.object(fe, "_do_request", lambda url, model, batch, timeout: [[float(i)] for i, _ in enumerate(batch)]):
            result = fe.embed_texts(["x", "y", "z"], warmup=False, batch_size=8)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["vectors"]), 3)
        self.assertEqual(result["dimensions"], 1)

    def test_a_single_input_that_always_fails_returns_not_ok(self):
        def stub(url, model, batch, timeout):
            if batch == ["ping"]:
                return [[0.0]]
            raise _http(500, b"always down")

        with mock.patch.object(fe, "_do_request", stub):
            result = fe.embed_texts(["only-one"], batch_size=8, retries=1)
        self.assertFalse(result["ok"])
        self.assertIn("reason", result)

    def test_warmup_failure_is_not_fatal(self):
        # Warm-up 500s but the real batches succeed: the run must still complete.
        state = {"warmed": False}

        def stub(url, model, batch, timeout):
            if batch == ["ping"] and not state["warmed"]:
                state["warmed"] = True
                raise _http(500, b"cold")
            return [[9.0] for _ in batch]

        with mock.patch.object(fe, "_do_request", stub):
            result = fe.embed_texts(["a", "b"], batch_size=8, retries=0)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["vectors"]), 2)

    def test_empty_input(self):
        result = fe.embed_texts([])
        self.assertTrue(result["ok"])
        self.assertEqual(result["vectors"], [])


class EndpointResolutionTests(unittest.TestCase):
    """Explicit arg > env > connectedServices.embeddings > default.

    The settings layer is the whole point: without it the 7 Python skills honored
    only FORGE_EMBEDDINGS_URL, so a setup that moved embedding in the config file
    reached the JS web-research client but not them.
    """

    def _agent_dir_with(self, embeddings):
        import json
        import tempfile

        directory = tempfile.mkdtemp()
        (Path(directory) / "settings.json").write_text(
            json.dumps({"connectedServices": {"embeddings": embeddings}}), encoding="utf-8"
        )
        return directory

    def test_explicit_wins(self):
        self.assertEqual(fe.endpoint_url("http://explicit/v1/embeddings", env={}), "http://explicit/v1/embeddings")

    def test_env_wins_over_settings(self):
        env = {
            "PI_FORGE_AGENT_DIR": self._agent_dir_with({"url": "http://settings:5/v1/embeddings"}),
            "FORGE_EMBEDDINGS_URL": "http://env:9/v1/embeddings",
        }
        self.assertEqual(fe.endpoint_url(env=env), "http://env:9/v1/embeddings")

    def test_settings_used_when_no_env(self):
        env = {"PI_FORGE_AGENT_DIR": self._agent_dir_with({"url": "http://laptop:8005/v1/embeddings", "model": "embed"})}
        self.assertEqual(fe.endpoint_url(env=env), "http://laptop:8005/v1/embeddings")
        self.assertEqual(fe.model_name(env=env), "embed")

    def test_default_when_nothing_set(self):
        import tempfile

        env = {"PI_FORGE_AGENT_DIR": tempfile.mkdtemp()}
        self.assertEqual(fe.endpoint_url(env=env), fe.DEFAULT_EMBEDDINGS_URL)
        self.assertEqual(fe.model_name(env=env), fe.DEFAULT_EMBEDDINGS_MODEL)


if __name__ == "__main__":
    unittest.main()

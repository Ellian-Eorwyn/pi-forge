#!/usr/bin/env python3
"""Standard-library smoke tests for organize-folder. Run with:

    python3 -m unittest test_organize_folder

from this directory, or:

    python3 forge/skills/organize-folder/scripts/test_organize_folder.py
"""

import csv
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "organize-folder.py"


def run(*args, env=None):
    merged = None
    if env is not None:
        merged = {**os.environ, **env}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=merged,
    )
    return result


def _char_frequency_vector(text):
    counts = Counter(char for char in text.lower() if char.isalpha())
    return [float(counts.get(chr(ord("a") + index), 0)) for index in range(26)]


class _StubEmbeddingsHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        inputs = payload.get("input", [])
        data = [
            {"index": index, "embedding": _char_frequency_vector(text)}
            for index, text in enumerate(inputs)
        ]
        body = json.dumps({"object": "list", "data": data, "model": "stub"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


class StubEmbeddingsServer:
    """A deterministic local /v1/embeddings endpoint for offline tests. It returns
    a 26-dimensional letter-frequency vector per input, so near-identical text
    yields near-identical vectors."""

    def __enter__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _StubEmbeddingsHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.url = f"http://{host}:{port}/v1/embeddings"
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def read_manifest(run_dir, name="manifest.csv"):
    with (run_dir / name).open(encoding="utf-8") as handle:
        return {row["relative_source_path"]: row for row in csv.DictReader(handle)}


class OrganizeFolderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.target = base / "fixture"
        (self.target / "sub").mkdir(parents=True)
        (self.target / "pics").mkdir()
        (self.target / "data.json").write_text('{"a":1}\n', encoding="utf-8")
        (self.target / "report.md").write_text(
            "markdown report body with a deliberately unique byte length\n",
            encoding="utf-8",
        )
        (self.target / "sub" / "notes.txt").write_text("dup content\n", encoding="utf-8")
        (self.target / "sub" / "notes_copy.txt").write_text("dup content\n", encoding="utf-8")
        (self.target / "sub" / "mysteryfile").write_text("ambiguous bytes here\n", encoding="utf-8")
        png = bytes([137, 80, 78, 71, 13, 10, 26, 10]) + bytes(20)
        (self.target / "pics" / "Screenshot_1.png").write_bytes(png + b"a")
        self.run_dir = base / "run"

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self, *extra, env=None):
        # Default to offline so the base tests stay hermetic; embedding tests opt
        # in explicitly via the stub server.
        args = ["scan", str(self.target), "--output", str(self.run_dir)]
        if "--no-embeddings" not in extra and env is None:
            args.append("--no-embeddings")
        result = run(*args, *extra, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_scan_outputs_and_duplicate(self):
        summary = self.scan()
        self.assertEqual(summary["fileCount"], 6)
        self.assertEqual(summary["duplicateCount"], 1)
        for name in ("manifest.csv", "scan.json", "profile.md", "profile.json", "review_queue.md", "skipped.md", "near_duplicates.md"):
            self.assertTrue((self.run_dir / name).is_file(), name)
        rows = read_manifest(self.run_dir)
        self.assertEqual(rows["sub/notes_copy.txt"]["is_duplicate"], "true")
        self.assertEqual(rows["sub/notes_copy.txt"]["duplicate_of"], "sub/notes.txt")

    def test_size_grouped_hashing(self):
        self.scan()
        rows = read_manifest(self.run_dir)
        # report.md has a unique size: fingerprint only, no full sha256.
        self.assertEqual(rows["report.md"]["sha256"], "")
        self.assertTrue(rows["report.md"]["fingerprint"].startswith("fp:"))
        # The duplicate pair shares a size and is fully hashed.
        self.assertNotEqual(rows["sub/notes.txt"]["sha256"], "")

    def test_full_hash_populates_all(self):
        self.scan("--full-hash")
        rows = read_manifest(self.run_dir)
        self.assertTrue(all(row["sha256"] for row in rows.values()))

    def test_round_trip_apply_undo(self):
        self.scan()
        plan = run("plan", str(self.run_dir))
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertTrue(json.loads(plan.stdout)["valid"])
        apply_result = run("apply", str(self.run_dir))
        self.assertEqual(apply_result.returncode, 0, apply_result.stderr)
        self.assertEqual(json.loads(apply_result.stdout)["failed"], 0)
        self.assertFalse((self.target / "data.json").exists())
        self.assertTrue((self.target / "Data" / "data.json").exists())
        undo_result = run("undo", str(self.run_dir))
        self.assertEqual(undo_result.returncode, 0, undo_result.stderr)
        self.assertTrue((self.target / "data.json").exists())
        # undo restores files to their original paths; it leaves the now-empty
        # destination folders behind rather than deleting anything.
        self.assertFalse((self.target / "Data" / "data.json").exists())

    def test_fingerprint_integrity_detects_change(self):
        self.scan()
        # report.md is fingerprint-only; changing it must fail the move.
        (self.target / "report.md").write_text("totally different and longer body\n", encoding="utf-8")
        apply_result = run("apply", str(self.run_dir))
        self.assertNotEqual(apply_result.returncode, 0)
        final = read_manifest(self.run_dir, "final_manifest.csv")
        self.assertEqual(final["report.md"]["final_status"], "failed")

    def test_edited_fingerprint_rejected(self):
        self.scan()
        manifest_path = self.run_dir / "manifest.csv"
        text = manifest_path.read_text(encoding="utf-8")
        text = text.replace("fp:", "fp:zz", 1)
        manifest_path.write_text(text, encoding="utf-8")
        plan = run("plan", str(self.run_dir))
        self.assertNotEqual(plan.returncode, 0)
        self.assertIn("fingerprint", plan.stdout + plan.stderr)

    def test_embeddings_disabled_leaves_similarity_empty(self):
        self.scan()
        rows = read_manifest(self.run_dir)
        for row in rows.values():
            self.assertEqual(row["content_cluster"], "")
            self.assertEqual(row["near_duplicate_of"], "")
            self.assertEqual(row["content_similarity"], "")
        scan = json.loads((self.run_dir / "scan.json").read_text(encoding="utf-8"))
        self.assertFalse(scan["embeddings"]["enabled"])
        self.assertIn("disabled", scan["embeddings"]["reason"])
        report = (self.run_dir / "near_duplicates.md").read_text(encoding="utf-8")
        self.assertIn("not computed", report)

    def test_embeddings_detect_near_duplicate(self):
        body = "the quick brown fox jumps over the lazy dog while the cat watches nearby"
        (self.target / "doc_a.txt").write_text(body + "\n", encoding="utf-8")
        (self.target / "doc_b.txt").write_text(body + " today as well\n", encoding="utf-8")
        with StubEmbeddingsServer() as stub:
            summary = self.scan(
                "--near-duplicate-threshold", "0.9",
                "--cluster-threshold", "0.7",
                env={"FORGE_EMBEDDINGS_URL": stub.url, "FORGE_EMBEDDINGS_MODEL": "stub"},
            )
        self.assertGreaterEqual(summary["nearDuplicateCount"], 1)
        self.assertTrue(summary["embeddings"]["enabled"])
        rows = read_manifest(self.run_dir)
        # doc_a sorts before doc_b, so doc_b points at doc_a.
        self.assertEqual(rows["doc_b.txt"]["near_duplicate_of"], "doc_a.txt")
        self.assertNotEqual(rows["doc_b.txt"]["content_similarity"], "")
        self.assertNotEqual(rows["doc_a.txt"]["content_cluster"], "")
        self.assertEqual(rows["doc_a.txt"]["content_cluster"], rows["doc_b.txt"]["content_cluster"])
        self.assertIn("near-duplicate", rows["doc_b.txt"]["note"])
        scan = json.loads((self.run_dir / "scan.json").read_text(encoding="utf-8"))
        self.assertTrue(scan["embeddings"]["enabled"])
        self.assertEqual(scan["embeddings"]["nearDuplicateCount"], summary["nearDuplicateCount"])
        report = (self.run_dir / "near_duplicates.md").read_text(encoding="utf-8")
        self.assertIn("doc_b.txt", report)

    def test_embeddings_exact_duplicates_excluded(self):
        # The exact-duplicate pair in the fixture must not also appear as a
        # near-duplicate; exact duplicates are handled by SHA-256.
        with StubEmbeddingsServer() as stub:
            self.scan(
                "--near-duplicate-threshold", "0.9",
                env={"FORGE_EMBEDDINGS_URL": stub.url, "FORGE_EMBEDDINGS_MODEL": "stub"},
            )
        rows = read_manifest(self.run_dir)
        self.assertEqual(rows["sub/notes_copy.txt"]["is_duplicate"], "true")
        self.assertEqual(rows["sub/notes_copy.txt"]["near_duplicate_of"], "")
        self.assertEqual(rows["sub/notes.txt"]["near_duplicate_of"], "")


if __name__ == "__main__":
    unittest.main()

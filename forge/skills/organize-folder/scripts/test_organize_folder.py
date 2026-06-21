#!/usr/bin/env python3
"""Standard-library smoke tests for organize-folder. Run with:

    python3 -m unittest test_organize_folder

from this directory, or:

    python3 forge/skills/organize-folder/scripts/test_organize_folder.py
"""

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "organize-folder.py"


def run(*args):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
    )
    return result


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

    def scan(self, *extra):
        result = run("scan", str(self.target), "--output", str(self.run_dir), *extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_scan_outputs_and_duplicate(self):
        summary = self.scan()
        self.assertEqual(summary["fileCount"], 6)
        self.assertEqual(summary["duplicateCount"], 1)
        for name in ("manifest.csv", "scan.json", "profile.md", "profile.json", "review_queue.md", "skipped.md"):
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


if __name__ == "__main__":
    unittest.main()

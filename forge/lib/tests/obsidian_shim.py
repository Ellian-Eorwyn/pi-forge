#!/usr/bin/env python3
"""A fake `obsidian` binary for tests, plus the registry and env around it.

Shared by the adapter's own tests and by the skill tests that exercise the
CLI-present code paths. Load it the way the rest of this repo loads modules by
path::

    spec = importlib.util.spec_from_file_location(
        "obsidian_shim", Path(__file__).resolve().parents[3] / "lib" / "tests" / "obsidian_shim.py"
    )

Two fidelity rules make the shim worth trusting:

- It always exits 0, including on failure, because the real CLI does. A shim that
  exited non-zero would let pi-forge pass tests by checking a return code it must
  never check.
- ``move`` and ``rename`` really move the file and really rewrite links across the
  fake vault, so the caller's hash checks, link-only diff, and backlink re-counts
  run against bytes on disk instead of a mock.
"""

import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

SHIM = '''#!/usr/bin/env python3
import json, os, re, sys, time
from pathlib import Path

script = json.loads(Path(os.environ["SHIM_SCRIPT"]).read_text())
argv = sys.argv[1:]
with open(os.environ["SHIM_LOG"], "a") as log:
    log.write(json.dumps(argv) + "\\n")

command = ""
params = {}
for token in argv:
    if "=" in token:
        key, _, value = token.partition("=")
        params[key] = value
    elif not command:
        command = token

state_path = Path(os.environ["SHIM_STATE"])
state = json.loads(state_path.read_text() or "{}")
calls = state.get("calls", {})
calls[command] = calls.get(command, 0) + 1
state["calls"] = calls
state_path.write_text(json.dumps(state))

entry = script.get(command)
if isinstance(entry, list):
    entry = entry[min(calls[command] - 1, len(entry) - 1)]
if isinstance(entry, dict):
    if entry.get("sleep"):
        time.sleep(float(entry["sleep"]))
    print(entry.get("output", ""))
    sys.exit(0)

vault = Path(os.environ["SHIM_VAULT"])

def notes():
    # Obsidian indexes no dot-directory, which matters here: the run's own
    # backup tree lives in one, and a shim that rewrote backups would quietly
    # destroy the evidence the caller verifies against.
    for note in sorted(vault.rglob("*.md")):
        if not any(part.startswith(".") for part in note.relative_to(vault).parts):
            yield note

if command == "backlinks":
    relative = params["path"]
    target = Path(relative).stem
    rows = []
    for note in notes():
        text = note.read_text()
        # Obsidian counts Markdown links as links too, so the shim must.
        count = (
            text.count("[[%s]]" % target)
            + text.count("[[%s|" % target)
            + len(re.findall(r"\\]\\(([^)]*/)?%s\\)" % re.escape(Path(relative).name), text))
        )
        if count:
            rows.append({"file": str(note.relative_to(vault)), "count": str(count)})
    print(json.dumps(rows) if params.get("format") == "json" else str(len(rows)))
    sys.exit(0)

if command in ("move", "rename"):
    source = vault / params["path"]
    if command == "rename":
        destination = source.parent / (params["name"] + source.suffix)
    else:
        target = vault / params["to"]
        destination = target / source.name if target.is_dir() else target
    if not source.exists():
        print('Error: File "%s" not found.' % params["path"])
        sys.exit(0)
    if os.environ.get("SHIM_REFUSE_MOVE"):
        print("Error: %s" % os.environ["SHIM_REFUSE_MOVE"])
        sys.exit(0)
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.rename(str(source), str(destination))
    old, new = source.stem, destination.stem
    for note in notes():
        text = note.read_text()
        updated = text.replace("[[%s]]" % old, "[[%s]]" % new)
        # Obsidian rewrites a Markdown link target to the shortest path that is
        # unique in the vault, which is the bare filename. It resolves that the
        # way it resolves a wikilink, not the way the filesystem does.
        updated = re.sub(
            r"\\]\\(([^)]*/)?%s\\)" % re.escape(source.name),
            "](%s)" % destination.name,
            updated,
        )
        if updated != text and os.environ.get("SHIM_MANGLE"):
            updated = updated.replace("The prose line.", "The prose line, mangled.")
        if updated != text:
            note.write_text(updated)
    qualify = os.environ.get("SHIM_PATHQUALIFY")
    if qualify:
        # Real Obsidian rewrites a bare [[X]] wikilink to a directory-qualified
        # target when the destination basename is ambiguous in the vault -- e.g. a
        # staged review copy of the same note still sits in _Pending Review/ during
        # a from-review apply. The qualified path goes stale once that copy is
        # cleared. Opt-in like SHIM_MANGLE: only a test that wants this corruption
        # asks for it, so the plain shim keeps resolving links by basename.
        new_stem = destination.stem
        qualified = "[[%s/%s]]" % (qualify.strip("/"), new_stem)
        for note in notes():
            text = note.read_text()
            updated = text.replace("[[%s]]" % new_stem, qualified)
            if updated != text:
                note.write_text(updated)
    label = "Renamed" if command == "rename" else "Moved"
    print("%s: %s -> %s" % (label, params["path"], destination.relative_to(vault)))
    sys.exit(0)

print(entry if isinstance(entry, str) else "")
sys.exit(0)
'''


class ShimEnvironment:
    """Fake obsidian binary, registry, and environment, active until cleanup.

    ``vault_path`` registers an existing vault (what a skill test needs); leave it
    out to have one created under a temp root.
    """

    def __init__(
        self,
        script=None,
        vault_path=None,
        vault_name="TestVault",
        cli=True,
        link_updates="always",
        register=True,
    ):
        self.root = Path(tempfile.mkdtemp())
        if vault_path is None:
            self.vault = self.root / "vaults" / vault_name
            self.vault.mkdir(parents=True, exist_ok=True)
        else:
            self.vault = Path(vault_path).resolve()
        (self.vault / ".obsidian").mkdir(parents=True, exist_ok=True)
        app = {} if link_updates == "unset" else {"alwaysUpdateLinks": link_updates == "always"}
        (self.vault / ".obsidian" / "app.json").write_text(json.dumps(app))

        self.config = self.root / "config"
        self.config.mkdir()
        registry = {"vaults": {"abc123": {"path": str(self.vault)}} if register else {}}
        if cli is not None:
            registry["cli"] = cli
        (self.config / "obsidian.json").write_text(json.dumps(registry))

        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.binary = self.bin / "obsidian"
        self.binary.write_text(SHIM)
        self.binary.chmod(self.binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        self.script_path = self.root / "script.json"
        self.write_script(script or {})
        self.log = self.root / "calls.jsonl"
        self.log.write_text("")
        self.state = self.root / "state.json"
        self.state.write_text("{}")

        self._saved = {}
        self._apply({
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "FORGE_OBSIDIAN_CONFIG_DIR": str(self.config),
            "SHIM_SCRIPT": str(self.script_path),
            "SHIM_LOG": str(self.log),
            "SHIM_STATE": str(self.state),
            "SHIM_VAULT": str(self.vault),
            "FORGE_OBSIDIAN_CLI": None,
            "SHIM_MANGLE": None,
            "SHIM_REFUSE_MOVE": None,
            "SHIM_PATHQUALIFY": None,
        })

    def _apply(self, values):
        for key, value in values.items():
            if key not in self._saved:
                self._saved[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def set_env(self, **values):
        self._apply(values)

    def write_script(self, script):
        merged = {"version": "1.12.7 (installer 1.12.7)"}
        merged.update(script)
        self.script_path.write_text(json.dumps(merged))

    def write(self, relative, text):
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def cleanup(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.root, ignore_errors=True)

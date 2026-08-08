#!/usr/bin/env python3
"""Reject Python imports that nothing in the file uses.

`biome` covers the JavaScript and TypeScript; nothing covered the Python, which
is the larger half of this repository -- roughly 62k lines under `forge/`
against 23k of TS and .mjs. The only Python gate was
`check-python-compat.py`, which asks whether 3.9 can parse a file, not whether
what it says is used. Ten dead imports had accumulated, including two that
looked like a half-wired feature: `vault-compose.py` imported `vault_lexicon`
and `vault_profile` and called neither.

Deliberately one rule rather than a linter. Adding `ruff` would mean a pip
dependency in a repository whose skills are promised to run on a bare `python3`,
and unused imports are the failure that actually happened here. Everything else
a linter would say can wait until it has cost something.

Escape hatch: `# noqa: F401` on the import line, matching the code Flake8 and
ruff use for it, so the marker means the same thing to a person reading it.

One shape genuinely needs that hatch, and this check cannot see it: a module
whose import another module reaches through it, as the eval cases do with
`_common.json`. Nothing in `_common.py` reads the name, so it looks dead from
inside the file and is load-bearing from outside. Mark those; do not delete
them because this said so.
"""

import ast
import sys
from pathlib import Path

SKIPPED_DIRECTORIES = {
    ".claude",
    ".git",
    "__pycache__",
    ".venv",
    "coverage",
    "dist",
    "node_modules",
    "venv",
}
ALLOW = "noqa: F401"


def python_files(root):
    for path in sorted(Path(root).rglob("*.py")):
        if SKIPPED_DIRECTORIES.intersection(path.parts):
            continue
        yield path


def _bound_names(tree):
    """Every name an import statement binds, with the line that bound it."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # `import a.b` binds `a`; `import a.b as c` binds `c`.
                yield (alias.asname or alias.name.split(".")[0], node.lineno, alias.name)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    continue
                yield (alias.asname or alias.name, node.lineno, alias.name)


def _used_names(tree):
    """Every name the file reads, plus the strings in `__all__`.

    `__all__` matters because a re-exported name is used by definition, and it
    is spelled as a string rather than a `Name` node.
    """
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            continue
        elif isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "__all__" in targets:
                for element in ast.walk(node.value):
                    if isinstance(element, ast.Constant) and isinstance(element.value, str):
                        used.add(element.value)
    # An attribute access `a.b.c` reads the Name `a`, which the Name branch above
    # already records, so attributes need no separate handling.
    return used


def failures_for(path):
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # `check-python-compat.py` owns parse failures and reports them better.
        return []
    if any(
        isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names)
        for node in ast.walk(tree)
    ):
        # A star import can supply any name, so nothing here can be called unused.
        return []
    lines = source.splitlines()
    used = _used_names(tree)
    failures = []
    for name, lineno, imported in _bound_names(tree):
        if name in used:
            continue
        if ALLOW in (lines[lineno - 1] if lineno - 1 < len(lines) else ""):
            continue
        label = imported if imported == name else "{} as {}".format(imported, name)
        failures.append((lineno, "unused import: {}".format(label)))
    return sorted(failures)


def main():
    root = Path(__file__).resolve().parent.parent
    failures = []
    for path in python_files(root):
        for lineno, message in failures_for(path):
            failures.append("{}:{}: {}".format(path.relative_to(root), lineno, message))

    if failures:
        print("Remove these, or mark them `# noqa: F401` if the import is for its side effect:", file=sys.stderr)
        for failure in failures:
            print("  {}".format(failure), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

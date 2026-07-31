#!/usr/bin/env python3
"""Reject Python that the oldest supported interpreter cannot parse.

Every skill invokes a bare ``python3``. On a stock macOS that is 3.9, so a file
using newer syntax is not a lint problem but a hard outage: a SyntaxError is
raised while the module is being parsed, which means every skill importing it
dies before its first line runs, with an error naming a library the user never
called.

The check runs under whatever ``python3`` resolves to -- 3.12 in CI, newer on a
developer's machine -- so it must not depend on the interpreter refusing
anything itself. Each file is parsed at the floor's ``feature_version``, which
rejects syntax added after it (a ``match`` statement, say) no matter how new the
interpreter running this is.

Walking the f-strings afterwards catches the one construct ``feature_version``
cannot: PEP 701 (3.12) *relaxed* a rule rather than adding syntax, allowing
backslashes inside an f-string expression, and ``ast`` models added syntax only.
Nothing about such a line looks wrong until it reaches an older Python.
"""

import ast
import sys
from pathlib import Path

MINIMUM_VERSION = "3.9"
# What ``ast.parse`` wants: (3, 9) rather than a second hand-written copy of the
# floor that could drift from the one in the messages.
MINIMUM_FEATURE_VERSION = tuple(int(part) for part in MINIMUM_VERSION.split("."))
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


def python_files(root):
    """Every tracked ``.py`` file, skipping vendored and generated trees.

    ``.claude`` holds worktrees whose files are copies of another branch's
    work; failing this check on those would report problems no edit here can
    fix.
    """
    for path in sorted(Path(root).rglob("*.py")):
        if SKIPPED_DIRECTORIES.intersection(path.parts):
            continue
        yield path


def _backslash_in_fstring(source, tree):
    for node in ast.walk(tree):
        if not isinstance(node, ast.FormattedValue):
            continue
        segment = ast.get_source_segment(source, node.value)
        if segment and "\\" in segment:
            yield node.lineno, "backslash inside f-string expression: {}".format(segment)


def failures_for(path):
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, feature_version=MINIMUM_FEATURE_VERSION)
    except SyntaxError as error:
        return [(error.lineno or 0, "does not parse: {}".format(error.msg))]
    return sorted(_backslash_in_fstring(source, tree))


def main():
    root = Path(__file__).resolve().parent.parent
    failures = []
    for path in python_files(root):
        for lineno, message in failures_for(path):
            failures.append("{}:{}: {}".format(path.relative_to(root), lineno, message))

    if failures:
        print("Python {} cannot parse these files:".format(MINIMUM_VERSION), file=sys.stderr)
        for failure in failures:
            print("  {}".format(failure), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

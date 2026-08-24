#!/usr/bin/env python3

"""Import PyMuPDF under the name it actually wants to be called.

PyMuPDF renamed its module from ``fitz`` to ``pymupdf`` in 1.24.3. The old name
still resolves, but the shim it leaves behind ends with

    message_warning('The `fitz` API is deprecated and will be removed in
    future. Use `import pymupdf` instead.')

and that notice goes to **stdout**, not stderr. Every skill script here
contracts to put a JSON document and nothing else on stdout in ``--json`` mode
(see ``SCRIPT_TOOL_CONTRACT.md``), so a bare ``import fitz`` anywhere in the
call path prepends a line of prose to the payload and every caller doing
``JSON.parse(stdout)`` dies on the first byte.

Preferring the new name is also what survives the shim's removal. Asking
``find_spec("fitz")`` whether PyMuPDF is installed is a question that will one
day answer "no" on a machine that has it, silently disabling PDF->Markdown with
a remediation line telling the user to install what they already have.

PyMuPDF stays an optional dependency, so every entry point here reports absence
rather than raising: callers have a degraded path for it.
"""

import importlib
import importlib.util

# New name first. A version old enough to lack it also predates the deprecation
# notice, so the fallback is quiet.
MODULE_NAMES = ("pymupdf", "fitz")


def _installed_name():
    for name in MODULE_NAMES:
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            continue
        if spec is not None:
            return name
    return None


def available():
    """Whether PyMuPDF can be imported, without importing it."""
    return _installed_name() is not None


def load():
    """The PyMuPDF module, or ``None`` when it is not installed."""
    name = _installed_name()
    if name is None:
        return None
    return importlib.import_module(name)


def version():
    """PyMuPDF's version string, or ``None`` when it is not installed.

    Falls back to ``VersionBind`` and then to a bare ``"available"``: some
    builds carry one of those and not ``__version__``, and a doctor report that
    says "installed, version unknown" is more use than one that says nothing.
    """
    module = load()
    if module is None:
        return None
    return getattr(module, "__version__", None) or getattr(module, "VersionBind", None) or "available"

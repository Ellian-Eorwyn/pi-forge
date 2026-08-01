#!/usr/bin/env python3
"""Optional adapter for the Obsidian CLI (``obsidian``, Obsidian 1.12.7+).

The vault skills are filesystem-only and work on any folder of Markdown, with or
without Obsidian installed. This module never changes that. It is an accelerator
and a verifier: when Obsidian happens to be running, it answers questions about
the vault that pi-forge would otherwise have to approximate, and it performs the
one operation pi-forge cannot do cheaply on its own — renaming a note while
rewriting every inbound link.

The framing that matters: Obsidian is the reference implementation of wikilink
resolution, frontmatter parsing, and property typing. Treat it as an *oracle*.
Where its answer disagrees with ours, that is a bug in ours worth fixing in the
pure-Python path, which then benefits vaults where Obsidian is never installed.

Design rules:

- Standard library only, Python 3.9 syntax, no third-party dependencies.
- ``probe`` and ``run`` never raise during normal use. Callers check ``available``
  or ``ok`` and fall back to their existing behavior, which stays the default.
- Vault *discovery* never uses this module. Skills already receive ``--vault``.
- Reads are free; writes are rationed. ``WRITE_COMMANDS`` is two commands and is
  not intended to grow. Everything outside ``READ_COMMANDS | WRITE_COMMANDS`` is
  refused, so a subcommand added by a future Obsidian release fails closed.

Things this module deliberately does not do, each learned the hard way:

- It never spawns ``obsidian`` to learn a vault's name. ``vault=`` accepts only a
  registered vault *name*, and naming an unopened vault opens its window and
  switches the user's active vault. The name is ``basename(path)`` and the
  registry that proves it is a JSON file on disk, so this is a read, not a call.
- It never trusts the exit code. The CLI exits 0 whether it succeeded or failed;
  only the ``Error: `` prefix on its output distinguishes the two.
- It never writes ``.obsidian/app.json``. Renaming without ``alwaysUpdateLinks``
  blocks on a GUI modal, so ``probe`` reports it and callers refuse; flipping a
  user's app settings to work around that is not ours to do.
- It never calls ``eval``, ``property:set``, ``create``, ``append`` or ``delete``.
  pi-forge's own atomic, hash-verified, journalled writes are strictly better,
  and ``property:set`` appends new keys to the end of frontmatter, which breaks
  the schema's property order.

Configuration:

- ``FORGE_OBSIDIAN_CLI`` — ``off`` disables the adapter globally (every probe
  reports unavailable); any other value is used as the binary path.
- ``FORGE_OBSIDIAN_CONFIG_DIR`` — overrides the directory holding
  ``obsidian.json``. Tests and CI set this; users should not need to.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote

MINIMUM_VERSION = (1, 12, 7)
DEFAULT_TIMEOUT = 15.0
# A vault that is registered but closed has to boot before it answers. That path
# is avoided (see the module docstring), but a cold app can still be slow.
BOOT_TIMEOUT = 45.0
RETRY_DELAY = 0.75

ERROR_PREFIX = "Error: "
# Emitted while the app's command registry is still partial, which means the
# command did not run. The only error worth retrying.
UNKNOWN_COMMAND_RE = re.compile(r'^Error: Command "[^"]+" not found\.')
# Not "Error: "-prefixed, so it needs naming explicitly.
FAILURE_LINES = frozenset({"vault not found."})

VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
# A line the CLI may rewrite while updating links: wikilinks, embeds, and
# Markdown links. Anything else changing on a line is prose, and prose is not
# something a move is allowed to touch.
LINK_LINE_RE = re.compile(r"\[\[|\]\(")

MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]*)\)")
EXTERNAL_SCHEMES = ("http://", "https://", "mailto:", "tel:", "data:", "obsidian://", "ftp://")

# Query commands: they change neither the vault nor the app. Commands that merely
# open a file or a tab are excluded too, since nothing here needs them and
# ``daily`` creates today's note as a side effect of opening it.
READ_COMMANDS = frozenset(
    {
        "aliases", "backlinks", "base:query", "base:views", "bases", "bookmarks",
        "commands", "daily:path", "daily:read", "deadends", "diff", "file", "files",
        "folder", "folders", "help", "history", "history:list", "history:read",
        "hotkey", "hotkeys", "links", "orphans", "outline", "plugin", "plugins",
        "plugins:enabled", "properties", "property:read", "random:read", "read",
        "recents", "search", "search:context", "snippets", "snippets:enabled",
        "sync:deleted", "sync:history", "sync:read", "sync:status", "tabs", "tag",
        "tags", "tasks", "template:read", "templates", "themes", "unresolved",
        "vault", "vaults", "version", "wordcount", "workspace",
    }
)

# The entire mutating surface this adapter is willing to use. Renaming and moving
# rewrite inbound links vault-wide, which is the whole reason the adapter exists.
WRITE_COMMANDS = frozenset({"move", "rename"})

# Refused with a specific message rather than the generic "unknown command", so a
# caller reaching for one of these learns why the answer is no.
DENIED_COMMANDS = frozenset(
    {
        "append", "base:create", "bookmark", "command", "create", "daily",
        "daily:append", "daily:prepend", "delete", "devtools", "eval",
        "history:open", "history:restore", "open", "plugin:disable",
        "plugin:enable", "plugin:install", "plugin:reload", "plugin:uninstall",
        "plugins:restrict", "prepend", "property:remove", "property:set",
        "random", "reload", "restart", "search:open", "snippet:disable",
        "snippet:enable", "sync", "sync:open", "sync:restore", "tab:open",
        "task", "template:insert", "theme", "theme:install", "theme:set",
        "theme:uninstall", "workspace:delete",
    }
)

DENIED_PREFIXES = ("dev:",)


def _disabled():
    return (os.environ.get("FORGE_OBSIDIAN_CLI") or "").strip().lower() == "off"


def config_directory():
    """Directory holding ``obsidian.json``, per Obsidian's platform conventions."""
    override = os.environ.get("FORGE_OBSIDIAN_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "obsidian"
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "obsidian"
        return Path.home() / "AppData" / "Roaming" / "obsidian"
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "obsidian"


def registry_path():
    return config_directory() / "obsidian.json"


def _read_registry():
    try:
        return json.loads(registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def registered_vaults():
    """Map resolved vault path to the name the CLI knows it by.

    Obsidian's registry stores only paths; the CLI's vault name is the path's
    basename. Returns an empty dict when the registry is missing or unreadable.
    """
    registry = _read_registry()
    if not isinstance(registry, dict):
        return {}
    vaults = registry.get("vaults")
    if not isinstance(vaults, dict):
        return {}
    found = {}
    for entry in vaults.values():
        if not isinstance(entry, dict):
            continue
        raw = entry.get("path")
        if not isinstance(raw, str) or not raw:
            continue
        found[str(Path(raw).expanduser().resolve())] = Path(raw).name
    return found


def cli_setting():
    """The registry's ``cli`` flag (Settings -> General), or None if absent."""
    registry = _read_registry()
    if not isinstance(registry, dict):
        return None
    value = registry.get("cli")
    return value if isinstance(value, bool) else None


def vault_name_for(vault_path):
    """The registered name for ``vault_path``, or None.

    None when the vault is not registered, and also when another registered vault
    shares its basename: ``vault=`` would then be ambiguous, and guessing which
    one Obsidian picks is not a guess worth making about someone's notes.
    """
    vaults = registered_vaults()
    if not vaults:
        return None
    resolved = str(Path(vault_path).expanduser().resolve())
    name = vaults.get(resolved)
    if name is None:
        return None
    if sum(1 for candidate in vaults.values() if candidate == name) > 1:
        return None
    return name


def link_update_setting(vault_path):
    """Whether Obsidian will update links on rename: always | off | unset.

    ``unset`` is the shipped default and it is not benign: the first rename in
    such a vault opens a modal asking the user what to do, and the CLI call hangs
    until someone answers it.
    """
    path = Path(vault_path).expanduser() / ".obsidian" / "app.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unset"
    if not isinstance(config, dict) or "alwaysUpdateLinks" not in config:
        return "unset"
    return "always" if config.get("alwaysUpdateLinks") is True else "off"


def binary_path(explicit=None):
    if _disabled():
        return None
    candidate = explicit or os.environ.get("FORGE_OBSIDIAN_CLI")
    if candidate:
        resolved = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
        return resolved
    return shutil.which("obsidian")


def _parse_version(text):
    match = VERSION_RE.search(text or "")
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _unavailable(reason, **extra):
    session = {
        "available": False,
        "canWrite": False,
        "reason": reason,
        "binary": None,
        "vaultName": None,
        "vault": None,
        "registered": False,
        "cliSetting": None,
        "appVersion": None,
        "linkUpdates": "unset",
        "disabled": False,
    }
    session.update(extra)
    return session


def probe(vault_path, binary=None, timeout=DEFAULT_TIMEOUT):
    """Decide whether this vault can be served by the CLI. Never raises.

    Always returns a session dict. ``available`` gates reads; ``canWrite`` gates
    the two mutating commands and additionally requires that Obsidian is
    configured to update links itself.
    """
    vault = str(Path(vault_path).expanduser().resolve())
    if _disabled():
        return _unavailable("disabled by FORGE_OBSIDIAN_CLI=off", vault=vault, disabled=True)

    resolved_binary = binary_path(binary)
    if not resolved_binary:
        return _unavailable("obsidian binary not found on PATH", vault=vault)

    vaults = registered_vaults()
    if not vaults:
        return _unavailable(
            "Obsidian's vault registry was not readable at {0}".format(registry_path()),
            vault=vault,
            binary=resolved_binary,
        )

    registered = vault in vaults
    name = vault_name_for(vault)
    setting = cli_setting()
    links = link_update_setting(vault)
    partial = {
        "vault": vault,
        "binary": resolved_binary,
        "registered": registered,
        "cliSetting": setting,
        "linkUpdates": links,
    }
    if not registered:
        return _unavailable("vault is not registered with Obsidian", **partial)
    if name is None:
        return _unavailable("another registered vault shares this folder name", **partial)
    if setting is False:
        return _unavailable("Obsidian's command line interface is turned off", **partial)

    session = dict(partial)
    session.update({"available": True, "canWrite": False, "reason": None, "vaultName": name, "disabled": False})
    version_result = run(session, "version", timeout=timeout)
    if not version_result["ok"]:
        session["available"] = False
        session["reason"] = version_result["reason"]
        return session
    version = _parse_version(version_result["output"])
    session["appVersion"] = version_result["output"].strip() or None
    if version is None or version < MINIMUM_VERSION:
        session["available"] = False
        session["reason"] = "Obsidian {0} predates the {1} CLI".format(
            session["appVersion"] or "version unknown",
            ".".join(str(part) for part in MINIMUM_VERSION),
        )
        return session
    session["canWrite"] = links == "always"
    return session


def _refusal(command):
    if command in DENIED_COMMANDS or command.startswith(DENIED_PREFIXES):
        return "{0} is not a command this adapter will run".format(command)
    if command not in READ_COMMANDS and command not in WRITE_COMMANDS:
        return "{0} is not a known read-only Obsidian command".format(command)
    return None


def _result(ok, output, reason=None, seconds=0.0, retried=False, indeterminate=False):
    return {
        "ok": ok,
        "output": output,
        "reason": reason,
        "seconds": seconds,
        "retried": retried,
        "indeterminate": indeterminate,
    }


def _failure_reason(output):
    first = (output or "").strip().splitlines()
    if not first:
        return None
    line = first[0].strip()
    if line.startswith(ERROR_PREFIX):
        return line[len(ERROR_PREFIX) :].strip()
    if line.lower() in FAILURE_LINES:
        return line
    return None


def _invoke(argv, timeout):
    started = time.time()
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return None, time.time() - started
    except OSError as error:
        return _result(False, "", "{0}: {1}".format(type(error).__name__, error)), time.time() - started
    output = (completed.stdout or "") + (completed.stderr or "")
    reason = _failure_reason(output)
    return _result(reason is None, output.strip(), reason), time.time() - started


def run(session, command, allow_write=False, timeout=None, **params):
    """Invoke one Obsidian subcommand. Never raises.

    Returns ``{"ok", "output", "reason", "seconds", "retried", "indeterminate"}``.
    The process exit code is ignored: the CLI exits 0 on failure, so ``ok`` comes
    from the output's first line instead.

    ``indeterminate`` is set when a write timed out. That is not the same as
    failure — the rename may well have happened — and the caller must reconcile
    against the filesystem rather than assume either outcome.
    """
    if not session.get("binary"):
        return _result(False, "", session.get("reason") or "Obsidian CLI unavailable")
    refusal = _refusal(command)
    if refusal:
        return _result(False, "", refusal)
    writes = command in WRITE_COMMANDS
    if writes and not allow_write:
        return _result(False, "", "{0} needs allow_write=True".format(command))
    if writes and not session.get("canWrite"):
        return _result(False, "", session.get("reason") or "writes are not enabled for this vault")

    # vault= must precede everything else. A trailing underscore is stripped so
    # Python keywords can still be passed (from_=1 -> from=1).
    argv = [session["binary"], "vault={0}".format(session["vaultName"]), command]
    for key, value in params.items():
        name = key[:-1] if key.endswith("_") else key
        if value is True:
            argv.append(name)
        elif value is not None and value is not False:
            argv.append("{0}={1}".format(name, value))

    limit = timeout or DEFAULT_TIMEOUT
    result, seconds = _invoke(argv, limit)
    if result is None:
        return _result(False, "", "timed out after {0:.0f}s".format(limit), seconds, indeterminate=writes)
    result["seconds"] = seconds
    if result["ok"] or not UNKNOWN_COMMAND_RE.match(result["output"]):
        return result

    # A partial command registry means the app was still booting and the command
    # provably did not execute, so retrying is safe even for a write. Exactly one
    # retry: anything more is a loop against a broken install.
    time.sleep(RETRY_DELAY)
    retry, retry_seconds = _invoke(argv, max(limit, BOOT_TIMEOUT))
    if retry is None:
        return _result(False, "", "timed out after retry", seconds + retry_seconds, True, indeterminate=writes)
    retry["seconds"] = seconds + retry_seconds
    retry["retried"] = True
    return retry


def run_json(session, command, allow_write=False, timeout=None, **params):
    """``run`` with the output parsed as JSON. Malformed JSON is a failure."""
    result = run(session, command, allow_write=allow_write, timeout=timeout, **params)
    if not result["ok"]:
        return result
    try:
        result["data"] = json.loads(result["output"] or "null")
    except ValueError:
        return _result(False, result["output"], "response was not JSON", result["seconds"], result["retried"])
    return result


def link_only_diff(before, after):
    """Whether ``after`` differs from ``before`` only on lines carrying links.

    Returns ``(ok, changed_line_numbers)`` with 1-based line numbers. A change in
    line count fails: the CLI rewrites link text in place, so anything structural
    is a surprise, and a surprise in someone's notes is a reason to restore the
    backup rather than to reason about it.
    """
    before_lines = (before or "").splitlines()
    after_lines = (after or "").splitlines()
    if len(before_lines) != len(after_lines):
        return False, []
    changed = []
    ok = True
    for index, (left, right) in enumerate(zip(before_lines, after_lines), start=1):
        if left == right:
            continue
        changed.append(index)
        if not (LINK_LINE_RE.search(left) or LINK_LINE_RE.search(right)):
            ok = False
    return ok, changed


def unresolved_markdown_links(vault, relative, text):
    """Markdown-link targets in ``text`` that no file answers to on disk.

    Obsidian resolves a Markdown link the same way it resolves a wikilink, by
    shortest unique path rather than by walking up from the linking note. So
    after a move it happily rewrites ``[T](Notes/T.md)`` to ``[T](T.md)`` from a
    note in a different folder: correct inside the app, broken in every other
    Markdown renderer, and broken for pi-forge, which resolves paths the way the
    filesystem does.

    This is the seam where Obsidian's convenience and a general-purpose vault
    part ways, so it is measured rather than assumed. Anchors, external schemes,
    and empty targets are ignored.
    """
    base = (Path(vault) / relative).parent
    broken = set()
    for raw in MARKDOWN_LINK_RE.findall(text or ""):
        target = raw.strip().strip("<>").split(" ", 1)[0]
        if not target or target.startswith("#") or target.lower().startswith(EXTERNAL_SCHEMES):
            continue
        target = target.split("#", 1)[0].split("?", 1)[0]
        if not target:
            continue
        try:
            decoded = unquote(target)
        except (ValueError, UnicodeDecodeError):
            decoded = target
        candidate = base / decoded
        if candidate.exists() or (candidate.suffix == "" and candidate.with_suffix(".md").exists()):
            continue
        broken.add(target)
    return broken


def compare_frontmatter(session, vault, relatives, parse):
    """Check pi-forge's frontmatter parser against Obsidian's, note by note.

    ``parse`` takes an absolute path and returns the properties pi-forge read from
    it, or None if it could not read the file. Only two things are compared: which
    keys each side found, and whether they agree on list-versus-scalar shape.
    Values are left alone because Obsidian normalizes types on the way out and
    those differences are noise, whereas a key one side never saw is a real hole
    in the hand-rolled YAML subset — the kind worth fixing in ``vault_schema`` so
    that vaults without Obsidian get the fix too.

    Returns ``{"checked", "disagreements": [{path, missing, extra, shape}]}``.
    """
    disagreements = []
    checked = 0
    for relative in relatives:
        result = run_json(session, "properties", path=relative, format="json")
        if not result["ok"] or not isinstance(result.get("data"), dict):
            continue
        theirs = result["data"]
        ours = parse(Path(vault) / relative)
        if ours is None:
            continue
        checked += 1
        missing = sorted(set(theirs) - set(ours))
        extra = sorted(set(ours) - set(theirs))
        shape = sorted(
            key
            for key in set(theirs) & set(ours)
            if isinstance(theirs[key], list) != isinstance(ours[key], list)
        )
        if missing or extra or shape:
            disagreements.append({"path": relative, "missing": missing, "extra": extra, "shape": shape})
    return {"checked": checked, "disagreements": disagreements}


def doctor(vault_path, binary=None, timeout=DEFAULT_TIMEOUT, session=None):
    """Probe plus one cheap live call, shaped for a skill's ``doctor`` output.

    Never reports a problem with the vault. Obsidian being closed, absent, or
    unregistered is a missing accelerator, not a broken vault, so ``ok`` in the
    calling skill must not depend on any of this. ``warnings`` carries only
    actionable near-misses: cases where the CLI is nearly usable and one setting
    stands in the way.
    """
    if session is None:
        session = probe(vault_path, binary=binary, timeout=timeout)
    report = {
        "available": session["available"],
        "canWrite": session["canWrite"],
        "reason": session["reason"],
        "binary": session["binary"],
        "vaultName": session["vaultName"],
        "registered": session["registered"],
        "appVersion": session["appVersion"],
        "linkUpdates": session["linkUpdates"],
        "warnings": [],
    }
    if session["available"]:
        probe_call = run(session, "files", total=True)
        report["reachable"] = probe_call["ok"]
        if probe_call["ok"]:
            report["detail"] = "Obsidian {0} responding for vault {1}".format(
                session["appVersion"], session["vaultName"]
            )
        else:
            report["detail"] = probe_call["reason"]
        if session["linkUpdates"] != "always":
            report["warnings"].append(
                "Obsidian CLI is available but link-safe moves are off: turn on Settings -> Files and links -> "
                "Automatically update internal links. Until then moves fall back to a plain rename and inbound "
                "links are left to basename resolution."
            )
        return report

    report["reachable"] = False
    report["detail"] = session["reason"]
    # Only worth a warning when the binary is right there and one setting is in
    # the way. No Obsidian at all is the normal case and says nothing.
    if session["binary"] and not session.get("disabled"):
        if not session["registered"]:
            report["warnings"].append(
                "Obsidian CLI is installed but this vault is not registered with Obsidian, so link-safe moves "
                "and the property-vocabulary check are unavailable."
            )
        elif session["vaultName"] is None:
            report["warnings"].append(
                "Obsidian CLI is installed but another registered vault shares this folder name, so the CLI "
                "cannot be targeted unambiguously."
            )
        elif session["cliSetting"] is False:
            report["warnings"].append(
                "Obsidian CLI is installed but turned off: enable Settings -> General -> Command line interface."
            )
    return report

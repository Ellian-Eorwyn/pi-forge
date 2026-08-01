#!/usr/bin/env python3
"""Moving a note, with or without taking its inbound links along.

Two skills relocate notes. ``vault-organizer`` files them into schema-derived
folders; ``vault-transcripts`` renames them from the timestamp they arrived with
to the title they earned. The second is the one that breaks links: Obsidian
resolves ``[[Note]]`` by basename regardless of folder, so a folder-only move is
invisible to wikilinks, while a changed filename is not.

pi-forge cannot rewrite inbound links cheaply on its own — it would have to parse
and resolve every link in the vault the way Obsidian does, and then be right
about it. Obsidian already does exactly that, so when it is running the move goes
through its CLI, and when it is not the move is a plain rename and behaves the
way this workflow always has.

The CLI's blast radius is the whole vault, so it is paid for up front:

- every note linking to the source is backed up before the call;
- the moved note's bytes are checked against the hash the plan was built on;
- every note the CLI rewrote must differ only on lines carrying links, or the
  whole operation is restored from backup and fails;
- one failure disables the CLI for the rest of the run, because a half-working
  CLI must not be re-tried once per note.

One thing it does that a general-purpose vault should not simply accept:
Obsidian rewrites a Markdown link target to the shortest path unique in the
vault and resolves that the way it resolves a wikilink, not the way the
filesystem does. The result is correct in Obsidian and broken in every other
Markdown renderer, so it is measured and reported rather than swallowed.
"""

import os
import shutil
import time

from obsidian_cli import (
    link_only_diff,
    probe,
    run as run_obsidian,
    run_json as run_obsidian_json,
    unresolved_markdown_links,
)
from vault_schema import UserError, sha256_bytes

# How long to let Obsidian's index catch up with a move before giving up on the
# post-move backlink count. Measured at ~75ms on a 2,500-note vault.
INDEX_SETTLE_ATTEMPTS = 5
INDEX_SETTLE_DELAY = 0.05


def backup_once(run_dir, relative, source):
    """Copy a note into the run's backup tree, keeping the first copy made.

    A move now touches more than the note being moved, so the same file can be
    backed up twice in one run. First write wins: the earlier copy is the more
    original one.
    """
    backup = run_dir / "backup" / relative
    if backup.exists():
        return backup
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, backup)
    return backup


class PlainMover:
    """`os.rename`, leaving inbound links to Obsidian's basename resolution."""

    mode = "rename"

    def __init__(self):
        self.warnings = []
        self.disabled = False

    def move(self, vault, run_dir, relative, destination_relative, expected):
        os.rename(vault / relative, vault / destination_relative)
        expected[destination_relative] = expected.pop(relative, None)
        return {"linkRewrite": "rename"}


class ObsidianMover:
    """Move through the Obsidian CLI so inbound links follow the note."""

    mode = "obsidian-cli"

    def __init__(self, session):
        self.session = session
        self.disabled = False
        self.warnings = []

    def move(self, vault, run_dir, relative, destination_relative, expected):
        source = vault / relative
        destination = vault / destination_relative
        inbound = self._inbound(relative)
        before = {}
        for linker in inbound:
            path = vault / linker
            if not path.is_file():
                continue
            backup_once(run_dir, linker, path)
            before[linker] = path.read_text(encoding="utf-8", errors="replace")

        result = run_obsidian(
            self.session, "move", allow_write=True, path=relative, to=destination_relative
        )
        # A timed-out write is indeterminate, not failed: the move may well have
        # happened, so the filesystem decides rather than the return value.
        landed = destination.is_file() and not source.exists()
        if not result["ok"] and not (result["indeterminate"] and landed):
            self._abort(vault, run_dir, list(before) + [relative])
            raise UserError("obsidian move failed: {0}".format(result["reason"]))
        if not landed:
            self._abort(vault, run_dir, list(before) + [relative])
            raise UserError("obsidian reported a move that the filesystem does not show")
        if sha256_bytes(destination.read_bytes()) != expected.get(relative):
            self._abort(vault, run_dir, list(before) + [relative])
            raise UserError("the moved note's contents changed during the move")

        touched = []
        for linker, original in sorted(before.items()):
            path = vault / linker
            current = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
            if current == original:
                continue
            ok, changed = link_only_diff(original, current)
            if not ok:
                self._abort(vault, run_dir, list(before) + [destination_relative])
                raise UserError(
                    "obsidian changed more than links in {0} (lines {1}); restored from backup".format(
                        linker, changed
                    )
                )
            touched.append(linker)
            broke = unresolved_markdown_links(vault, linker, current) - unresolved_markdown_links(
                vault, linker, original
            )
            if broke:
                self.warnings.append(
                    "{0}: Obsidian rewrote Markdown link target(s) {1} into a form only Obsidian "
                    "resolves".format(linker, ", ".join(sorted(broke)))
                )
            if linker in expected:
                expected[linker] = sha256_bytes(path.read_bytes())

        expected[destination_relative] = expected.pop(relative, None)
        after = self._inbound_count(destination_relative)
        if after is not None and after != len(inbound):
            # Not a failure. A link can be unresolved before a move and resolvable
            # after it, or the reverse. Worth saying; not worth losing the run over.
            self.warnings.append(
                "{0}: inbound links went from {1} to {2} across the move".format(
                    destination_relative, len(inbound), after
                )
            )
        return {
            "linkRewrite": "obsidian-cli",
            "inboundBefore": len(inbound),
            "inboundAfter": after,
            "linksRewrittenIn": touched,
        }

    def _inbound(self, relative):
        result = run_obsidian_json(self.session, "backlinks", path=relative, format="json", counts=True)
        rows = result.get("data") if result["ok"] else None
        if not isinstance(rows, list):
            return []
        return [row["file"] for row in rows if isinstance(row, dict) and isinstance(row.get("file"), str)]

    def _inbound_count(self, relative):
        # Obsidian's index registers the new path a beat after the move returns —
        # measured at ~75ms — so the first query fails. Without this retry the
        # check silently answers None every time, which reads as reassurance and
        # is not. Bounded, because it is advisory either way.
        for attempt in range(INDEX_SETTLE_ATTEMPTS):
            if attempt:
                time.sleep(INDEX_SETTLE_DELAY)
            result = run_obsidian_json(self.session, "backlinks", path=relative, format="json")
            rows = result.get("data") if result["ok"] else None
            if isinstance(rows, list):
                return len(rows)
        return None

    def _abort(self, vault, run_dir, relatives):
        self.disabled = True
        for relative in dict.fromkeys(relatives):
            backup = run_dir / "backup" / relative
            if not backup.is_file():
                continue
            target = vault / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, target)


def resolve_mover(link_rewrite, vault):
    """Pick the move strategy for a run, and say why when it is not the CLI.

    ``auto`` uses the CLI when it can and falls back silently when it cannot, so
    a vault with no Obsidian behaves exactly as it always has. ``require`` turns
    that fallback into an error for anyone who wants the guarantee, and ``off``
    refuses the CLI even when it is right there.

    Returns ``(mover, reason)``; ``reason`` is None when the CLI is in use.
    """
    if link_rewrite == "off":
        return PlainMover(), "disabled with --link-rewrite off"
    session = probe(vault)
    if session["canWrite"]:
        return ObsidianMover(session), None
    reason = session["reason"] or (
        "Obsidian is available but 'Automatically update internal links' is off, so a rename would "
        "block on a dialog"
    )
    if link_rewrite == "require":
        raise UserError("--link-rewrite require, but link-safe moves are unavailable: {0}".format(reason))
    return PlainMover(), reason

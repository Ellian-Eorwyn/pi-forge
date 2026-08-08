#!/usr/bin/env python3
"""A copy of every fixture's source, outside the vault and outside the repository.

The suite pins fixtures by sha256 and materializes them from the vault. That
works until the vault moves, and the vault is a working notebook — notes get
filed, renamed, and reclassified. Four fixtures are already unreachable from
their pinned paths because `vault-organizer` filed them somewhere else, which
takes four cases with them.

This is the copy that does not move. It holds the *source* bytes rather than
the excerpted ones, so a fixture can still be re-excerpted or re-pinned from it,
and it records where each file came from so a run can say whether it read the
vault or the archive.

Three properties, each of which the code checks rather than assumes:

- **Outside the repository.** Not gitignored — outside. A gitignore entry is one
  `git add -f` away from publishing someone's notes, and these are real notes.
- **Outside the vault.** A backup inside the thing it is backing up is not one.
- **Verifiable.** Every archived file is checked against the hash in
  `fixtures.json`, so a silently corrupted archive cannot quietly replace the
  vault as the source of truth.
"""

import json
import os
import sys
from pathlib import Path

EVALS_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(EVALS_ROOT))

import harness  # noqa: E402

# Sits beside the agent directory the rest of pi-forge already uses, so there is
# one place to look for machine-local state rather than two.
DEFAULT_ARCHIVE = Path.home() / ".pi-forge" / "eval-sources"


def archive_root(explicit=None):
    root = Path(explicit or os.environ.get("FORGE_EVAL_ARCHIVE") or DEFAULT_ARCHIVE).expanduser()
    # The two placements that would defeat the point, refused rather than
    # documented: inside the repository it can be committed, and inside the
    # vault it moves with the thing it exists to survive.
    for forbidden, why in (
        (harness.FORGE_ROOT.parent, "inside the repository, where it could be committed"),
        (harness.DEFAULT_VAULT, "inside the vault, which is what it exists to be independent of"),
    ):
        try:
            root.resolve().relative_to(forbidden.resolve())
        except (ValueError, OSError):
            continue
        raise harness.EvalError(f"the archive cannot live at {root}: that is {why}")
    return root


def sources_dir(root):
    return root / "sources"


def manifest_path(root):
    return root / "manifest.json"


def read_manifest(root):
    path = manifest_path(root)
    return harness.load_json(path) if path.exists() else {"fixtures": {}}


def _from_vault(spec, vault):
    """The source as the vault has it now, or None with a reason."""
    relative = spec["path"]
    denied = next((prefix for prefix in harness.DENIED_PREFIXES if relative.startswith(prefix)), None)
    if denied:
        return None, f"now under the denied prefix {denied!r}"
    source = vault / relative
    if not source.exists():
        return None, "no longer at its pinned path"
    raw = source.read_text(encoding="utf-8")
    if spec.get("sha256") and harness.sha256_text(raw) != spec["sha256"]:
        return None, "content has drifted from the pinned hash"
    return raw, None


def _from_frozen(fixture_id, spec):
    """The frozen copy, but only when it is provably the whole source.

    Usable only for a fixture with no excerpt rule, where the frozen bytes are
    the source bytes, and only when they still hash to the pin. An excerpted
    fixture's frozen copy is a subset, and archiving a subset as if it were the
    source would quietly make re-pinning impossible later.
    """
    if (spec.get("excerpt") or {}).get("mode", "full") != "full":
        return None, "the frozen copy is an excerpt, not the whole source"
    path = harness.FROZEN / f"{fixture_id}.md"
    if not path.exists():
        return None, "not frozen either"
    raw = path.read_text(encoding="utf-8")
    if spec.get("sha256") and harness.sha256_text(raw) != spec["sha256"]:
        return None, "the frozen copy does not match the pinned hash"
    return raw, None


def capture(vault=None, root=None, allow_frozen=True):
    """Copy every fixture's source into the archive.

    Returns ``(fixture_id, status, detail)`` rows in the shape `freeze` uses, so
    the two read the same way. Statuses: `archived` (fresh from the vault),
    `recovered` (from the frozen copy, because the vault no longer has it),
    `current` (already archived and matching), `unresolvable`.
    """
    root = archive_root(root)
    vault_root = harness.vault_root(vault)
    sources = sources_dir(root)
    sources.mkdir(parents=True, exist_ok=True)
    manifest = read_manifest(root)
    report = []

    for fixture_id, spec in sorted(harness.fixtures().items()):
        target = sources / f"{fixture_id}.md"
        raw, vault_problem = _from_vault(spec, vault_root)
        origin = "vault"
        if raw is None and allow_frozen:
            raw, frozen_problem = _from_frozen(fixture_id, spec)
            origin = "frozen"
            if raw is None:
                # Already archived and still good is a fine outcome even when
                # neither live source can be read: that is the archive doing
                # its job.
                if target.exists() and harness.sha256_text(target.read_text(encoding="utf-8")) == spec.get("sha256"):
                    report.append((fixture_id, "current", f"vault copy {vault_problem}; the archived copy stands in"))
                    continue
                report.append((fixture_id, "unresolvable", f"{vault_problem}, and {frozen_problem}"))
                continue
        elif raw is None:
            report.append((fixture_id, "unresolvable", vault_problem))
            continue

        unchanged = target.exists() and target.read_text(encoding="utf-8") == raw
        if not unchanged:
            target.write_text(raw, encoding="utf-8")
        manifest["fixtures"][fixture_id] = {
            "vaultPath": spec["path"],
            "sha256": harness.sha256_text(raw),
            "bytes": len(raw.encode("utf-8")),
            "capturedFrom": origin,
            "capturedAt": harness.run_state.utc_now(),
            **({"note": f"the vault copy {vault_problem}"} if origin == "frozen" else {}),
        }
        if origin == "frozen":
            report.append((fixture_id, "recovered", f"{vault_problem}; taken from the frozen copy"))
        else:
            report.append((fixture_id, "current" if unchanged else "archived", spec["path"]))

    manifest["readme"] = [
        "Source copies of every eval fixture, outside the vault and outside the repository.",
        "Written by `run.py archive`. The suite reads the vault first and falls back to here,",
        "so a note that moves stops being a broken fixture and becomes a line in the run output.",
        "These are real notes. Do not move this into the repository, and do not sync it.",
    ]
    manifest_path(root).write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _write_readme(root)
    return report


def _write_readme(root):
    (root / "README.md").write_text(
        "# pi-forge eval fixture sources\n\n"
        "Source copies of the notes `forge/evals` measures against, kept here so the suite\n"
        "survives the vault being reorganised. Written by `forge/evals/run.py archive`.\n\n"
        "**These are real vault notes.** They are deliberately outside the pi-forge repository\n"
        "rather than gitignored inside it, because a gitignore is one `git add -f` away from\n"
        "publishing them. Do not move this directory into a synced location.\n\n"
        "`manifest.json` records, per fixture, the vault path it came from, its sha256, and\n"
        "whether it was read from the vault or recovered from a frozen copy.\n\n"
        "To check it: `python3 forge/evals/run.py archive --check`\n",
        encoding="utf-8",
    )


def verify(root=None):
    """Check every archived file against the hash in `fixtures.json`."""
    root = archive_root(root)
    sources = sources_dir(root)
    fixtures = harness.fixtures()
    report = []
    for fixture_id, spec in sorted(fixtures.items()):
        path = sources / f"{fixture_id}.md"
        if not path.exists():
            report.append((fixture_id, "absent", "not in the archive"))
            continue
        digest = harness.sha256_text(path.read_text(encoding="utf-8"))
        if spec.get("sha256") and digest != spec["sha256"]:
            report.append((fixture_id, "corrupt", f"archived {digest[:12]}, pinned {spec['sha256'][:12]}"))
        else:
            report.append((fixture_id, "ok", spec["path"]))
    for path in sorted(sources.glob("*.md")) if sources.is_dir() else ():
        if path.stem not in fixtures:
            report.append((path.stem, "orphan", "archived but no longer a fixture"))
    return report


def resolve(fixture_id, spec, vault, root=None):
    """The source for one fixture: the vault if it still has it, else the archive.

    Returns ``(text, origin)``. The vault wins when it matches, because the
    vault is where a deliberate edit happens and the archive should not mask
    one. The archive is used only when the vault cannot supply the pinned bytes.
    """
    raw, problem = _from_vault(spec, vault)
    if raw is not None:
        return raw, "vault"
    try:
        path = sources_dir(archive_root(root)) / f"{fixture_id}.md"
    except harness.EvalError:
        return None, problem
    if not path.exists():
        return None, problem
    raw = path.read_text(encoding="utf-8")
    if spec.get("sha256") and harness.sha256_text(raw) != spec["sha256"]:
        return None, f"{problem}, and the archived copy does not match the pinned hash either"
    return raw, "archive"

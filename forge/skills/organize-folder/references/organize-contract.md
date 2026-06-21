# Folder Organization Contract

Organize a folder and its subfolders in place without losing data. Files move
only after the user reviews and agrees to a manifest. Duplicates are relocated,
never deleted. Every move is reversible.

## Run Layout

```text
<run-dir>/
  scan.json            # target, settings, and immutable per-file provenance
  manifest.csv         # user-editable plan: category, destination, status
  skipped.md           # protected and skipped paths, with reasons
  plan_report.md       # written by `plan`: summary, validation, planned moves
  move_log.jsonl       # written by `apply`: append-only record of every move
  final_manifest.csv   # written by `apply`: manifest plus final_status/final_path
  undo_log.jsonl       # written by `undo`: append-only record of reversals
```

The run directory is created by `scan` and must not pre-exist. Keep it outside
the target folder so it is never reorganized.

## Manifest Schema

`manifest.csv` is the contract between the agent and the user. Header order is
fixed; `plan` and `apply` reject a reordered header.

| Column | Editable | Meaning |
|---|---|---|
| `relative_source_path` | No | Path of the file relative to the target. |
| `sha256` | No | Content hash from the scan. Checked for tampering and re-verified before each move. |
| `size_bytes` | No | File size at scan time. |
| `modified` | No | Modification time at scan time (UTC). |
| `extension` | No | Lowercased file extension. |
| `detected_type` | No | MIME type guessed from the name, or `unknown`. |
| `category` | Yes | Deterministic category; the model may correct it. |
| `confidence` | No | 0-1 score for the deterministic category. |
| `is_duplicate` | No | `true` if another file has identical content. |
| `duplicate_of` | No | The primary copy this file duplicates. |
| `proposed_destination` | Yes | Destination path relative to the target. |
| `status` | Yes | `pending`, `duplicate`, or `keep`. |
| `note` | Yes | Free text; the scan flags low-confidence rows here. |

The user and model change only `category`, `proposed_destination`, `status`,
and `note`. Editing an immutable column (especially `sha256`) is rejected by
`plan` so accidental edits cannot silence the safety checks.

## Categories

Deterministic categories map to destination folders at the target root:
`Images`, `Videos`, `Audio`, `Documents`, `Spreadsheets`, `Presentations`,
`Archives`, `Code`, `Data`, `Fonts`, `Applications`, and `Other`. The model may
override any category and destination; categories are a starting point, not a
fixed taxonomy.

## Confidence and Review

- Known extensions score `0.95`.
- A type guessed only from MIME scores `0.6` (or `0.5` for an unrecognized MIME group).
- No extension and no MIME guess scores `0.3`.

Rows below the confidence threshold (default `0.75`, set with
`--confidence-threshold`) carry a review note. The agent should open the content
of each low-confidence file before finalizing its category and destination,
rather than trusting the deterministic guess.

## Statuses

Before apply, `status` is one of:

- `pending` — move the file to `proposed_destination`.
- `duplicate` — an exact-content duplicate; move it under `_duplicates/`,
  preserving its original relative path so copies never collide.
- `keep` — leave the file exactly where it is.

`apply` writes `final_status` into `final_manifest.csv`: `moved`, `kept`, or
`failed`. A `failed` row records why in `note` and the batch continues.

## Duplicate Handling

Files are grouped by SHA-256. Within a group the lexicographically first path is
the primary copy and keeps a normal destination; the rest are marked
`is_duplicate=true`, point to the primary in `duplicate_of`, and default to
`_duplicates/<original relative path>`. Duplicates are moved, never deleted, so
the user can review and remove them deliberately.

## Protected Paths

`scan` refuses to run when the target is a filesystem root, the home directory, a
system tree (for example `/System`, `/Library`, `/usr`, `/Applications`), or a
project or repository root (a directory containing markers such as `.git`,
`package.json`, `pyproject.toml`, `Cargo.toml`, or `go.mod`). Reorganizing those
locations would break how software functions.

During the walk these are skipped and recorded in `skipped.md`, never traversed
or moved:

- Hidden files and directories (names beginning with `.`).
- Symbolic links (files and directories).
- Repositories and dependency trees (`.git`, `.svn`, `.hg`, `node_modules`,
  `__pycache__`, `.venv`, `venv`, `site-packages`, and similar).
- Application and package bundles (`.app`, `.bundle`, `.framework`, `.xcodeproj`,
  `.photoslibrary`, and similar).
- Nested project roots discovered anywhere in the tree.

`plan` and `apply` additionally reject any `proposed_destination` that is
absolute, escapes the target with `..`, or passes through a protected directory.

## Apply and Undo Semantics

`apply` re-reads `scan.json` and `manifest.csv`, re-runs full validation, and
refuses to move anything while errors remain. For each movable row it:

1. Re-computes the source hash and aborts that file if it changed since the scan.
2. Refuses to overwrite an existing destination.
3. Creates destination folders and moves the file.
4. Appends the move to `move_log.jsonl` and flushes immediately.

`apply` is resumable: rerunning skips files already recorded in `move_log.jsonl`
or already present at their destination. `undo` replays `move_log.jsonl` in
reverse, moving each file back to its original location, and records the result
in `undo_log.jsonl`. No file is ever deleted by any command.

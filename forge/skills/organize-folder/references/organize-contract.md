# Folder Organization Contract

Organize a folder and its subfolders in place without losing data. Files move
only after the user reviews and agrees to a manifest. Duplicates are relocated,
never deleted. Every move is reversible.

## Run Layout

```text
<run-dir>/
  scan.json            # target, settings, and immutable per-file provenance
  manifest.csv         # user-editable plan: category, destination, status
  profile.json         # structured folder summary (distributions, clusters)
  profile.md           # model-facing folder summary read before designing layout
  review_queue.md      # files to open before trusting their category
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
| `parent_folder` | No | Folder containing the file, relative to the target (empty at the root). |
| `filename` | No | The file's name. |
| `sha256` | No | Full content hash; present for same-size duplicate candidates (and all files under `--full-hash`). Checked for tampering and re-verified before each move. |
| `fingerprint` | No | Cheap size-plus-edge hash present for every file. Used for integrity when no full `sha256` is recorded. |
| `size_bytes` | No | File size at scan time. |
| `modified` | No | Modification time at scan time (UTC). |
| `extension` | No | Lowercased file extension. |
| `detected_type` | No | MIME type guessed from the name, or `unknown`. |
| `peek` | No | Short content signal: text head, image dimensions, or archive listing. Empty when none applies. |
| `name_cluster` | No | Label grouping similar filenames (for example `camera`, `screenshots`, `invoice`, `generic`). |
| `category` | Yes | Deterministic category; the model may correct it. |
| `confidence` | No | 0-1 score for the deterministic category. |
| `is_duplicate` | No | `true` if another file has identical content. |
| `duplicate_of` | No | The primary copy this file duplicates. |
| `proposed_destination` | Yes | Destination path relative to the target. |
| `status` | Yes | `pending`, `duplicate`, or `keep`. |
| `note` | Yes | Free text; the scan flags low-confidence rows here. |

The user and model change only `category`, `proposed_destination`, `status`,
and `note`. Editing an immutable column (especially `sha256` or `fingerprint`)
is rejected by `plan` so accidental edits cannot silence the safety checks.

## Categories

Deterministic categories map to destination folders at the target root:
`Images`, `Videos`, `Audio`, `Documents`, `Spreadsheets`, `Presentations`,
`Archives`, `Code`, `Data`, `Fonts`, `Applications`, and `Other`. The model may
override any category and destination; categories are a starting point, not a
fixed taxonomy.

## Designing the Layout

The deterministic categories above are only a fallback for a folder with no
discernible theme. For most folders the model designs a layout that fits the
folder's actual contents, read from `profile.md`. Apply these principles:

1. **Build on existing structure when there is evidence of it.** If the
   profile's **Folders** and **Name clusters** already imply a scheme — even a
   partial or inconsistent one — extend and regularize that scheme rather than
   inventing a parallel one. Design a layout from scratch only when the folder
   has no usable structure.
2. **Top-level categories, then subcategories as needed.** Choose a flat tier of
   meaningful top-level folders, and add subfolders beneath one only where a
   real sub-grouping exists. Derive both tiers from the contents' own dimensions
   (topic, document type, then optionally date) rather than from generic
   file-type buckets when the folder is domain-specific.
3. **Keep nesting shallow.** Use **at most 2–3 levels of folders beneath the
   target**. Deeper trees are harder to browse than they are worth.
4. **Use the fewest categories that still organize effectively.** Prefer the
   smallest set of top-level folders that cleanly covers the contents. Don't
   create a folder for a one-off distinction; merge thin categories upward into
   a broader one (or into `Other`).
5. **Scale folder count to file count.** The more files a category holds, the
   more a person benefits from splitting it into subfolders so no single folder
   forces them to scan dozens or hundreds of files at once — but split only
   along a distinction that is itself a meaningful category, never arbitrarily.
   A category with few files stays a single folder.

**Worked example.** A folder of sewing material — clothing patterns, sewing
machine manuals, and reusable template files — has no single right answer in the
generic categories. A good layout gives it top-level `Patterns`, `Manuals`, and
`Templates`. Because `Patterns` holds far more files than the others, it splits
by garment type into `Patterns/Bottoms`, `Patterns/Tops`, `Patterns/Bags`, and
`Patterns/Hats` — garment type being a real sub-category — rather than staying
one flat `Patterns` folder or exploding into a separate top-level folder per
garment type. `Manuals` and `Templates` each stay a single folder until they
grow enough to warrant their own subdivisions.

## Confidence and Review

- Known extensions score `0.95`.
- A type guessed only from MIME scores `0.6` (or `0.5` for an unrecognized MIME group).
- No extension and no MIME guess scores `0.3`.

Rows below the confidence threshold (default `0.75`, set with
`--confidence-threshold`) carry a review note. `review_queue.md` lists exactly
the files that need inspection (low confidence, unknown type, generic name, or
uncategorized), capped to keep review bounded. The agent should open those files
before finalizing their category and destination rather than trusting the
deterministic guess; high-confidence files rarely need per-file inspection.

## Profile

`scan` writes `profile.json` and `profile.md` summarizing the whole folder:
per-folder counts and sizes, category and extension distributions, filename
clusters with examples, modified-year clusters, the largest files, a duplicate
summary, and a handful of representative content peeks. The agent reads
`profile.md` first to understand what the folder is for and to design a
destination layout that fits it, instead of crawling every file. Regenerate it
from an existing run with `profile <run-directory>`.

## Hashing and Integrity

For speed, `scan` computes a cheap `fingerprint` (file size plus a hash of the
head and tail blocks) for every file, and a full `sha256` only for files whose
size collides with another file. Two files with identical content always share a
size, so this detects every exact duplicate without reading unique-size files
end to end. `--full-hash` computes a full `sha256` for every file when maximum
integrity is preferred over speed.

`plan` and `apply` verify both `sha256` and `fingerprint` against `scan.json`,
and `apply` re-checks each source before moving it using the strongest hash
recorded for that file: the full `sha256` when present, otherwise the
`fingerprint`.

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

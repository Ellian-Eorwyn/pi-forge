---
name: organize-folder
description: Organize a messy folder and all its subfolders in place through a reviewable, user-editable manifest. Use to sort a pile of files into category folders, route exact duplicates into a duplicates folder instead of deleting them, and propose a destination for every file. The user edits the manifest spreadsheet to correct categories and destinations before anything moves; only on agreement does the agent move files, with full undo and safeguards that refuse system paths, repositories, dependency trees, and bundles.
---

# Organize Folder

Help a user tidy a folder and its subfolders by moving files into clear category
folders. Nothing moves until the user reviews and agrees to a plan. Duplicates
are relocated, never deleted, and every move is reversible.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path. Confirm
   capabilities and safeguards when uncertain:

   ```bash
   python3 <skill-directory>/scripts/organize-folder.py doctor
   ```

2. Confirm the target folder with the user. Create a new run directory under
   `forge-output/organize-folder/<folder-name>/`; if it exists, use a numbered
   suffix. Scan the folder into a reviewable manifest and profile:

   ```bash
   python3 <skill-directory>/scripts/organize-folder.py scan <target-folder> \
     --output <new-run-directory>
   ```

   The scan is recursive, skips hidden paths and symlinks, refuses system paths
   and project roots, and leaves repositories, dependency trees, and bundles
   untouched (recorded in `skipped.md`). It writes `manifest.csv`, `scan.json`,
   `profile.md`/`profile.json`, and `review_queue.md`, and routes exact-content
   duplicates to `_duplicates/`. For speed it fingerprints every file and only
   computes a full SHA-256 for files whose size collides with another (the only
   exact-duplicate candidates); pass `--full-hash` to hash every file. Use
   `--confidence-threshold <0-1>` only when the user wants a stricter or looser
   review bar than the `0.75` default.

3. Read [references/organize-contract.md](references/organize-contract.md), then
   read `profile.md` to understand what the folder holds: its folders, category
   and extension distributions, filename clusters, date clusters, and sample
   content peeks. Use these signals to design a destination layout that fits this
   specific folder rather than forcing the default categories. Aim for a flat
   tier of meaningful top-level categories with subfolders only where a real
   sub-grouping exists, nesting no more than 2–3 levels deep. Build on any
   structure the profile's folders and name clusters already imply rather than
   inventing a parallel one; prefer the fewest categories that still organize the
   contents cleanly; and split a category into subfolders only as its file count
   grows large enough that a flat folder would be hard to scan — and only along a
   distinction that is itself a real category. See the "Designing the Layout"
   section of the contract for the full principles and a worked example.

4. Open the files listed in `review_queue.md` (low confidence, unknown type, or
   generic names) before trusting their category; high-confidence files rarely
   need per-file inspection. In `manifest.csv`, correct `category` and
   `proposed_destination` to realize the layout you designed in step 3, editing
   by cluster where possible — a whole name cluster or source folder at once —
   rather than row by row. Edit only `category`, `proposed_destination`,
   `status`, and `note`;
   never change `sha256`, `fingerprint`, or other provenance columns. Present the
   resulting plan to the user: the destination layout, how many files move,
   duplicates routed to `_duplicates/`, and anything skipped as protected. Tell
   the user they can edit `manifest.csv` directly to change categories,
   destinations, or set a file's `status` to `keep` to leave it in place.

5. After the user edits the manifest, validate it and produce a plan report:

   ```bash
   python3 <skill-directory>/scripts/organize-folder.py plan <run-directory>
   ```

   Resolve every reported error in `manifest.csv` and rerun `plan` until it is
   valid. Share `plan_report.md` and get explicit agreement before moving files.

6. Once the user agrees, move the files:

   ```bash
   python3 <skill-directory>/scripts/organize-folder.py apply <run-directory>
   ```

   `apply` re-validates the manifest, re-verifies each file's hash immediately
   before moving it, refuses to overwrite existing files, and records every move
   in `move_log.jsonl`. Report moved, kept, and failed counts from
   `final_manifest.csv`. The command is resumable if interrupted.

7. If the user wants to revert, reverse every move:

   ```bash
   python3 <skill-directory>/scripts/organize-folder.py undo <run-directory>
   ```

## Safety and Failure Handling

- Never move a file until the user has reviewed the manifest and agreed.
- Refuse to organize a filesystem root, the home directory, a system tree, or a
  project or repository root; ask the user for a specific content subfolder.
- Skip hidden paths and symlinks. Never traverse or move repositories,
  dependency trees, virtual environments, or application bundles.
- Detect duplicates by exact SHA-256 content match and move them to
  `_duplicates/`. Never delete a file in any command.
- Keep destinations inside the target folder. Reject absolute paths, `..`
  escapes, and destinations that pass through protected directories.
- Re-verify each source against its strongest recorded hash (full `sha256` when
  present, otherwise the `fingerprint`) before moving; record a file whose
  content changed as `failed` and continue the batch rather than moving stale
  content.
- Do not edit `manifest.csv` provenance columns; the integrity check refuses a
  manifest whose `sha256` or `fingerprint` values were altered.
- Do not install packages; the script uses only the Python standard library.

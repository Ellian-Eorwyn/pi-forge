# Coding Change Workflow

Make changes that a reviewer can read quickly and trust. The work is judged by
the clarity of the diff and the honesty of the record, not by volume.

## Inspect Before Editing

Always profile the repository with `coding.mjs inspect` before changing code.
Read `repo_profile.md` and `repo_profile.json` and confirm:

- the language(s) and package manager actually in use;
- the build, test, lint, and type-check commands available;
- the convention files present (`tsconfig.json`, `.editorconfig`, ESLint,
  Prettier, Ruff, pytest, and similar);
- the git state, including the branch and whether the working tree is already
  dirty.

Do not assume conventions. When the profile is ambiguous, read representative
source files and matching neighbors before writing anything.

## Output Run Layout

Write generated artifacts to the chosen output directory, never into the source
tree:

```text
repo_profile.json
repo_profile.md
change_summary.md
run_log.md
working/        # optional scratch: drafts, scripts, captured command output
```

`repo_profile.*` are produced by the script and must not be hand-edited.
`change_summary.md` and `run_log.md` are written by you. Keep deterministic
command output separate from your own commentary.

## Git Safety

- Check `git status` before and after the change.
- Never run destructive git operations unless the user explicitly requests them:
  `git reset --hard`, `git clean -fd`, `git push --force`, history rewrites
  (`rebase`, `commit --amend`, `filter-branch`), and branch or stash deletion.
- Preserve uncommitted user work. If the tree was already dirty, confirm which
  changes are the user's and leave them intact.
- Do not commit, push, or open pull requests unless the user asks.

## Small Reviewable Patches

- Change the minimum needed to achieve the goal. Avoid drive-by reformatting and
  unrelated refactors.
- Reuse existing utilities and helpers instead of adding parallel ones.
- Match surrounding naming, structure, comment density, and idiom.
- Keep generated rationale in `change_summary.md`, not in code comments.

## Verification

Run the commands the profile reports, and record each in `run_log.md`:

- tests (for example `npm test`, `pytest`);
- linters (for example `npm run lint`, `ruff check`);
- type checks (for example `tsc --noEmit`, `mypy`);
- a smoke test of the touched path when no formal test exists.

Record the exact command, exit status, and a short outcome. If a check fails or
cannot be run, say so plainly. Never report a check as passing when it did not,
and never disable or skip a check to make output look clean.

## change_summary.md Template

```markdown
# Change Summary

## Summary
One or two sentences on what changed.

## Motivation
Why the change was made and what problem it addresses.

## Files changed
- `path/to/file` — what changed and why.

## Verification
- `command` — exit status and outcome.

## Follow-ups & uncertainties
- Open questions, known gaps, or unverified assumptions. State "None" if empty.
```

The `## Summary`, `## Motivation`, `## Files changed`, and `## Verification`
headings are required; `validate` checks for them.

## run_log.md Template

Append one entry per command in order. Keep raw output verbatim and clearly
separated from interpretation.

```markdown
# Run Log

## 2026-06-19T14:02:11Z — npm test
Exit status: 0
Outcome: 42 passed, 0 failed.

## 2026-06-19T14:05:30Z — npm run lint
Exit status: 1
Outcome: 2 errors in src/parser.ts; fixed in the next edit.
```

## Utility Scripts

When the task is to write a utility script for document processing, scraping,
conversion, data cleaning, or reporting, follow the forge defaults: preserve
original files, write to explicit output paths, prefer deterministic behavior,
make assumptions visible, and avoid silent destructive changes.

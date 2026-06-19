---
name: coding
description: Assist with code repositories, scripts, automation, debugging, and small software tools for information processing. Use to inspect a repository before editing, detect its language, package manager, build, test, and lint setup and conventions, make small targeted reviewable patches, run tests, linters, and type checks, write utility scripts for document processing, scraping, conversion, data cleaning, and reporting, and record a commit-ready change summary and run log without destructive git operations.
---

# Coding

Make small, reviewable, tested changes to a code repository while preserving the
user's existing work. Inspect before editing and record what was done.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path. Report the
   available toolchain before relying on any tool:

   ```bash
   node <skill-directory>/scripts/coding.mjs doctor
   ```

   Add `--json` for machine-readable output.
2. Choose a new output directory under `forge-output/coding/<repo-stem>/`. If it
   exists, use the next numbered suffix. Profile the repository before touching
   any code:

   ```bash
   node <skill-directory>/scripts/coding.mjs inspect <repo> --output <new-directory>
   ```

   `inspect` is read-only with respect to the repository. Read both
   `repo_profile.md` and `repo_profile.json` before editing. Note the detected
   languages, package manager, build/test/lint commands, convention files, and
   the git state, including any pre-existing uncommitted changes.
3. Read [references/change-workflow.md](references/change-workflow.md). Confirm a
   clean or known git state and state the intended change scope before editing.
   If the working tree is already dirty, confirm which changes are the user's.
4. Make small targeted edits. Reuse existing utilities and match the project's
   conventions rather than introducing new patterns or broad rewrites. Append
   every command you run to `run_log.md` in the output directory.
5. Run the project's tests, linters, and type checks using the commands from the
   profile. Record every result, including failures, in `run_log.md`. Never
   claim a check passed when it did not, and never silence a failing check.
6. Write `change_summary.md` describing what changed, why, the files touched, how
   it was verified, and any follow-ups or uncertainties. Then validate:

   ```bash
   node <skill-directory>/scripts/coding.mjs validate <output-directory>
   ```

   Resolve every validation error before completion.

## Safety and Failure Handling

- Never run destructive git operations — `reset --hard`, `clean -fd`, force
  push, history rewrites, or branch/stash deletion — unless the user explicitly
  requests them. Prefer additive, recoverable actions.
- Check `git status` before and after the change. Preserve uncommitted user work
  and do not commit unless the user asks.
- Do not install system packages or global tools. Report missing capabilities
  through `doctor` and let the user decide.
- Keep generated commentary out of source files. Put rationale in
  `change_summary.md`, not in code comments, unless a comment genuinely aids the
  reader and matches surrounding style.
- Surface test failures, skipped steps, and partial work honestly. If a change
  cannot be verified, say so in `change_summary.md` rather than implying success.
- For utility scripts that transform data or files, follow the forge defaults:
  preserve originals, write to explicit output paths, and avoid silent
  destructive changes.

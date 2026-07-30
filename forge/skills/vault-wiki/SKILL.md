---
name: vault-wiki
description: Install the seven wiki entry templates and expand wiki entity notes into complete, cited reference cards using canonical sources like the Stanford Encyclopedia of Philosophy and Wikipedia. Use when the user asks to expand the wiki, fill in the figures or concepts, flesh out wiki notes, research and add detail to wiki entries, add sources or citations to wiki notes, install the wiki templates, or make the wiki notes more complete.
---

# Vault Wiki

A wiki note is a reference card: it defines a thing so other notes can link to
it. This skill installs the per-kind templates and fills thin notes in from
canonical sources, citing what it used.

It is the only pi-forge skill that writes into an existing note's **body**, so
the permission is narrow by construction. The kind spec names the sections a
generator owns; only those are rewritten, matched by their visible heading; and
every merge is re-read and refused unless everything else survived byte for byte.
`## Notes` is yours — never written, never read.

Companion to `vault-connections`, which creates wiki notes from research runs and
links them. That skill still never touches a body.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path, and the vault
   path from the user or known configuration.
2. Check the environment before a long run:

   ```bash
   python3 <skill-directory>/scripts/vault-wiki.py doctor --vault <vault>
   ```

   This verifies the schema parses, that every shipped template agrees with its
   kind spec, which templates are installed, how many notes of each kind are
   incomplete, whether each source resolver reaches its site, and whether both
   endpoints are reachable. It warns if the `chat` endpoint is actually reasoning,
   which makes bulk drafting far slower.

   Two `doctor` warnings are worth acting on before a long run rather than after:
   a **TLS failure** (this Python cannot find a CA bundle — set `SSL_CERT_FILE`,
   on macOS to `/etc/ssl/cert.pem`), and a **throttled search backend** (SearXNG
   answering HTTP 200 with zero results because its upstream engines have
   rate-limited it). Either one turns a whole run into "no source resolved".
3. Install the templates if `doctor` says they are missing or stale:

   ```bash
   python3 <skill-directory>/scripts/vault-wiki.py template-install --vault <vault> --dry-run
   python3 <skill-directory>/scripts/vault-wiki.py template-install --vault <vault>
   ```

   Installing is a no-op on an identical file and **refuses to overwrite a
   template the owner has modified** unless `--force` is passed. Until the
   templates exist, `vault-connections import-run` also fails closed for wiki
   kinds, so this unblocks both skills.
4. **Start small.** A first pass on a kind should be ten notes, not all of them:

   ```bash
   python3 <skill-directory>/scripts/vault-wiki.py expand --vault <vault> --kind figure --only-empty --limit 10
   ```

   `expand` never writes to the vault — it only produces proposals under the run
   directory — so it is safe to run and inspect before anything is applied.
   `--kind` takes a comma-separated list or `all` (default `concept,term`).
   `--only-empty` selects just the notes missing a managed section. `--no-web`
   drafts without sources for inspecting prose shape only; such a run is marked
   uncited and **refused at apply**.

   To target named notes, repeat `--title` once per note. It is deliberately not
   comma-separated: vault titles contain commas, so
   `--title "Actor-Network Theory, ANT"` is one note, not two.
5. Review the proposals with the user, ten at a time:

   ```bash
   python3 <skill-directory>/scripts/vault-wiki.py review --vault <vault> --run <run-dir> --limit 10 --offset 0
   ```

   For each, give the title, the sources it used, and the full body it would
   write. Say when the reviewer flagged one and what its objection was. Flagged
   proposals sort first, so the first page shows the ones that need judgment.
6. Apply. The batch form takes everything unflagged and cited:

   ```bash
   python3 <skill-directory>/scripts/vault-wiki.py apply --vault <vault> --run <run-dir> --accept-batch
   ```

   `--accept-batch` deliberately **skips every flagged proposal** — those must be
   named individually with `--accept w-004,w-011` once the user has decided.
   `--dry-run` shows the exact operation list first. Use `--reject` for ids the
   user turned down.
7. If a batch was wrong, undo it wholesale:

   ```bash
   python3 <skill-directory>/scripts/vault-wiki.py revert --vault <vault> --run <run-dir>
   ```

   Every edited file was copied under `<run-dir>/backup/` before it was written.
   Revert restores from there and skips any file edited since the run applied,
   rather than clobbering newer work.
8. Report the run directory and the counts, including what was held back and why.

## What to relay to the user

- **Held-back notes are the interesting output.** A note with no on-topic
  canonical source, a draft that overran its length budget, or a merge the
  ownership check refused is reported with its reason and left alone. Say which
  and why rather than only reporting successes.
- **A flagged note is the reviewer's doubt, not a verdict.** It is still
  proposed, with the objection attached, and it is the user's call.
- **Not every note gets a source.** A page that neither names the subject in its
  title nor discusses it repeatedly is discarded, so a coined phrase like "God
  Trick" — which no encyclopedia has an entry on — ends up held back rather than
  drafted uncited. That is the correct outcome, not a failure to explain away.
- **Sources are looked up through their own APIs and indexes** where they have
  them (Wikipedia, SEP, IEP), and only fall back to general web search otherwise.
  So a rate-limited search backend degrades coverage rather than emptying the run.
- **`weakSources` means every source only *covers* the subject** — a broader
  encyclopedia entry that discusses it rather than an entry about it. Still
  citeable and still included in `--accept-batch`, since the reviewer sees the
  same text, but worth mentioning when you present it.

## Guarantees

- Only the sections the kind spec declares are written. `## Notes` and every
  heading the spec does not claim keep their bytes and their position, and the
  merge is refused if that is not true of the result. The one exception is the
  run of blank lines *between* sections, which has to move to insert a heading.
- An existing section is never moved or renamed. A note whose heading is an
  alias — `## Key Ideas` where the spec says `## Key Points` — is updated in
  place rather than growing a second near-identical heading.
- Frontmatter, the BOM, and line-ending style are preserved exactly. This skill
  never changes a property; run `vault-organizer` for that.
- Link sections are additive. Several figure notes list more colleagues in the
  section than they carry in `related`, so the union is written and nothing is
  dropped.
- No note is created, moved, renamed, or deleted. `expand` only ever rewrites
  managed sections of notes that already exist.
- A citation can only name a source this run actually fetched and archived. A
  quotation must appear in that archived text. A stated year must appear in a
  source or in the note's existing text.
- The drafting and reviewing models see byte-identical source text, so the
  reviewer never objects to a claim the drafter had support for.
- Every write is backed up first, journaled, and revertible.

## Rules

- Never hand-edit a note the pipeline is going to write, a proposal manifest, or
  a run journal outside the script.
- Never apply a flagged proposal through `--accept-batch`. If the user says
  "apply the good ones", that is `--accept-batch`; if they say "apply everything",
  list the flagged ones and their objections and get the ids.
- Never claim a held-back or skipped note was updated. The `warnings` array says
  what was skipped and why.
- Never write `## Notes`, and never quote it back to the user as though the
  pipeline produced it.
- Never add a wiki domain, subdomain, or note type to the schema note. All seven
  wiki subdomains already exist; if `doctor` says otherwise, tell the user which
  rows are missing and let them decide.
- Never edit an installed template to make a run pass. Fix the shipped copy in
  `references/templates/` and its spec, then re-install.
- Keep stdout machine-readable; diagnostics belong on stderr.

## Reference

Read [references/wiki-note-format.md](references/wiki-note-format.md) for the
note shape, the section-ownership contract, the deterministic checks, and the
calibration constants. The per-kind sections live in
[references/wiki-kinds.json](references/wiki-kinds.json) and the source
preference order in
[references/canonical-sources.json](references/canonical-sources.json).

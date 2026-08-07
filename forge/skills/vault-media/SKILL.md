---
name: vault-media
description: Catalog books, films, television, music, and games in an Obsidian vault as `work` notes under an `entertainment` domain - metadata fetched from Open Library, MusicBrainz, TVmaze, Steam, TMDB, or IGDB, and the owner's own rating and thoughts recorded verbatim beside it, never inferred. Use to add something just finished, keep a To Read or To Watch list, or promote a backlog row. Not for books read as scholarly sources, which are `source` notes.
---

# Vault Media

The vault already holds a book you read *as a source*: `type: source` under the
Sources root, filed by kind, cited from a chapter. This skill is for the other
relationship — the novel you read on a train, the album you had on all August,
the game you finished and have opinions about.

Those are `type: work` notes under `entertainment`, and the schema has had a
definition for `work` since before there was anything to put in it: *"a named
book, film, game, album, or text treated as a recurring subject in its own
right, as distinct from `source`."*

Both notes may exist for the same object. They are different notes about the
same thing and `related` is what joins them.

## The one rule that matters

**Metadata is fetched. Judgment is quoted.**

Title, year, director, ISBN, developer, cover — all of that comes from a
provider and is checkable. The rating and the thoughts come from the owner and
are *copied*, never composed. A note where those two kinds of content are
indistinguishable is worthless within a year.

So they live in different blocks, and the separation is enforced in code:

- `## Details` — a body table of fetched fields.
- `## Thoughts` — the owner's words, verbatim, in plain prose. Never a
  `[!reflection]` callout: that block means *generated interpretation* and is
  explicitly not for anything the owner wrote.
- `> [!summary]` — one drafted sentence, grounded in the fetched record and
  checked against it before anyone sees it.

**A rating the owner did not give does not exist.** Not null, not zero, not a
provider's average promoted into the owner's voice. The key is absent. Every
provider returns a score of some kind — Metacritic, TMDB vote average, IGDB
aggregate — and every one is a fact about other people; they are recorded in the
Details table under their own names, where they cannot be read as a personal
verdict.

Never offer to supply a rating or a reaction the owner has not expressed, and
never infer one from how they said something. "I finally got round to it" is not
an opinion. If they gave no rating, the note has no rating; that is a complete
and correct note, not a gap to fill.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path, and the vault
   path from the user or the injected vault context.

2. Check the environment before a first run:

   ```bash
   python3 <skill-directory>/scripts/vault-media.py doctor --vault <vault> --probe
   ```

   This reports whether the schema declares the `entertainment` domain, its five
   subdomains, and a `rating` property that is **human-owned**; whether the five
   folders exist; which provider keys are configured; and which TLS trust store
   the providers will use. `--probe` also calls each usable provider once.

   A `rating` property that is approved but *not* human-owned is reported as a
   failure, not a warning. Human-owned is what withholds it from the classifier
   prompt; without that marker the classifier is shown the field and fills it in,
   which is precisely the fabrication this skill exists to prevent.

3. **When the request came as prose, parse it first.**

   ```bash
   python3 <skill-directory>/scripts/vault-media.py parse --text "add Hades and the new Murderbot book, both an 8"
   ```

   Returns one object per work, with `thoughts` as a **verbatim span** of what
   the user said. A value that is not literally present in the input is dropped
   with a warning — a paraphrase filed under the owner's name is a fabrication
   even when it is accurate. Relay those warnings; do not quietly re-add the
   dropped text.

4. **Search before adding when the title is ambiguous.**

   ```bash
   python3 <skill-directory>/scripts/vault-media.py search --medium game --query "Hades"
   ```

   Writes nothing. Results are re-ranked deterministically before display,
   because providers rank for full-text relevance and that is routinely the
   wrong record: Steam answers "Hades" with *Hades II*, and MusicBrainz answers
   "Radiohead In Rainbows" with a Vitamin String Quartet covers album.

5. **Add the note.** Dry run is the default; `--apply` writes it.

   ```bash
   python3 <skill-directory>/scripts/vault-media.py add --vault <vault> \
     --medium book --query "Piranesi" --rating 9 \
     --thoughts "The house is the whole point." --apply
   ```

   Pass `--rating` and `--thoughts` **only** with what the user actually said.
   Pass `--pick <provider:id>` when `search` showed the right record is not the
   top one. If the top two matches score within 15 points the result warns that
   the choice was close — surface that rather than presenting the note as
   settled.

6. **Backlog entries are list notes, not stub notes.**

   ```bash
   python3 <skill-directory>/scripts/vault-media.py backlog --vault <vault> \
     --medium movie --query "Stalker" --why "Ellie mentioned the long takes" --apply
   python3 <skill-directory>/scripts/vault-media.py promote --vault <vault> \
     --medium movie --title "Stalker" --rating 9 --thoughts "..." --apply
   ```

   `promote` builds the full note and removes the backlog row in one step,
   reusing the year already recorded so the thing is not looked up twice.

## Review points

- **Show the note before writing it.** `add` without `--apply` returns the
  complete text in `data.preview`. Media notes are short and the user can read
  one in ten seconds; do not apply a batch unseen.
- **Name what was skipped.** A run with no TMDB key cannot answer a film query
  at all. Say that, rather than reporting no results.
- **Never present a drafted lead as verified when it was dropped.**
  `leadVerified: false` means the sentence failed the grounding check or the
  model was unreachable, and the note went out without a summary. That is the
  designed outcome, not a defect — say so plainly.

## Providers

Checked against the live services on 2026-08-06. Re-verify before trusting this
past ~2027.

| Medium | Provider | Key | Notes |
| --- | --- | --- | --- |
| Books | Open Library | no | Work-level records, covers by id |
| Music | MusicBrainz + Cover Art Archive | no | 1 req/s; publishes remaining budget |
| Shows | TVmaze | no | Running/ended status, network, IMDb ids |
| Games | Steam | no | **PC only**; Metacritic scores |
| Movies | TMDB | **yes** | The only usable film source |
| Games | IGDB | **yes** | `client_id:client_secret` from Twitch; console and handheld |

Keys live in `connectedServices.apiKeys` in the forge agent settings, each
overridable by `FORGE_API_KEY_<PROVIDER>`. `doctor` reports configured or
absent; it never prints a value.

**Deliberately absent**, having been probed rather than assumed:

- **Google Books** returns HTTP 429 to an anonymous request with
  `quota_limit_value: "0"` — the anonymous quota is literally zero, not a limit
  that clears. This also means the Obsidian **Book Search plugin cannot work in
  its default configuration**, since it queries Google Books.
- **RAWG** returned HTTP 522 on three attempts. The host is down, not throttling.
- **BoardGameGeek's XML API** now returns 401. It was open for years, which is
  why it is worth recording: board games have no free API and are entered by hand.

## Model calls

Two stages, both on `chat`, both **unmeasured** — they appear in neither
`STAGE_SERVICES` nor `STAGES_HELD_ON_CHAT`, which means nobody has scored them
yet, not that `chat` is known to be right. Measure in `forge/evals` before
routing either elsewhere.

- `parse-media-request` — prose to structured items, one work per object.
- `draft-media-lead` — one or two sentences from the fetched record.

The lead is checked before use: `ungrounded_terms` compares every proper noun
and year in the drafted sentence against the fetched record, and a sentence
naming anything the record does not contain is **dropped**, not repaired. This
is the mechanical form of the grounding rule — the test is whether the claim
survives the model's weights being replaced. A media note with no summary is
merely plainer; one with an invented director is wrong in a way nobody catches.

## Safety

- Never writes outside the five `entertainment` subdomain folders and the
  backlog notes inside them.
- Never overwrites an existing note without `--overwrite`.
- Never sets `rating` or `## Thoughts` from anything but the owner's own words.
- Never edits the schema note. A missing domain, subdomain, or property is
  reported by `doctor` for the owner to add.
- Dry run is the default for everything that writes.

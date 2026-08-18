---
name: vault-transcripts
description: Process raw voice-note and meeting transcripts in an Obsidian vault inbox - give each recording a real title, clean speech-to-text artifacts without rewriting the speaker's meaning, summarize, and keep the recording as its own linked source note. Use before vault-organizer processes the inbox; use split to separate recordings from notes processed earlier. For untranscribed recordings use transcription; outside a vault use transcript-cleanup.
---

# Vault Transcripts

A transcription app drops files like `20260724 131748-9788991C.md` into the vault
inbox: no frontmatter, no headings, no paragraphs, and a filename that says
nothing about the recording. This skill gives each one a name, a summary, and a
readable body, and keeps the original transcription beside it as a source note
the new note links to.

Run it **before** `vault-organizer` inbox processing. This skill decides what a
recording is called and how it reads; the organizer decides where it belongs and
replaces the frontmatter with its own classification.

Both the processed note and the recording's own note carry `date` — the day of
the recording, taken from the filename or the spoken date, not the day the
transcript was processed. Today's date is used only when neither is known.
`date` is human-owned, so the organizer carries it forward but can never supply
it; a note that leaves this skill without one never gets one.

Everything runs on the local LAN endpoints. Nothing is sent anywhere. That is a
requirement, not a detail — this inbox contains therapy sessions and private
conversations.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path, and the vault
   path from the user or the injected vault context.
2. Check the environment before a long run:

   ```bash
   python3 <skill-directory>/scripts/vault-transcripts.py doctor --vault <vault>
   ```

   This reports whether the vault and inbox are writable, the schema note parses
   and defines the vocabulary this skill writes, the optional voice note parses
   and which stages select owner/source/no voice, and both endpoints answer. The
   `chat` check also reports whether that endpoint is actually non-thinking:
   cleanup is one call per chunk, so a thinking endpoint there wastes hundreds of
   hidden tokens on every chunk.
3. Dry run first — dry run is always the default:

   ```bash
   python3 <skill-directory>/scripts/vault-transcripts.py process --vault <vault>
   ```

   Use `--limit 5` for a first trial on an inbox this skill has not seen. One
   stderr line per unit with an ETA; stdout stays a single JSON result.
4. A dry run also writes an **Inbox Review** note at the top of `00 Inbox`
   (`! Inbox Review.md`) and stages each proposed note into
   `00 Inbox/_Pending Review/`. This is the review surface the user works in
   (see "Reviewing and approving in the vault" below). Read `report.md` and tell
   the user what it holds — proposed renames, summaries, notes held for invented
   words and which words, duplicate pairs, the Verification section, the run
   directory — and point them at the review note to open the proposals, tick what
   to keep, and apply.
5. Get explicit approval, then apply. The user's own click on the review note's
   apply link is approval; from the chat, apply the reviewed run:

   ```bash
   python3 <skill-directory>/scripts/vault-transcripts.py process --vault <vault> --apply --from-review
   ```

   `--from-review` reads the run and the approvals from the review note. To apply
   a whole run unreviewed instead (every passing note, no waivers), resume it
   directly with `--apply --run <run-directory>`. "Process my voice notes and
   apply it" is approval; a vague "sort out my inbox" is not.
6. Offer to run `vault-organizer inbox` next, which files the cleaned notes —
   both halves of each pair, the note and its recording.

The steps above are the **review-first** flow: use it when the user wants to
preview and approve before anything is written. When they instead want the inbox
just handled, use the autonomous flow below.

## Autonomous runs, and a single note end-to-end

`--autonomous` makes a run file its own routine work and stop only for the
genuinely serious set, then report what it did. Reach for it when the user asks
you to just handle their voice notes rather than to prepare something to review
("process my voice notes", "sort out my inbox", "clean up and file this").

```bash
python3 <skill-directory>/scripts/vault-transcripts.py process --vault <vault> --autonomous
```

- **What it files on its own:** every recording that clears the gates — renamed,
  cleaned, summarized, with its recording kept as a linked source note. No tick.
- **What still waits for a person (the serious set):** a cleaned transcript the
  diagnose→fix loop could not make faithful, a structural failure, and unusable
  (corrupt speech-to-text) input. These stay in `00 Inbox`; the standing
  `! Inbox Review.md` becomes a receipt of what was filed plus a "Needs a
  decision" list of exactly these, with the reason on each. Relay that list.
- **The diagnose→fix loop is why "usually right" is safe to apply unreviewed.**
  When the thinking model flags a cleaned transcript as unfaithful, it names what
  was dropped and quotes the source; the non-thinking model restores exactly that
  from the source; the thinking model re-checks. That repeats up to a small bound
  before the note is held — so most flags are repaired automatically, and only a
  note it genuinely cannot fix reaches you.

**Scope to one note (or a few) with `--note`**, which *implies* `--autonomous`
unless you pass `--no-autonomous`. A selector is a vault-relative path, a
filename, or a stem; it is repeatable; a selector that matches nothing fails
loudly rather than processing the whole inbox.

```bash
python3 <skill-directory>/scripts/vault-transcripts.py process --vault <vault> --note "<filename>"
```

For "run the transcript on this one note, categorize it, and file it," run the
scoped process above, then hand the notes it reports under `data.filed` (both the
cleaned note and its recording, still in `00 Inbox` with frontmatter now) to the
organizer, scoped the same way:

```bash
python3 <organizer-skill-directory>/scripts/vault-organizer.py inbox --vault <vault> --note "<filed-path>" --autonomous
```

That is the whole single-note end-to-end path: cleanup on this note, then classify
and file it, each touching only that note and each stopping only for a serious
issue. If the transcripts stage reports the note under `data.held`, do **not** run
the organizer on it — surface the hold instead.

## Reviewing and approving in the vault

A dry run leaves a control note the user reviews in Obsidian rather than reading a
report back to them:

- **`00 Inbox/! Inbox Review.md`** sorts to the top of the inbox and lists the run.
  Notes that cleared every gate are under **To process**, ticked by default. Notes
  the gate **held for invented words** are under **Held**, unticked, each showing
  the exact words added. Structural holds and duplicate pairs are under **Needs a
  decision**, for information.
- **`00 Inbox/_Pending Review/`** holds each proposed note under its real name, so
  the review note's `[[wikilinks]]` open them and the user can **edit them in
  place**. Both folders are skipped by this skill's scan and by `vault-organizer`,
  so they are never reprocessed or filed.

The user opens a proposal, and then, per note:

- **keep it** — leave it ticked (passed notes) or tick it (held notes);
- **approve with a small change** — edit the staged note, then tick it;
- **waive a few invented words** — tick a held note as-is; the applied note keeps
  a collapsed `> [!provenance]-` stamp of exactly what was let through;
- **reject** — untick it. It is not applied and its recording stays in the inbox
  for a later run. To re-clean a held chunk with the thinking-model retry instead,
  rerun the dry run with `--retry-failed`.

Applying (`--from-review`) **recomputes the gate from the bytes on disk**, never a
stored verdict — the same safety property as `vault-compose apply`. A waiver only
ever green-lights the words the gate held for that note; a word an edit introduced
is not covered and re-holds the note. On apply the staged folder is cleared and the
control note resets to its empty state.

### One-time setup for the apply link

The review note's **Apply approved notes** link fires the Obsidian *Shell commands*
plugin, which this skill cannot configure for the user. It is a one-time setup, and
the skill then finds the command on its own — no id to copy:

1. Obsidian → **Settings** → (Community plugins section, left column) → **Shell
   commands** — this opens the plugin's own settings tab.
2. Click **New shell command**. A command row appears; paste into its field:
   `python3 <absolute skill-directory>/scripts/vault-transcripts.py process --vault "<vault>" --apply --from-review`
   (It auto-saves. Optionally click the row's gear to set an **Alias** like
   "Apply approved inbox notes".)
3. That's it. On the next dry run the skill reads the plugin's config, finds the
   command whose text runs `--from-review`, and embeds its `execute` URI in the
   review note.

No id or environment variable is needed; `VAULT_TRANSCRIPTS_APPLY_COMMAND_ID` still
overrides the lookup if you want to set it explicitly. Until a matching command
exists, the review note prints the exact terminal command instead of the link, and
`doctor` reports the link is off. The plugin is desktop-only.

To run it without a clickable link at all, enable the command in the Obsidian
command palette from its Shell-commands settings and trigger it with `Cmd/Ctrl-P`
(or a hotkey) — the review note is still where you tick and edit; only the trigger
differs.

## A day of short memos as one log

Memos recorded through a day are fragments alone — one real recording is 61 words
and ends "I don't remember what it is" — and `process` makes one note per file, so
a day of thinking becomes a dozen notes that mean nothing apart. `daily` merges a
day and writes one note plus the recordings it was made from.

```bash
python3 <skill-directory>/scripts/vault-transcripts.py daily --vault <vault>
```

Grouping is deterministic and needs no model: same day, same recording type,
owner-authored, two or more. It is deliberately **not** content similarity — a
day of memos spans whatever the day held, and a similarity threshold splits
exactly the group a person would have made on purpose. `conversation`, `meeting`,
`therapy`, and `lecture` never group; each is a document in its own right.

The day's recordings are merged **in memory only**, each one's `*MM:SS*` offsets
rebased onto its filename's start stamp so the day reads on one clock. Cleanup
then reads the whole day as a single document, which is what makes `--tiny-words`
meaningful: a 61-word fragment gets no summary alone and a real edit as part of a
1,400-word day. A `--- recording s-0001 ---` divider marks each recording, and the
model returns a title, one summary paragraph, and topical sections with the
recordings each drew on. Everything that has to be exact is written in code: the
`(~09:36)` markers come from the filename stamps, the `## Source Recordings` list
from the group, and the `> [!provenance]-` block from the run.

Output is one journal note plus one source note per recording — seven files for
six recordings, not twelve — all left in `00 Inbox` for `vault-organizer` to file.
Dry-run by default; **get approval before `--apply`**.

A day where dedupe held any recording for review is not merged at all: a log
silently built from five of six is worse than no log, because nothing about it
looks wrong. Those days are reported and fall through to `process`.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--daily-min-recordings` | `2` | How many same-day recordings make a log. |
| `--scan` | `inbox` | `filed` also finds frontmatter-less exports already moved into the sources tree. |

This mode needs the vault's `0.04 Note Format.md` to declare a `### Block order`
table: the log is assembled in the order that note declares, and a vault that
declares none is told so rather than guessed at.

## Re-exports of recordings the vault already has

A transcription app re-exports: the same recording arrives in the inbox again,
sometimes a byte or two different, long after the note made from it was filed.
Processing those a second time grows a near-twin of every note the vault already
had. `reconcile` finds them by comparing the recording's text — never the
filename, which drifts — against every recording already in the vault.

```bash
python3 <skill-directory>/scripts/vault-transcripts.py reconcile --vault <vault>
```

A match moves to the recoverable quarantine at `.vault-transcripts/duplicates/`,
byte-intact and journaled. Anything that does not match stays exactly where it is
and is listed for the user: it may be a genuinely new recording, or it may be an
edited one worth looking at. Deterministic and offline, dry-run by default;
relay both lists and **get approval before `--apply`**.

## Reprocessing notes the pipeline already wrote

The recording never changes; what the pipeline made of it does. When the cleanup
register or the note layout changes, `reprocess` regenerates the summary,
reflection, and cleaned text of every filed transcript note from its recording.

```bash
python3 <skill-directory>/scripts/vault-transcripts.py reprocess --vault <vault>
```

It selects filed notes with frontmatter and a `# Transcript` marker, reading the
recording inline or through the link `split` left. **The inbox is left alone** —
those notes are `process`'s input. **Therapy recordings are excluded** by their
filename label, and a note the classifier reads as therapy while its name says
otherwise is held rather than reprocessed: the exclusion is allowed to be
over-cautious and never the reverse.

Three things survive untouched, and none of them are the pipeline's to decide:
the note's **name**, which every wikilink in the vault points at; its
**frontmatter**, byte for byte, because that is the organizer's classification;
and the **recording section**, reattached exactly as found.

Read `reprocess-report.md` — it shows each summary before and after, which is the
thing worth judging — then **get approval and rerun with `--apply --run
<run-directory>`**. Every rewritten note is backed up first.

## Splitting notes processed before the pair existed

Processing writes two notes: the note made from the recording, and the recording
under its own name, which the note links to. Notes processed before that keep
the recording inline under `# Transcript`. `split` moves it out, deterministically
and with every endpoint down — no model reads either half, because what the
recording is about was decided when the note was first processed.

```bash
python3 <skill-directory>/scripts/vault-transcripts.py split --vault <vault>
```

It plans one split per note that has frontmatter and a `# Transcript` marker
still holding text; already-split notes, raw inbox exports, and notes with no
marker are left alone. The recording's note is written straight into the sources
tree, inheriting the note's own `domain` and `subdomain` — a domain the schema
does not define holds the note back rather than being guessed at. A note that is
itself `type: source` becomes `type: note`: the recording it now points at is the
source.

Read `split-report.md`, relay the counts and the type conversions, **get explicit
approval, then `--apply`**. Every rewritten note is copied to `backup/` under the
run directory first, and the recording is written before the note is rewritten,
so an interruption leaves the original intact rather than the recording lost.

## Settings

These four are the user's taste, not defaults to re-litigate each run. They are
fingerprinted into a run, so a resumed run refuses to change them.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--filename-pattern` | `date-type-topic` | `2026-07-24 - Therapy - Facing Family Dynamics.md`. Also `date-topic` and `date-time-topic`. |
| `--summary-style` | `callout` | A `> [!summary]` callout. Also `paragraph` and `heading`. |
| `--speaker-policy` | `names` | Real names where the transcript or the roster justifies them, roles otherwise. Also `roles` and `generic`. See the reference doc. |
| `--tiny-words` / `--tiny-summary` | `120` / `omit` | Under 120 words: light cleanup, no summary. |
| `--voice` / `--no-voice` | vault policy / off | Select a policy note or explicitly disable it. |
| `--lexicon` / `--no-lexicon` | vault note / off | Select a speakers-and-terms note or disable corrections and the roster. |
| `--profile` / `--no-profile` | vault register / off | Select a personal-context register or disable the layer. |

`--owner <name>` is only consulted by `--speaker-policy roles`, where it lets the
recorder's own name through while other names stay generic.

## How long a run takes, and the two knobs that matter

Cleanup is generation-bound. The model emits roughly as much text as it reads, so
a run costs about `output tokens / tokens per second` and nothing else — chunk
size does not change the total. On a backend serving ~55 tokens/second, a
34-file inbox is around an hour, and an 18,000-word recording is about five
minutes on its own. That is the floor, not a symptom. Before treating a slow run
as a defect, divide the words by the rate; if they match, the run is working.

The one thing that changes the total is **which service a chunk is routed to**.
Single-speaker cleanup goes to `think`, which was measured far more faithful on
this stage (a 1.000 pass rate against 0.250 for the non-thinking profile) and
costs about 7x the wall time for it: on one real run, 30.4s per multi-speaker
chunk on `chat` against 219.8s per single-speaker chunk on `think`, because
`think` spends roughly ten tokens reasoning for every token it answers with. Solo
voice notes are therefore the slow ones by design. That trade is a routing
decision, not a defect, and it belongs in `forge_routing` rather than here.

Do not add a `max_tokens` ceiling to cleanup to bound this. It reads as an
obvious win and is not: a ceiling sized for the visible answer truncates
`think`'s reasoning into a hard failure, and the runaway it appears to prevent
does not exist — failed chunks average *less* time than successful ones on the
same service.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--jobs <n>` | `1` | Clean `n` files at once. Chunks inside a file stay serial — each is written against the tail of the last — so this only helps when several files are queued. The chat backend serves 2 slots, so `2` is the ceiling that helps, and it competes with interactive turns on the same server. |
| `--retry-failed` | off | Re-attempt chunks a previous run recorded as failed or held. A plain resume inherits them so it does not pay for the same derail twice, which also means the file can never change until this flag is passed. |
| `--auto-retry` | off | On an invented-words failure, spend the corrective retry on the thinking model straight away instead of holding the chunk for review (the pre-review-lane behaviour). |

A chunk is **held** when the cleaned text carries more content words the source
does not than the budget allows — the fabrication gate doing its job, the model
having reached for a better word than the speaker's. By default `process` does
**not** spend a corrective retry on it: that retry runs on the thinking model and
costs ~90–220s on a solo note, and a few invented words are usually a
mis-transcription fixed or a stutter smoothed. So the chunk keeps its best-effort
text, the note assembles, and it waits in the review lane below for you to waive,
edit, or reject. `--retry-failed` re-cleans a held chunk with the retry;
`--auto-retry` restores the old spend-it-immediately behaviour. A *structural*
defect (a kept timestamp, a stray heading, a speaker label on a solo note) is not
waivable and still gets its automatic retry.

When a run's thinking-model review shares the endpoint an interactive session is
using, a turn there can preempt the review ("background inference preempted by
interactive activity"). The run retries past brief bursts on its own; if it still
cannot proceed it exits with its state intact and the exact resume command in the
error — resume with `--run <run-directory>`, in the foreground or with the session
idle so the two do not compete for the endpoint.

Two input shapes worth knowing about. An export whose two-part timestamps are
really elapsed **minutes** (`*01:03*` = 63 minutes, which reads as 63 seconds under
`MM:SS`) is detected by its impossible speaking rate and read as `HH:MM` instead of
being held as corrupt; the report says it made that assumption. A speaker-labelled
export that carries **no timestamps** is reported in its own lane ("Looks Like A
Transcript — No Timestamps") and left alone rather than filed as a raw note;
`--unlabeled` cleans these verbatim, with no clock markers since there are none.

## Terms and speakers

`99 Meta/99.02 Schemas/0.02 Speakers and Terms.md` holds two tables the owner
edits. `## Terms` maps a correct spelling to the forms the transcriber produces;
`## Speakers` marks who turns up in recordings. Both are optional, and the
standalone `transcription` dictionary merges underneath the terms table.

Every person note under the directory's contacts folder joins the roster
automatically, at `sometimes`, with its role compiled from the note — so the
table only needs rows for what a note cannot say:

| `Appears` | Meaning |
| --- | --- |
| `always` | Offered on every recording. For the handful of recurring voices — a partner, a therapist, a standing one-to-one — where nobody says a name aloud. |
| `sometimes` | The default. Offered only when the recording mentions their name, an alias, or something close enough to be that name misheard. |
| `never` | Never proposed as a speaker. Their name still gets spelled correctly when they are talked *about*. |

Report the Lexicon section when it appears: what was corrected in code, who was
named from the roster, and the spellings the model fixed that are not recorded
yet. Offer to add those with `transcription`'s `dict add`, or as a row in the
note — either lands where the next run will find it.

## Rules

- Two ways to run. The **review-first** flow is dry-run by default and `--apply`
  needs approval — use it when the user wants to preview. The **autonomous** flow
  (`--autonomous`, and implied by `--note`) files routine work on its own and
  holds only the serious set — use it when the user wants the inbox handled. Match
  the flow to the request; when in doubt on a whole-inbox run, prefer review-first.
- Autonomy changes *who approves*, never *what is checked at write*: every
  auto-applied note still passes the same gates, is backed up, and recomputes from
  the bytes on disk before it is written. It only ever files what cleared; a
  serious failure is held, never quietly filed.
- Nothing is ever deleted. Duplicates go to `.vault-transcripts/duplicates/`,
  every rewritten note is copied into the run's `backup/` first, and renames are
  journaled to `renames.jsonl`.
- A processed note is written in place first and renamed afterwards, in two
  journalled steps, because a rename can rewrite links inside other notes and
  would otherwise invalidate their planning hashes. This is the rename that
  matters: a recording arrives named for when it happened and leaves named for
  what it says, and a changed basename is exactly what Obsidian's basename
  resolution cannot paper over. When Obsidian 1.12.7+ is running and
  "Automatically update internal links" is on, the rename goes through its CLI so
  every inbound link follows the note — each linking note backed up first,
  verified after, restored if anything but a link line changed. Otherwise the old
  name is left behind in whatever pointed at it, exactly as before, and the run
  says so. `--link-rewrite {auto,off,require}` chooses; see `docs/obsidian-cli.md`.
- The original transcription is always preserved verbatim under `# Transcript`.
  If a check cannot prove that, the note is held rather than written.
- The register is spoken-to-written: filler, false starts, repeated phrases, and
  unambiguous circumlocutions come out, and the speaker's voice, meaning, and
  meaningful hedges stay. **Therapy is the exception** and keeps the older,
  stricter contract — nothing condensed, weighted hesitation preserved.
- **A `meeting` is kept as concise minutes, not verbatim cleanup.** The recording
  is preserved and linked as its own source note, so the minutes may paraphrase
  and compress. Meetings are therefore exempt from the verbatim gate (added
  words, length ratio, rare-word retention, utterance-locatable) and from the
  verbatim fidelity review; only the structural checks and the note-level
  thinking review (title, summary, speaker names, no fabrication) apply. Only
  `meeting` summarizes — conversation and therapy stay verbatim, lecture keeps
  structured full content. `--auto-retry` / the invented-words review lane below
  concern the verbatim types; a meeting never lands in the held-for-invented lane.
- Everything the pipeline generates is a callout, above the speaker's words: the
  summary open, reflections and connections collapsed. `references/loom-notes.css`
  styles them; without it notes still read correctly.
- Notes held for review keep their original name and body. Relay them to the
  user; do not resolve them by rerunning with different options.
- Never touch a pair that shares a recording id but differs in content. One is
  usually a truncated re-export, and sometimes the truncated one is the copy
  carrying the user's handwritten notes. Neither contains the other.
- Leave verification on. `--no-verify` is for when the thinking backend is down,
  and the report then says plainly that nothing was reviewed.
- Re-running is safe: a processed note has frontmatter, so it is skipped rather
  than cleaned twice.
- Apply owner-authored policy only to a single-speaker `memo` or `journal`.
  Never imitate the owner's prose in meetings, conversations, or therapy
  sessions. Apply source-derived policy to lectures and confidently identified
  podcasts, videos, webinars, and other external sources. Hold ambiguous
  classification for review rather than guessing owner authorship.
- The roster names voices; it never counts them. An `always` cue reading "the
  second voice in home recordings" says who a second voice is when there is one,
  and the model otherwise reads it as a promise that there is one — splitting a
  solo memo's sign-off off under that name and holding the note. An owner-authored
  memo or journal that comes back with two speakers is therefore classified once
  more with the roster withheld, and only a second voice found unprompted holds it.
- Clean external sources as structured full content: remove filler and
  redundancy, regroup related passages, and add headings while preserving every
  substantive claim, example, qualification, and disagreement.
- After an owner memo's or journal's cleaned text, add its reflection, then the
  raw transcript. A journal gets non-empty `## Observations`,
  `## Interpretations`, `## Open questions`, and `## Connections`; a memo gets
  `## Context`, `## Open questions`, `## Next steps`, and `## Connections`. No
  other recording type gets a reflection, and reflection is excluded from
  transcript-fidelity comparisons.
- Reflection sources from the vault first. Material from outside the vault is
  admissible only as text this run actually read — a link in the recording, or a
  citation already imported into a vault note — and such a connection begins
  `Outside vault:` and carries that source's URL. Nothing is fetched at
  reflection time, so a fact the model merely recalls cannot be checked: it is
  dropped from the reflection and reported as dropped.

## Reference

`references/transcript-note-format.md` — the note layout, the fidelity
invariants, the per-type cleanup style, the speaker rules, and what each
deterministic check catches. Read it before changing a prompt in
`scripts/vault-transcripts.py`; the cleanup prompt is a copy of that contract and
the two have to agree.

## Personal context

`99 Meta/99.02 Schemas/0.03 Personal Context.md` is a register of small cards
about the owner — people, history, reading, health, working preferences. Each row
names a card note; only that note's `## Context` bullets ever enter a prompt.
Cards are capped at 700 characters each, so they stay condensed by construction.

Two gates decide whether a card may be injected, and they are asymmetric:

| Column | Meaning |
| --- | --- |
| `Tier` | `always` (every prompt the card is allowed in), `when-relevant` (only when the material contains a literal trigger), `on-request` (never automatic). |
| `Scope` | Whose material the card may sit beside: `universal`, `owner-authored`, `source-derived`. Blank means `owner-authored`. |
| `Applies` | Blank means anywhere the scope allows. Naming routes means the card is refused everywhere the pipeline has not *positively established* one of them. |

This skill asserts a route only for `journal` and `therapy` recordings, via
`TYPE_TO_ROUTES`. A meeting, conversation, lecture, or memo asserts nothing, so
every route-gated card is refused there with no per-card configuration — which is
how clinical and life-history material stays out of a work meeting.

The layer reaches the **summary** and **journal reflection** calls only. It is
deliberately absent from cleanup and classification: cleanup runs behind
`check_chunk`, which rejects a chunk containing words the source did not, so a
card naming someone would invite the model to write that name and the gate would
then discard the chunk. Classification is the call that *decides* the recording
type and material role, so nothing is established yet.

A missing, malformed, or unresolvable register never fails a run — it warns and
the run proceeds without the layer. Report the profile warnings when `doctor` or
a run surfaces them, and offer to add a card or a trigger when the model clearly
lacked context it could have had.

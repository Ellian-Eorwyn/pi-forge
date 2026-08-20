# Transcript Note Format

The contract for what `vault-transcripts` writes into a vault note, and how the
cleaned transcript is allowed to differ from the raw one. Parts A, B, and C are
embedded verbatim in `CLEANUP_SYSTEM` in `scripts/vault-transcripts.py`; if you
change one, change both.

Part A used to restate the invariants from
`../../transcript-cleanup/references/faithful-cleanup.md`. It no longer does. A
transcript kept in a vault is read as a note rather than consulted as a record —
the record is the recording, which now sits in its own note beside this one — so
this skill converts speech into written prose, and the standalone
`transcript-cleanup` skill keeps the more conservative contract that file
describes. The two are intentionally different, and that file is unaffected by
changes here.

## Note layout

Processing one recording writes **two** notes: the note made from it, and the
recording itself under its own name.

```markdown
---
type: meeting
status: raw
capture_type: meeting
processed_by:
  - "vault-transcripts"
---

> [!summary]
> One paragraph on what this recording was and what mattered in it.

> [!reflection]- Observations
> - <owner memo or journal only, non-empty sections only — see Part B2 for
>   which sections each type gets>

> [!reflection]- Open questions
> - ...

> [!connections]- Connections
> - [[A vault note]] — why it relates

<handwritten preamble, if the export had any, verbatim>

<cleaned transcript — headings ## or deeper>

# Transcript

[[<this note's name> - Transcript]]
```

and beside it, `<this note's name> - Transcript.md`:

```markdown
---
type: source
status: complete
parent: "[[<the note above>]]"
source_kind: transcript
capture_type: voice
---

<the original transcription, byte for byte>
```

Why two: a note about a recording is read at the length of what it says, not the
length of what was said, and the recording is a source in its own right — with
`type: source` and `source_kind: transcript` it files into the vault's sources
tree with every other source rather than riding along inside a note about it. A
vault whose schema note defines no `source` type or no `transcript` source kind
cannot describe the second note, so it keeps getting the single combined note
with the recording inline; every rule below holds for both shapes.

Rules the script enforces, not suggestions:

- The generated section contains exactly one level-one heading, `# Transcript`.
  Everything the cleanup writes is `##` or deeper.
- Everything the pipeline generates is a callout, and it all sits above the
  speaker's words: the summary open, every reflection and connections section
  collapsed. A reader can tell at a glance which prose is the speaker's and
  which is the machine's, which a `##` heading could not do — as headings the
  reflection sections were indistinguishable from ones the cleanup wrote. The
  handwritten preamble stays down with the cleaned text, because it is the
  owner's writing and not apparatus. `forge/lib/vault-format/loom-notes.css`
  styles these callout types; a vault without it still reads correctly, since
  folding is Markdown rather than CSS.
- **The callout types come from the vault's registry**, declared in
  `99 Meta/99.02 Schemas/0.04 Note Format.md` and checked by
  `forge/lib/vault_format.py`. This skill writes three of the nine — `summary`,
  `reflection`, `connections` — and a fourth would need a row there first, since
  an unregistered callout renders as stock blue with a pencil icon. The registry
  is not injected into the cleanup prompt; `render_callout` in
  `forge/lib/vault_reflection.py` applies the syntax after the checks pass.
- When a note is applied through the inbox review with invented words **waived**,
  a collapsed `> [!provenance]-` callout is added just above `# Transcript`,
  naming the words let through and that the owner approved them. `provenance` is
  already a registered callout, so this needs no schema change. It records a
  human's decision, not the cleanup's, and is added at apply time — after the
  gate is recomputed on the reviewed bytes — never by the cleanup prompt.
- Whichever note holds the recording holds all of it, byte for byte, including
  its handwritten preamble and any trailing text. The cleanup is a convenience;
  the transcription is the record.
- The transcript section of a processed note is exactly one wikilink and nothing
  else. That is what makes the pointer unambiguous on the way back in.
- The marker is also read on the way *in*. A note that already carries it has
  been through this pipeline before, so processing starts from what follows it —
  following the link when the section is one, exactly one level deep, since a
  recording that happens to contain a marker is a recording and not another
  pointer. A link resolving to nothing reads as no recording, which skips the
  note rather than processing a stub as if somebody had said it.
  Normally frontmatter makes a processed note skip entirely, but the two can come
  apart — strip the frontmatter off a processed note and the marker remains — and
  without this the leftover marker parses as handwritten preamble, gets copied
  into the generated section, and holds every note in the run for a level-one
  heading the cleanup never wrote.
- The recording's note carries no `processed_by`: nothing processed it, which is
  the whole point of it. `parent` is its only tie back to the note made from it,
  and `vault-organizer` carries `parent`, `type`, and `source_kind` forward when
  it files, since filing replaces frontmatter wholesale.
- `status: complete` on the recording. A verbatim record is finished the moment
  it is written; there is no later pass that revises it. A vault whose schema
  lacks that status gets `raw`.
- Frontmatter carries only `type`, `status: raw`, `capture_type`, and
  `processed_by`, all validated against the vault's schema note.
  `vault-organizer` replaces this block when it files the note and reads the
  first three as advisory hints, so they are accurate rather than complete.
  Domain, subdomain, project, and people are the organizer's judgment, not this
  skill's.
- `capture_type` stays the recording's own channel — `voice` or `meeting`. It
  records how the note entered the vault, which cleanup does not change, and
  which is what makes a cleaned transcript findable as a recording. What the
  pipeline *did* is `processed_by: ["vault-transcripts"]`: the cleaned body is
  substantially model-transformed, and a reader deserves to know that without
  losing how the note arrived. The organizer carries both forward when it
  refiles the note. A vault whose schema note has no `processed_by` property
  simply does not get the key.
- A note under `--tiny-words` (120 by default) gets no summary. The descriptive
  filename already carries the gist of a two-sentence reminder.
- Generated titles pass through `safe_title` in `forge/lib/vault_schema.py`, the
  one place every vault skill names notes. A title may never contain `#`, `^`,
  `[`, `]`, or `|`: Obsidian cannot resolve a `[[wikilink]]` to such a note and
  the file will not sync to mobile. Renaming a transcript therefore repairs an
  unsafe source name as a side effect.

One known consequence: `note_title` in `forge/lib/vault_schema.py` reads the
first level-one heading, so a processed note reports its title as "Transcript"
to other vault skills. The descriptive filename and the summary-first body are
what classification actually reads, so this costs nothing today — but it is the
reason to think twice before adding a second `#` heading.

## Part A — the register (every recording type)

These outrank every style rule below.

```text
- The register is spoken-to-written: what the speaker would have written had they
  typed this instead of saying it. Turn spoken delivery into clear, readable prose.
- Meaning comes first, not the exact words: you may rephrase and smooth for
  readability, but every claim, point, name, number, and shade of the speaker's
  meaning and intent must survive unchanged, in their own voice and register.
- Do not summarize. Output the full cleaned transcript.
```

- Remove filler and verbal scaffolding — "like", "um", "you know", "kind of",
  "sort of", "I mean", "basically", "essentially", "literally", "actually",
  "obviously", "honestly" — along with false starts, restarted sentences,
  repeated phrases, and self-echoes that carry no meaning.
- Condense a circumlocution into the plain statement it was reaching for, but
  only when the meaning is unambiguous. The calibration example: *"I would also
  like it to have, essentially, if it fails to categorize a note, there should be
  like a maybe in the system"* becomes *"I would also like a 'failed
  categorization' folder for notes it can't confidently categorize."* Two possible
  readings means keep the one that loses no meaning.
- Rephrasing is fine; inventing is not. The added-words check is a coarse backstop
  against a wholesale rewrite, not a ban on synonyms — the thinking verify pass
  reads for the fabrication a word count cannot tell from a paraphrase.
- Keep hedges that qualify a claim. "I think", "maybe", and "I don't know" mean
  something when they mark how sure the speaker is; the same words as pure
  delivery are filler.
- Never add facts, names, dates, conclusions, or numbers absent from the source,
  and never state something more certainly than the speaker did.
- Never drop substance, and never delete a whole utterance or exchange. Small
  talk survives, in its short readable form.
- Fixing transcription punctuation and casing is part of the job.
- Leave garbled or uncertain passages visible rather than repairing them by
  guessing. Keep `[unclear]`-style markers.
- Timestamps are dropped from the cleaned text; they remain in the recording.
- Tables only when the speaker is genuinely listing tabular data — never as
  decoration.

**Therapy is the exception** and keeps the older, stricter contract — the
speaker's exact words. Pure filler and false starts come out, but nothing is
condensed or swapped for a synonym, and hesitation and repetition that carries
weight stays. What a session is *for* is partly in how something was said, and
that is not delivery to be reshaped.

## Part B — style by recording type

| Type | Cleaned output |
| --- | --- |
| `memo` | Readable first-person prose — the note the speaker would have typed. `##` headings only when the memo clearly moves between several distinct topics. No speaker labels. |
| `journal` | Chronological first-person paragraphs in the writer's own register. Voice, emotion, and meaningful self-correction preserved; the filler and false starts a written entry would never have contained removed. |
| `conversation` | Dialogue as `**Name:** what they said` paragraphs, one per turn, each turn in readable written form. |
| `therapy` | As `conversation`, at the highest fidelity of all. Hesitation and repetition that carries weight is kept. No clinical language, interpretation, or diagnosis that was not spoken. |
| `meeting` | **Concise minutes, not a transcript** — the exception to the verbatim contract. Brief prose under `##` topic headings, paraphrased and compressed, attributing points to who made them where it matters; not turn-by-turn dialogue. Closing `## Decisions` and `## Action Items` bullets **only** when the recording contains explicit decisions or assignments; `Unassigned` and `Not stated` rather than an inferred owner or deadline. The verbatim recording is preserved and linked as its own source note. |
| `lecture` | `##` and `###` headings following the material, the lecturer's own examples kept, audience questions as dialogue. |
| `other` | Treat as `memo` if one voice, `conversation` if several. |

An external-source lecture, podcast, video, webinar, or other confidently
identified source receives structured full-content cleanup. The editor may
remove filler and redundancy, regroup related passages, and improve headings
and readability, but may not condense the material into study notes: every
substantive claim, example, qualification, and disagreement survives.

Owner-authored mode is valid only for a single-speaker memo or journal.
Conversation and therapy recordings retain their speaker-aware contracts and a
meeting becomes minutes; none imitate the owner's prose. Unknown material
receives no voice rules and stays reviewable.

For an owner journal, written text receives mechanical correction only. Spoken
text receives the spoken-to-written edit: filler, false starts, accidental
repetition, and unambiguous circumlocution are removed while emphasis,
meaningful self-correction, uncertainty, and sequence remain.

A note under the tiny threshold gets punctuation, casing, filler removal, and one
short paragraph. No headings, no lists, no restructuring.

## Part C — speakers

The transcriber re-labels every utterance and splits one voice into several
labels, so labels are an upper bound on how many people are talking, never an
answer. A 56-minute meeting in this corpus carries 640 speaker lines.

1. Consecutive blocks with the same label are merged into one turn
   **deterministically, in code**, before the model sees the chunk. This is not
   the model's job and it is not allowed to un-merge them.
2. A turn that continues the previous speaker's sentence mid-thought belongs to
   that speaker, even across a label change. This one is the model's judgment.
3. When the classifier reports one effective speaker, labels are dropped
   entirely and the recording is cleaned as a solo memo.
4. What a label is *called* is the `--speaker-policy` decision:

   | Policy | Behavior |
   | --- | --- |
   | `names` (default) | A real name when the transcript itself justifies it — someone was addressed by name, or introduced themselves — and the classifier was confident. Otherwise a role, otherwise `Speaker N` renumbered after merging. |
   | `roles` | Role words only (`Therapist`, `Interviewer`, `Instructor`), plus `--owner`'s own name. Never guesses at anyone else's name. |
   | `generic` | `Speaker N`, renumbered after merging. No relabelling. |

5. Labels the export already supplies as real names are kept under every policy.
   The source knew something the model would only be guessing at.
6. A name is never inferred from subject matter. Speaking about Gillian is not
   evidence that Gillian is speaking. **The roster does not weaken this rule.**
   It is a source of *identity* — who this voice is — never a source of *topic*,
   and never a source of *count*. A cue that places someone in a kind of
   recording ("the second voice in home recordings") says which voice is theirs
   when a second voice is there; it is not evidence that one is. How many people
   are talking is settled from the transcript before the roster is consulted.
   Left unguarded this is the corpus's largest source of held notes: an `always`
   entry promises a second voice, the model files a solo memo's trailing "um,
   yeah, that's pretty much it" under that name, and the recording is held for
   owner-authored material with two speakers. A classification that trips that
   check is asked once more with the roster withheld.
7. Under `names`, the roster is a third source of justification alongside the
   transcript, offered as `knownSpeakers` and used two ways:
   - **Spelling.** A name the transcript does state, in whatever form the
     transcriber heard, is written the way the vault files that person.
   - **Identity.** A roster entry's cue and role describe one particular voice.
     Which label is that voice is settled by what each label actually says —
     whose work, whose meeting, whose title. Because the owner knows who they
     record and the model does not, a roster identification is accepted at
     `medium` confidence where a transcript-only one needs `high`.

   Two deterministic gates stand behind that, both of them because the observed
   failure mode is a real person attached to the wrong label:
   - Only a name that was actually offered is accepted. Anything else claiming
     roster provenance is the model inventing a person.
   - The cited evidence must appear in the transcript. The roster settles who
     may be present; only the transcript settles which voice they are, and
     quoting the cue back proves that second step was never taken.

   Both failures drop the name to `unknown` with a warning. The thinking model
   is then told not to flag a roster name for being absent from the excerpt —
   that is what the roster is for — but to check the attribution like any other
   claim, and to say which voice a misplaced name belongs to.

The raw section always keeps the original labels, whatever the policy did.

## Part D — terms

Specialist vocabulary is mistranscribed the same way every time, and no context
recovers it. Corrections come in two tiers, and both leave the raw section alone:

1. **Recorded variants** are replaced in code, before the chunk reaches a model.
   Longest variant first, whole-word by default. This costs nothing and cannot
   go wrong, and because it happens before chunking, the corrected spelling is
   what the added-words check compares against.
2. **Near misses** — a canonical spelling that something in this chunk sounds
   like but is not — are offered to the model as `glossary`, with the words the
   transcriber produced. The model rewrites only where sound and sense both fit,
   and may never introduce an offered term into a passage that did not say it.
   Offered terms are added to the added-words allowance, since a correction is a
   new word by construction.

A near miss requires the same opening letter and a 0.72 similarity, calibrated
against real mistranscriptions: an engine mangles a term's vowels and endings,
almost never its first sound, and without that constraint "Lojong" matches the
ordinary word "jong" more strongly than it matches several of its own real
variants. Sampled utterances are corrected before the fidelity comparison, or
every successful correction would read as the cleanup drifting from the source.

Corrections the model made that are not yet recorded are proposed in the report,
so the next run can make them free.

## The reflection

Not part of the cleanup contract above: the reflection is a separate model call,
mirrored by `JOURNAL_REFLECTION_SYSTEM`, `MEMO_REFLECTION_SYSTEM`, and the shared
`REFLECTION_SOURCE_RULES` in `scripts/vault-transcripts.py`.

An owner memo and an owner journal each receive a generated reflection, rendered
as collapsed callouts above the cleaned authorial section. No other recording
type does. Empty sections are omitted, and a short recording legitimately gets
one section or none at all.

| Type | Sections, in order |
| --- | --- |
| `journal` | `Observations`, `Interpretations`, `Open questions`, `Connections` |
| `memo` | `Context`, `Open questions`, `Next steps`, `Connections` |

Each becomes `> [!reflection]- <Section>`, except `Connections`, which becomes
`> [!connections]- Connections`.

The journal set is introspective; the memo set is not. A memo is a working note —
a task, an idea, a plan, a thought caught before it was lost — so `Context` names
what it belongs to, `Next steps` names an action it implied without stating, and
nothing comments on the owner's state of mind. `Interpretations` on an errand list
is either empty or padding, which is why memos do not get it.

Where a connection may come from, in both sets:

- The vault first: a valid wikilink from hybrid search, checked against the notes
  that search actually returned.
- Otherwise, `outsideSources` — text this pipeline read, carrying the URL it came
  from. There are two ways such text exists without a network call: the owner put
  a link in the recording, or the material was researched earlier and imported
  into a vault note that kept its citations. Nothing is fetched at reflection
  time.
- A connection drawn from outside the vault begins `Outside vault:` and ends with
  its source's URL in parentheses.
- **A fact the model merely remembers is not admissible.** There is no way to
  check it, so a connection that cites nothing, or cites a URL this run never
  read, is dropped from the rendered reflection and reported as dropped. This is
  the one place the pipeline discards model output rather than holding the note:
  raising instead would cost the note its summary *and* its reflection over a
  single oversold line, and the exclusion is recorded either way.

The reflection is not part of transcript fidelity measurement. It is stripped
out of the passage the fidelity reviewer sees, along with the summary — both are
callouts, and cleanup never writes one — so a summary paraphrasing an utterance
can never answer for a cleanup that dropped it.

## What the checks enforce, and what the reviewer judges

Deterministic checks are exact and free, so the thinking model's budget goes to
judgment instead of catching malformed output. Since the 2026-08 overhaul they
split into hard structure and advisory measurement.

**Structural** faults are contract violations no judgment can waive. Any one of
them holds the note back with its original name and body — including a note the
reviewer itself revised, which gets exactly one corrective re-ask at deep
reasoning before holding (`structural_after_fix`):

| Check | Catches |
| --- | --- |
| Exactly one `# ` heading, and it is `# Transcript` | A body that wrote a document title. |
| Raw section byte-identical to the source body | Any drift in the thing that must not drift. |
| Preamble present in the generated section | Handwritten notes quietly eaten by the pipeline. |
| Summary shape: one paragraph, at most 120 words | A summary that grew into a second note. |
| Frontmatter keys and values against the schema note | Metadata the organizer would strip or reject. |
| Title charset, length, reserved names, and medium-words | Filenames that break Obsidian links or say nothing. |

Two former gates are now silent normalizers on the writer's output: a surviving
`*MM:SS*` line is dropped (the raw transcript keeps the clock) and a stray
level-one heading is demoted — neither is worth a model turn.

**Advisory measurements** are word-overlap proxies for lost content. They are
computed at assembly and travel to the reviewer as named suspicions, never as
holds — a lecture told to remove filler and regroup trips all of them without
losing anything a reader would miss:

| Measurement | Suspects |
| --- | --- |
| Added words, on prose lines only | Invention past the ceiling — or a synonym the editor reached for. |
| Rare-word retention | A dropped passage. Long infrequent words are content; filler is short and common. |
| Cleaned/source length ratio, `0.4`–`1.1` (`0.3` floor under the tiny threshold) | A cleanup that summarized instead of cleaning, or padded. |
| Sampled utterance containment | Passages that vanished, located by sliding window. |

**The review pass** is where judgment lives: one thinking call per note reads
the *whole* raw transcript beside the *whole* assembled note (advisories
attached), in the note's own register — verbatim for therapy, minutes for
meetings, spoken-to-written for the rest — and answers one of three ways:

- **ok** — the note tells the transcript's truth; it finishes.
- **fixed** — something real was dropped, invented, or misstated, and the
  transcript itself supplies the repair: the reviewer returns the complete
  corrected body (and summary when that was the fault), which is rebuilt through
  the structural checks above and finishes as `reviewed-fixed`.
- **hold** — the defect is in the source (garbled audio, wrong attribution in
  the export itself): the note holds with reason code `source_defect` and the
  reviewer's own sentence on what a human should look at.

This is the principle the whole skill rests on made uniform: no information
lost, not every word kept — and the entity that judges meaning is also allowed
to repair it, because holding a note over a fixable omission was the single
largest cost of the staged pipeline this replaced. The one exception is a run
with no reviewer (`--no-verify`, or an unreachable thinking service): there the
advisory floors re-arm as holds, because an unreviewed note must never read as
approved. A meeting, being minutes rather than verbatim cleanup, is exempt from
the overlap measurements entirely; its review judges coverage of decisions and
action items instead.

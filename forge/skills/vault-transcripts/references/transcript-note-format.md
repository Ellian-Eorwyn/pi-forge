# Transcript Note Format

The contract for what `vault-transcripts` writes into a vault note, and how the
cleaned transcript is allowed to differ from the raw one. Part A restates the
fidelity invariants from `../../transcript-cleanup/references/faithful-cleanup.md`,
which remains their source. Parts A, B, and C are also embedded verbatim in
`CLEANUP_SYSTEM` in `scripts/vault-transcripts.py`; if you change one, change
both.

## Note layout

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

<handwritten preamble, if the export had any, verbatim>

<cleaned transcript — headings ## or deeper>

<owner memo or journal reflection only, non-empty sections only — see Part B2 for
which sections each type gets>

# Transcript

<the original transcription, byte for byte>
```

Rules the script enforces, not suggestions:

- The generated section contains exactly one level-one heading, `# Transcript`.
  Everything the cleanup writes is `##` or deeper.
- Everything after `# Transcript` is the source body unchanged, including its
  handwritten preamble and any trailing text. The cleanup is a convenience; the
  transcription is the record.
- The marker is also read on the way *in*. A note that already carries it has
  been through this pipeline before, so processing starts from what follows it.
  Normally frontmatter makes such a note skip entirely, but the two can come
  apart — strip the frontmatter off a processed note and the marker remains — and
  without this the leftover marker parses as handwritten preamble, gets copied
  into the generated section, and holds every note in the run for a level-one
  heading the cleanup never wrote.
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

## Part A — fidelity invariants (every recording type)

These outrank every style rule below.

```text
- Change as little wording as possible while making the transcript clean and readable.
- Preserve the speaker's intent, uncertainty, and nuance.
- Do not summarize. Output the full cleaned transcript.
```

- Keep hedges. "I think", "maybe", and "I don't know" are content, not noise.
- Never add facts, names, dates, conclusions, or certainty absent from the source.
- Never drop substance. Every point made must survive.
- Removing filler, false starts, stutters, and verbal scaffolding is the job. So
  is fixing transcription punctuation and casing.
- Leave garbled or uncertain passages visible rather than repairing them by
  guessing. Keep `[unclear]`-style markers.
- Timestamps are dropped from the cleaned text; they remain in the raw section.
- Tables only when the speaker is genuinely listing tabular data — never as
  decoration.

## Part B — style by recording type

| Type | Cleaned output |
| --- | --- |
| `memo` | Flowing first-person paragraphs. `##` headings only when the memo clearly moves between several distinct topics. No speaker labels. |
| `journal` | Chronological paragraphs, minimal intervention. Voice, emotion, and self-correction preserved; only obvious filler removed. |
| `conversation` | Dialogue as `**Name:** what they said` paragraphs, one per turn. |
| `therapy` | As `conversation`, at the highest fidelity of all. Hesitation and repetition that carries weight is kept. No clinical language, interpretation, or diagnosis that was not spoken. |
| `meeting` | `##` heading per topic. Closing `## Decisions` and `## Action Items` bullets **only** when the recording contains explicit decisions or assignments; `Unassigned` and `Not stated` rather than an inferred owner or deadline. |
| `lecture` | `##` and `###` headings following the material, the lecturer's own examples kept, audience questions as dialogue. |
| `other` | Treat as `memo` if one voice, `conversation` if several. |

An external-source lecture, podcast, video, webinar, or other confidently
identified source receives structured full-content cleanup. The editor may
remove filler and redundancy, regroup related passages, and improve headings
and readability, but may not condense the material into study notes: every
substantive claim, example, qualification, and disagreement survives.

Owner-authored mode is valid only for a single-speaker memo or journal.
Conversation, therapy, and meeting recordings retain their speaker-aware
contracts and never imitate the owner's prose. Unknown material receives no
voice rules and stays reviewable.

For an owner journal, written text receives mechanical correction only. Spoken
text receives a light fidelity edit: filler, false starts, and accidental
repetition may be removed while emphasis, meaningful self-correction,
uncertainty, wording, and sequence remain.

A note under the tiny threshold gets punctuation, casing, and one short
paragraph. No headings, no lists, no restructuring.

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
   It is a source of *identity* — who this voice is — never a source of *topic*.
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

An owner memo and an owner journal each receive a generated reflection after the
cleaned authorial section. No other recording type does. Empty sections are
omitted, and a short recording legitimately gets one section or none at all.

| Type | Sections, in order |
| --- | --- |
| `journal` | `## Observations`, `## Interpretations`, `## Open questions`, `## Connections` |
| `memo` | `## Context`, `## Open questions`, `## Next steps`, `## Connections` |

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

The reflection is not part of transcript fidelity measurement.

## What the checks enforce before a model ever reviews the result

Deterministic, exact, and free, so the thinking model's budget goes to judgment
instead of catching malformed output. Any failure holds the note back with its
original name and body, and puts it in the run's review queue.

| Check | Catches |
| --- | --- |
| Added words, on prose lines only | Invention. A word was either spoken or it was not; heading text is exempt because the editor authors structure. |
| Rare-word retention | A dropped passage. Long infrequent words are content; filler is short and common. |
| Cleaned/source length ratio | A cleanup that summarized instead of cleaning, or padded. |
| Sampled utterance containment | Passages that vanished, located by sliding window rather than by re-reading the whole file. |
| Exactly one `# ` heading, and it is `# Transcript` | A cleanup that wrote a document title. |
| Raw section byte-identical to the source body | Any drift in the thing that must not drift. |
| Preamble present in the generated section | Handwritten notes quietly eaten by the pipeline. |
| Surviving `*MM:SS*` lines | Timestamps left in the cleaned text. |
| Frontmatter keys and values against the schema note | Metadata the organizer would strip or reject. |
| Title charset, length, reserved names, and medium-words | Filenames that break Obsidian links or say nothing. |

After those pass, the thinking model reviews every note's type, title, summary,
and speaker naming against excerpts of the raw transcript, plus a sample of
utterances checked against the cleaned text. It can flag, and a flag is either
redone with reasoning or handed to a human — it never silently drops a result,
and an unreachable reviewer is reported as "not verified" rather than treated as
approval.

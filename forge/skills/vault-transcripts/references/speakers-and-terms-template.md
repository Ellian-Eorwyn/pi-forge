---
type: system
status: active
domain: meta
subdomain: schemas
capture_type: manual
---

# Speakers and Terms

Two tables the transcript pipeline reads. Both are optional; delete the rows
below and keep the headings, or drop a section entirely.

Install as `99 Meta/99.02 Schemas/0.02 Speakers and Terms.md`.

## Terms

Specialist vocabulary a speech-to-text engine gets wrong. `Term` is how you
spell it; `Variants` are the forms you have actually seen it come out as,
comma-separated. Anything listed here is corrected in code, before a model reads
the transcript — free, exact, and logged.

You do not have to record every variant. A spelling in this table is also
offered to the model whenever a passage merely *sounds* like it, so a novel
mangling gets caught the first time and can be recorded afterwards.

`Kind` is `name`, `acronym`, or `term`, and is organizational only.

| Term | Variants | Kind | Notes |
| --- | --- | --- | --- |
| `Bodhicitta` | `Buddhic chitta`, `Buddhicitta`, `Buddhistta` | `term` | Awakening mind. |
| `CalNEXT` | `Cal Next`, `Cow Next` | `acronym` | Statewide emerging-technology program. |

## Speakers

Who turns up in your recordings. **Every person note in the directory's contacts
folder is already on this roster**, at `sometimes`, with their role read from
their note — so you only need a row here to say something the note cannot.

`Appears` decides when someone is worth showing the model:

- `always` — offered on every recording. Use it for the few recurring voices a
  transcript never names out loud: a partner, a therapist, a standing 1:1.
- `sometimes` — the default. Offered only when the recording mentions their
  name, an alias, or something close enough to be that name misheard.
- `never` — never proposed as a speaker. Their name is still spelled correctly
  when they are talked *about*. Use it for people you cite but never record.

`Aliases` are what people are *called out loud* — a nickname, a short form — not
mistranscriptions; those belong in `## Terms`. `Cue` is what identifies this
person's voice in a recording, and it is what lets a speaker be named when
nobody says a name aloud. Write it as a fact about the recording, not the topic:
"the other voice in my Thursday therapy session" works, "cares a lot about heat
pumps" does not.

| Person | Appears | Aliases | Cue |
| --- | --- | --- | --- |
| `[[Alexi Miller]]` | `sometimes` | `Alexi` | NBI colleague; joins CalNEXT calls. |
| `[[Alan K Meier]]` | `never` |  | Cited in lectures, never a speaker. |

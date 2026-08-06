# Composed note format

The contract between `scripts/vault-compose.py` and the notes it proposes. Read
this before changing a prompt in that script.

## The run spec

```json
{
  "version": 1,
  "intent": "synthesis" | "conversation" | "research",
  "request": "what the user asked for, in their words",
  "noteType": "note",
  "titleHint": null,
  "date": "2026-08-05",
  "maxNotes": 3,
  "sources": [
    {
      "kind": "vault-note",
      "label": "Codebook Consistency",
      "text": "<verbatim>",
      "url": null,
      "wikilink": "[[Codebook Consistency]]",
      "occurredAt": null,
      "origin": {"path": "04 Technology/Codebook Consistency.md"}
    }
  ]
}
```

| Field | Read by | Meaning |
| --- | --- | --- |
| `text` | every grounding check | The material verbatim. The only field a check reads. |
| `label` | the provenance block | What a citation is credited to. |
| `url` | the grounding check | A URL this unit licenses the note to cite. |
| `wikilink` | the grounding check | A `[[link]]` this unit licenses. |
| `origin` | the provenance block | Where it came from. Never read by a check. |

An `intent` accepts only certain `kind`s — `synthesis` takes `vault-note` and
`file`, `research` takes `web-claim` and `file`, `conversation` takes `chat`,
`file` and `vault-note` — so a spec that mixes a research claim into a synthesis
run is refused rather than quietly composed.

## Stages

| Stage | Service | What it does |
| --- | --- | --- |
| `load_spec` | none | Validate, hash, and fingerprint the source set. |
| `outline` | chat | One call. How many notes, what each is called, which blocks each gets, and which sources each block rests on. No prose. |
| `draft` | chat | One call per note. Plain lines per block. |
| gates | none | Everything below. |
| `render` | none | `vault_compose.render_note` in the declared block order. |
| `verify` | think | One batch. Flags claims the sources do not support. |
| `propose` | none | `report.md` with ids. Nothing is written. |
| `apply` | none | Writes only the accepted ids, exclusive-create. |

The outline/draft split exists for the same reason `vault-capture` splits its
own: a call that decides structure and a call that writes prose have different
failure modes, and mixing them means a bad structural choice arrives wearing
finished sentences.

## What the drafter may not write

Blocks come back as **plain lines**. Callout syntax, frontmatter, and `#` titles
are added by the renderer once a block has passed its checks. This is
`vault-capture`'s `fold_reflection` trick generalized, for the reason its
docstring gives: never put a deterministic check at the mercy of a model getting
`>` prefixes right.

Only `body` may carry `##` sub-headings.

## Deterministic checks

| Check | Holds? | Catches |
| --- | --- | --- |
| `ungrounded_specifics` names | yes | A capitalized mid-sentence token in no cited source. |
| `ungrounded_specifics` links | yes | A URL no cited source carries. |
| `ungrounded_specifics` wikilinks | yes | A `[[link]]` to a note no cited source names. |
| `uncertain_names`, `numbers` | no | Handed to the reviewer, which reads the sources anyway. |
| drafter-written structure | yes | Callouts, frontmatter, `#` titles, stray `##`. |
| length | yes | Under 40 words, or over 1200. |
| `check_grammar` errors | yes | Anything violating the vault's own block order. |
| `missing_required_properties` | **no** | Reported as a warning. Every composed note lacks `domain` by design. |

The per-block narrowing is the important one. A block is checked against the
sources *it* cited, not against the whole set, so a note whose sections are each
individually plausible cannot be collectively a collage.

This is the opposite of the choice `vault-transcripts daily` makes, and both are
right: a day of memos is merged and cleaned as one document *before* the model
sees any section boundary, so its citations attribute an already-unified text.
Here each source is held separately and quoted separately, so the citation is a
real claim about provenance and can be enforced as one.

## What code writes, never the model

- The `> [!provenance]-` block. `0.04 Note Format.md` requires it to be accurate
  about what made the note, and a model cannot be accurate about that.
- The frontmatter, from `INTENT_CAPTURE_TYPE` and the spec's date.
- The filename, from `safe_title` with a numbered suffix on collision.
- The block order, from the vault's declared grammar.

## Frontmatter

```yaml
type: <spec.noteType>
status: raw
capture_type: chat | generated
date: <spec.date>
```

No `domain`. Guessing one buries a note where nothing looks for it, and
`vault-organizer` reads the note to decide. `capture_type` records the *channel*;
the provenance block records the machine's hand. One property cannot answer both,
and overloading it leaves a reader unable to tell a research note from a
conversation without opening it.

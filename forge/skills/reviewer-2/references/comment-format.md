# Comment Format

The mechanical contract: the shape of `comments.json`, the callout grammar the
script renders it into, and what each deterministic check catches. The renderer
in `scripts/reviewer-2.py` is the implementation of this document; if you change
one, change both.

The judgment side — what deserves a comment, how hard to push, how to engage a
theoretical tradition — is `review-rubric.md`. This file is only about what is
representable.

## What the run produces

A new note in `00 Inbox` named `<Article title> - Reviewer 2 - <YYYY-MM-DD>`:

```markdown
---
type: note
status: raw
related:
  - "[[<the article>]]"
capture_type: generated
---
<the article body, byte for byte, with comment callouts between its blocks>

%% R2 review boundary <stamp> - content below is reviewer-generated %%

---

## Reviewer 2 · Meta Review
...
## Provenance
...
```

The article note itself is never opened for writing. There is no flag that makes
this skill edit it.

## comments.json

```json
{
  "schema_version": 1,
  "article": {"path": "<vault-relative path>", "body_sha256": "<from index.json>"},
  "research_runs": ["<absolute path to a web-research run directory>"],
  "comments": [
    {
      "id": "r-001",
      "anchor": "b-014",
      "category": "gap | evidence | logic | theory | structure | strength",
      "severity": "major | minor",
      "quoted_text": "the phrase in the anchored block this is about",
      "critique": "markdown prose engaging the argument",
      "fix": "what to do about it",
      "insert_text": "prose the author can paste in, with (Author, Year) citations",
      "citations": [
        {"key": "gieryn1999", "work": "doi:10.7208/9780226824420", "quote": "an exact quotation from the archived source"}
      ]
    }
  ],
  "meta": {
    "assessment": "one paragraph: what this article is trying to do and whether it does it",
    "weaknesses": [{"rank": 1, "text": "...", "comment_ids": ["r-004", "r-007"]}],
    "fix_plan": [{"step": 1, "text": "...", "comment_ids": ["r-004"]}]
  }
}
```

Fields, and what the script does with them:

| Field | Required | Notes |
| --- | --- | --- |
| `id` | yes | `r-NNN`, unique. Start at the `next_comment_id` that `index` reported, which is past any ids an earlier review left in the article. |
| `anchor` | yes | A block id from `index.json`. Blocks of kind `r2-comment` belong to an earlier review and cannot be anchored to. |
| `category` | yes | Fixed vocabulary. Decides the callout type and label. |
| `severity` | yes, except `strength` | `major` or `minor`. A `strength` takes none. |
| `quoted_text` | no | Must appear in the anchored block, compared with whitespace collapsed so a hard wrap does not fail. |
| `critique` | yes | Multi-paragraph markdown is fine. |
| `fix` | yes, except `strength` | The instruction. A criticism without one does not render. |
| `insert_text` | no | Omit for structural or broad comments. Not everything has a paste-ready fix. |
| `citations` | no | Each entry must resolve against a linked research run. |

## Citations

`work` is looked up in a register built from the runs listed in `research_runs`.
Accepted forms, matching how `web-research` keys a work:

- `doi:10.7208/9780226824420` — also accepts a full `https://doi.org/...` URL
- `pmid:12345678`
- `arxiv:2101.00001`
- `title:<normalized title>|<year>` — the `normalized_title` field in `works.jsonl`
- `work:w-0001` — the work id in an academic run
- `source:src-0001` — a source id in a deep run

A `work` that resolves to nothing is a hard error naming the comment. This is
the fabricated-citation firewall and it is the reason the skill is safe to point
at real scholarship: a reference cannot reach the review copy unless a research
run actually retrieved it.

Each resolved work carries an `evidence_level`:

| Level | What was read | What may be claimed |
| --- | --- | --- |
| `full_text` | The source text was retrieved and archived by a deep run | Anything the text supports, including direct quotation |
| `abstract` | An abstract came back from a catalogue provider | What the work is about, hedged |
| `metadata` | Title, authors, venue, year only | That the work exists and is relevant, hedged |

Below `full_text`, the comment must contain the exact phrase **verify against
full text** in its `critique` or its `fix`, and a `quote` on that citation is
refused outright. The check is literal because the failure it prevents is
specific: an author who pastes a confident sentence about what a book argues,
when all anyone read was its abstract, has been handed a mistake in their own
voice.

When a comment has `insert_text` and a citation, the cited year and one of the
cited author surnames must appear in that text. A citation nothing points at is
decoration.

`gap`, `evidence`, and `theory` comments may not offer `insert_text` without at
least one citation. `logic` and `structure` comments may: a bridge paragraph or
a restated thesis cites nothing.

## Callout grammar

Rendered per comment, inserted after its anchor block with a blank line on each
side:

```markdown
> [!r2-theory]- R2 r-003 · Theory · major
> On: “Most participants described professional astronomers as colleagues.”
>
> The finding is reported without the conceptual apparatus that would make it
> interesting.
>
> **What to do:** Situate the finding in the demarcation literature; verify
> against full text before quoting the argument.
>
> > [!quote]+ Suggested text
> > This pattern is what Gieryn (1999) calls boundary work.
> >
> > — cites (Gieryn, 1999)
```

| Category | Callout | Label |
| --- | --- | --- |
| `gap` | `[!r2-gap]` | Research gap |
| `evidence` | `[!r2-evidence]` | Thin evidence |
| `logic` | `[!r2-logic]` | Logic |
| `theory` | `[!r2-theory]` | Theory |
| `structure` | `[!r2-structure]` | Structure |
| `strength` | `[!r2-strength]` | Strength |

The types are prefixed because a review comment is apparatus about an article,
not content in a note. The vault's own callout registry
(`99 Meta/99.02 Schemas/0.04 Note Format.md`) reads the unprefixed names as the
latter, and this skill used to borrow them: `structure` rendered as `[!abstract]`
put a criticism of an article in the same cyan that means "here is what this note
is", and `strength` took the green that means "sourced and checkable". The prefix
keeps the two vocabularies from colliding. `vault_format.py` knows `r2-` is a
namespace and exempts it from the note registry;
`forge/lib/vault-format/loom-notes.css` styles it as a left bar rather than a
filled box, so the article's own prose keeps the weight on the page.

Review copies rendered before the rename carry the unprefixed types. The strip
grammar reads both, so they still round-trip; they render stock until the review
is regenerated.

The outer callout is collapsed (`-`) so the article still reads as an article;
the reader opens the ones they want. The suggested-text callout is expanded
(`+`) because that is the part they came for. Both are ordinary Obsidian
callouts, so a theme that does not style nested ones still shows a readable
blockquote.

A comment the thinking model flagged gains one line as the first line of its
body:

```markdown
> **Flagged in verification:** <the reviewer's objection>
```

Flagged comments are still rendered. Verification is advisory about quality: it
can mark something and put it first in the report, it never deletes it.

## The round trip

The marker line is the strip anchor:

```
^> \[!(r2-gap|r2-evidence|r2-logic|r2-theory|r2-structure|r2-strength
      |question|warning|failure|example|abstract|success)\]- R2 (r-\d{3}) · <label>( · (major|minor))?$
```

The alternation is built from `CATEGORIES` plus `LEGACY_CALLOUTS`, so it always
covers what this version writes and what earlier versions wrote. Dropping the
legacy half would leave an older review copy's comments unrecognised, and the
strip is the only thing separating them from the author's own prose.

Every line of a comment starts with `>`, every insertion is wrapped in blank
lines, and the appended tail begins with a unique `%% R2 review boundary %%`
line. Together those make the operation exactly reversible, and before anything
is written the script proves it: it strips this run's comment ids back out of
the rendered copy and compares the result to the original body byte for byte. A
render that cannot reproduce the article does not write a file.

Stripping is scoped to one run's ids, which is what lets a second review of an
already-reviewed draft still prove it changed nothing — the earlier reviewer's
blocks are ordinary body text to this one. The `strip` subcommand is the
unscoped version, for an author who has taken the advice and wants a clean
draft back.

Two consequences worth knowing:

- Text inside a fenced code block is invisible to all of this. An article may
  quote this comment syntax in a code fence and nothing will happen to it.
- No comment field may contain a line that would itself render as a marker or
  contain the boundary string. That is checked and refused rather than escaped.

## Meta review

```markdown
## Reviewer 2 · Meta Review

<assessment: one paragraph>

### Biggest weaknesses

1. <weakness> (r-004, r-007)

### Fix plan

1. <step> (r-004)

### References

- <script-generated, one line per cited work, deduplicated>

## Provenance

- Source note, source body SHA-256, review run, research runs, verification counts
```

`fix_plan` is required. A review that lists complaints without an order of
operations leaves the author to work out which fix invalidates which, and that
is the work a reviewer is supposed to have done. A `major` comment missing from
the plan is a warning, not an error.

References and provenance are generated by the script from the register, never
written by hand. The model writes the judgment; code writes everything that has
to be exact.

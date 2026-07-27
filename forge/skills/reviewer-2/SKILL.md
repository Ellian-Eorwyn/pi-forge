---
name: reviewer-2
description: Peer-review a scholarly article note in an Obsidian vault the way a constructive Reviewer 2 would - find research gaps, thin evidence, faulty inferences, structural problems, and shallow theoretical engagement, back each criticism with real literature from research runs, and write a separate review-copy note whose comments carry the exact prose the author can paste in, plus a ranked meta review and a step-by-step revision plan. Use when the user says review my article, be reviewer 2 on this, peer review this draft, critique my paper, tear this apart, or what would a reviewer say. Substance only - this skill never comments on grammar or prose style and never modifies the article. For literature search with no critique attached use web-research; run vault-organizer afterwards to file the review copy.
---

# Reviewer 2

Reviewer 2 is a joke about a real failure. The objection is often right; it
arrives without the remedy, so the author is told they are wrong and left to
guess what right would look like. This skill keeps the criticality and supplies
the missing half: every criticism names its fix, and where the fix needs
literature, the literature is real and was actually retrieved.

You write the review. That is not an implementation detail — a critique is a
deliverable, and a local model asked to be incisive about an argument produces
plausible-sounding nothing. The script is the machinery around your judgment: it
splits the article into anchorable blocks, refuses comments that misquote it,
refuses citations no research run contains, batches your comments past the
thinking model for review, and renders the result.

Nothing here can modify the article. The output is a **new note** — the article's
body reproduced byte for byte with comment callouts between its paragraphs, a
meta review appended, and a fix plan to work through in order. Because it only
ever creates a file, it needs no approval gate the way `vault-organizer` does.
The guarantee is mechanical rather than promised: before anything is written,
the rendered copy has this run's comments stripped back out and the result must
equal the original body byte for byte, or nothing is written at all.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path, and the vault
   path from the user or the injected vault context.
2. Check the environment before a first run:

   ```bash
   python3 <skill-directory>/scripts/reviewer-2.py doctor --vault <vault>
   ```

   This reports whether the vault and inbox are writable, whether the schema
   note defines what a generated note must say, and whether the thinking service
   answers. `render` refuses to write without it unless you pass `--no-verify`.
3. Index the article:

   ```bash
   python3 <skill-directory>/scripts/reviewer-2.py index "<article.md>" --vault <vault>
   ```

   This creates the run and reports the block ids you anchor comments to, the
   heading outline, and the first comment id to use.
4. **Read the article and reconstruct its argument before writing anything.**
   What is the central claim, what would have to be true for it to hold, what
   does the article offer as reason to believe it. A review written while
   reading finds sentence-level problems; a review written after this finds the
   problem that makes half of those symptoms. `references/review-rubric.md` is
   the standard to hold.
5. Optionally census the empirical claims that carry no citation, which is the
   category a close reading misses most often:

   ```bash
   python3 <skill-directory>/scripts/reviewer-2.py inventory --run <run-directory>
   ```
6. **For every criticism that turns on what the literature says, go and find
   out.** Run `web-research academic "<query>"` for scholarly works, and
   `web-research deep` when the point needs what a source actually argues rather
   than that it exists. Cite only what those runs returned; a citation that
   resolves to nothing is refused, and that is deliberate.
7. Author the comments file at `forge-output/reviewer-2/<slug>/comments.json`,
   following `references/comment-format.md`. Then render:

   ```bash
   python3 <skill-directory>/scripts/reviewer-2.py render --run <run-directory> --comments <comments.json>
   ```

   Use `--dry-run` first on an unfamiliar article: the copy lands in the run
   directory and the vault is untouched. Validation reports every problem at
   once, so fix them in one pass rather than one per attempt.
8. Read `report.md` and tell the user:
   - the overall assessment and the ranked weaknesses, in your own words
   - comments the thinking model flagged, **first**, with its objection — these
     are in the review copy, marked, and are the ones to check
   - how many comments there are, by severity and category
   - where the review copy is, and that the article was not modified
   - which criticisms carry suggested text and which are structural
9. Offer `vault-organizer inbox` to file the review copy, and `web-research` for
   any thread the review opened and did not close.

## Settings

| Flag | Default | Meaning |
| --- | --- | --- |
| `--dry-run` | off | Render into the run directory without adding a note to the vault. |
| `--no-verify` | off | Skip the thinking-model review of the comments. |
| `--schema` | the vault's | The schema note. |
| `--limit` | all | Blocks the claim census examines. |
| `--think-url`, `--think-model` | connectedServices | The service that reviews the comments. |
| `--base-url`, `--model` | connectedServices | The bulk service, used only by `inventory`. |

`strip <review-copy.md>` returns the article with every comment removed, for
when the author has taken the advice and wants a clean draft back.

## Rules

- Never edit, move, or rename the article. The review copy is the only output,
  and if the user wants changes made to the draft itself, that is a vault edit
  and goes through the skills that gate them.
- Never cite a work that is not in a linked research run. If a point is worth
  citing, it is worth a research run; if the search found nothing, say the
  literature is thin rather than inventing a source that sounds right.
- Never comment on grammar, spelling, or prose style. If the only problems are
  stylistic, say the article is substantively sound and stop — that is a real
  review outcome, not a failure to find enough.
- Every criticism names its fix. Where the fix is a sentence, write the sentence
  in the article's register; where it is a move, describe the move and skip the
  suggested text. Strengths need no fix.
- When only an abstract or catalogue metadata was read, hedge and tell the
  author to verify against full text. The script enforces the words; the honesty
  is the point.
- Leave verification on. `--no-verify` is for when the thinking backend is down,
  and the report and the note then say plainly that nothing was reviewed. That
  is not the same as approval.
- Criticize the argument, never the author. They are not in the room and cannot
  answer, which is exactly why the register matters.
- Do not review a review copy. Re-review the revised draft instead; comments
  from an earlier round are kept verbatim and cannot be anchored to, and new
  ids continue past them.
- Expect `vault-connections` to see the review copy as a near-duplicate of the
  article, because it contains the article. Explain it rather than acting on it.

## Verification

Every comment is reviewed by the thinking model in batches before anything is
written. Each item carries the full text of the passage the comment is anchored
to and the real metadata, abstracts, and archived quotations of everything it
cites — a reviewer shown a paraphrase has nothing to check against and approves
whatever it is given.

It flags a comment that misreads its passage, that is really about style, that
attributes to a source something the source cannot support, that states thin
evidence confidently, or whose fix does not address its own critique. It is told
not to flag a comment for phrasing, severity, or theoretical disagreement.

A flagged comment is still written, marked in the note and listed first in the
report. Verification is advisory about quality: it can mark something and hand
it to a human, it never deletes it.

## Reference

`references/comment-format.md` — the comments file, the callout grammar, the
citation register, and what each deterministic check catches. Read it before
changing the renderer; the two have to agree.

`references/review-rubric.md` — what deserves a comment, how the evidence norms
of quantitative, qualitative, historical, interpretive, and normative work
differ, how to write the fix, and the tone. Read it before writing the review.

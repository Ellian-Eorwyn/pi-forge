# Schema proposal contract

How a run turns "I want to catalogue X" into rows that can be pasted into the
owner's schema note without anyone having to trust the model that drafted them.

## The problem this is shaped around

`docs/service-split-handoff.md` §2.1 measured what moving work to the
non-thinking service costs. It is not volume and it is not accuracy: it is
**breadth**. The same corpus, contract, and temperature produced 17 items across
8 categories on the thinking backend and 17 across 4 on the non-thinking one. The
model settled for the obvious categories and never considered the rest.

Schema design is that failure mode's ideal habitat. Asked how to catalogue
anything, a model reaches for identification, description, and classification,
and never asks who governs the names, where the specimen came from, what scale
the condition is graded on, or whether a dated record is a different kind of note
from a reference card. Every one of those is where a real schema decision lives.

Four mechanisms carry the load, and none is the model reasoning better.

## 1. The enumeration is shipped

`references/catalog-dimensions.json` holds fourteen dimensions. The model is
handed all of them and decides which apply and what the field calls each; it
never produces the list.

`enumeration_clause` goes into the frame prompt verbatim, and it names the
dimensions that get *skipped* rather than only listing the full set — that
phrasing, not the list, is what moved §2.1's numbers.

**A dimension the model drops is restored as applicable**, with a warning. The
whole reason for shipping a list is that omission is the failure; treating a
silent omission as "does not apply" would reintroduce it exactly where it cannot
be seen.

`often_missed` marks the ones a model reaches for last. They are the reason the
file exists: naming authority, provenance, condition scales, measurement units,
external registry identifiers, legal and ethical constraints, the
reference-versus-event split, and retention.

## 2. The practice is fetched, or there is no proposal

Research shells out to `web-research deep`, which returns a claim register with
archived source text and runs its own verification pass. Python in this repo has
no HTTP client — `vault_schema.py` and `forge_llm.py` both commit to the standard
library — so the subprocess bridge in `vault-wiki.py` is the seam, reused rather
than rebuilt.

Practice extraction is one bounded call per applicable dimension. The claim
register is shared across those calls and the dimension is what varies, so the
system prompt stays byte-stable and the server's prefix cache stays warm.

**A stated practice citing no claim is dropped**, with a warning, and reported as
a dimension the research did not reach. That is the deterministic version of "do
not answer from your weights", and it is the one rule that makes the whole
pipeline trustworthy with a weak model. An empty practice is a legitimate,
useful answer and is reported as one.

With no network, no `web-research`, or `--no-web`, the run completes and proposes
nothing. The report says so in those words.

## 3. The moves are closed

Eleven moves, listed in `SKILL.md`. Two properties of the set matter more than
its contents:

- **`approved-property` is absent.** The vault's property list is global and
  closed, so a new property is inherited by every note type and a nested value is
  stripped on the next filing pass. This is why phenology is a body table. The
  model is told this by name in `notAvailable`, because a model asked for "good
  metadata" and not told otherwise proposes a property every time.
- **Numbers are not in the contract.** The model supplies value, label, and
  definition. `free_numbers` supplies the number, and a subdomain's free list
  excludes the numbers domain-level projects already hold, because
  `<domain>.<nn>` is one namespace shared by both.

The reconcile prompt carries its own §2.1 clause naming the moves that get
skipped — `already-covered`, `body-table`, `topic-hub` — because those are the
correct answers a model reaching for novelty will not offer.

### The topic-hub rule

The schema note's own change policy says a new recurring area "should usually
begin as topic hubs" and be promoted "only when it is stable, recurring, and
useful as a storage boundary". That is encoded rather than left to the model:

- **≥ `SUBDOMAIN_NOTE_THRESHOLD` notes already on the route** → keep `domain` or
  `subdomain`. Volume is the evidence that it is a storage boundary.
- **the field keeps dated event records** (the `record_split` dimension came back
  with an established practice) → keep it. Such records accumulate by
  construction, so the route is opened before the notes exist.
- **neither** → demote to `topic-hub`, with the count and the threshold in the
  reason.

The second clause exists because a pure note-count rule would have refused the
`nature` domain, which was correct and had zero notes when it was proposed.

## 4. Nothing is proposed that has not been proved

`prove_candidate` builds a candidate schema note with `candidate_schema_text` and
puts it through the real compiler:

1. the insertions apply cleanly (`insert_schema_row` refuses a duplicate value,
   a missing column, an unsafe label);
2. `parse_schema_note` parses it — this catches duplicate and reserved numbers,
   unknown parents, and malformed rows;
3. `validate_derived_paths` finds no two routes compiling to one folder;
4. `check_schema_drift` against the real vault raises no `high` finding that was
   not in the baseline captured during survey.

Proposals are proved cumulatively, then **the whole accepted set is proved
together**. Two rows can each be legal against the original note and collide with
each other; without the second pass that lands in the owner's file.

A drafted wiki kind is proved too: the spec goes through
`vault_wiki.validate_proposed_kind_spec`, the template is rendered from it, and
`vault_wiki.template_spec_drift` proves the two agree — the same check
`vault-wiki doctor` runs on installed templates.

`apply` re-proves against the schema note **as it is at that moment**. A row
proved against a note the owner has since edited is not proved.

## Phases and run layout

`propose` is one resumable run over `forge/lib/run_state.py`.

| Phase | Service | Output |
| --- | --- | --- |
| `frame` | chat | `brief.json` — field, cluster, applicable dimensions, queries |
| `research` | node subprocess | `research/` — a full `web-research deep` run |
| `practices` | chat, one call per dimension | `practices.jsonl` |
| `survey` | none | `survey.json` — routes, note counts, free numbers, drift baseline |
| `reconcile` | chat, one call per practice | `moves.jsonl` |
| `draft` | chat, one call per new wiki kind | in-memory, proved before it lands |
| `validate` | none | `validation.json` — the insertions and what was held |
| `verify` | think, `forge_verify` | `verified.jsonl` |
| `report` | none | `report.md`, `handoff.md`, `proposals.jsonl`, `patches/` |

```
<vault>/.vault-curator/
  cache/                  compiled schema, keyed by the schema note's SHA-256
  decisions.jsonl         every accepted and rejected proposal key, appended forever
  runs/<timestamp>/
    run_state.json  run_events.jsonl
    brief.json  claims.json  practices.jsonl  survey.json  moves.jsonl
    research/             the web-research run, with its own run state
    proposals.jsonl  validation.json  verified.jsonl
    patches/  report.md  handoff.md  backup/  apply-log.jsonl
```

Proposal ids are `s-NNN` for the schema side and `r-NNN` for the repo side. The
`key` a decision is recorded under is a hash of subject, move, and value rather
than the id, so a rejected proposal stays rejected across runs even though the
ids renumber.

## Verification prompt

The verifier is told what does **not** justify a flag, per §2.4 — without that
clause a reviewer drifts toward flagging taste, which floods escalation with work
that changes nothing. Here it is: *a defensible design is ok even if you would
have named it differently; a terse definition is ok; a move one level higher or
lower is ok unless the practice contradicts it.*

Flagged proposals are still proposed with the objection attached. Nothing is
dropped for being doubted.

## Calibration constants

First guesses. Both `vault-transcripts` and `vault-capture` had constants that
real runs disproved; expect the same here, and record what moved them.

| Constant | Value | Why |
| --- | --- | --- |
| `SUBDOMAIN_NOTE_THRESHOLD` | 12 | Below this a new route is a topic hub. Wholly untested against a real run. |
| `MAX_QUERIES` | 12 | The model's four, plus four templated, plus three site-restricted. |
| `MAX_CLAIMS_PER_CALL` | 60 | The register is repeated in every practice call, so it has to fit beside the contract. |
| `CLAIM_EXCERPT_CHARS` | 400 | Enough to judge a claim, short enough to send 60 of them. |
| `MAX_DEFINITION_CHARS` | 180 | A schema definition is one sentence; the existing rows average well under this. |
| `MAX_GUIDANCE_CHARS` | 320 | Matches vault-wiki's "one or two lines" prose budget. |
| `MAX_KIND_SECTIONS` | 8 | Past this a card stops being a card. The shipped ten run 4–7. |

## What this skill cannot do, and why

| Refused | Reason |
| --- | --- |
| Add a frontmatter property | Global and closed; inherited by every note type, nested values stripped on filing. |
| Register a project | A judgment about grouping files, not a fact about a field. |
| Renumber anything | `vault-organizer renumber`, and the owner's call. |
| Edit a row's label or definition | The owner's edit. Nothing here can reach an existing row. |
| Write repo files | Patches go under `patches/`; the repo is reviewed like any other code. |
| Register a new wiki kind end to end | `WIKI_KIND_SUBDOMAIN`, `WIKI_KIND_TYPE`, and `WIKI_TEMPLATE_NAMES` in `forge/lib/vault_wiki.py` are code. The handoff names the three lines. |

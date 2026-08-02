# Wiki note format

The shape of a wiki entity note, and the contract governing which parts of one a
generator may write.

## What a wiki note is

A reference card. It defines a thing so other notes can link to it, and it is
deliberately not an essay: the vault schema says a wiki note "exists so other
notes can link to it, and it defines the thing rather than developing your own
thinking about it." Your own thinking about a concept stays a `type: note` in its
topical domain. That constraint is why every length budget here is small.

## The seven kinds

Kind resolves from **subdomain**, never from `type`. Seven kinds collapse into
four note types — a practice and a term both file as `concept`, a figure as
`person` — so `type` cannot tell them apart.

| Kind | Subdomain | `type` | Template |
| --- | --- | --- | --- |
| concept | `concepts` | `concept` | `Wiki Concept.md` |
| practice | `practices` | `concept` | `Wiki Practice.md` |
| place | `places` | `place` | `Wiki Place.md` |
| event | `events` | `event` | `Wiki Event.md` |
| term | `terms` | `concept` | `Wiki Term.md` |
| work | `works` | `work` | `Wiki Work.md` |
| figure | `figures` | `person` | `Wiki Figure.md` |

## Body shape

Every kind has the same spine: a level-one title, a lead paragraph that defines
the subject, the kind's own sections, `## Sources`, `## Notes`, and a trailing
footnote block. The per-kind sections, their aliases, their fill modes, and their
budgets are in [wiki-kinds.json](wiki-kinds.json), which is also the prompt text
sent to the drafting model — the `guidance` strings are what the model reads.

The lead is rendered as an `> [!abstract]` callout. A wiki note is skimmed before
it is read, and the lead is the sentence that decides whether to keep reading, so
it is worth setting apart. Wrapping is idempotent — the lead is rewritten on
every expansion — and a note whose lead is still plain prose gets the callout the
next time it is expanded. `forge/lib/vault-format/loom-notes.css`
styles it alongside the callouts the transcript pipeline writes; without that
snippet it renders as a stock Obsidian abstract callout, which is fine.

`abstract` is Obsidian's own name for the callout the vault's registry
(`99 Meta/99.02 Schemas/0.04 Note Format.md`) calls `summary`; they are one type
and render identically, and `vault_format.ALIASES` folds one onto the other.

The registry's other blocks — `key`, `define`, `evidence`, `caution`, `question` —
are **not** used in card sections, and the section spine stays plain `##`
headings. Ownership here is by visible heading text, and
`assert_only_managed_changed` byte-compares every unmanaged section across 467
notes, so a section that became a callout would stop being findable and the merge
would refuse the note. Adopting callouts per section is a `render` key in
[wiki-kinds.json](wiki-kinds.json) plus a parser that accepts a callout title as
a section anchor, and it is deliberately not done here.

The schema's approved-property list is closed and strips anything else, so no
per-kind structured data can live in frontmatter. A figure's lifespan, a work's
year, and a place's region belong in the lead sentence, which is where a reader
wants them anyway.

## Section ownership

Ownership is by **visible heading text**, not by injected markers. A marker that
drifts leaves a generator writing into the wrong place, and a note full of HTML
comments is worse to read.

- A section the spec declares and the note has: content replaced, **its own
  heading line kept**. A note saying `## Key Ideas` where the spec says
  `## Key Points` is recognized through the alias list and updated in place
  instead of growing a second near-identical heading.
- A section the spec declares and the note lacks: inserted at its spec position,
  relative to the declared sections already present.
- Any other heading — `## Notes`, `## Key Texts`, `## Terms`, anything at all —
  keeps its heading, its content, and its position.
- An existing section is **never moved or renamed**. Reordering a note the owner
  arranged is a change they did not ask for.
- `## Notes` is owner-authored: never written, never read, and never quoted back
  as pipeline output. It arrives blank in a new note as a slot to write into.

`assert_only_managed_changed` re-parses the merged body and byte-compares the
title and every unmanaged section, in order, against the original. A mismatch
**refuses that note**; it is not a warning. That check is the safety net under
everything else, and it turns a merge bug into one skipped note rather than
overwritten prose.

One honest caveat on "byte for byte": the run of blank lines that *separates* two
sections is not preserved, because inserting a heading requires putting a blank
line in front of it. Every non-blank line, and all whitespace inside a section,
is preserved exactly.

## Citations

`## Sources` holds a plain bullet list of links. It renders in both reading and
source mode and it is what a reader skims.

Footnote **definitions** go in an unheaded block at the very end of the file.
Obsidian hoists them into its own rendered footnote area, so putting them *under*
`## Sources` would leave a visibly empty heading in reading mode. The full URL
appears once, in `## Sources`; each footnote is a short locator.

Because the definitions trail the last heading, in source mode they sit visually
below `## Notes` on notes that keep `## Notes` last — which most existing notes
do. This is cosmetic: the parser peels the footnote block before section parsing,
so the definitions are never part of the `## Notes` block, and the ownership
check confirms it.

Markers are deduplicated to one per source per section. With a single source,
marking every bullet `[^1]` teaches the reader nothing after the first and ruins
the skim; one marker attributes the section, and `## Sources` carries the
reference.

## Deterministic checks

These run before either model, because a byte-exact invariant beats a model's
opinion and costs nothing.

1. **Citation reachability.** A URL that this run did not fetch and hash cannot
   appear. The primary anti-fabrication gate.
2. **Quote exactness.** Quoted text of five words or more must appear in an
   archived source, after whitespace normalization.
3. **Footnote integrity.** Every marker has a citation entry, every entry is
   referenced, and every entry names an archived source.
4. **Invented dates.** A year in the draft must appear in a source or in the
   note's existing text.
5. **Length budgets.** Per section, per bullet, and per kind. Over budget holds
   the note back; nothing is silently truncated.
6. **Link resolution.** A `[[Target]]` with no note behind it is unwrapped to
   plain text and reported — the sentence survives, the dead link does not.
7. **Section ownership.** The merge invariant above.

Two formatting misses are repaired rather than rejected, because the content is
right and only the punctuation is wrong: a bullet section returned as bare lines
gets its markers added, and footnote markers are moved tight against the end of
their sentence. A section returned as one long paragraph is *not* repaired — that
is a content problem, and the budget check still catches it.

Grounded checks (2 and 4) are skipped when a run has no sources, because they
would reject every draft on its first date. Such a run is marked uncited and
**refused at apply**: skipping a check honestly is fine, faking one is not.

## Source policy

[canonical-sources.json](canonical-sources.json) is editorial policy — who to
trust — and is separate from `web-research`'s `domain-strategies.json`, which is
about how to fetch a given host.

A URL is never guessed. SEP entry slugs are topic-based rather than name-based, so
there is no `/entries/latour/` to construct, and a guessed URL that 404s is
indistinguishable from a real one that was never checked.

Each source declares how it is looked up, and a **native lookup is preferred over
general web search**:

| `resolve.method` | How | Used by |
| --- | --- | --- |
| `mediawiki` | the site's own opensearch API | Wikipedia |
| `index` | a published table of contents, fetched once and cached for a week | SEP |
| `wordpress` | the site's WordPress REST search | IEP |
| `search` | site-restricted general web search | Britannica, PubMed, the rest |

Native lookup is not a micro-optimization. General web search goes through
SearXNG, whose upstream engines rate-limit and CAPTCHA the instance — and a
throttled SearXNG answers **HTTP 200 with zero results**, which is
indistinguishable from "this subject has no source" and silently empties an entire
run. Native lookup is also simply better: the SEP publishes all 2,511 of its
entries on one page, so one cached fetch resolves every lookup offline, and it
finds entries a site-restricted search misses outright — `entries/madhyamaka/` and
`entries/twotruths-india/` both came back empty from web search.

Findings from real runs that shape the rest:

- **Always judge the page that arrived, never the title the resolver promised.**
  A resolver hit can be a redirect elsewhere: Wikipedia's "Situated knowledge"
  redirects to "Knowledge", and trusting the index title drafted a note about
  situated knowledge from the general article on knowledge. Re-checking the
  fetched page correctly downgraded it to `covers`.
- **Score candidates, don't take the first acceptable one.** A 2,511-entry index
  is alphabetical, not relevance-ordered.
- **There is no "page title is a substring of the subject" rule.** It reads as
  generous and is actively wrong: it matched the SEP's entry on *truth* to a note
  about the two truths doctrine, because "truth" is a substring of "two truths
  doctrine".
- **Outbound HTTPS needs a CA bundle chosen explicitly.** A macOS framework Python
  points OpenSSL at an `etc/openssl/cert.pem` that does not exist, so every
  request fails verification while `curl` on the same machine succeeds — and a
  swallowed TLS error looks exactly like "no source found". `tls_context()` walks
  `SSL_CERT_FILE`, then system bundles, then certifi. Verification is never
  disabled: an unverified fetch is precisely what a citation must not rest on.
- **The bare title is queried before any disambiguator** on the `search` path.
  Adding topic words makes the query worse, because `web-research` picks its
  search engines from the query text:
  `Sheila Jasanoff science and technology studies scholar site:en.wikipedia.org`
  routes to arXiv and returns telescope papers, while the bare name returns her
  article first.
- **A page must bear on the subject, not merely mention it.** Searching the SEP
  for "Bruno Latour" returns an entry on the phenomenology of information
  technology that cites him; drafting from it produces confident prose whose
  citation does not support it, and a reviewer handed the same page cannot tell.

Relevance is graded, not binary, because a strict title test would leave a whole
class of notes permanently unciteable:

| Grade | Test | Meaning |
| --- | --- | --- |
| `about` | the subject appears in the page title, either as a substring or by two thirds of its significant words | the page is an entry on this subject |
| `covers` | the subject is named at least three times in the page text | a broader entry discusses it |
| rejected | neither | discarded in favour of the next source |

The word-overlap rule is what keeps the SEP's "The Theory of Two Truths in India"
for a note called "Two Truths Doctrine, Saṃvṛti and Paramārtha", while still
rejecting "God and Other Ultimates" for "God Trick" (one word of two) and
"Feminist Ethics" for "Alison Jaggar" (none). Dash variants are folded, because
Wikipedia titles its article "Actor–network theory" with an en-dash where the
vault note uses a hyphen.

The grade is carried into both the drafting and the reviewing prompt rather than
smoothed away: "the SEP entry on feminist epistemology discusses this" is a
different claim from "the SEP has an entry on this", and the reviewer is told to
flag a claim that generalizes a `covers` source's wider topic onto the subject. A
proposal whose every source only covers it is marked `weakSources`.

Note titles in this vault are `Canonical Name, Gloss` — "Śūnyatā, Emptiness" —
so the text before the first comma is what a source is matched against.

## Model output shape

Bullet sections are requested as **JSON arrays of strings**, not newline-joined
strings. An unescaped newline inside a JSON string was the single largest source
of unparseable draft responses, and each one costs a whole note. A string is still
accepted if the model regresses, and a bare multi-line string gets its bullet
markers added.

Sections arrive as a skeleton to fill — `{"key_ideas": [], "position": ""}` —
rather than as a list of ids to enumerate, because the non-thinking service
reliably drops sections when asked to produce the keys itself
(`docs/service-split-handoff.md` §2.1). Drafting also gets one corrective retry
that shows the model its own error, since a single call per note means one bad
response otherwise loses the note silently.

An unreferenced citation entry is pruned rather than treated as fatal: it supports
no claim, so dropping it is lossless. A marker with no entry stays fatal, because
that is a claim pointing at nothing.

Citations naming the same source *and* the same locator are collapsed under one
label. A model handed two sources emitted ten entries for one of them, which
rendered `[^1]` and `[^2]` as identical footnotes — to a reader that looks like a
defect. Differing locators (`§2` versus `§4`) stay distinct.

A draft that has sources but places no markers at all is still proposed, because
`## Sources` attributes it and discarding a good note over missing punctuation is
worse — but it is reported, so per-claim citation is never assumed to have happened
when it did not.

## Calibration constants

First guesses, and the ones already moved once. Both `vault-transcripts` and
`vault-capture` had constants that real runs disproved; expect the same here.

| Constant | Value | Why |
| --- | --- | --- |
| Figure lead | 400 chars | Raised from 320: name, dates, nationality, field, and contribution genuinely need it. |
| Other leads | 240–320 | One or two sentences. |
| "One or two lines" prose | 320 | Raised from 240–260, which real drafts overran routinely. |
| One-line prose (`origin`) | 220 | Actually one line. |
| Bullets per section | 3–6 | Past this a card stops being skimmable. |
| Chars per bullet | 90–180 | Names need less than claims. |
| Total managed chars | 1000–2000 | The whole point is that this stays a card. |
| `SOURCE_EXCERPT_CHARS` | 4000 | Per source, shared by drafter and reviewer. |
| `DRAFT_SOURCE_BUDGET` | 8000 | Across all of a note's sources. |
| `VERIFY_PACKET_CHARS` | 30000 | Holds ~3 notes per review call, not 20. |

The drafter and the reviewer share one excerpt budget deliberately. When the
reviewer saw less, it flagged claims the drafter had genuine support for and said
so — "the excerpt cuts off mid-sentence" — a false flag that costs a thinking
escalation and teaches the operator to distrust the reviewer. Full source text
means review batches three notes per call rather than twenty; correctness is
worth the calls.

## Cost

`expand --kind figure` across 266 notes: roughly 14 planning calls plus 266
drafting calls on `chat`, 300–500 page fetches rate-limited by host, and ~89
review calls on `think` plus escalations. Realistically **1.5–2.5 hours**,
dominated by fetching and drafting. This is why the workflow starts at
`--limit 10`.

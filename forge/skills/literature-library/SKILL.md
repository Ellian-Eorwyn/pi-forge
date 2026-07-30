---
name: literature-library
description: Turn a citation file into a library of real documents - parse a .ris export, derive `Author - Date - Title` filenames, acquire each PDF from open-access or institutional sources, and convert to clean Markdown. Use when the user has citations, a reference-manager export, or an academic web-research run and wants the papers, not the metadata. To find new literature use web-research; for evidence from files on disk use literature-extraction.
---

# Literature Library

Acquire the documents a citation list refers to, name them so a human can read
the directory, and convert them to Markdown without pretending the conversion
was lossless.

## Natural Language Routing

Use this skill when the user has a `.ris` file, a reference-manager export, a
folder of citations, or a finished `web-research` academic run, and wants the
full text rather than the catalogue metadata. Phrases that route here: "download
the PDFs for these", "get me these papers", "build a library from this .ris",
"convert these articles to Markdown".

Do not use this for finding new sources — that is `web-research`. Do not use it
to extract claims and evidence from documents already on disk — that is
`literature-extraction`, which runs after this one.

Inside an Obsidian vault, write runs to the vault workflow root:

```bash
99 Meta/99.06 Workflows/Literature Library/<source-stem>/
```

Outside a vault, use `forge-output/literature-library/<source-stem>/`.

## Command Card

- `doctor --json`: local capability check. Touches no network.
- `parse <citation-file> --output <run-directory> --contact-email <address>`: parse the citation file, merge duplicate DOIs, derive filenames, and scaffold a resumable run. **Downloads nothing.**
- `parse … --repair-replacement-chars`: guess a right single quote for letter-flanked U+FFFD, recording every substitution.
- `acquire <run-directory>`: resolve open-access status and fetch every pending record. Publishes verified PDFs under hash-bound move operations.
- `acquire … --allow-browser`: after the direct paths fail, retry **open-access** records through the browser service.
- `acquire … --institutional`: also attempt closed-access records, but only if this machine egresses from the institution's network.
- `acquire … --retry-deferred`: requeue `deferred-institutional` records. Pair with `--institutional` once the machine is on the network that deferred them.
- `acquire … --batch-size 50 --batch-pause 30 --chunk-size 8 --host-delay-ms 3000`: pacing. Batches carry the pause; chunks bound how much an interrupted run repeats.
- `convert <run-directory>`: convert every acquired PDF to Markdown with bibliographic frontmatter, escalating scanned documents to OCR. Add `--refresh-all` to reconvert.
- `retry <run-directory> [--item <id> | --disposition <d> | --all-failed]`: requeue terminal failures.
- `detect-egress --json`: report whether this machine currently egresses from the institution.
- `status <run-directory> --json`: durable progress, dispositions, and input drift.
- `validate <run-directory> --json --read-only`: machine-readable gate, also called by `vault-connections import-run`.

## Dispositions

| Disposition | Meaning |
| --- | --- |
| `acquired` | a verified PDF is published under `pdf/` |
| `converted` | Markdown is published under `markdown/` beside the PDF |
| `deferred-institutional` | closed access and this machine is not on the institution's network; resumable |
| `manual` | reachable but refused or unparseable; queued in `manual_queue.md` |
| `not-found` | every candidate returned 404 |
| `no-candidate` | no open-access location and no usable identifier |
| `blocked` | a published file already exists with different content; needs review |

`manual` and `deferred-institutional` are **terminal**, not failures: the run did
everything it could from here.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path, then check
   capabilities:

   ```bash
   python3 <skill-directory>/scripts/literature-library.py doctor
   ```

2. Parse the citation file first and show the user the plan. `library_plan.md`
   lists every filename that acquisition would publish, which review flags fired,
   and which records carry no DOI — all before any request is made.

   ```bash
   python3 <skill-directory>/scripts/literature-library.py parse <citation-file> \
     --output <run-directory> --contact-email <address>
   ```

3. Acquire. Open-access records come first and need no credentials; add
   `--institutional` only when the user's library VPN is already connected.

   ```bash
   python3 <skill-directory>/scripts/literature-library.py acquire <run-directory> --allow-browser
   ```

4. Inspect or resume a stopped run without repeating completed records:

   ```bash
   python3 <skill-directory>/scripts/literature-library.py status <run-directory> --json
   python3 <skill-directory>/scripts/literature-library.py retry <run-directory> --all-failed
   ```

   Report what actually happened. On a real 49-record humanities export with no
   VPN, 21 were acquired, 18 were deferred as closed access, 9 were refused by
   publisher bot protection, and 1 had no candidate at all. Roughly half a
   philosophy reading list is not reachable without institutional access, and
   telling the user that up front is more useful than a progress bar.

5. Convert the acquired PDFs to Markdown:

   ```bash
   python3 <skill-directory>/scripts/literature-library.py convert <run-directory>
   ```

6. Gate the run before handing it to `vault-connections import-run`:

   ```bash
   python3 <skill-directory>/scripts/literature-library.py validate <run-directory> --json --read-only
   ```

## Filenames

Names are `Author - Date - Title`, using the first author's surname only:
`Carel - 2014 - Epistemic injustice in healthcare.pdf`.

The whole assembled stem goes through `vault_schema.safe_title`, so the result is
byte-identical to what every other vault skill would compute for the same string.
Consequences worth stating to the user rather than hiding:

- `:` and other path-unsafe characters are **removed**, so `Science: A Study`
  becomes `Science A Study`. `[` and `]` become parentheses and `|` becomes `-`.
- Names are capped at 120 characters. A longer title is truncated at a word
  boundary with no ellipsis, `titleTruncated` is recorded, and the full title is
  preserved in `library_index.jsonl` and in the Markdown frontmatter.
- Corporate authors are filed under the organization name, truncated to 40
  characters, and flagged for review.
- A missing author files as `Unknown`; a missing year files as `n.d.`.
- Two records that would produce the same filename are lettered citation-style
  (`2014a`, `2014b`) in source order.
- Two records sharing a DOI are merged; the duplicate is recorded, not fetched.

## Acquisition and Access

Records are classed `open-access` or `institutional` from their Unpaywall status,
and every attempt is labeled with which class it used.

**This skill never handles credentials.** Institutional access works only when
the user has already connected their library VPN out of band; the skill observes
whether the machine egresses from a campus network and defers closed records
otherwise. It never stores, prompts for, or transmits a password, and a deferred
record leaves the run resumable rather than failed.

Browser-assisted fetching runs on a remote Playwright service, which egresses
from a different host than the user's machine. It is therefore restricted to
open-access records: a remote browser cannot carry institutional access, and
routing a licensed resource through it would both fail and misrepresent the
request. This is enforced in the acquirer, not by convention.

Do not oversell the browser stage. Measured on a real 49-record export, it
recovered **1 of 11** records that the direct path could not get. The publishers
that refuse a plain HTTP client for open-access content -- Wiley, Sage, Taylor &
Francis, ACM, Cambridge, PhilPapers -- run commercial bot management that also
fingerprints headless Chromium, so a stock browser does not satisfy it either.
The fix for those is institutional access, not a cleverer fetch.

## Conversion

Routing is by measurement, not by another skill's warning text: each PDF is
probed with PyMuPDF, and anything under 200 alphanumeric characters per page or
with more than a quarter of its pages empty escalates from `file-conversion`'s
structural path to `document-ingest` OCR. On a real 21-document humanities corpus
the sparsest born-digital article still carried ~1450 characters per page, so the
threshold has real clearance.

**`--ocr-backend local` is mandatory** when invoking `document-ingest`. It
otherwise tries a remote OCR service first, which would ship the user's documents
off their machine.

Published Markdown gets YAML frontmatter carrying the bibliographic record, the
source URL, the PDF hash, and which method produced it. Two things are corrected
on the way out:

- `file-conversion` names its output with `safe_stem`, which turns
  `Author - Year - Title` into `Author---Year---Title` and uses that as the H1.
  The real title replaces it.
- Every warning from the child run becomes a `needs_review` entry. OCR output is
  always marked "not guaranteed verbatim" — never present it as exact text.

**Repository coversheets are detected, not removed.** Green open-access PDFs from
institutional repositories often begin with a page of portal boilerplate, terms
of use, and citation guidance, which downstream evidence extraction would
otherwise quote as if the author wrote it. 4 of 21 documents in the reference
corpus had one. The span is reported in `needs_review` and left in place: a false
positive that stripped text would delete the opening of an article, and that
trade is not worth making silently.

## Why This Is Slow On Purpose

Publishers treat rapid sequential downloading from an institutional IP range as
"systematic downloading", which violates the licence. The standard remedy is
suspending access **for the entire campus**, not for the individual account. So:
one request per host every few seconds, one connection per host, conservative run
caps, a circuit breaker that trips a host after repeated refusals, an honest
User-Agent carrying the contact address, and stricter limits for institutional
records than for open-access ones. There is no flag to disable this, and none
should be added.

## Verification

- `library_plan.md` must be reviewed before acquisition; it is the only place the
  derived filenames appear before files exist.
- `validate` reports `complete: true` once every record reaches a terminal
  disposition. `manual` and `deferred-institutional` are terminal on purpose —
  they mean the run finished what it could, and the deferrals are carried as
  warnings.
- `library_index.jsonl` is the authoritative per-record manifest and records the
  source PDF hash and which Markdown method produced each file.

## Safety and Failure Handling

- Never store, prompt for, or transmit credentials. Institutional access is
  established by the user out of band and only observed here.
- Never bypass bot protection with stealth or fingerprint-evasion tooling.
  Behaving like an ordinary browser is acceptable; evading detection is not, and
  it is what gets institutions blocked.
- Honor `robots.txt` when scraping a landing page for a PDF link, and scrape only
  one level deep. There is no `--ignore-robots` flag.
- Verify a download by its `%PDF` magic number and by opening it, never by its
  `Content-Type`. Publishers routinely serve HTML with a PDF content type.
- Publish by precomputed, hash-bound move operations. Never overwrite a
  destination whose hash does not match what the journal expects; block it for
  review instead.
- Continue a batch past individual failures; every failure lands in the manual
  queue with the stage it reached and why.
- PDF to Markdown is delegated to `file-conversion`, and scanned or low-text PDFs
  escalate to `document-ingest` for OCR. Propagate their warnings rather than
  claiming the Markdown is verbatim.
- BibTeX input is not implemented. Say so and ask for a RIS export rather than
  half-parsing it.

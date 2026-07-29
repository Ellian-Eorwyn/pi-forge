# Personal context: register and card format

Two files make up the layer. A register the owner edits, listing cards and their
routing, and one ordinary vault note per card. Parsed by `forge/lib/vault_profile.py`.

## The register

`99 Meta/99.02 Schemas/0.03 Personal Context.md`, resolved like its siblings:
the canonical path, else a unique `0.03 Personal Context.md` anywhere outside
dotted directories, `00 Inbox`, and workflow run directories.

```markdown
---
type: system
status: active
domain: meta
subdomain: schemas
capture_type: manual
---

# Personal Context

## Cards

| Card | Tier | Scope | Applies | Triggers | Notes |
| --- | --- | --- | --- | --- | --- |
| `[[Working Preferences]]` | `always` | `universal` |  |  | Naming and presentation. |
| `[[People in My Life]]` | `when-relevant` | `owner-authored` | `personal` | `Gillian`, `Kodama` | Route-gated. |
| `[[Mental Health History]]` | `when-relevant` | `owner-authored` | `personal/therapy`, `personal/grief` | `OCD`, `dissociate` | Tightly gated. |
```

`Card` and `Tier` are the required columns; `Scope`, `Applies`, `Triggers`, and
`Notes` are optional. Values may be backticked. `Applies` and `Triggers` are
comma-separated.

| Column | Values | Default |
| --- | --- | --- |
| `Tier` | `always`, `when-relevant`, `on-request` | `when-relevant` |
| `Scope` | `universal`, `owner-authored`, `source-derived` | `owner-authored` |
| `Applies` | `domain` or `domain/subdomain` routes | empty — unrestricted |
| `Triggers` | literal terms or names | empty |

### The two gates

**Scope** asks whose material this is, reusing `vault_voice`'s context modes so
the two layers agree on what `owner` means. A `source-derived` prompt — someone
else's lecture — sees only `universal` cards.

**Applies** asks which part of the owner's life this is, and it is the privacy
gate. Empty is permissive. Non-empty is deny-by-default: the card is refused
unless the site *positively establishes* one of its routes, so a stage that does
not know where a note is filed refuses every route-gated card. That asymmetry is
the design — sensitive cards carry routes, harmless cards do not have to
enumerate every domain they are welcome in.

The site's route is ancestor-expanded, not the card's. A site at
`personal/therapy` matches a card gated to `personal`; a site that only knows
`personal` does **not** match a card gated to `personal/therapy`.

### Trigger matching

Exact, over accent-folded and space-folded token windows, so `madhya maka`
matches `Madhyamaka` and `Nagarjuna` matches `Nāgārjuna`. Deliberately *not*
`vault_lexicon.similarity`: fuzzy matching earns its place there because a missed
near-miss leaves a mistranscription standing, but here a false positive puts
personal material into a prompt that should not have it. Tier-1
`apply_corrections` has already normalized known variants by the time any of
these stages run, so exact matching loses nothing.

## A card

```markdown
---
type: note
status: active
domain: personal
subdomain: context
capture_type: manual
---

# People in My Life

## Context

- Gillian Eorwyn — my spouse, married 2023-12-31. "Fub" between us.
- Sopagna Braje — my therapist; the other voice in therapy recordings.

## Detail

Anything longer lives here and is never put in a prompt.
```

Only flat bullets directly under `## Context` are injected. An indented
sub-bullet is dropped with a warning — nesting is how a card becomes a document.
A card is capped at `MAX_CARD_BULLETS` (14) then `MAX_CARD_CHARS` (700); over
budget, whole trailing bullets are dropped, never part of one.

The register stores a wikilink rather than a path, so `vault-organizer` refiling
a card does not break it. Resolution is a basename scan; two notes with the card's
name refuse rather than guess, which is worth knowing when naming a card — a
card called `Mental Health` collides with a therapy note of that name.

## Budgets

| Constant | Value | Why |
| --- | --- | --- |
| `MAX_CARD_CHARS` | 700 | ~175 tokens. Enforced at parse time so cards stay condensed by construction. |
| `DEFAULT_PREFIX_BUDGET` | 900 | Half of `vault_voice`'s prefix budget; the always-tier stacks on it in the same system message. |
| `DEFAULT_CONTEXT_BUDGET` | 1200 | Per-item. Roughly two cards. |
| `MAX_SELECTED_CARDS` | 3 | Caps *triggered* cards only — the always-tier renders separately under its own budget. |

Treat these as calibration targets and record what real runs disprove, the way
`NEAR_MISS_RATIO` carries its calibration in a comment.

## Where the layer is and is not used

| Stage | Gets it | Why |
| --- | --- | --- |
| Transcript summary | system prefix + payload | Where "better summaries" is won. |
| Journal reflection | system prefix + payload | Highest-value site. |
| Connection judgment | system prefix + per-pair payload | Replaced a hardcoded biography in `CONNECTION_SYSTEM`. |
| Inbox classification | always-tier prefix only | The domain is being predicted, so no route is established. |
| Capture drafting | always-tier prefix only | The domain is the organizer's later decision. |
| Transcript cleanup | **never** | `check_chunk` rejects words the source did not contain. |
| Capture draft payload | **never** | `check_draft` makes an absent name a hard problem. |

The last two are the important ones. Those stages run behind a deterministic
fabrication check, and a card naming a person invites the model to write that
name, at which point the gate throws the note away. Widening the allowance is the
wrong fix — that check is what catches real fabrication. There are regression
tests asserting no profile-derived key reaches either payload; do not remove them.

## Degradation

Unlike the schema and voice policies, a malformed register never fails a run.
`compiled_profile_for` returns `(None, "none", [reason])` and the run proceeds
without the layer. A single unparseable row costs its own card; the rest of the
register still compiles. This is a deliberate departure from
`compiled_voice_for`, justified by the profile being enrichment rather than a
contract.

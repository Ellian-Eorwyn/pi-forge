#!/usr/bin/env python3
"""Parse and compile a vault-owned personal-context layer: who the owner is.

The other config notes tell the pipeline how the owner writes and what things
are called. Neither tells it who the owner is, so every prompt that summarizes
a therapy session or judges whether two notes connect is written by a model
that does not know the owner is a sociologist, that Kodama was a cat, or that
a Rorty note and a work note about disagreement belong together *for this
person*. That knowledge is personal, so it lives in notes the owner edits.

A register note declares the cards; each card is an ordinary vault note whose
``## Context`` bullets are the only part a prompt ever sees. Splitting them
that way is what lets a card be minimal in a prompt and complete on the page.

The register also carries an optional ``## Owner`` record: what to call this
person, and their pronouns. A card is background a prompt may consult; the name
is how the output addresses them, which is why it is a declared field rather
than another bullet a model has to notice and interpret.

Two gates decide whether a card may be injected, and they are deliberately
asymmetric:

* **Scope** answers "is this material the owner's at all", reusing the voice
  policy's context modes so the two layers agree on what ``owner`` means.
* **Applies** answers "which part of the owner's life is this". It is optional,
  but a card that sets it is refused wherever the pipeline has not *positively
  established* the route -- including sites that simply do not know. That is
  what keeps clinical and life-history material out of a work meeting without
  every harmless card having to enumerate ten domains.

Unlike the schema and voice policies, a malformed register never fails a run.
This layer is additive enrichment rather than a contract, so a bad row costs
its own card and nothing else.
"""

import json
import re
from pathlib import Path

import vault_lexicon
import vault_voice
from vault_schema import (
    INBOX_DIR,
    PROTECTED_DIRS,
    UserError,
    heading_index,
    link_basename,
    section_bounds,
    sha256_file,
    sha256_text,
    strip_inline_code,
    table_after,
    wikilink_target,
)

DEFAULT_PROFILE = "99 Meta/99.02 Schemas/0.03 Personal Context.md"
PROFILE_BASENAME = "0.03 Personal Context.md"
COMPILED_PROFILE_VERSION = 2

CARDS_SECTION = "Cards"
CONTEXT_SECTION = "Context"
OWNER_SECTION = "Owner"

# Register column value -> profile key. The columns read as English because a
# person maintains them; the keys are what the pipeline passes around.
OWNER_FIELDS = {"name": "name", "pronouns": "pronouns", "full name": "full_name"}
# A name, not a biography. Long enough for a full name with particles, short
# enough that a paragraph pasted into the wrong row cannot become a salutation.
MAX_OWNER_FIELD_CHARS = 40

TIER_ALWAYS = "always"
TIER_RELEVANT = "when-relevant"
TIER_REQUEST = "on-request"
TIER_VALUES = (TIER_ALWAYS, TIER_RELEVANT, TIER_REQUEST)
DEFAULT_TIER = TIER_RELEVANT

DEFAULT_SCOPE = vault_voice.SCOPE_OWNER

# A card is a flat fact list by construction. 700 characters is roughly 175
# tokens, or a dozen terse bullets: enough to say who someone is, too little to
# hold a narrative. Enforcing it at parse time is what keeps cards condensed by
# construction rather than by discipline, and it makes the whole layer small
# enough that `doctor` can print all of it.
MAX_CARD_CHARS = 700
MAX_CARD_BULLETS = 14

# Half of vault_voice.DEFAULT_PREFIX_BUDGET. The profile prefix stacks on top of
# the voice prefix in the same system message, and at classification on a schema
# dump as well, so half is the most it can take without doubling the fixed cost
# of every call.
DEFAULT_PREFIX_BUDGET = 900
# Larger than the voice context budget (900), because a selected card is the
# substance where a voice context item is a single rule. Holds about two cards.
DEFAULT_CONTEXT_BUDGET = 1200
# In the spirit of the lexicon's term and speaker limits: past the second card
# the marginal effect on an answer approaches zero and the dilution cost does not.
MAX_SELECTED_CARDS = 3
MAX_TRIGGER_MATERIAL_CHARS = 40000

BULLET_RE = re.compile(r"^[-*]\s+(.+?)\s*$")
INDENTED_BULLET_RE = re.compile(r"^\s+[-*]\s+")
WORKSPACE_MARKER = ".forge-workspace"


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------

class AmbiguousProfileError(UserError):
    """More than one register note in the vault.

    A subclass because this is vault state rather than a mistake in the command:
    the run asked for nothing that cannot be honoured, it just cannot tell which
    card layer was meant. Callers degrade — warn, continue without the layer —
    where a bad ``--profile`` path stays fatal.
    """


def resolve_profile_path(vault, raw_profile=None, disabled=False):
    """Return the register note, or ``None`` when disabled or absent."""
    if disabled:
        return None
    vault = Path(vault)
    if raw_profile:
        path = Path(raw_profile).expanduser()
        if not path.is_absolute():
            path = vault / path
        if not path.is_file():
            raise UserError(f"personal context note does not exist: {path}")
        return path.resolve()
    default = (vault / DEFAULT_PROFILE).resolve()
    if default.is_file():
        return default
    matches = []
    for candidate in vault.rglob(PROFILE_BASENAME):
        if _is_addressable(vault, candidate, _workspace_roots(vault)):
            matches.append(candidate.resolve())
    if len(matches) > 1:
        listed = ", ".join(str(path) for path in sorted(matches))
        raise AmbiguousProfileError(f"more than one '{PROFILE_BASENAME}' in the vault ({listed}); pass --profile")
    return matches[0] if matches else None


def resolve_profile_or_warn(vault, raw_profile=None, disabled=False):
    """Resolve the register, degrading to no layer when the vault is ambiguous.

    Returns ``(path, warnings)``. An explicit ``--profile`` that does not exist
    still raises, because the run named a card layer and cannot quietly proceed
    without it. Ambiguity is the vault's own state, so it costs the layer and a
    warning rather than the run -- the same bargain ``compiled_profile_for``
    makes for a malformed register, and the reason every skill can share this.
    """
    try:
        return resolve_profile_path(vault, raw_profile, disabled=disabled), []
    except AmbiguousProfileError as error:
        return None, [f"personal context layer disabled: {error}"]


def _workspace_roots(vault):
    """Directories carrying the workflow-run marker, collected once.

    Run directories hold backup copies of notes. Without this a card's own
    backup could be mistaken for a second card and the register would refuse an
    ambiguity it does not have.
    """
    vault = Path(vault)
    return {marker.parent.resolve() for marker in vault.rglob(WORKSPACE_MARKER)}


def _is_addressable(vault, path, workspace_roots=frozenset()):
    """Whether a note is somewhere the register may point at."""
    resolved = path.resolve()
    parts = resolved.relative_to(Path(vault).resolve()).parts
    if any(part.startswith(".") or part in PROTECTED_DIRS for part in parts):
        return False
    if parts and parts[0] == INBOX_DIR:
        return False
    return not any(root in resolved.parents for root in workspace_roots)


def _basename_index(vault):
    """``{stem: [path, ...]}`` for every addressable note, built in one pass.

    The register stores wikilinks rather than paths so that vault-organizer
    refiling a card does not break it, which means resolution is a vault scan.
    One scan for every card beats one scan per card.
    """
    vault = Path(vault)
    roots = _workspace_roots(vault)
    index = {}
    for path in vault.rglob("*.md"):
        if _is_addressable(vault, path, roots):
            index.setdefault(path.stem, []).append(path.resolve())
    return index


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def _cell_values(cell):
    """A cell holding a comma-separated list of optionally backticked values.

    Split before unwrapping, for the reason ``vault_lexicon._cell_values``
    gives: a cell of several backticked values is itself wrapped end to end.
    """
    parts = (strip_inline_code(part) for part in str(cell).split(","))
    return [part for part in parts if part]


def _parse_route(value):
    """Normalize one ``domain`` or ``domain/subdomain`` token."""
    parts = [part.strip().lower() for part in str(value).split("/") if part.strip()]
    if not parts or len(parts) > 2:
        return None
    if not all(re.fullmatch(r"[a-z0-9][a-z0-9-]*", part) for part in parts):
        return None
    return "/".join(parts)


def parse_owner(lines):
    """The optional ``## Owner`` record: what to call this person.

    Returns ``(owner_or_none, warnings)``. An absent section is silent — most
    vaults never declare one, and without it every stage behaves exactly as it
    did before the section existed. A section that is present but unreadable
    warns and yields nothing: no name at all is better than a mangled one, since
    getting someone's name wrong is the failure this record exists to prevent.
    """
    try:
        heading_index(lines, OWNER_SECTION)
    except UserError:
        return None, []
    try:
        rows = table_after(lines, OWNER_SECTION, ["Field", "Value"])
    except UserError as error:
        return None, [f"{OWNER_SECTION}: {error}; nothing can address the owner by name"]

    fields = {}
    warnings = []
    for row in rows:
        label = strip_inline_code(row["Field"]).lower()
        key = OWNER_FIELDS.get(label)
        if key is None:
            warnings.append(f"{OWNER_SECTION}: unknown field {label!r}; use one of {', '.join(OWNER_FIELDS)}")
            continue
        value = " ".join(strip_inline_code(row["Value"]).split())
        if not value:
            continue
        if len(value) > MAX_OWNER_FIELD_CHARS:
            warnings.append(f"{OWNER_SECTION}: {label} is over {MAX_OWNER_FIELD_CHARS} characters; ignored")
            continue
        if key in fields:
            warnings.append(f"{OWNER_SECTION}: duplicate field {label}; the later row is ignored")
            continue
        fields[key] = value

    if "name" not in fields:
        warnings.append(f"{OWNER_SECTION}: no `name` row, so nothing can address the owner by name")
        return None, warnings
    return fields, warnings


def parse_profile_note(text):
    """Parse the register's owner record and card table.

    Structural problems in the card table raise, because a register whose table
    cannot be read is unusable. Row problems only skip their own row: losing
    every card because one cell has a typo is the wrong trade for an enrichment
    layer. The owner record never raises at all -- see :func:`parse_owner`.
    """
    lines = str(text).splitlines()
    owner, owner_warnings = parse_owner(lines)
    rows = table_after(lines, CARDS_SECTION, ["Card", "Tier"])
    cards = []
    warnings = list(owner_warnings)
    seen = set()
    for order, row in enumerate(rows):
        raw = strip_inline_code(row["Card"])
        name = link_basename(wikilink_target(raw)) if raw.startswith("[[") else raw
        if not name:
            warnings.append(f"{CARDS_SECTION}: a row names no card; skipped")
            continue
        if name.lower() in seen:
            warnings.append(f"{CARDS_SECTION}: duplicate card {name}; the later row is skipped")
            continue
        seen.add(name.lower())

        tier = (strip_inline_code(row["Tier"]) or DEFAULT_TIER).lower()
        if tier not in TIER_VALUES:
            warnings.append(f"{CARDS_SECTION}: {name} has tier {tier!r}; use one of {', '.join(TIER_VALUES)}; skipped")
            continue

        scope = (strip_inline_code(row.get("Scope", "")) or DEFAULT_SCOPE).lower()
        if scope not in vault_voice.KNOWN_SCOPES:
            warnings.append(
                f"{CARDS_SECTION}: {name} has scope {scope!r}; "
                f"use one of {', '.join(vault_voice.KNOWN_SCOPES)}; skipped"
            )
            continue

        routes = set()
        bad_route = None
        for token in _cell_values(row.get("Applies", "")):
            route = _parse_route(token)
            if route is None:
                bad_route = token
                break
            routes.add(route)
        if bad_route is not None:
            # An unreadable cell in the privacy gate means refusal, not a guess.
            warnings.append(f"{CARDS_SECTION}: {name} has an unreadable Applies value {bad_route!r}; skipped")
            continue

        triggers = _cell_values(row.get("Triggers", ""))
        if tier == TIER_RELEVANT and not triggers:
            warnings.append(f"{CARDS_SECTION}: {name} is when-relevant with no triggers, so it can never be selected")

        cards.append(
            {
                "order": order,
                "name": name,
                "link": raw if raw.startswith("[[") else f"[[{name}]]",
                "tier": tier,
                "scope": scope,
                "routes": frozenset(routes),
                "triggers": triggers,
                "note": strip_inline_code(row.get("Notes", "")),
                "facts": [],
            }
        )
    return {"cards": cards, "owner": owner, "warnings": warnings}


def parse_card_note(text):
    """The injectable facts of one card: flat bullets under ``## Context``.

    Nesting is how a card becomes a document, so an indented bullet is dropped
    rather than flattened. Over-budget cards lose whole trailing bullets, never
    part of one.
    """
    lines = str(text).splitlines()
    try:
        start, end = section_bounds(lines, CONTEXT_SECTION)
    except UserError:
        return [], [f"has no '## {CONTEXT_SECTION}' section"]
    facts = []
    warnings = []
    nested = 0
    for line in lines[start + 1:end]:
        if INDENTED_BULLET_RE.match(line):
            nested += 1
            continue
        match = BULLET_RE.match(line)
        if match:
            facts.append(re.sub(r"\s+", " ", match.group(1)).strip())
    if nested:
        warnings.append(f"dropped {nested} indented bullet(s); a card is a flat fact list")
    if not facts:
        return [], warnings + [f"'## {CONTEXT_SECTION}' has no bullets"]
    if len(facts) > MAX_CARD_BULLETS:
        warnings.append(f"has {len(facts)} bullets; keeping the first {MAX_CARD_BULLETS}")
        facts = facts[:MAX_CARD_BULLETS]
    kept = []
    used = 0
    for fact in facts:
        if used + len(fact) + 1 > MAX_CARD_CHARS:
            warnings.append(f"is over {MAX_CARD_CHARS} characters; dropped {len(facts) - len(kept)} trailing bullet(s)")
            break
        kept.append(fact)
        used += len(fact) + 1
    return kept, warnings


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def load_profile(vault, profile_path):
    """Parse the register and read every card it resolves.

    Returns ``(profile, content_hash, warnings)``. A card that cannot be
    resolved or read is dropped with a warning; the rest of the register still
    compiles.
    """
    # Resolved, because the index holds resolved paths and a symlinked vault
    # root (/var -> /private/var on macOS) otherwise breaks every relative_to.
    vault = Path(vault).resolve()
    text = Path(profile_path).read_text(encoding="utf-8")
    register_digest = sha256_file(Path(profile_path))
    try:
        parsed = parse_profile_note(text)
    except UserError as error:
        # The card table is a contract and a register that declares cards it
        # cannot list is broken. The owner record is not part of that contract:
        # someone's name is still their name when the table below it has a typo,
        # and a register that declares nothing else is a complete register.
        owner, owner_warnings = parse_owner(text.splitlines())
        if owner is None:
            raise
        return (
            {"cards": [], "owner": owner},
            sha256_text(register_digest),
            owner_warnings + [f"the card table could not be read ({error}); only the owner record survives"],
        )
    warnings = list(parsed["warnings"])
    index = _basename_index(vault)
    cards = []
    digests = [register_digest]
    for card in parsed["cards"]:
        matches = index.get(card["name"], [])
        if not matches:
            warnings.append(f"card {card['name']} has no note in the vault; skipped")
            continue
        if len(matches) > 1:
            listed = ", ".join(str(path.relative_to(vault)) for path in sorted(matches))
            warnings.append(f"card {card['name']} matches more than one note ({listed}); skipped")
            continue
        path = matches[0]
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            warnings.append(f"card {card['name']} could not be read ({error}); skipped")
            continue
        facts, card_warnings = parse_card_note(text)
        warnings.extend(f"card {card['name']} {warning}" for warning in card_warnings)
        if not facts:
            continue
        digests.append(sha256_text(f"{card['name']}:{sha256_file(path)}"))
        cards.append({**card, "facts": facts, "path": path.relative_to(vault).as_posix()})
    # The owner record lives in the register itself, so the register digest that
    # already leads this list covers it: renaming yourself busts the cache.
    profile = {"cards": cards, "owner": parsed["owner"]}
    return profile, sha256_text("|".join(digests)), warnings


def compiled_profile_for(vault, profile_path, cache_dir=None):
    """Load the layer, caching by a hash over the register and every card.

    Never raises. A malformed register costs the layer, not the run -- the
    deliberate departure from ``compiled_voice_for``, which fails a run because
    a voice policy is a contract where this is enrichment.
    """
    if profile_path is None:
        return None, "none", []
    profile_path = Path(profile_path)
    try:
        register_hash = sha256_file(profile_path)
    except OSError as error:
        return None, "none", [f"personal context note could not be read ({error})"]
    cache_path = Path(cache_dir) / "compiled-profile.json" if cache_dir else None
    if cache_path and cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if (
                cached.get("version") == COMPILED_PROFILE_VERSION
                and cached.get("register_hash") == register_hash
                and cached.get("profile_hash") == _rehash(vault, register_hash, cached.get("profile"))
            ):
                return _revive(cached["profile"]), cached["profile_hash"], cached.get("warnings", [])
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            pass
    try:
        profile, profile_hash, warnings = load_profile(vault, profile_path)
    except (UserError, OSError, UnicodeDecodeError) as error:
        return None, "none", [f"personal context note could not be compiled ({error})"]
    # A register that only declares an owner is a complete, useful layer: the
    # name still reaches every prompt. Only a register with neither is empty.
    if not profile["cards"] and not profile["owner"]:
        return None, profile_hash, warnings + ["personal context note resolved no usable cards"]
    if cache_path:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    {
                        "version": COMPILED_PROFILE_VERSION,
                        "register_hash": register_hash,
                        "profile_hash": profile_hash,
                        "profile": _plain(profile),
                        "warnings": warnings,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass
    return profile, profile_hash, warnings


def _plain(profile):
    """JSON-safe form: frozensets do not survive a round trip."""
    return {
        "cards": [{**card, "routes": sorted(card["routes"])} for card in profile["cards"]],
        "owner": profile.get("owner"),
    }


def _revive(plain):
    return {
        "cards": [{**card, "routes": frozenset(card["routes"])} for card in plain["cards"]],
        "owner": plain.get("owner"),
    }


def _rehash(vault, register_hash, plain):
    """Recompute a cached profile's content hash against the notes on disk.

    A card body can change without the register changing, so the register hash
    alone cannot validate the cache. Mirrors the digest order in
    :func:`load_profile`, which starts from the register.
    """
    if not isinstance(plain, dict):
        return None
    vault = Path(vault)
    digests = [register_hash]
    for card in plain.get("cards", []):
        path = vault / card.get("path", "")
        if not path.is_file():
            return None
        digests.append(sha256_text(f"{card['name']}:{sha256_file(path)}"))
    return sha256_text("|".join(digests))


# --------------------------------------------------------------------------
# Sites and selection
# --------------------------------------------------------------------------

def expand_routes(routes):
    """Ancestor-expand a site's routes: ``personal/therapy`` implies ``personal``.

    Expanding the *site* rather than the card is what makes ``personal`` cards
    reach a therapy note while ``personal/therapy`` cards stay out of anything
    filed merely under ``personal``.
    """
    expanded = set()
    for route in routes or ():
        normalized = _parse_route(route)
        if not normalized:
            continue
        expanded.add(normalized)
        if "/" in normalized:
            expanded.add(normalized.split("/", 1)[0])
    return frozenset(expanded)


def profile_site(context_mode, routes=(), stage=""):
    """Where a prompt sits: whose material it is, and what it is filed under."""
    if context_mode not in vault_voice.CONTEXT_MODES:
        raise UserError(f"unknown profile context mode: {context_mode}")
    return {"context_mode": context_mode, "routes": expand_routes(routes), "stage": stage}


def _scope_ok(card, site):
    return site["context_mode"] in vault_voice.SCOPE_TO_CONTEXT.get(card["scope"], frozenset())


def _route_ok(card, site):
    """An unrestricted card travels with its scope; a route-gated one needs proof.

    ``not card["routes"]`` is the common case and is permissive. A card that
    names routes is refused unless the site positively establishes one of them,
    so a site that knows nothing about where it is refuses every gated card.
    """
    if not card["routes"]:
        return True
    return bool(card["routes"] & site["routes"])


def _trigger_hits(card, windows):
    """Triggers this material contains, matched exactly over folded windows.

    Deliberately not :func:`vault_lexicon.similarity`. Fuzzy matching earns its
    place there because a missed near-miss is a mistranscription left standing;
    here a false positive puts personal material in a prompt that should not
    have it, so the match has to be exact.
    """
    return [trigger for trigger in card["triggers"] if vault_lexicon.fold(trigger) in windows]


def select_cards(profile, material, site, tier=None, limit=MAX_SELECTED_CARDS):
    """The cards this prompt may have, always-tier first then most-triggered.

    ``limit`` caps the *triggered* cards only. The always-tier is not competing
    for the same room: it renders into the system prefix under its own budget,
    while triggered cards go per-item. Counting both against one cap let two
    always cards starve the selection down to a single triggered card -- exactly
    the material the trigger fired for.
    """
    if not profile or not profile.get("cards"):
        return []
    if site["context_mode"] == vault_voice.CONTEXT_NONE:
        return []
    windows = vault_lexicon.folded_windows(str(material or "")[:MAX_TRIGGER_MATERIAL_CHARS])
    always = []
    relevant = []
    for card in profile["cards"]:
        if card["tier"] == TIER_REQUEST:
            continue
        if tier is not None and card["tier"] != tier:
            continue
        if not _scope_ok(card, site) or not _route_ok(card, site):
            continue
        if card["tier"] == TIER_ALWAYS:
            always.append({**card, "matched": []})
            continue
        matched = _trigger_hits(card, windows)
        if matched:
            relevant.append({**card, "matched": matched})
    always.sort(key=lambda card: card["order"])
    relevant.sort(key=lambda card: (-len(card["matched"]), card["order"]))
    return always + relevant[:limit]


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

PREFIX_HEADER = (
    "Background about the vault owner, for interpretation only. These are "
    "standing facts, not part of the material being processed: never quote "
    "them, and never introduce one into text that did not already say it."
)


def _render(cards, header, budget):
    if not cards:
        return ""
    lines = [header]
    used = len(header)
    for card in cards:
        used = vault_voice.append_group(lines, used, f"{card['name']}:", card["facts"], budget)
    return "\n".join(lines) if len(lines) > 1 else ""


def owner_sentence(profile, site):
    """One line naming this person, for the top of a system prompt.

    Gated on the same context mode the cards are, so a stage held behind the
    fabrication gate stays behind it. A name is the most quotable fact there is,
    and the stages that may not know anything about the owner are exactly the
    ones that would put it somewhere it does not belong.
    """
    owner = (profile or {}).get("owner")
    if not owner or site["context_mode"] == vault_voice.CONTEXT_NONE:
        return ""
    who = f"{owner['name']} ({owner['pronouns']})" if owner.get("pronouns") else owner["name"]
    return (
        f"The vault owner is {who}. Call them {owner['name']} wherever the output speaks to or "
        'about them; do not write "the user" or "the owner". Never insert their name into text '
        "drawn from a source that did not already say it."
    )


def profile_prefix(profile, site, budget=DEFAULT_PREFIX_BUDGET):
    """The owner line and always-tier block, byte-stable for a given site.

    The two are independent: a register that names an owner and declares no
    always-tier card still gets its name into every prompt.
    """
    sentence = owner_sentence(profile, site)
    cards = _render(select_cards(profile, "", site, tier=TIER_ALWAYS, limit=MAX_SELECTED_CARDS), PREFIX_HEADER, budget)
    if not sentence:
        return cards
    return f"{sentence}\n\n{cards}" if cards else sentence


def profile_context(profile, site, material, budget=DEFAULT_CONTEXT_BUDGET):
    """The per-item block, for call sites that append to a system prompt."""
    cards = [card for card in select_cards(profile, material, site) if card["tier"] != TIER_ALWAYS]
    return _render(cards, "Also relevant here:", budget)


def profile_offers(cards):
    """Selected cards as compact prompt rows, for JSON user payloads."""
    offers = []
    for card in cards:
        offer = {"card": card["name"], "facts": card["facts"]}
        if card.get("matched"):
            offer["because"] = card["matched"]
        offers.append(offer)
    return offers


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def profile_fingerprint(profile_hash):
    return profile_hash[:16] if profile_hash and profile_hash != "none" else None


def profile_state(profile_path, profile_hash, site):
    """Serializable identity used by resumable workflows."""
    return {
        "profile_path": str(profile_path) if profile_path else None,
        "profile_hash": profile_fingerprint(profile_hash),
        "profile_compiler_version": COMPILED_PROFILE_VERSION,
        "profile_context_mode": site["context_mode"] if site else None,
    }


def profile_digest(profile):
    """Counts for the doctor report."""
    if not profile:
        return {"cards": 0, "facts": 0, **{tier: 0 for tier in TIER_VALUES}, "route_gated": 0, "chars": 0}
    cards = profile.get("cards", [])
    digest = {
        "cards": len(cards),
        "facts": sum(len(card["facts"]) for card in cards),
        "route_gated": sum(1 for card in cards if card["routes"]),
        "chars": sum(len(fact) + 1 for card in cards for fact in card["facts"]),
    }
    for tier in TIER_VALUES:
        digest[tier] = sum(1 for card in cards if card["tier"] == tier)
    return digest


__all__ = [
    "COMPILED_PROFILE_VERSION",
    "AmbiguousProfileError",
    "DEFAULT_CONTEXT_BUDGET",
    "DEFAULT_PREFIX_BUDGET",
    "DEFAULT_PROFILE",
    "MAX_CARD_CHARS",
    "MAX_OWNER_FIELD_CHARS",
    "MAX_SELECTED_CARDS",
    "OWNER_FIELDS",
    "OWNER_SECTION",
    "PROFILE_BASENAME",
    "TIER_ALWAYS",
    "TIER_RELEVANT",
    "TIER_REQUEST",
    "TIER_VALUES",
    "compiled_profile_for",
    "expand_routes",
    "load_profile",
    "owner_sentence",
    "parse_card_note",
    "parse_owner",
    "parse_profile_note",
    "profile_context",
    "profile_digest",
    "profile_fingerprint",
    "profile_offers",
    "profile_prefix",
    "profile_site",
    "profile_state",
    "resolve_profile_or_warn",
    "resolve_profile_path",
    "select_cards",
]

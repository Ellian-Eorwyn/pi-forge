#!/usr/bin/env python3
"""A media catalog for an Obsidian vault: what was taken in, and what it was worth.

The vault already knows how to hold a book you read *as a source* — `type: source`
under the Sources root, filed by kind, cited from a dissertation chapter. This
skill is for the other relationship: the novel you read on a train, the album you
had on all August, the game you finished and have opinions about. Those are
`type: work` notes under `entertainment`, and the schema has had a place for them
since before there was anything to put in it.

Three ideas hold it together.

**Metadata is fetched; judgment is quoted.** Title, year, director, ISBN, and
cover come from a provider and are checkable. The rating and the thoughts come
from the owner and are copied, never composed. A note where those two kinds of
content are indistinguishable would be worthless in a year, so they live in
different blocks: fetched facts in the `## Details` table, the owner's words
under `## Thoughts`, and the one drafted sentence in the lead callout, verified
against the fetched record before anyone sees it.

**A rating the owner did not give does not exist.** Every provider returns a
score of some kind and every one of them is a fact about other people. They are
recorded under their own names in the Details table — "Metacritic", "TMDB
average" — where they cannot be read as a personal verdict. The `rating`
property is human-owned in the schema, which means the classifier is never shown
it and never sets it, and this skill only writes what the owner actually said.

**The no-key tier is the real one.** Books, music, shows and PC games all work
with no credentials at all. Films need a TMDB key because no keyless film source
exists; console games need IGDB. A provider that needs a key it does not have is
skipped *by name and with a reason*, because "nothing matched" and "TMDB is not
configured" are different answers and only one of them is the owner's to fix.
"""

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SKILL_DIR))
sys.path.insert(0, str(SKILL_DIR.parents[2] / "lib"))

import media_http
import media_notes
import media_providers
from media_providers import MEDIA_KINDS

try:
    import forge_llm
    import forge_routing
except ImportError:  # pragma: no cover - only when run outside the forge tree
    forge_llm = None
    forge_routing = None

from vault_schema import (  # noqa: E402
    UserError,
    parse_frontmatter,
    resolve_schema_path,
)
import vault_schema  # noqa: E402

RUN_ROOT = ".vault-media"
MEDIUM_CHOICES = list(MEDIA_KINDS)

# Model stages. Both are new and neither is measured, so they appear in no
# routing table and fall through to `chat` -- a stage in neither STAGE_SERVICES
# nor STAGES_HELD_ON_CHAT is unmeasured, not "fine on the default".
STAGE_PARSE = "parse-media-request"
STAGE_LEAD = "draft-media-lead"

LEAD_SYSTEM = """You write one or two sentences introducing a work, for a personal catalog note.

You are given a record fetched from a media database. Write only what that record
supports. If the record does not name a director, do not name one; if it gives no
year, do not give one. You may not add plot details, critical reception,
influences, awards, or trivia from your own knowledge, however confident you are.
Anything you cannot point at in the record does not go in the sentence.

Do not evaluate the work. No "acclaimed", "beloved", "masterful", "underrated".
The reader's own opinion goes elsewhere in the note and yours is not wanted.

`subjects`, `genres` and `platforms` are catalog classifications, not a reading of
the work. Say a book is filed under a subject, or say nothing; do not turn
"Labyrinths, Curiosities and wonders" into "explores themes of labyrinths and
wonder". The record says where a librarian shelved it, not what it is about.

Prefer the plain facts a catalog card would carry: who made it, when, and what
form it takes. A short sentence that says only that is a good answer.

Return JSON: {"lead": "<one or two sentences>"}"""

PARSE_SYSTEM = """You extract what someone wants added to their media catalog.

Return every distinct work mentioned, one object each. For each, give the title
as they said it, the medium if it is clear, a year if they gave one, their rating
if they gave a number, and their opinion as a VERBATIM SPAN of their own words.

The `thoughts` field must be text copied exactly from the input. Do not
paraphrase, tidy, complete, or improve it. If they said nothing about what they
thought, use null. Never write an opinion they did not express.

The `rating` field is a number from 1 to 10 and only when they gave one. "I loved
it" is not a rating. Use null.

Return JSON: {"items": [{"title": str, "medium": "book"|"music"|"game"|"movie"|"show"|null,
"year": int|null, "rating": int|null, "thoughts": str|null}]}"""


# ----------------------------------------------------------------------------
# Result shape (forge/SCRIPT_TOOL_CONTRACT.md)
# ----------------------------------------------------------------------------


def ok(data=None, artifacts=None, warnings=None):
    return {"status": "ok", "artifacts": artifacts or [], "warnings": warnings or [], "errors": [], "data": data}


def fail(code, message, data=None):
    return {
        "status": "error",
        "artifacts": [],
        "warnings": [],
        "errors": [{"code": code, "message": message}],
        "data": data,
    }


def emit(result):
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if result["status"] == "ok" else 1


# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------


def api_keys(env=None):
    """Keys from ``connectedServices.apiKeys``, each overridable by environment.

    Same ladder as every other forge provider: ``FORGE_API_KEY_TMDB`` beats the
    persisted value. Only the media providers are read out; an empty string is
    treated as absent, because a key persisted as "" would make a provider send
    `Bearer ` and read the resulting 401 as the service refusing us.
    """
    env = env if env is not None else os.environ
    keys = {}
    if forge_llm is not None:
        services = forge_llm.load_connected_services(env)
        persisted = services.get("apiKeys") if isinstance(services.get("apiKeys"), dict) else {}
        for provider in media_providers.MEDIA_PROVIDERS:
            value = persisted.get(provider)
            if isinstance(value, str) and value.strip():
                keys[provider] = value.strip()
    for provider in media_providers.MEDIA_PROVIDERS:
        override = env.get(f"FORGE_API_KEY_{provider.upper()}")
        if isinstance(override, str) and override.strip():
            keys[provider] = override.strip()
    return keys


def load_schema(vault, schema_flag=None):
    schema_path = resolve_schema_path(vault, schema_flag)
    schema = vault_schema.parse_schema_note(schema_path.read_bytes().decode("utf-8-sig"))
    return schema, schema_path


def destination(vault, schema, medium):
    domain = schema["domains"].get("entertainment")
    if not domain:
        raise UserError(
            "this vault's schema has no `entertainment` domain; add the row to the Domains table first"
        )
    subdomain_value = media_notes.MEDIUM_SUBDOMAIN[medium]
    subdomain = (schema["subdomains"].get("entertainment") or {}).get(subdomain_value)
    if not subdomain:
        raise UserError(f"this vault's schema has no `entertainment/{subdomain_value}` subdomain")
    return vault / vault_schema.domain_folder(domain) / vault_schema.subdomain_folder(domain, subdomain)


def run_directory(vault, mode):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    path = vault / RUN_ROOT / "runs" / f"{stamp}-{mode}"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ----------------------------------------------------------------------------
# doctor
# ----------------------------------------------------------------------------


def cmd_doctor(args):
    vault = Path(args.vault).expanduser().resolve()
    checks, warnings = [], []

    checks.append({"check": "vault", "ok": vault.is_dir(), "detail": str(vault)})
    checks.append({"check": "vault_writable", "ok": os.access(vault, os.W_OK), "detail": str(vault)})

    schema = None
    try:
        schema, schema_path = load_schema(vault, args.schema)
        checks.append({"check": "schema", "ok": True, "detail": str(schema_path)})
    except (UserError, OSError) as exc:
        checks.append({"check": "schema", "ok": False, "detail": str(exc)})

    if schema:
        has_domain = "entertainment" in schema["domains"]
        checks.append(
            {"check": "schema_domain_entertainment", "ok": has_domain,
             "detail": "declared" if has_domain else "add an `entertainment` row to the Domains table"}
        )
        subs = schema["subdomains"].get("entertainment") or {}
        missing = [v for v in media_notes.MEDIUM_SUBDOMAIN.values() if v not in subs]
        checks.append(
            {"check": "schema_subdomains", "ok": not missing,
             "detail": "all five declared" if not missing else f"missing: {', '.join(missing)}"}
        )
        rating_ok = "rating" in schema["property_order"]
        human_owned = "rating" in vault_schema.human_owned_properties(schema)
        checks.append(
            {"check": "schema_property_rating", "ok": rating_ok and human_owned,
             "detail": "approved and human-owned" if rating_ok and human_owned
             else ("declared but NOT human-owned - the classifier would invent ratings" if rating_ok
                   else "not in Approved properties")}
        )
        if rating_ok and not human_owned:
            warnings.append(
                "`rating` is approved but not marked human-owned; the classifier will be shown it and will fill it in"
            )
        for medium in MEDIUM_CHOICES:
            try:
                folder = destination(vault, schema, medium)
                checks.append({"check": f"folder_{medium}", "ok": folder.is_dir(), "detail": str(folder)})
            except UserError as exc:
                checks.append({"check": f"folder_{medium}", "ok": False, "detail": str(exc)})

    checks.append({"check": "tls_trust_store", "ok": True, "detail": media_http.ssl_source()})

    keys = api_keys()
    providers = []
    for provider_id, provider in sorted(media_providers.MEDIA_PROVIDERS.items()):
        capabilities = provider["capabilities"]()
        needs_key = bool(capabilities.get("auth_required"))
        configured = bool(keys.get(provider_id))
        entry = {
            "provider": provider_id,
            "media": provider["media"],
            "keyRequired": needs_key,
            "keyConfigured": configured,
            "rateLimit": capabilities.get("rateLimit"),
        }
        if args.probe and (configured or not needs_key):
            probe_query = {"book": "dune", "music": "kind of blue", "show": "severance",
                           "movie": "arrival", "game": "hades"}[provider["media"][0]]
            try:
                context = {
                    "base": media_providers.provider_base(provider_id),
                    "limiter": media_http.HostLimiter(spacing_ms=provider.get("spacing_ms", 0)),
                    "limit": 1,
                    "api_key": keys.get(provider_id),
                    "kind": "movie",
                }
                results = provider["search"](probe_query, context)
                entry["probe"] = {"ok": True, "results": len(results),
                                  "budget": context["limiter"].budgets() or None}
            except media_http.MediaHTTPError as exc:
                entry["probe"] = {"ok": False, "error": exc.code, "detail": str(exc)[:160]}
        elif args.probe:
            entry["probe"] = {"ok": False, "error": "no_key", "detail": "skipped; no key configured"}
        providers.append(entry)

    if not keys.get("tmdb"):
        warnings.append("no TMDB key: `movie` searches cannot run at all, and `show` falls back to TVmaze")
    if not keys.get("igdb"):
        warnings.append("no IGDB key: `game` searches reach PC titles through Steam only")

    if forge_llm is not None:
        for stage in (STAGE_PARSE, STAGE_LEAD):
            service = forge_routing.service_name_for(stage) if forge_routing else "chat"
            checks.append({"check": f"stage_{stage}", "ok": True, "detail": f"routes to `{service}`"})
        try:
            reachable = forge_llm.service_doctor(forge_llm.resolve_service("chat"))
            checks.append({"check": "chat_endpoint", "ok": bool(reachable.get("reachable")),
                           "detail": json.dumps(reachable)[:200]})
        except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
            checks.append({"check": "chat_endpoint", "ok": False, "detail": str(exc)[:160]})
    else:
        warnings.append("forge_llm not importable; `add` will run without a drafted lead")

    failed = [c["check"] for c in checks if not c["ok"]]
    return ok(
        data={"checks": checks, "providers": providers, "failed": failed,
              "keysConfigured": sorted(keys)},
        warnings=warnings,
    )


# ----------------------------------------------------------------------------
# search
# ----------------------------------------------------------------------------


def cmd_search(args):
    report = media_providers.search(
        args.medium,
        args.query,
        api_keys=api_keys(),
        limit=args.limit,
        year_hint=args.year,
    )
    # Bound what comes back: stdout is spent out of the caller's context window,
    # and a full provider record per candidate is several hundred tokens of
    # nothing anyone reads.
    trimmed = [
        {
            "id": f"{c['provider']}:{c['externalId']}",
            "title": c["title"],
            "year": c["year"],
            "creators": c["creators"][:3],
            "matchScore": c.get("matchScore"),
            "url": c["url"],
        }
        for c in report["results"][: args.limit]
    ]
    return ok(
        data={
            "medium": report["medium"],
            "query": report["query"],
            "candidates": trimmed,
            "attempts": report["attempts"],
            "skipped": report["skipped"],
        },
        warnings=[s["detail"] for s in report["skipped"]],
    )


# ----------------------------------------------------------------------------
# add
# ----------------------------------------------------------------------------


def draft_lead(medium, item, warnings):
    """One or two sentences about the work, grounded in the fetched record.

    Returns ``(lead, verified)``. When the model is unreachable the note is
    still written, with the lead omitted rather than guessed: a media note with
    no summary is merely plainer, while one with an invented summary is wrong in
    a way nobody will catch a year from now.
    """
    if forge_llm is None:
        warnings.append("forge_llm unavailable; note written with no lead sentence")
        return None, False

    record = {
        "title": item.get("title"),
        "year": item.get("year"),
        "creators": item.get("creators"),
        "medium": medium,
        **{k: v for k, v in (item.get("detail") or {}).items() if k not in ("providerScore",)},
    }
    messages = [
        {"role": "system", "content": LEAD_SYSTEM},
        {"role": "user", "content": json.dumps(record, ensure_ascii=False, indent=1)},
    ]
    try:
        service = forge_routing.service_for(STAGE_LEAD, None) if forge_routing else forge_llm.resolve_service("chat")
        value, _record = forge_llm.call_json_with_retry(
            service, messages, temperature=0, max_tokens=300, task=STAGE_LEAD
        )
    except Exception as exc:  # noqa: BLE001 - a drafting failure degrades the note, never fails the run
        warnings.append(f"lead drafting failed ({str(exc)[:120]}); note written with no lead sentence")
        return None, False

    lead = (value or {}).get("lead")
    if not isinstance(lead, str) or not lead.strip():
        warnings.append("lead drafting returned nothing usable; note written with no lead sentence")
        return None, False
    lead = lead.strip()

    unsupported = ungrounded_terms(lead, record)
    if unsupported:
        warnings.append(
            "lead mentioned "
            + ", ".join(sorted(unsupported)[:4])
            + " which the fetched record does not contain; lead dropped"
        )
        return None, False
    return lead, True


YEAR_RE = re.compile(r"\b(1[6-9]\d{2}|20\d{2}|21\d{2})\b")
_STOPWORDS = frozenset(
    """a an and are as at be by for from has have in is it its of on or that the to was were will with
    this these those which who whom whose about into over under after before between during""".split()
)


def ungrounded_terms(lead, record):
    """Capitalized names and years in the lead that the record never mentions.

    This is the mechanical form of the grounding rule: model knowledge is an
    index, not a source, so a claim that cannot be pointed at in the fetched
    record does not belong in the note. It deliberately checks only proper nouns
    and years — the two things a model confabulates that a reader would take as
    fact — rather than trying to verify prose, which it cannot do.
    """
    haystack = json.dumps(record, ensure_ascii=False).casefold()
    unsupported = set()
    for year in YEAR_RE.findall(lead):
        if year not in haystack:
            unsupported.add(year)
    # Proper nouns, ignoring the first word of each sentence, which is
    # capitalized for grammar rather than because it names anything.
    for sentence in re.split(r"(?<=[.!?])\s+", lead):
        words = sentence.split()
        for word in words[1:]:
            bare = word.strip("\"'“”‘’(),.;:!?—–-")
            if len(bare) < 3 or not bare[0].isupper() or bare.casefold() in _STOPWORDS:
                continue
            if bare.casefold() not in haystack:
                unsupported.add(bare)
    return unsupported


def cmd_add(args):
    vault = Path(args.vault).expanduser().resolve()
    schema, _schema_path = load_schema(vault, args.schema)
    warnings = []

    report = media_providers.search(
        args.medium, args.query, api_keys=api_keys(), limit=max(args.limit, 5), year_hint=args.year
    )
    warnings.extend(s["detail"] for s in report["skipped"])
    if not report["results"]:
        return fail(
            "no_match",
            f"no {args.medium} matched {args.query!r}",
            data={"attempts": report["attempts"], "skipped": report["skipped"]},
        )

    if args.pick:
        chosen = next((c for c in report["results"] if f"{c['provider']}:{c['externalId']}" == args.pick), None)
        if chosen is None:
            return fail("no_such_candidate", f"{args.pick} is not among the candidates for {args.query!r}")
    else:
        chosen = report["results"][0]
        runner_up = report["results"][1] if len(report["results"]) > 1 else None
        # A close second means the top hit is a guess. Say so rather than
        # letting a confident-looking note stand on a coin flip.
        if runner_up and (chosen.get("matchScore", 0) - runner_up.get("matchScore", 0)) < 15:
            warnings.append(
                f"top match {chosen['title']!r} ({chosen.get('matchScore')}) barely beat "
                f"{runner_up['title']!r} ({runner_up.get('matchScore')}); use --pick to be sure"
            )

    lead, verified = draft_lead(args.medium, chosen, warnings)

    provenance = (
        f"Catalogued by vault-media from {chosen['provider']} "
        f"({chosen.get('url') or chosen['externalId']}) on {datetime.date.today().isoformat()}."
    )
    if lead:
        provenance += " Lead drafted from that record and checked against it."
    if args.rating is not None or (args.thoughts or "").strip():
        provenance += " Rating and thoughts are the author's own."

    filename, text = media_notes.build_note(
        medium=args.medium,
        item=chosen,
        property_order=schema["property_order"],
        lead=lead,
        thoughts=args.thoughts,
        rating=args.rating,
        status=args.status,
        date=args.date or datetime.date.today().isoformat(),
        parent=media_notes.MEDIUM_HUB[args.medium],
        provenance=provenance,
    )

    folder = destination(vault, schema, args.medium)
    target = folder / filename
    run_dir = run_directory(vault, "add")
    proposal = {
        "id": f"{chosen['provider']}:{chosen['externalId']}",
        "medium": args.medium,
        "title": chosen["title"],
        "path": str(target.relative_to(vault)),
        "exists": target.exists(),
        "rating": args.rating,
        "hasThoughts": bool((args.thoughts or "").strip()),
        "leadVerified": verified,
    }
    (run_dir / "proposal.json").write_text(json.dumps(proposal, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (run_dir / "note.md").write_text(text, encoding="utf-8")

    if target.exists() and not args.overwrite:
        return fail(
            "exists",
            f"{target.relative_to(vault)} already exists; pass --overwrite to replace it",
            data={"proposal": proposal, "runDirectory": str(run_dir)},
        )

    if not args.apply:
        return ok(
            data={"dryRun": True, "proposal": proposal, "runDirectory": str(run_dir), "preview": text},
            artifacts=[str(run_dir / "note.md")],
            warnings=warnings,
        )

    folder.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return ok(
        data={"dryRun": False, "proposal": proposal, "runDirectory": str(run_dir)},
        artifacts=[{"path": str(target.relative_to(vault)), "kind": "media-note"}],
        warnings=warnings,
    )


# ----------------------------------------------------------------------------
# backlog
# ----------------------------------------------------------------------------


def backlog_path(vault, schema, medium):
    return destination(vault, schema, medium) / (media_notes.MEDIUM_BACKLOG[medium] + ".md")


def cmd_backlog(args):
    vault = Path(args.vault).expanduser().resolve()
    schema, _ = load_schema(vault, args.schema)
    path = backlog_path(vault, schema, args.medium)
    warnings = []

    report = media_providers.search(args.medium, args.query, api_keys=api_keys(), limit=5, year_hint=args.year)
    warnings.extend(s["detail"] for s in report["skipped"])
    if report["results"]:
        item = report["results"][0]
    else:
        # A backlog entry is worth keeping even with nothing fetched: wanting to
        # read something is not contingent on a database having heard of it.
        item = media_providers.candidate("manual", args.query, args.query, year=args.year)
        warnings.append(f"nothing matched {args.query!r}; recorded as a bare title")

    row = media_notes.backlog_row(item, args.why)
    if not path.exists():
        return fail(
            "no_backlog_note",
            f"{path.relative_to(vault)} does not exist; create the backlog note first",
            data={"row": row},
        )

    text = path.read_text(encoding="utf-8")
    existing = media_notes.parse_backlog_table(text)
    if any(r["cells"] and r["cells"][0].casefold() == row[0].casefold() for r in existing):
        return fail("already_listed", f"{item['title']!r} is already on {path.stem}")

    lines = text.splitlines()
    insert_at = (existing[-1]["line"] + 1) if existing else _table_insert_point(lines)
    lines.insert(insert_at, "| " + " | ".join(row) + " |")
    updated = "\n".join(lines).rstrip() + "\n"

    if not args.apply:
        return ok(data={"dryRun": True, "path": str(path.relative_to(vault)), "row": row}, warnings=warnings)
    path.write_text(updated, encoding="utf-8")
    return ok(
        data={"dryRun": False, "path": str(path.relative_to(vault)), "row": row},
        artifacts=[{"path": str(path.relative_to(vault)), "kind": "backlog-note"}],
        warnings=warnings,
    )


def _table_insert_point(lines):
    for index, line in enumerate(lines):
        if line.strip().startswith("|") and set(line.strip().strip("|").replace(" ", "")) <= {"-", ":", "|"}:
            return index + 1
    return len(lines)


def cmd_promote(args):
    """Turn a backlog row into a full note, reusing what was already fetched.

    This is the one weakness of keeping the backlog as separate list notes: the
    thing gets looked up twice and the two records can disagree. Promoting from
    the row rather than from scratch is what closes it.
    """
    vault = Path(args.vault).expanduser().resolve()
    schema, _ = load_schema(vault, args.schema)
    path = backlog_path(vault, schema, args.medium)
    if not path.exists():
        return fail("no_backlog_note", f"{path.relative_to(vault)} does not exist")

    text = path.read_text(encoding="utf-8")
    rows = media_notes.parse_backlog_table(text)
    match = next((r for r in rows if r["cells"] and r["cells"][0].casefold() == args.title.casefold()), None)
    if match is None:
        listed = ", ".join(r["cells"][0] for r in rows if r["cells"])[:200]
        return fail("not_listed", f"{args.title!r} is not on {path.stem}; it lists: {listed or '(nothing)'}")

    add_args = argparse.Namespace(
        vault=str(vault), schema=args.schema, medium=args.medium, query=args.title,
        year=_row_year(match["cells"]), limit=5, pick=None, rating=args.rating,
        thoughts=args.thoughts, status=args.status, date=args.date, apply=args.apply,
        overwrite=args.overwrite,
    )
    result = cmd_add(add_args)
    if result["status"] != "ok":
        return result

    if args.apply:
        lines = text.splitlines()
        del lines[match["line"]]
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        result["data"]["removedFromBacklog"] = str(path.relative_to(vault))
        result["artifacts"].append({"path": str(path.relative_to(vault)), "kind": "backlog-note"})
    else:
        result["data"]["wouldRemoveFromBacklog"] = str(path.relative_to(vault))
    return result


def _row_year(cells):
    if len(cells) < 2:
        return None
    try:
        return int(cells[1].strip())
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------
# parse (the conversational front door)
# ----------------------------------------------------------------------------


def cmd_parse(args):
    """Turn "add Hades and the new Murderbot book, both an 8" into structured items.

    One work per returned object, and the opinion is a verbatim span rather than
    a summary. The verbatim requirement is checked here rather than trusted:
    a `thoughts` value that is not literally present in the input is dropped,
    because a paraphrase filed under the owner's name is a fabrication even when
    it is accurate.
    """
    if forge_llm is None:
        return fail("no_model", "forge_llm is not importable; cannot parse a request")
    text = args.text or sys.stdin.read()
    if not text.strip():
        return fail("empty", "no request text given")

    messages = [{"role": "system", "content": PARSE_SYSTEM}, {"role": "user", "content": text}]
    service = forge_routing.service_for(STAGE_PARSE, None) if forge_routing else forge_llm.resolve_service("chat")
    value, _record = forge_llm.call_json_with_retry(
        service, messages, temperature=0, max_tokens=800, task=STAGE_PARSE
    )

    warnings = []
    items = []
    haystack = _normalize_spaces(text).casefold()
    for raw in (value or {}).get("items") or []:
        if not isinstance(raw, dict) or not (raw.get("title") or "").strip():
            continue
        thoughts = raw.get("thoughts")
        if isinstance(thoughts, str) and thoughts.strip():
            if _normalize_spaces(thoughts).casefold() not in haystack:
                warnings.append(
                    f"dropped thoughts for {raw['title']!r}: the model returned a paraphrase, not your words"
                )
                thoughts = None
        else:
            thoughts = None
        rating = raw.get("rating")
        if rating is not None:
            try:
                rating = int(rating)
                if not 1 <= rating <= 10:
                    raise ValueError
            except (TypeError, ValueError):
                warnings.append(f"dropped a rating of {raw.get('rating')!r} for {raw['title']!r}: not 1-10")
                rating = None
        medium = raw.get("medium") if raw.get("medium") in MEDIUM_CHOICES else None
        items.append(
            {
                "title": str(raw["title"]).strip(),
                "medium": medium,
                "year": raw.get("year") if isinstance(raw.get("year"), int) else None,
                "rating": rating,
                "thoughts": thoughts,
            }
        )
    return ok(data={"items": items}, warnings=warnings)


def _normalize_spaces(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


# ----------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(prog="vault-media.py")
    sub = parser.add_subparsers(dest="mode", required=True)

    doctor = sub.add_parser("doctor", help="check schema vocabulary, folders, keys, endpoints")
    doctor.add_argument("--vault", required=True)
    doctor.add_argument("--schema")
    doctor.add_argument("--probe", action="store_true", help="also call each usable provider once")
    doctor.set_defaults(handler=cmd_doctor)

    search = sub.add_parser("search", help="query providers for a title; writes nothing")
    search.add_argument("--medium", required=True, choices=MEDIUM_CHOICES)
    search.add_argument("--query", required=True)
    search.add_argument("--year", type=int)
    search.add_argument("--limit", type=int, default=5)
    search.set_defaults(handler=cmd_search)

    add = sub.add_parser("add", help="build a media note from a fetched record plus your own words")
    add.add_argument("--vault", required=True)
    add.add_argument("--schema")
    add.add_argument("--medium", required=True, choices=MEDIUM_CHOICES)
    add.add_argument("--query", required=True)
    add.add_argument("--year", type=int)
    add.add_argument("--limit", type=int, default=5)
    add.add_argument("--pick", help="provider:id from `search`, when the top match is not the one")
    add.add_argument("--rating", type=int, help="1-10, and only what you actually said")
    add.add_argument("--thoughts", help="your own words, copied verbatim")
    add.add_argument("--status", default="complete",
                     choices=["raw", "active", "in-progress", "complete", "someday", "archived"])
    add.add_argument("--date", help="YYYY-MM-DD; when you finished it. Defaults to today")
    add.add_argument("--overwrite", action="store_true")
    add.add_argument("--apply", action="store_true", help="write the note; without this it is a dry run")
    add.set_defaults(handler=cmd_add)

    backlog = sub.add_parser("backlog", help="add a row to a To Read / To Watch list note")
    backlog.add_argument("--vault", required=True)
    backlog.add_argument("--schema")
    backlog.add_argument("--medium", required=True, choices=MEDIUM_CHOICES)
    backlog.add_argument("--query", required=True)
    backlog.add_argument("--year", type=int)
    backlog.add_argument("--why", help="why you want to get to it")
    backlog.add_argument("--apply", action="store_true")
    backlog.set_defaults(handler=cmd_backlog)

    promote = sub.add_parser("promote", help="turn a backlog row into a full note")
    promote.add_argument("--vault", required=True)
    promote.add_argument("--schema")
    promote.add_argument("--medium", required=True, choices=MEDIUM_CHOICES)
    promote.add_argument("--title", required=True)
    promote.add_argument("--rating", type=int)
    promote.add_argument("--thoughts")
    promote.add_argument("--status", default="complete",
                         choices=["raw", "active", "in-progress", "complete", "someday", "archived"])
    promote.add_argument("--date")
    promote.add_argument("--overwrite", action="store_true")
    promote.add_argument("--apply", action="store_true")
    promote.set_defaults(handler=cmd_promote)

    parse_mode = sub.add_parser("parse", help="extract works, ratings and verbatim thoughts from a request")
    parse_mode.add_argument("--text", help="the request; reads stdin when omitted")
    parse_mode.set_defaults(handler=cmd_parse)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return emit(args.handler(args))
    except UserError as exc:
        return emit(fail("user_error", str(exc)))
    except media_http.MediaHTTPError as exc:
        return emit(fail(exc.code, str(exc)))
    except ValueError as exc:
        return emit(fail("invalid_input", str(exc)))


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Field observations and phenology for a vault's natural-history wiki.

The species cards in the wiki say what a raccoon is. This skill covers the other
half of a naturalism practice: what it is doing here, this month, and what the
owner actually saw.

Three ideas hold it together.

**Researched and observed knowledge live in one table, distinguishable.** Every
phenology row carries a ``Basis`` cell -- ``sourced``, ``inferred``, or
``observed``. ``vault-wiki`` may write the first two and is forbidden the third,
so a window derived from the owner's own records can never be confused with one a
model drafted, and a card can carry both without either laundering the other.

**Region is a value, not an assumption.** A window with no region attached is not
a fact about anywhere: raccoons breed two months apart across their range. The
region a query is asked in comes from ``home region`` in the vault's Personal
Context note, so moving house is a one-line edit rather than a sweep through
every card. A species with no row for the active region reports as missing data
rather than answering with somewhere else's calendar.

**Observations are notes; measurements are not.** An observation is a thing the
owner saw once, which is exactly what a note is for, and it needs no property the
vault schema does not already have -- ``date`` for when, ``parent`` for the
species card, ``related`` for the place. A weather station emitting a reading
every few minutes is not that, and turning a year of it into notes would bury a
2,500-note vault; that data belongs in a store beside the vault, which a later
mode will read.

Everything here is deterministic. No model is called, nothing is fetched, and
``compile`` and ``report`` never write to a note.
"""

import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
import vault_phenology
import vault_profile
import vault_wiki
from vault_schema import (
    UserError,
    compiled_schema_for,
    compile_destination,
    derived_properties,
    normalized_date_value,
    parse_frontmatter,
    relative_path,
    resolve_schema_path,
    safe_title,
    selected_notes,
    serialize_frontmatter,
    split_frontmatter,
    validate_filename_title,
)

WORKFLOW = "vault-naturalist"
STATE_DIR = ".vault-naturalist"

OBSERVATION_TYPE = "observation"
OBSERVATION_DOMAIN = "nature"
OBSERVATION_SUBDOMAIN = "observations"
RECORD_HEADING = "Record"

# The structured half of an observation. Kept in a body table for the same reason
# phenology is: the vault's approved-property list is closed and global, so a
# `count` property would be inherited by every note type in the vault.
RECORD_COLUMNS = (
    {"id": "field", "heading": "Field", "values": (), "guidance": ""},
    {"id": "value", "heading": "Value", "values": (), "guidance": ""},
)
RECORD_FIELDS = ("Species", "Region", "Place", "Count", "Life stage", "Behavior", "Weather", "Observer")


def structured(status, artifacts=None, warnings=None, errors=None, data=None):
    return {
        "status": status,
        "artifacts": artifacts or [],
        "warnings": warnings or [],
        "errors": errors or [],
        "data": data,
    }


def error_entry(code, message):
    return {"code": code, "message": message}


def print_json(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def skill_root():
    return Path(__file__).resolve().parents[1]


def references_root():
    """The wiki skill's references, which own the kind specs and vocabulary.

    Read rather than copied. Two files naming the same phenology events is one
    file too many: the copy that is not read drifts, and a row drafted against
    one and compiled against the other fails for no visible reason.
    """
    return skill_root().parents[0] / "vault-wiki" / "references"


def load_specs_and_vocabulary():
    specs = vault_wiki.load_kind_specs(references_root() / "wiki-kinds.json")
    vocabulary = vault_phenology.load_event_vocabulary(references_root() / "phenology-events.json")
    return specs, vocabulary


def resolve_vault(raw):
    vault = Path(raw).expanduser().resolve()
    if not vault.is_dir():
        raise UserError(f"vault root does not exist: {vault}")
    return vault


def home_region(vault, raw_profile=None, override=None):
    """The region a query is asked in, and where that answer came from.

    An explicit ``--region`` wins so a query can be asked about somewhere the
    owner does not live -- planning a trip, or checking what a card claims about
    a range it was researched in.
    """
    if override:
        return override, "flag"
    path, _warnings = vault_profile.resolve_profile_or_warn(vault, raw_profile)
    if not path:
        return None, "absent"
    # Never raises: a malformed register costs the layer, not the run.
    profile, _hash, _profile_warnings = vault_profile.compiled_profile_for(
        vault, path, cache_dir=vault / STATE_DIR / "cache"
    )
    region = ((profile or {}).get("owner") or {}).get("home_region")
    return region, "profile" if region else "undeclared"


# --------------------------------------------------------------------------- #
# Phenology
# --------------------------------------------------------------------------- #


def build(vault, schema_path):
    specs, vocabulary = load_specs_and_vocabulary()
    return vault_phenology.build_index(vault, schema_path, specs, vocabulary)


def command_phenology_compile(args):
    vault = resolve_vault(args.vault)
    schema_path = resolve_schema_path(vault, args.schema)
    index = build(vault, schema_path)
    artifacts = []
    if not args.dry_run:
        path, digest = vault_phenology.write_index(vault / STATE_DIR / "cache", index)
        index = dict(index, hash=digest)
        artifacts.append({"path": relative_path(vault, path), "kind": "phenology-index"})
    warnings = []
    if index["problems"]:
        warnings.append(
            f"{len(index['problems'])} phenology rows could not be read and are absent from the index"
        )
    return structured(
        "ok",
        artifacts=artifacts,
        warnings=warnings,
        data={
            "dryRun": args.dry_run,
            "counts": index["counts"],
            "regions": index["regions"],
            "problems": index["problems"],
        },
    )


def command_phenology_report(args):
    vault = resolve_vault(args.vault)
    schema_path = resolve_schema_path(vault, args.schema)
    index = vault_phenology.read_index(vault / STATE_DIR / "cache") if not args.rebuild else None
    stale = index is None
    if index is None:
        index = build(vault, schema_path)
    month = args.month or datetime.date.today().month
    region, region_source = home_region(vault, args.profile, args.region)
    warnings = []
    if stale and not args.rebuild:
        warnings.append("no compiled index was present, so one was built for this report but not written")
    if not region:
        warnings.append(
            "no home region is declared, so every region's windows are reported together; "
            "add a `home region` row to the Owner table in the Personal Context note, or pass --region"
        )
    matches = vault_phenology.expected_in_month(index, month, region=region)
    missing = vault_phenology.species_without_region(index, region) if region else []
    return structured(
        "ok",
        warnings=warnings,
        data={
            "month": month,
            "region": region,
            "regionSource": region_source,
            "expected": [
                {
                    "note": record["note"],
                    "common": record["common"],
                    "scientific": record["scientific"],
                    "kind": record["kind"],
                    "events": record["events"],
                }
                for record in matches
            ],
            "counts": {"species": len(matches), "cardsWithoutLocalData": len(missing)},
            "cardsWithoutLocalData": missing,
        },
    )


# --------------------------------------------------------------------------- #
# Observations
# --------------------------------------------------------------------------- #


def observation_destination(schema, title):
    if OBSERVATION_DOMAIN not in schema["domains"]:
        raise UserError(
            f"the schema note has no '{OBSERVATION_DOMAIN}' domain; add it before recording observations"
        )
    if OBSERVATION_SUBDOMAIN not in schema["subdomains"].get(OBSERVATION_DOMAIN, {}):
        raise UserError(
            f"the schema note has no '{OBSERVATION_DOMAIN}/{OBSERVATION_SUBDOMAIN}' subdomain; add it first"
        )
    if OBSERVATION_TYPE not in schema["types"]:
        raise UserError(f"the schema note has no '{OBSERVATION_TYPE}' note type; add it first")
    folder = compile_destination(schema, {"domain": OBSERVATION_DOMAIN, "subdomain": OBSERVATION_SUBDOMAIN})
    return folder / f"{title}.md"


def observation_title(date, species, place):
    """``YYYY-MM-DD Common Name - Place``.

    The date prefix is load-bearing rather than decorative: it is the evidence
    tier every date backfill in this repo reads first, so an observation note
    keeps its own date even if its frontmatter is ever rebuilt.
    """
    common = species.split(",")[0].strip()
    stem = f"{date} {common}" + (f" - {place}" if place else "")
    return validate_filename_title(safe_title(stem), "observation title")


def record_table(values):
    rows = [{"field": field, "value": values[field]} for field in RECORD_FIELDS if values.get(field)]
    return vault_wiki.render_table(rows, RECORD_COLUMNS)


def observation_body(title, species, note_text, values):
    lines = [f"# {title}", ""]
    if note_text:
        lines.extend([note_text.strip(), ""])
    table = record_table(values)
    if table:
        lines.extend([f"## {RECORD_HEADING}", "", table, ""])
    lines.extend(["## Notes", ""])
    return "\n".join(lines)


def command_observe(args):
    vault = resolve_vault(args.vault)
    schema_path = resolve_schema_path(vault, args.schema)
    schema, _hash = compiled_schema_for(vault, schema_path, cache_dir=vault / STATE_DIR / "cache")

    date = normalized_date_value(args.date) if args.date else datetime.date.today().isoformat()
    if not date:
        raise UserError(f"--date must be a real YYYY-MM-DD date: {args.date}")
    region, _source = home_region(vault, args.profile, args.region)
    warnings = []

    species = args.species.strip()
    card = find_species_card(vault, schema_path, species)
    if card is None:
        warnings.append(
            f"no species card is filed for '{species}'; the observation still links to it, "
            "so creating the card later resolves the link"
        )

    title = observation_title(date, species, (args.place or "").strip())
    destination = observation_destination(schema, title)
    path = vault / destination
    if path.exists():
        raise UserError(f"an observation note already exists at {destination.as_posix()}")

    metadata = {
        "type": OBSERVATION_TYPE,
        "status": "raw",
        "domain": OBSERVATION_DOMAIN,
        "subdomain": OBSERVATION_SUBDOMAIN,
        "date": date,
        "parent": f"[[{species}]]",
        "capture_type": "generated" if "generated" in schema["capture_types"] else None,
    }
    if region:
        metadata["related"] = [f"[[{region}]]"]
    if "created" in derived_properties(schema):
        metadata["created"] = datetime.date.today().isoformat()
    metadata = {key: value for key, value in metadata.items() if value not in (None, "", [])}
    if metadata.get("capture_type") != "generated":
        warnings.append(
            "the schema does not define capture_type: generated, so this note is written unmarked"
        )

    values = {
        "Species": f"[[{species}]]",
        "Region": f"[[{region}]]" if region else "",
        "Place": args.place or "",
        "Count": args.count or "",
        "Life stage": args.life_stage or "",
        "Behavior": args.behavior or "",
        "Weather": args.weather or "",
        "Observer": args.observer or "",
    }
    text = serialize_frontmatter(metadata, schema) + observation_body(title, species, args.note, values)

    if not args.dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return structured(
        "ok",
        artifacts=[] if args.dry_run else [{"path": destination.as_posix(), "kind": "observation"}],
        warnings=warnings,
        data={
            "dryRun": args.dry_run,
            "path": destination.as_posix(),
            "date": date,
            "species": species,
            "region": region,
            "speciesCard": card,
            "body": text if args.dry_run else None,
        },
    )


def find_species_card(vault, schema_path, species):
    """The filed species card a title names, or None.

    Matched on basename, the way Obsidian resolves a ``[[wikilink]]``, so an
    observation can name a card by its title without knowing where it is filed.
    """
    wanted = species.strip().casefold()
    for path in selected_notes(vault, schema_path, "vault", None):
        if path.stem.casefold() != wanted:
            continue
        try:
            split = split_frontmatter(path.read_bytes())
        except OSError:
            continue
        metadata = parse_frontmatter(split["frontmatter_text"])
        if vault_wiki.kind_for_metadata(metadata) in vault_wiki.SPECIES_KINDS:
            return relative_path(vault, path)
    return None


# --------------------------------------------------------------------------- #
# Doctor
# --------------------------------------------------------------------------- #


def command_doctor(args):
    vault = resolve_vault(args.vault)
    schema_path = resolve_schema_path(vault, args.schema)
    schema, schema_hash = compiled_schema_for(vault, schema_path, cache_dir=vault / STATE_DIR / "cache")
    warnings = []
    data = {"vault": str(vault), "schemaHash": schema_hash}

    subdomains = schema["subdomains"].get(vault_wiki.WIKI_DOMAIN, {})
    missing_routes = []
    if vault_wiki.WIKI_DOMAIN not in schema["domains"]:
        missing_routes.append(f"domain `{vault_wiki.WIKI_DOMAIN}`")
    for kind in vault_wiki.SPECIES_KINDS:
        subdomain = vault_wiki.WIKI_KIND_SUBDOMAIN[kind]
        if subdomain not in subdomains:
            missing_routes.append(f"subdomain `{vault_wiki.WIKI_DOMAIN}/{subdomain}`")
    for note_type in (vault_wiki.WIKI_KIND_TYPE["animal"], OBSERVATION_TYPE):
        if note_type not in schema["types"]:
            missing_routes.append(f"note type `{note_type}`")
    if OBSERVATION_DOMAIN not in schema["domains"]:
        missing_routes.append(f"domain `{OBSERVATION_DOMAIN}`")
    elif OBSERVATION_SUBDOMAIN not in schema["subdomains"].get(OBSERVATION_DOMAIN, {}):
        missing_routes.append(f"subdomain `{OBSERVATION_DOMAIN}/{OBSERVATION_SUBDOMAIN}`")
    data["missingSchemaRows"] = missing_routes
    if missing_routes:
        warnings.append(
            "the schema note is missing rows this skill needs: " + ", ".join(missing_routes)
            + ". Adding them is the owner's edit; no skill writes the schema note."
        )

    data["createdProperty"] = "created" in derived_properties(schema)
    if not data["createdProperty"]:
        warnings.append("the schema does not define a derived `created` property; observation notes go undated")

    region, region_source = home_region(vault, args.profile, args.region)
    data["homeRegion"] = region
    data["homeRegionSource"] = region_source
    if not region:
        warnings.append(
            "no `home region` row in the Personal Context Owner table, so no query knows where 'here' is"
        )

    if missing_routes:
        data["phenology"] = None
        return structured("ok", warnings=warnings, data=data)

    specs, vocabulary = load_specs_and_vocabulary()
    # Inspected rather than required: doctor reports, it does not refuse. A
    # missing template blocks `vault-wiki expand`, not the phenology this skill
    # compiles out of cards that already exist.
    templates = {}
    for kind in vault_wiki.SPECIES_KINDS:
        result = vault_wiki.inspect_wiki_template(
            vault, schema, kind,
            required_fields=specs[kind]["required_placeholders"],
            known_fields=specs[kind]["placeholders"],
        )
        templates[kind] = {"ok": result["ok"], "errors": result["errors"]}
    data["templates"] = templates
    absent = sorted(kind for kind, entry in templates.items() if not entry["ok"])
    if absent:
        warnings.append(
            f"species templates not installed or stale: {', '.join(absent)};"
            " run `vault-wiki template-install`"
        )

    index = vault_phenology.build_index(vault, schema_path, specs, vocabulary)
    data["phenology"] = {
        "counts": index["counts"],
        "regions": index["regions"],
        "problems": index["problems"],
    }
    if index["problems"]:
        warnings.append(f"{len(index['problems'])} phenology rows could not be read")
    if region:
        missing = vault_phenology.species_without_region(index, region)
        data["phenology"]["cardsWithoutLocalData"] = missing
        if missing:
            warnings.append(f"{len(missing)} species cards carry no phenology for {region}")
    return structured("ok", warnings=warnings, data=data)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def add_common(parser):
    parser.add_argument("--vault", required=True)
    parser.add_argument("--schema")
    parser.add_argument("--profile")
    parser.add_argument("--region", help="ask about this region instead of the declared home region")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Field observations and phenology for a natural-history wiki.")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check the vocabulary, templates, region, and phenology tables")
    add_common(doctor)

    compile_parser = sub.add_parser("compile", help="compile every Phenology table into a queryable index")
    add_common(compile_parser)
    compile_parser.add_argument("--dry-run", action="store_true")

    report = sub.add_parser("report", help="what is expected this month in the active region")
    add_common(report)
    report.add_argument("--month", type=int, help="1 through 12; defaults to the current month")
    report.add_argument("--rebuild", action="store_true", help="recompile rather than reading the cached index")

    observe = sub.add_parser("observe", help="record one field observation as a note")
    add_common(observe)
    observe.add_argument("--species", required=True, help="the species card's title, e.g. 'Raccoon, Procyon lotor'")
    observe.add_argument("--date", help="YYYY-MM-DD; defaults to today")
    observe.add_argument("--place")
    observe.add_argument("--count")
    observe.add_argument("--life-stage")
    observe.add_argument("--behavior")
    observe.add_argument("--weather")
    observe.add_argument("--observer")
    observe.add_argument("--note", help="free prose describing what was seen")
    observe.add_argument("--dry-run", action="store_true")

    return parser.parse_args(argv)


COMMANDS = {
    "doctor": command_doctor,
    "compile": command_phenology_compile,
    "report": command_phenology_report,
    "observe": command_observe,
}


def main(argv=None):
    args = parse_args(argv)
    try:
        print_json(COMMANDS[args.command](args))
    except UserError as error:
        print_json(structured("error", errors=[error_entry("user_error", str(error))]))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

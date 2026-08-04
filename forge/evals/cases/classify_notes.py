#!/usr/bin/env python3
"""Can the model file a note against a human-maintained schema?

The stage is `vault-organizer`'s classifier: one note in, one JSON object of
schema-constrained frontmatter out. Ground truth is free here — routing is a
pure function of `domain`, `subdomain`, `project`, and `source_kind`, compiled
by `vault_schema.compile_destination`, so the folder a note already lives in is
the answer the classifier should have produced. The note's own frontmatter is
the key; the model is shown the body with the frontmatter removed.

Two things are scored separately on purpose. `validate_classification` is the
gate production runs, and failing it means the reply was unusable at all. Path
agreement is the quality question underneath: a reply can be perfectly valid
and still file the note in the wrong place.
"""

import _common

DIMENSION = "categorization"
SKILL = "vault-organizer"
JUDGE = False

FIXTURES = [
    "classify-reification",
    "classify-agnotology",
    "classify-kuhn",
    "classify-tomatoes",
    "classify-focaccia",
    "classify-calnext",
    "classify-piforge-memo",
    "classify-retrieval-memo",
]

# The properties that decide where a note lands. `status`, `capture_type`, and
# the link properties are real output but they do not move the note, so they are
# reported rather than scored: a disagreement there is a preference, not an error.
ROUTING_PROPERTIES = ("type", "domain", "subdomain", "project", "source_kind")


def _schema():
    return _common.harness.load_lib("vault_schema").parse_schema_note(_common.fixture("vault-schema"))


def _title(fixture_id):
    return _common.harness.fixtures()[fixture_id]["path"].rsplit("/", 1)[-1].removesuffix(".md")


def _truth(fixture_id, schema):
    """The note's own frontmatter, reduced to the routing decision it encodes.

    Normalized through the schema exactly as the model's reply will be, so the
    two are compared on the same footing. `project` is keyed by its wikilink
    (`[[Pi Forge]]`), not by a slug, so nothing is stripped here.
    """
    _, _, parsed = _common.note_parts(fixture_id)
    classification = _common.harness.load_lib("vault_classification")
    schema_lib = _common.harness.load_lib("vault_schema")
    raw = {key: parsed[key] for key in ROUTING_PROPERTIES if parsed.get(key)}
    metadata, _warnings = classification.normalize_metadata(raw, schema)
    try:
        destination = str(schema_lib.compile_destination(schema, metadata))
    except Exception as error:
        raise _common.harness.EvalError(
            f"fixture {fixture_id!r} does not compile to a destination ({error}); "
            "its frontmatter cannot serve as ground truth"
        ) from error
    return metadata, destination


def items():
    return build(FIXTURES)


def build(fixture_ids):
    schema = _schema()
    classification = _common.harness.load_lib("vault_classification")
    organizer = _common.harness.load_skill("vault-organizer")
    built = []
    for fixture_id in fixture_ids:
        _, body, _ = _common.note_parts(fixture_id)
        excerpt, _truncated = organizer.excerpt_body(body)
        truth, destination = _truth(fixture_id, schema)
        built.append(
            {
                "id": fixture_id,
                # The classifier never sees the answer: no frontmatter, and the
                # current path is given as the inbox, because a note already
                # sitting in `02 Craft/2.02 Cooking` has been told where it goes.
                "messages": classification.build_messages(
                    schema,
                    _title(fixture_id),
                    f"00 Inbox/{_title(fixture_id)}.md",
                    "",
                    excerpt,
                    think_prefill=False,
                ),
                "max_tokens": 1024,
                "truth": truth,
                "destination": destination,
            }
        )
    return built


def score(item, content, record=None):
    schema = _schema()
    classification = _common.harness.load_lib("vault_classification")
    schema_lib = _common.harness.load_lib("vault_schema")

    parsed = _common.parse_json(content)
    if parsed is None:
        return _common.failure("reply was not JSON")

    validated, warnings, errors = classification.validate_classification(parsed, schema)
    gates = {"parsed": True, "validates": not errors and validated is not None}
    if validated is None:
        return {"ok": False, "gates": gates, "metrics": {}, "notes": errors}

    metadata = validated["metadata"]
    matched = {key: (item["truth"].get(key) or None) == (metadata.get(key) or None) for key in ROUTING_PROPERTIES}

    try:
        destination = str(schema_lib.compile_destination(schema, metadata))
    except Exception as error:
        destination = None
        errors = [*errors, f"metadata does not compile to a path: {error}"]
    gates["compiles"] = destination is not None
    gates["destinationMatches"] = bool(destination) and destination == item["destination"]

    notes = list(errors)
    if warnings:
        notes.extend(f"warning: {warning}" for warning in warnings)
    if destination and destination != item["destination"]:
        notes.append(f"filed at {destination}, expected {item['destination']}")
    for key, ok in matched.items():
        if not ok:
            notes.append(f"{key}: got {metadata.get(key)!r}, expected {item['truth'].get(key)!r}")

    return {
        "ok": all(gates.values()),
        "gates": gates,
        "metrics": {
            "routingPropertiesCorrect": sum(1 for ok in matched.values() if ok) / len(matched),
            # A classifier that sends everything to review is not doing the job,
            # and one that never does is not noticing when it should.
            "needsReview": 1.0 if validated["needs_review"] else 0.0,
        },
        "notes": notes,
        "output": {"metadata": metadata, "destination": destination, "expected": item["destination"]},
    }

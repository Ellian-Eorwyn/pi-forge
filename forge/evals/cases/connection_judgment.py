#!/usr/bin/env python3
"""Should these two notes be linked? A closed-form judgment with a real answer key.

Sixteen pairs, and the two halves are not equally strong evidence:

- **Eight positives** are pairs Ellie's vault already links, taken from a
  figure's own `related:` list. A missing link there is a real miss, so recall
  on this half is a sound measurement.
- **Eight negatives** are a wiki figure against a gardening, cooking, work, or
  software note. These are floor tests, not discriminators: any usable model
  should reject them. They exist to catch the failure mode where a model says
  yes to everything, which recall alone cannot see.

What is deliberately *not* here is the tempting middle — two densely-linked
concepts from different thinkers that Ellie happens not to have linked. Absence
of a link in a hand-maintained vault is not evidence of non-relation, and
scoring against it would measure how completely the vault is linked rather than
how well the model judges.

Candidate pairs are frozen rather than retrieved. In production, embeddings
narrow the field first — but `embed` and `task` are members of one router with
`MODEL_ROUTER_MAX=1`, so calling both in a loop would time a ~6s model swap per
pair instead of the model.
"""

import _common

DIMENSION = "pair-judgment"
SKILL = "vault-connections"
JUDGE = False

# (left fixture, right fixture, should-connect)
PAIRS = [
    ("figure-thomas-kuhn", "concept-anomaly", True),
    ("figure-thomas-kuhn", "concept-normal-science", True),
    ("figure-bruno-latour", "concept-black-boxing", True),
    ("figure-bruno-latour", "concept-inscription", True),
    ("figure-susan-leigh-star", "concept-boundary-objects", True),
    ("figure-susan-leigh-star", "concept-infrastructural-inversion", True),
    ("figure-ian-hacking", "concept-looping-effects", True),
    ("figure-ian-hacking", "concept-making-up-people", True),
    ("figure-thomas-kuhn", "classify-focaccia", False),
    ("figure-thomas-kuhn", "classify-tomatoes", False),
    ("figure-bruno-latour", "classify-focaccia", False),
    ("figure-bruno-latour", "classify-demand-response", False),
    ("figure-susan-leigh-star", "classify-tomatoes", False),
    ("figure-susan-leigh-star", "classify-demand-response", False),
    ("figure-ian-hacking", "classify-focaccia", False),
    ("figure-ian-hacking", "classify-piforge-memo", False),
]


def _skill():
    return _common.harness.load_skill("vault-connections")


def _entry(fixture_id):
    """The note as the propose stage would carry it: title, path, and filing."""
    path = _common.harness.fixtures()[fixture_id]["path"]
    _frontmatter, body, parsed = _common.note_parts(fixture_id)
    return {
        "title": path.rsplit("/", 1)[-1].removesuffix(".md"),
        "path": path,
        "domain": parsed.get("domain"),
        "subdomain": parsed.get("subdomain"),
    }, body


def items():
    connections = _skill()
    built = []
    for left_id, right_id, expected in PAIRS:
        left, left_body = _entry(left_id)
        right, right_body = _entry(right_id)
        user = (
            f"NOTE A\n{connections.note_brief(left, left_body)}\n\n"
            f"NOTE B\n{connections.note_brief(right, right_body)}"
        )
        built.append(
            {
                "id": f"{left_id}+{right_id}",
                "messages": [
                    # `connection_system(args)` only appends the vault's voice
                    # policy, which is per-vault configuration; the constant is
                    # what stays comparable between runs.
                    {"role": "system", "content": connections.CONNECTION_SYSTEM},
                    {"role": "user", "content": user},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": 512,
                "expected": expected,
            }
        )
    return built


def score(item, content, record=None):
    connections = _skill()
    parsed = _common.parse_json(content)
    if not isinstance(parsed, dict):
        return _common.failure("reply was not a JSON object")
    try:
        judgment = connections.validate_judgment(parsed)
    except Exception as error:
        # Production drops an invalid judgment rather than guessing, so a pair
        # lost here is a pair the propose loop would never have shown.
        return {"ok": False, "gates": {"parsed": True, "validates": False}, "metrics": {}, "notes": [str(error)]}

    correct = judgment["connect"] == item["expected"]
    return {
        "ok": correct,
        "gates": {"parsed": True, "validates": True, "agrees": correct},
        "metrics": {
            # Kept separate so precision and recall can be read off the report
            # rather than averaged into one uninformative accuracy figure.
            "truePositive": 1.0 if item["expected"] and judgment["connect"] else 0.0,
            "falseNegative": 1.0 if item["expected"] and not judgment["connect"] else 0.0,
            "falsePositive": 1.0 if not item["expected"] and judgment["connect"] else 0.0,
            "trueNegative": 1.0 if not item["expected"] and not judgment["connect"] else 0.0,
        },
        "notes": [] if correct else [
            f"said connect={judgment['connect']}, expected {item['expected']}: {judgment['reason']!r}"
        ],
        "output": judgment,
    }

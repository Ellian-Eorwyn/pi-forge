#!/usr/bin/env python3
"""The classification calls where the schema, not the topic, decides.

Three of these are `type: source`, which routes into the sources tree by
`source_kind` rather than into the domain folder the subject would suggest — an
article about role-playing games belongs under `10 Sources/10.02 Article`, not
under Academic, and the `project` it carries must not pull it back out. The
fourth is thirteen words long: almost no signal, and the schema still has one
right answer for it.
"""

import classify_notes

DIMENSION = "categorization"
SKILL = "vault-organizer"
JUDGE = False

FIXTURES = [
    "classify-memo-transcript",
    "classify-retrieval-transcript",
    "classify-zagal",
    "classify-demand-response",
    "classify-website",
    "classify-policy",
    "classify-mycology",
    "classify-lecture",
]


def items():
    return classify_notes.build(FIXTURES)


def score(item, content, record=None):
    return classify_notes.score(item, content, record)

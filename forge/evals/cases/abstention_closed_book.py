#!/usr/bin/env python3
"""What the model knows, against what it will make up when it does not know.

No source. Half the questions have a checkable answer in the domains pi-forge
works in — grid flexibility, data centre cooling, science and technology studies
— and half are about things that do not exist, named in the same register as the
real ones so that pattern-matching produces a confident answer.

This is the parametric half of the pair. It matters because two skills lean on
it directly: `vault-curator` proposes vault schema changes from researched
background, and `web-research` scopes a search before it has read anything. Both
are places where a model that invents a plausible programme name does real
damage that no deterministic check downstream would see.

`abstention-grounded` is the one that should drive routing; this one explains
its results. A model can be scrupulous about a source it was given and still
confabulate freely when it has none.
"""

import _abstention
import _common

DIMENSION = "abstention"
SKILL = "vault-curator"
JUDGE = False
TIER = "quick"

MAX_TOKENS = 256


def items():
    built = []
    for entry in _common.harness.expectations("abstention-closed-book")["items"]:
        built.append(
            {
                **entry,
                "messages": [
                    {"role": "system", "content": _abstention.CLOSED_BOOK_SYSTEM},
                    {"role": "user", "content": entry["question"]},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": MAX_TOKENS,
            }
        )
    return built


def score(item, content, record=None):
    return _abstention.score_reply(item, content, record, "known")

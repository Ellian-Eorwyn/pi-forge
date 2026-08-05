#!/usr/bin/env python3
"""Answering from a source, or saying the source does not answer it.

Half of these questions the transcript answers and half it does not, and the
unanswerable half is what the case is for. Every pi-forge stage that reads a
document is asked to abstain rather than infer, and nothing currently measures
whether a model does. `docs/service-split-handoff.md` §7.4 records the cost of
finding out late: a review pass approved an extraction that tripled a balance
and invented a deadline.

Scored with `_abstention.score_reply`, which prices a wrong answer at exactly
what a right one earns. See that module for why accuracy is the wrong statistic
here.
"""

import _abstention
import _common

DIMENSION = "abstention"
SKILL = "vault-transcripts"
JUDGE = False
TIER = "quick"

MAX_TOKENS = 256


def items():
    built = []
    for entry in _common.harness.expectations("abstention-grounded")["items"]:
        source = _common.fixture(entry["fixture"])
        built.append(
            {
                **entry,
                "messages": [
                    {"role": "system", "content": _abstention.SYSTEM},
                    # Source first, question last: the source is the long part
                    # and several items share one, so putting it in the stable
                    # prefix lets the server's cache carry it between questions.
                    {"role": "user", "content": f"Source:\n\n{source}\n\nQuestion: {entry['question']}"},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": MAX_TOKENS,
            }
        )
    return built


def score(item, content, record=None):
    return _abstention.score_reply(item, content, record, "grounded")

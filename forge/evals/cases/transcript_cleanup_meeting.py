#!/usr/bin/env python3
"""Multi-speaker meetings: the same cleanup with turn structure to preserve.

Diarized labels across eight chunks of two different meetings — two meetings
rather than one so the case is not eight windows of a single conversation. The extra thing being measured here is
whether the turn structure survives — a model that merges two speakers into one
paragraph has lost information no word-level check would catch, which is why the
labels are also read by `check_chunk`.
"""

import _cleanup

DIMENSION = "faithful-cleanup"
SKILL = "vault-transcripts"
JUDGE = True

FIXTURES = ["transcript-vpp-chunk", "transcript-brattle"]
MAX_CHUNKS = 8


def items():
    # One meeting, chunked. Eight windows of the same conversation exercise the
    # same turn-structure problem repeatedly, which is what the count needs to be
    # for a verdict — and the later chunks are mid-discussion rather than
    # introductions, so they are not all the same shape.
    return _cleanup.build(FIXTURES)[:MAX_CHUNKS]


def score(item, content, record=None):
    return _cleanup.score(item, content, record)


def repair(item, content, scored):
    return _cleanup.repair(item, content, scored)


def judge_context(item_id):
    return _cleanup.context(item_id)

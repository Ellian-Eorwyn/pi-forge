#!/usr/bin/env python3
"""Long-context reasoning at the lcr-60k rung. See `cases/_longcontext.py`."""

import _longcontext

DIMENSION = "long-context"
SKILL = "vault-projects"
JUDGE = False
TIER = "standard"
MIN_CONTEXT_TOKENS = 62000

RUNG = _longcontext.RUNGS["lcr-60k"]


def items():
    return _longcontext.build(RUNG)


def score(item, content, record=None):
    return _longcontext.score(item, content, record)

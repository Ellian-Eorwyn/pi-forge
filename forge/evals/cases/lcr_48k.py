#!/usr/bin/env python3
"""Long-context reasoning at the lcr-48k rung. See `cases/_longcontext.py`."""

import _longcontext

DIMENSION = "long-context"
SKILL = "vault-projects"
JUDGE = False
TIER = "standard"
MIN_CONTEXT_TOKENS = 50000

RUNG = _longcontext.RUNGS["lcr-48k"]


def items():
    return _longcontext.build(RUNG)


def score(item, content, record=None):
    return _longcontext.score(item, content, record)

#!/usr/bin/env python3
"""Single-speaker voice notes: does the cleanup keep the speaker's own words?

Seven of these eight have a counterpart the pipeline already produced and Ellie
kept, so the judge has a reference to read against. The eighth is raw ASR
straight out of the inbox — filler, self-repair, and the speaker correcting the
transcriber mid-sentence ("Pi is spelled P I, not P Y") — which is the case
where a model that paraphrases instead of condensing shows itself.
"""

import _cleanup

DIMENSION = "faithful-cleanup"
SKILL = "vault-transcripts"
JUDGE = True

FIXTURES = [
    "transcript-context",
    "transcript-retrieval",
    "raw-asr-piforge",
    "transcript-export",
    "transcript-deepsearch",
    "transcript-knowledgebase",
    "transcript-vaultintegration",
    "transcript-vaultmanager",
]


def items():
    return _cleanup.build(FIXTURES)


def score(item, content, record=None):
    return _cleanup.score(item, content, record)


def repair(item, content, scored):
    return _cleanup.repair(item, content, scored)


def judge_context(item_id):
    return _cleanup.context(item_id)

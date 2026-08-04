#!/usr/bin/env python3
"""Shared setup for the cases that drive `vault-transcripts`.

The pipeline normally derives its `record` from a classification call. The
cleanup and summary cases skip that on purpose: a cleanup scored against a
record the same model just got wrong measures two things at once. So each
fixture declares its record here, taken from the frontmatter the pipeline
already wrote for that note.

Voice, profile, and lexicon are all left unset. Each of them splices per-vault
configuration into the prompt, and a benchmark whose prompt changes when Ellie
edits a preferences note is not measuring the model.
"""

import _common

CHECK_SPEAKER_MAP = {}

# recording_type and material_role come from the note's own frontmatter and
# filename; speakers from what the transcript actually contains.
RECORDS = {
    "transcript-context": {
        "recording_type": "memo",
        "material_role": "primary",
        "title": "Personal Context Notes For Local LLM",
        "speakers": {},
        "effective_speakers": 1,
        "drop_labels": True,
        "reference": "transcript-context-cleaned",
    },
    "transcript-retrieval": {
        "recording_type": "memo",
        "material_role": "primary",
        "title": "Improving Vault Context Retrieval",
        "speakers": {},
        "effective_speakers": 1,
        "drop_labels": True,
        "reference": "transcript-retrieval-cleaned",
    },
    "raw-asr-piforge": {
        "recording_type": "memo",
        "material_role": "primary",
        "title": "Research Extension For Qualitative Coding",
        "speakers": {},
        "effective_speakers": 1,
        "drop_labels": True,
        "reference": None,
    },
    "transcript-vpp-chunk": {
        "recording_type": "meeting",
        "material_role": "primary",
        "title": "Introduction To Virtual Power Plants",
        # Diarized as Speaker N and left that way: putting names here would be
        # asserting who spoke, which is the classify stage's job, not this one's.
        "speakers": {},
        "effective_speakers": 2,
        "drop_labels": False,
        "reference": None,
    },
    "transcript-brattle": {
        "recording_type": "meeting",
        "material_role": "primary",
        "title": "Discussion Of Brattle Report On VPP Costs",
        "speakers": {},
        "effective_speakers": 2,
        "drop_labels": False,
        "reference": None,
    },
    "transcript-export": {
        "recording_type": "memo",
        "material_role": "primary",
        "title": "Exporting Containerized Research Repository",
        "speakers": {},
        "effective_speakers": 1,
        "drop_labels": True,
        "reference": "transcript-export-cleaned",
    },
    "transcript-deepsearch": {
        "recording_type": "memo",
        "material_role": "primary",
        "title": "Research Assistant App Deep Search Mode",
        "speakers": {},
        "effective_speakers": 1,
        "drop_labels": True,
        "reference": "transcript-deepsearch-cleaned",
    },
    "transcript-knowledgebase": {
        "recording_type": "memo",
        "material_role": "primary",
        "title": "Agentic Repository Knowledge Base Design",
        "speakers": {},
        "effective_speakers": 1,
        "drop_labels": True,
        "reference": "transcript-knowledgebase-cleaned",
    },
    "transcript-vaultintegration": {
        "recording_type": "memo",
        "material_role": "primary",
        "title": "PyForge Obsidian Vault Integration Feature",
        "speakers": {},
        "effective_speakers": 1,
        "drop_labels": True,
        "reference": "transcript-vaultintegration-cleaned",
    },
    "transcript-vaultmanager": {
        "recording_type": "memo",
        "material_role": "primary",
        "title": "Vault Manager Feature Requirements",
        "speakers": {},
        "effective_speakers": 1,
        "drop_labels": True,
        "reference": "transcript-vaultmanager-cleaned",
    },
    "transcript-schemadesign": {
        "recording_type": "memo",
        "material_role": "primary",
        "title": "Py Vault Implementation Schema Design",
        "speakers": {},
        "effective_speakers": 1,
        "drop_labels": True,
        "reference": "transcript-schemadesign-cleaned",
    },
}


def skill():
    return _common.harness.load_skill("vault-transcripts")


def blocks(fixture_id):
    """The transcript's parsed blocks, exactly as the pipeline would read them."""
    transcripts = skill()
    schema_lib = _common.harness.load_lib("vault_schema")
    body = schema_lib.split_frontmatter(_common.fixture(fixture_id).encode("utf-8"))["body"]
    return transcripts.parse_transcript(transcripts.transcript_source(body))["blocks"]


def chunks(fixture_id):
    return skill().chunk_blocks(blocks(fixture_id))


def speaker_map(fixture_id):
    """Labels the transcript actually uses, mapped to themselves.

    The pipeline resolves labels to real names during classification. Keeping
    them as they are asks the cleanup only to preserve the turn structure, which
    is the part `check_chunk` can verify.
    """
    record = RECORDS[fixture_id]
    if record["drop_labels"]:
        return {}
    return {label: label for label in skill().ordered_labels(blocks(fixture_id))}


def word_count(fixture_id):
    return sum(len(str(block.get("text", "")).split()) for block in blocks(fixture_id))


def is_tiny(fixture_id, tiny_words=120):
    return word_count(fixture_id) < tiny_words

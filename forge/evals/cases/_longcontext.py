#!/usr/bin/env python3
"""The long-context corpus, its rungs, and the questions asked at each.

Shaped after the Artificial Analysis Long Context Reasoning benchmark: the
questions are not retrievable by keyword and cannot be answered from one place
in the corpus. Each needs two facts that sit in different documents, far apart.

The corpus is two quarterly reports on the same project — six months apart, same
structure, same vocabulary — plus four longer research reports *about that same
project*. The padding is deliberately not neutral. Unrelated filler would let a
model find the answer by looking for the only document that mentions the
subject; same-project padding means keyword search surfaces the wrong document
first, which is the property that makes this measure reading rather than lookup.

Three rungs, same questions, increasing distance. Comparing a model against
itself across rungs says something no single score does: whether it stops being
able to do this as the corpus grows, and where.

Cost is dominated by prefill, and prefill is paid once. The corpus is byte
identical across every question in a rung and only the question varies after it,
so the server's prefix cache carries the corpus between items — the same
discipline `docs/service-split-handoff.md` §2.2 requires of every skill. A 60k
rung prefills in about 40 seconds on `chat-27b` and then costs only generation.
"""

import _common

harness = _common.harness

# The two documents every question is about. Always present, always whole, and
# always at opposite ends of the corpus.
ANCHORS = ("lcr-arpae-q6", "lcr-arpae-q8")

# Same-project research reports, inserted between the anchors. Their only job is
# to push the two apart, so a rung's size is set entirely by how many of them
# are included.
PADDING = ("lcr-market", "lcr-claude-dc", "lcr-gemini-dc", "lcr-perplexity")

# The anchors alone are about 44,000 tokens, so the smallest rung cannot be
# smaller than that without cutting an answer out of the corpus. The first
# attempt used rungs of 24k/64k/110k and the 24k rung held one truncated
# document — half its questions were unanswerable because the evidence was not
# there, which measures the harness rather than the model.
#
# The ceiling is set by the deployment, not by the slot. Context is compressed
# above ~95,000 tokens on this stack, and a compressed corpus is not the corpus
# the rung claims to be. The anchors sit at the two ends here, so compression
# eats the padding between them and leaves the evidence adjacent: a 110k rung
# scored 10 of 10 that way, measuring an easier task than its own label while
# looking like a clean pass.
#
# So every rung sits under the threshold with room for the largest output budget
# any model here adds. `think-27b` reserves 12,000 tokens before it answers, and
# that counts against the same ceiling:
#
#     80,000 prompt + 12,000 headroom + 256 answer = 92,256  < 95,000
#
# The middle rung is 60k rather than 64k so the task tier keeps a real margin
# against its 65,538 ceiling instead of a few hundred tokens of one. That also
# matches how pi-forge is actually used: no stage sends more than ~66k today.
COMPRESSION_THRESHOLD = 95_000

RUNGS = {"lcr-48k": 48_000, "lcr-60k": 60_000, "lcr-80k": 80_000}

SYSTEM = """You answer questions about a set of documents.

Return exactly one JSON object and nothing else:
{"answer": "<the answer, as briefly as possible>", "documents": ["<which documents you used>"]}

- Answer only from the documents. If they do not contain the answer, write
  "Not stated" and leave "documents" empty.
- Several documents describe the same project at different times. Check which
  one you are reading before you answer: a fact from the wrong quarter is wrong.
- The answer is a phrase or a number, not a paragraph."""


def corpus_text(budget_tokens):
    """Both anchors, whole, with as much padding between them as the budget allows.

    The answer content is identical at every rung and only the distance between
    the two anchors changes. That is what makes a comparison across rungs mean
    something: a model that scores worse at 80k than at 48k lost it to
    distance, not to having been shown less.
    """
    budget_chars = int(budget_tokens * harness.forge_llm.PROMPT_CHARACTERS_PER_TOKEN)

    def block(fixture_id, body=None):
        return f"\n\n=== DOCUMENT: {fixture_id} ===\n\n{body if body is not None else _common.fixture(fixture_id)}"

    head, tail = block(ANCHORS[0]), block(ANCHORS[1])
    remaining = budget_chars - len(head) - len(tail)
    if remaining < 0:
        raise harness.EvalError(
            f"a {budget_tokens:,}-token rung cannot hold both anchor documents "
            f"({(len(head) + len(tail)) / harness.forge_llm.PROMPT_CHARACTERS_PER_TOKEN:,.0f} tokens); "
            f"raise the rung or the questions lose their evidence"
        )
    middle = []
    for fixture_id in PADDING:
        body = _common.fixture(fixture_id)
        chunk = block(fixture_id, body[: max(0, remaining - len(block(fixture_id, "")))])
        if len(chunk) > remaining:
            break
        middle.append(chunk)
        remaining -= len(chunk)
    return (head + "".join(middle) + tail).strip()


def build(rung_tokens):
    """Items for one rung: the same corpus prefix, one question each."""
    corpus = corpus_text(rung_tokens)
    built = []
    for entry in harness.expectations("long-context")["items"]:
        built.append(
            {
                **entry,
                "id": f"{entry['id']}",
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    # Corpus first and question last, so the expensive half is a
                    # byte-stable prefix the server can cache across the rung.
                    {"role": "user", "content": f"{corpus}\n\n=== QUESTION ===\n{entry['question']}"},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": 256,
            }
        )
    return built


def score(item, content, record=None):
    """Correct, incorrect, or declined — the same three-way split as `_abstention`.

    Two of the questions per rung have no answer in the corpus. Scoring them the
    same way as the abstention case is deliberate: a model that answers a
    long-context question it cannot support is doing the same thing as one that
    invents a programme name, and the two should be counted alike.
    """
    import _abstention

    if _common.truncated(record):
        return _common.truncation_failure(record)
    reply = _common.parse_json(content)
    if not isinstance(reply, dict):
        return _common.failure("reply was not a JSON object")
    answer = str(reply.get("answer") or "").strip()
    if not answer:
        return _common.failure("reply carried no answer field", gates={"parsed": True, "answered": False})

    answerable = item["answerable"]
    declined = any(token in answer.lower() for token in _abstention.ABSTENTIONS)
    if declined:
        outcome = "abstained" if answerable else "correct"
    elif not answerable:
        outcome = "incorrect"
    else:
        outcome = "correct" if _abstention.answer_correct(answer, item) else "incorrect"

    notes = []
    if outcome == "incorrect":
        notes.append(
            f"answered {answer[:120]!r}"
            + ("; the corpus does not answer this" if not answerable else f", expected one of {item['accept']}")
        )
    elif outcome == "abstained":
        notes.append(f"declined a question the corpus answers ({item['accept'][0]})")

    return {
        "ok": outcome != "incorrect",
        "gates": {"parsed": True, "didNotConfabulate": outcome != "incorrect"},
        "metrics": {
            "correct": 1.0 if outcome == "correct" else 0.0,
            "incorrect": 1.0 if outcome == "incorrect" else 0.0,
            "abstained": 1.0 if outcome == "abstained" else 0.0,
            "omniscienceIndex": (1.0 if outcome == "correct" else 0.0) - (1.0 if outcome == "incorrect" else 0.0),
            # Whether it named the documents it used. Not gated — the answer is
            # what matters — but a model that gets the answer right while citing
            # the wrong quarter got there by a route that will not generalise.
            "citedCorrectly": 1.0 if set(item.get("documents", [])) & set(reply.get("documents") or []) else 0.0,
        },
        "notes": notes,
        "output": answer,
        "keepRaw": outcome == "incorrect",
    }

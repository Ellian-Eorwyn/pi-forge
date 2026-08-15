#!/usr/bin/env python3
"""Shared machinery for the meeting-brief case: the prompt, the key, the matcher.

Kept apart from the case module because three separate things need testing on
their own — that the prompt is still the skill's own text, that a key's evidence
really is in its transcript, and that fact matching is neither so loose it
credits a near-miss nor so strict it punishes a rephrasing.
"""

import collections
import re

import _common

harness = _common.harness

EXPECTATIONS = _common.EVALS_ROOT / "expectations"
PRIVATE = EXPECTATIONS / ".private"

transcripts = harness.load_skill("vault-transcripts")
capture = harness.load_skill("vault-capture")


# Which of the skill's fidelity rules this case carries over, named by a phrase
# out of each. An inclusion list rather than an exclusion list, because the two
# fail in opposite directions: a rule the exclusion list has not learned about
# yet gets silently included, while a rule this list names and cannot find stops
# the case.
#
# The cleanup block also contains rules about *that* task's shape — its "cleaned"
# field, its timestamps, its headings — and one that contradicts a brief outright
# ("never delete a whole utterance ... even small talk survives"). A brief is
# selective by definition. Carrying those across would leave a model obeying
# instructions about fields it was never given, and its confusion would read as
# a model failure when it was a prompt failure.
# Narrowed 2026-08-14, when the skill made meetings exempt from the fidelity
# rules ("paraphrase and compress freely"). A brief is minutes, so the three
# word-level rules this used to borrow — delete-key-not-thesaurus,
# condensing-drops-words, leave-garbled-visible — no longer belong on it: they
# would hold a brief to a verbatim contract its own skill has dropped. What
# survives is the pair a brief is actually scored on and that the skill's meeting
# style still keeps: not fabricating, and not flattening the uncertainty that
# separates a decision from a proposal ("Do not invent facts, names, numbers, or
# decisions that were not said").
_FIDELITY_RULES_USED = (
    "Preserve the speaker's intent, uncertainty, and nuance",
    "Never add facts, names, dates, conclusions",
)

# The abstention contract, and the single most load-bearing sentence here: it is
# what `abstainedCorrectly` measures compliance with, and what
# `variants/meeting-brief-no-abstention.json` strips to prove the scorer can see
# it go. Taken as one sentence out of the meeting style rule, because the rest
# of that rule is Markdown formatting for an output shape this case does not use.
# When meetings moved to minutes the verb shifted ("Write ..." became
# "... writing ..."), so presence is checked on the enduring clause below while
# the injected bullet keeps the imperative form.
_ABSTENTION_RULE = (
    "Write \"Unassigned\" or \"Not stated\" rather than inferring an owner or a deadline."
)
_ABSTENTION_PRESENT = (
    "\"Unassigned\" or \"Not stated\" rather than inferring an owner or a deadline"
)


def _flat(text):
    """Whitespace collapsed, so a hard-wrapped prompt compares as one sentence."""
    return " ".join(str(text).split())


def _skill_rules():
    """The rules this case borrows from the skill, checked to still be there.

    Extracted rather than copied, on the same argument as `doc-cleanup-ocr`
    pulling its prompt out of the skill's JavaScript: a rule restated in a case
    is a rule that can drift from the one production uses, and then the case
    measures a contract nothing enforces.
    """
    # The header gained a trailing clause when meetings became exempt from these
    # rules ("... below — except for a `meeting` ..."), so match on its opening
    # words and let the bullet extraction below ignore the header prose.
    match = re.search(
        r"^Fidelity rules, which outrank every style rule below\b(.*?)\n\nStyle by",
        transcripts.CLEANUP_SYSTEM,
        re.DOTALL | re.MULTILINE,
    )
    if not match:
        raise harness.EvalError(
            "vault-transcripts.CLEANUP_SYSTEM no longer has a 'Fidelity rules' block; "
            "meeting-brief builds its prompt from it and must be re-read against the new one"
        )
    bullets = re.findall(r"^- .*?(?=\n- |\Z)", match.group(1).strip(), re.DOTALL | re.MULTILINE)
    kept, missing = [], []
    for phrase in _FIDELITY_RULES_USED:
        found = next((bullet.rstrip() for bullet in bullets if phrase in bullet), None)
        (kept.append(found) if found else missing.append(phrase))
    # Compared with whitespace collapsed: the skill hard-wraps its prompt, so
    # this sentence spans two indented lines there and one here.
    if _flat(_ABSTENTION_PRESENT) in _flat(transcripts.CLEANUP_SYSTEM):
        kept.append(f"- {_ABSTENTION_RULE}")
    else:
        missing.append(_ABSTENTION_RULE)
    if missing:
        raise harness.EvalError(
            f"vault-transcripts no longer states {missing!r}. meeting-brief scores against those "
            f"rules, so the case has to be re-read against the skill's new wording rather than "
            f"quietly measuring a contract that no longer exists"
        )
    return "\n".join(kept)


# The brief schema is authored here rather than imported, because no skill has a
# long-meeting stage yet — `vault-transcripts` chunks a meeting at 12,000
# characters and summarizes it from the chunk summaries, so nothing in
# production ever reads a whole meeting in one call. That is exactly the gap
# this case exists to measure, and it means this is the one case in the suite
# whose prompt is not already running somewhere. A pass here is therefore
# evidence about the model, not the guarantee the other cases give that
# production would have accepted the output. If the case earns its keep the
# prompt should graduate into the skill and this note should go.
BRIEF_SYSTEM = f"""You read a full meeting transcript and write the brief a participant would want the next morning.

Return exactly one JSON object and nothing else:
{{"decisions": ["<what was decided>"],
  "actions": [{{"what": "<the task>", "owner": "<who, or 'Unassigned'>", "due": "<when, or 'Not stated'>"}}],
  "dates": ["<date or deadline mentioned, with what it is for>"],
  "figures": ["<number, cost, or quantity stated, with what it measures>"],
  "open_questions": ["<what was raised and left unresolved>"]}}

Rules carried over from how this vault already cleans transcripts, which outrank
everything below:
{_skill_rules()}

Additional rules for this brief:
- A decision is something the participants settled. A proposal nobody agreed to
  is not a decision; if it matters, it is an open question.
- Every figure and every date must appear in the transcript. Do not convert,
  round, or compute a new number from ones that were said.
- An action with no owner takes "Unassigned", and one with no deadline takes
  "Not stated". Never infer either from context or from who happened to raise it.
- Leave a list empty when the meeting genuinely offers nothing for it. An empty
  list is a finding; a padded one is not."""


def load_key(fixture_id):
    """The answer key for one meeting, public or private, or None if absent.

    A missing private key degrades the case to fewer items rather than failing:
    the repository is public and those keys are not, so a clone will simply have
    fewer meetings to measure. It must never look like a model scored zero.
    """
    for directory in (EXPECTATIONS, PRIVATE):
        path = directory / "meeting-brief" / f"{fixture_id}.json"
        if path.exists():
            return harness.load_json(path)
    return None


def available_keys(fixture_ids):
    return [(fixture_id, key) for fixture_id in fixture_ids if (key := load_key(fixture_id)) is not None]


# --- matching ---------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9]+")
# Words too common to carry any evidence that a fact was actually covered. Kept
# deliberately short: a longer list starts removing domain terms, and it is the
# domain terms that make a match mean something.
_STOP = frozenset(
    "a an the and or of to in on for with by is are was were be been will would this that these those "
    "at from as it its their there here they we you he she has have had not no but if then than so".split()
)


def _stem(word):
    """Crude suffix stripping, enough that `capture` and `capturing` are one word.

    Without it the matcher penalises a model for conjugating differently from
    whoever wrote the key — "they assume you capture 30%" would fail to match a
    fact whose canonical form says "capturing". That is measuring the key's
    wording, not the model's reading, and it under-credits every model equally
    but not fairly. A real stemmer would be better and is not worth a dependency
    for this; the failure mode of over-stemming here is two related words
    counting as one, which loosens the match slightly rather than corrupting it.
    """
    for suffix in ("ing", "ies", "ed", "es", "s"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            stem = word[: -len(suffix)] + ("y" if suffix == "ies" else "")
            # "modelled" -> "modell" -> "model", so British and American
            # spellings of the same word do not read as different words.
            if len(stem) > 3 and stem[-1] == stem[-2] and stem[-1] not in "aeiou":
                stem = stem[:-1]
            return stem
    return word


def _tokens(text):
    return [_stem(word) for word in _WORD.findall(str(text or "").lower()) if word not in _STOP]


# How many of a phrasing's rarest words must appear for the fact to count as
# covered. Three is enough to pin a specific claim — "34", "forecast", "2030"
# cannot co-occur by accident — while leaving the model free to write the
# sentence its own way.
DISTINCTIVE_TOKENS = 3


def _distinctive(phrasing, source):
    """The rarest content words of a phrasing, measured against the transcript.

    The first version of this required *every* content word of the reference
    phrasing to appear. Measured against a real brief, that scored 6 of 24 on an
    output that had plainly reported sixteen of the figures: the model wrote
    "34 percent: Forecast share of homes with smart thermostats in 2030" and the
    key said "even in 2030 the report forecasts only 34% of homes having smart
    thermostats", so it missed on "even", "only" and "having". That measures the
    key's prose, not the model's reading, and it under-credits every model
    equally but not informatively.

    Rarity is computed against the source rather than a fixed stoplist because
    what is distinctive is a property of the document: "thermostat" is a rare
    word in general and the commonest word in a thermostat meeting.
    """
    counts = _source_counts(source)
    tokens = set(_tokens(phrasing))
    if len(tokens) <= DISTINCTIVE_TOKENS:
        return tokens
    return set(sorted(tokens, key=lambda token: (counts.get(token, 0), -len(token)))[:DISTINCTIVE_TOKENS])


_COUNTS_CACHE = {}


def _source_counts(source):
    key = id(source)
    cached = _COUNTS_CACHE.get(key)
    if cached is None:
        cached = collections.Counter(_tokens(source))
        _COUNTS_CACHE.clear()  # one transcript at a time; the cache is for the inner loop
        _COUNTS_CACHE[key] = cached
    return cached


def fact_matched(fact, output_text, source=""):
    """Whether the brief covers one reference fact.

    Matched on the fact's most distinguishing words rather than on its wording.
    A model that writes the same fact in its own words has covered it, and
    scoring that as a miss would measure paraphrase rather than reading. The
    `aliases` give the key a way to say which rewordings count when the
    canonical form uses a term the transcript itself does not.
    """
    haystack = set(_tokens(output_text))
    for phrasing in [fact["canonical"], *fact.get("aliases", [])]:
        needed = _distinctive(phrasing, source)
        if needed and needed <= haystack:
            return True
    return False


_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")


def numbers_in(text):
    """Every number in a string, normalized so 1,200 and 1200 compare equal."""
    found = set()
    for raw in _NUMBER.findall(str(text or "")):
        value = raw.replace(",", "").rstrip(".")
        if not value:
            continue
        found.add(value.rstrip("0").rstrip(".") if "." in value else value)
    return found


_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
    "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 1_000_000, "billion": 1_000_000_000}


def spoken_numbers(text):
    """Numbers a transcript writes as words, as digits.

    Speech-to-text spells numbers out, and a brief writes them as digits. Without
    this, a model that correctly reported "six or seven hundred bucks" as 600 and
    700 was flagged for inventing two numbers — measured on the first real run of
    this case. The fabrication check is only meaningful if the comparison is
    against what was *said*, in whatever notation.
    """
    # Standard accumulation with alternatives. Units add into a group; a scale
    # word multiplies every pending group and banks it. Two things this has to
    # get right, both from real transcripts:
    #   "a hundred and twenty"    -> 120, not {100, 20}
    #   "six or seven hundred"    -> 600 and 700, not {6, 700}
    # Each cost a correct brief a fabrication flag before it was handled.
    found, groups, total = set(), [0], 0
    seen = False

    def bank():
        nonlocal groups, total, seen
        if seen:
            found.update(str(total + group) for group in groups)
        groups, total, seen = [0], 0, False

    for word in re.findall(r"[a-z]+", str(text or "").lower()):
        if word in _UNITS:
            value = _UNITS[word]
            # A unit no smaller than the one pending is an alternative reading
            # ("six or seven"), not an addition ("twenty five").
            if groups[-1] and value >= groups[-1]:
                groups.append(value)
            else:
                groups[-1] += value
            seen = True
        elif word in _SCALES:
            scale = _SCALES[word]
            groups = [(group or 1) * scale for group in groups]
            found.update(str(total + group) for group in groups)
            if scale >= 1000:
                total += groups[-1]
                groups = [0]
            seen = True
        elif word in ("or", "and", "point", "half"):
            continue
        else:
            bank()
    bank()
    return found


def invented_numbers(output_text, source):
    """Numbers the brief states that the transcript never did.

    The check that makes this case worth running on a small model: a brief with
    a plausible wrong cost in it is worse than no brief, and unlike a missing
    decision nothing downstream would catch it.

    Years and small integers are excluded. A model writing "three vendors" when
    the transcript said "3 vendors", or referring to 2026 in a meeting held in
    2026, is not fabricating; chasing those would bury the real finding in noise.
    """
    said = numbers_in(source) | spoken_numbers(source)
    invented = []
    for value in sorted(numbers_in(output_text)):
        if value in said:
            continue
        try:
            numeric = float(value)
        except ValueError:
            continue
        if numeric <= 12 or (1900 <= numeric <= 2100 and "." not in value):
            continue
        invented.append(value)
    return invented


def brief_text(brief):
    """Everything the model wrote, flattened, for the checks that scan prose."""
    parts = []
    for value in (brief or {}).values():
        if isinstance(value, list):
            for entry in value:
                if isinstance(entry, dict):
                    parts.extend(str(v) for v in entry.values())
                else:
                    parts.append(str(entry))
        elif value is not None:
            parts.append(str(value))
    return "\n".join(parts)


ABSTENTION_TOKENS = ("unassigned", "not stated", "not specified", "none stated", "no owner", "no deadline")


def abstained(value):
    return any(token in str(value or "").lower() for token in ABSTENTION_TOKENS)

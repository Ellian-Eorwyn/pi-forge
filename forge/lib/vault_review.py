"""Render and parse an in-vault Inbox Review note.

A dry run stages its proposed notes into a visible folder and writes one control
note at the top of the inbox. The reviewer opens the proposed notes, ticks the
ones to keep, edits any that need a light touch, and clicks a link that applies
exactly what they approved. This module owns the two halves of that surface —
rendering the note and reading it back — and nothing else, so the same surface
can serve other inbox skills later.

The design keeps the machine-readable state minimal and robust to hand edits:

- The note's frontmatter carries the ``run`` directory, so the apply step needs
  no arguments (a shell-command URI cannot pass any).
- Approval is a ticked ``- [x] [[Note name]]`` task line. The parser reads only
  the checkbox state and the first wikilink target per line, so extra prose,
  sub-bullets, and reordering do not confuse it.
- Nothing but the tick is stored: approving a note means "apply it", and the
  apply step recomputes the meaning-first gate on the reviewed bytes before it
  writes. A wholesale rewrite is caught by the thinking verify pass upstream, not
  waived by a human here.

Two Obsidian rendering rules are load-bearing here (see reviewer-2): a callout
directly under a paragraph line renders as plain text, so every callout gets a
blank line in front of it; and a blank line inside a callout must be a bare ``>``
or the callout ends early.
"""

import re
from dataclasses import dataclass
from typing import List, Optional, Set

# The staged proposed notes live in a visible sub-folder of the inbox so Obsidian
# indexes them and `[[wikilinks]]` from the review note resolve. The review note
# itself sorts to the very top of the inbox: a leading "! " orders before digits
# and letters in Obsidian's default A-Z file explorer.
PENDING_DIRNAME = "_Pending Review"
REVIEW_NOTE_NAME = "! Inbox Review.md"

# The name (without extension) the parser recognises, so a caller can match a
# scanned file against "is this the control note".
REVIEW_NOTE_STEM = REVIEW_NOTE_NAME[:-3]

_TASK_RE = re.compile(r"^\s*-\s*\[([ xX])\]\s*.*?\[\[([^\]|#]+?)(?:\s*[|#][^\]]*)?\]\]")
_RUN_RE = re.compile(r"^run:\s*(.+?)\s*$", re.MULTILINE)


@dataclass
class ReviewItem:
    """One proposed note as it appears in the review note.

    ``name`` is the staged note's basename without ``.md`` — the wikilink target
    and the key the apply step matches on.
    """

    name: str
    source: str = ""
    summary: str = ""
    facts: str = ""
    reason: str = ""


@dataclass
class ReviewDecisions:
    """What a reviewed note says to do."""

    run_directory: Optional[str]
    approved: Set[str]  # note basenames (no extension) that are ticked


def parse_review_note(text: str) -> ReviewDecisions:
    """Read a (possibly hand-edited) review note back into decisions."""
    run_match = _RUN_RE.search(_frontmatter(text))
    run_directory = _unquote(run_match.group(1)) if run_match else None
    approved = set()
    for line in text.splitlines():
        match = _TASK_RE.match(line)
        if not match:
            continue
        if match.group(1).lower() == "x":
            approved.add(match.group(2).strip())
    return ReviewDecisions(run_directory=run_directory, approved=approved)


def render_review_note(
    *,
    generated_at: str,
    run_directory: str,
    decisions: list,
    apply_uri: Optional[str],
    apply_command: str,
    to_process: Optional[list] = None,
    applied: Optional[list] = None,
    empty: bool = False,
) -> str:
    """Render the control note.

    ``to_process`` cleared every gate and is ticked by default — approving them
    all is the common case of a dry run. ``applied`` is what an *autonomous* run
    already filed on its own, shown as a receipt with no checkbox; passing it (even
    empty) switches the note into autonomous mode, where the surface exists to
    report what happened and list what still needs a person, not to be ticked.
    ``decisions`` is everything the run could not settle (structural holds,
    unfaithful cleanups, duplicate pairs, schema changes it will not make on its
    own), shown for information with no checkbox. ``empty`` renders the standing
    note when no run is pending.
    """
    to_process = to_process or []
    autonomous = applied is not None
    lines = [
        "---",
        "type: index",
        "status: active",
        f'run: "{run_directory}"',
        "---",
        "",
    ]
    if empty:
        lines += [
            "> [!summary] Inbox Review",
            "> Nothing is waiting. Run `vault-transcripts process` to prepare a dry run,",
            "> then reopen this note to approve what it proposes.",
            "",
        ]
        return "\n".join(lines) + "\n"

    if autonomous:
        held = "Nothing needs you." if not decisions else f"{_count(len(decisions), 'note')} still needs you."
        lines += [
            "> [!summary] Inbox Review",
            f"> Autonomous run {generated_at}. "
            f"{_count(len(applied), 'note')} filed automatically. {held}",
            ">",
            "> These were filed for you. Resolve anything under “Needs a decision”, then re-run.",
            "",
            "## Filed automatically",
            "",
        ]
        if applied:
            for item in applied:
                detail = " · ".join(part for part in (item.facts, item.summary) if part)
                lines.append(f"- [[{item.name}]]" + (f" — {detail}" if detail else ""))
            lines.append("")
        else:
            lines += ["- _Nothing needed filing._", ""]
    else:
        lines += [
            "> [!summary] Inbox Review",
            f"> Dry run {generated_at}. "
            f"{_count(len(to_process), 'note')} to process, "
            f"{_count(len(decisions), 'note')} needing a decision.",
            ">",
            "> Tick what to keep, edit any staged note in place, then apply below.",
            "",
        ]

    if to_process or not autonomous:
        lines += ["## To process", ""]
        if to_process:
            for item in to_process:
                lines += _item_block(item, checked=True)
        else:
            lines += ["- _None cleared every check._", ""]

    lines += ["## Needs a decision", ""]
    if decisions:
        for item in decisions:
            reason = item.reason or "held for review"
            lines.append(f"- `{item.source or item.name}` — {reason}")
        lines.append("")
    else:
        lines += ["- _None._", ""]

    if autonomous:
        # Nothing here is tickable, so there is no Apply section; the run already
        # filed what it could and the held items need a person, not a checkbox.
        return "\n".join(lines) + "\n"

    lines += ["## Apply", ""]
    if apply_uri:
        lines += [
            f"[✅ Apply approved notes]({apply_uri})",
            "",
            "Or from a terminal:",
            "",
            "```bash",
            apply_command,
            "```",
            "",
        ]
    else:
        lines += [
            "The one-click link needs the shell command configured once (its id is"
            " not set). Until then, apply from a terminal:",
            "",
            "```bash",
            apply_command,
            "```",
            "",
        ]
    return "\n".join(lines) + "\n"


def apply_uri(vault_name: str, command_id: Optional[str]) -> Optional[str]:
    """The shell-commands URI that fires the configured apply command, or None
    when no command id is set."""
    if not command_id:
        return None
    return f"obsidian://shell-commands?vault={_uri_escape(vault_name)}&execute={_uri_escape(command_id)}"


def _item_block(item: ReviewItem, *, checked: bool) -> List[str]:
    box = "x" if checked else " "
    block = [f"- [{box}] [[{item.name}]]"]
    detail = " · ".join(part for part in (item.facts, item.summary) if part)
    if detail:
        block.append(f"    - {detail}")
    block.append("")
    return block


def _frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    return text[:end] if end != -1 else text


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}{'' if number == 1 else 's'}"


def _uri_escape(value: str) -> str:
    from urllib.parse import quote

    return quote(str(value), safe="")

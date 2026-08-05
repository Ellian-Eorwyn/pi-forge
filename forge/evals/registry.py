#!/usr/bin/env python3
"""The model registry: what is declared, what is actually loaded, and adding to it.

`models.json` is a list of claims about endpoints. This module compares those
claims against the endpoints themselves, and writes new ones by reading a live
server rather than by hand.

Hand-writing an entry is how the registry goes wrong. `contextTokens` was
32,768 for months after the backend moved to 65,538, which quietly halved the
task tier's real budget; `expectParams` was asserted by two entries and enforced
on neither. Both are values a server will state if asked. `add_model` asks.
"""

import json
import sys
from pathlib import Path

EVALS_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(EVALS_ROOT))

import harness  # noqa: E402

forge_llm = harness.forge_llm

# A probe long enough that a reasoning backend has something to reason about,
# and short enough that a slow one still answers quickly. The answer is checkable
# without being guessable from the question alone.
PROBE_MESSAGES = [
    {"role": "user", "content": "A meeting ran from 09:15 to 11:45. How many minutes was that? Reply with the number only."}
]

# The largest reasoning preamble measured on this stack across a full run of real
# cases (`think-27b` on :8008 spent 3,500-9,938 tokens before answering). Used as
# a floor rather than a starting point, because the alternative — extrapolating
# from one short probe — is what set it to 3,000 and truncated four cases.
MEASURED_REASONING_HEADROOM = 12000


def survey(model_ids=None):
    """Every registry entry against what its endpoint is serving right now.

    One fingerprint per distinct endpoint, not per entry: several entries share
    a URL when a router or a backend variant swaps the weights behind it, and
    the whole point of the survey is showing which of them currently matches.
    """
    rows = []
    cache = {}
    for model_id in model_ids or sorted(harness.models()):
        service = harness.resolve_model(model_id)
        key = (service.get("fingerprintUrl") or service["url"], service["model"])
        if key not in cache:
            cache[key] = harness.served_fingerprint(service)
        fingerprint = cache[key]
        problems = harness.check_served(service, fingerprint)
        rows.append(
            {
                "id": model_id,
                "label": service["label"],
                "url": service["url"],
                "tier": service.get("tier"),
                "contextTokens": service["contextTokens"],
                "claims": harness.identity_claims(service),
                "servedParams": harness.served_params(fingerprint),
                "servedQuant": harness.served_quant(fingerprint),
                "servedPath": fingerprint.get("modelPath"),
                "state": fingerprint.get("state"),
                "error": fingerprint.get("error"),
                "problems": problems,
                "unconfirmed": harness.attribution_warning(service, fingerprint),
                "runnable": not problems and not fingerprint.get("error"),
            }
        )
    return rows


def ambiguous_pairs(rows):
    """Entries that share an endpoint and could not be told apart if both loaded.

    Two entries on one URL is normal and intended. Two entries on one URL that
    make the *same* claims is a trap: whichever is loaded, both pass, and the
    run is labelled by whichever id the operator happened to type.
    """
    pairs = []
    for index, left in enumerate(rows):
        for right in rows[index + 1 :]:
            if left["url"] != right["url"]:
                continue
            if not left["claims"] or not right["claims"]:
                pairs.append((left["id"], right["id"], "one of them asserts no identity"))
            elif set(left["claims"]) == set(right["claims"]) == {"expectParams"}:
                pairs.append((left["id"], right["id"], "both assert only a parameter count"))
    return pairs


def render_survey(rows):
    lines = [
        f"{'entry':<14} {'tier':<7} {'ctx':>7}  {'serving':<28} {'state':<9} status",
        f"{'-' * 14} {'-' * 7} {'-' * 7}  {'-' * 28} {'-' * 9} {'-' * 30}",
    ]
    for row in rows:
        params = f"{row['servedParams'] / 1e9:.1f}B" if row["servedParams"] else "?"
        serving = f"{params} {row['servedQuant'] or ''}".strip()
        if row["servedPath"]:
            serving = f"{serving} {Path(row['servedPath']).name}"[:28]
        status = "unreachable" if row["error"] else ("runnable" if row["runnable"] else "mismatch")
        lines.append(
            f"{row['id']:<14} {row['tier'] or '-':<7} {row['contextTokens']:>7}  "
            f"{serving:<28} {row['state'] or '-':<9} {status}"
        )
    lines.append("")
    for row in rows:
        for problem in row["problems"]:
            lines.append(f"  {row['id']}: {problem}")
        if row["unconfirmed"] and not row["problems"]:
            lines.append(f"  {row['id']}: unconfirmed — {row['unconfirmed']}")
    for left, right, why in ambiguous_pairs(rows):
        lines.append(f"  {left} and {right} share an endpoint and {why}; a run could be mislabelled either way")

    runnable = [row["id"] for row in rows if row["runnable"]]
    lines.append("")
    lines.append(f"runnable right now: {', '.join(runnable) if runnable else 'none'}")
    return "\n".join(lines)


def _context_tokens(fingerprint):
    """Per-request context, which is the backend's total divided by its slots.

    `meta.n_ctx` is already per-slot where the server reports it. Falling back to
    the launch arguments means dividing, because `--ctx-size` is the whole KV
    cache shared across `--parallel` slots — reading it straight is how a
    registry entry ends up claiming twice the context a request can use.
    """
    meta = fingerprint.get("meta") or {}
    if isinstance(meta.get("n_ctx"), int) and meta["n_ctx"] > 0:
        return meta["n_ctx"]
    settings = fingerprint.get("settings") or {}
    try:
        total = int(settings.get("ctx-size"))
    except (TypeError, ValueError):
        return None
    try:
        slots = max(1, int(settings.get("parallel", 1)))
    except (TypeError, ValueError):
        slots = 1
    return total // slots


def probe(url, model, timeout=180.0, chat_template_kwargs=None, fingerprint_url=None):
    """Read an endpoint and work out what an entry for it would have to say.

    Returns the proposed entry plus the evidence for each field, because a value
    copied from a probe with no record of where it came from is a value the next
    person has to re-derive.
    """
    service = forge_llm.resolve_service("chat", base_url=url, model=model, env={}, settings={})
    service.update({"id": "probe", "label": "probe", "scheduling": {**service["scheduling"], "enabled": False}})
    service["fingerprintUrl"] = fingerprint_url
    if chat_template_kwargs is not None:
        service["chatTemplateKwargs"] = chat_template_kwargs

    fingerprint = harness.served_fingerprint(service)
    if fingerprint.get("error"):
        raise harness.EvalError(f"could not fingerprint {url}: {fingerprint['error']}")

    entry = {"url": url, "model": model}
    evidence = {}

    context_tokens = _context_tokens(fingerprint)
    if context_tokens:
        entry["contextTokens"] = context_tokens
        evidence["contextTokens"] = f"served metadata / launch arguments at {fingerprint.get('readFrom')}"
    params = harness.served_params(fingerprint)
    if params:
        entry["expectParams"] = int(params)
        evidence["expectParams"] = f"{params / 1e9:.2f}B reported by the endpoint"
    quant = harness.served_quant(fingerprint)
    if quant:
        entry["expectQuant"] = quant
        evidence["expectQuant"] = "ftype reported by the endpoint"
    if fingerprint.get("modelPath"):
        entry["expectModelPath"] = fingerprint["modelPath"]
        # Two sources can supply this and they are not equally direct, so the
        # evidence line has to say which one did. A proxy port has no launch
        # argv to read; only the stack state API can name the weights behind it.
        from_stack = (fingerprint.get("stack") or {}).get("modelPath") == fingerprint["modelPath"]
        evidence["expectModelPath"] = (
            "model_path reported by the stack state API for the backend behind this port"
            if from_stack and not fingerprint.get("args")
            else "first .gguf in the launch arguments"
        )
    if fingerprint.get("sizeGiB"):
        entry["sizeGiB"] = fingerprint["sizeGiB"]
        evidence["sizeGiB"] = "model size reported by the endpoint"

    service["contextTokens"] = entry.get("contextTokens") or forge_llm.SLOT_CONTEXT_TOKENS
    reasoning = _probe_reasoning(service, timeout)
    entry.update(reasoning["entry"])
    evidence.update(reasoning["evidence"])
    if fingerprint_url:
        entry["fingerprintUrl"] = fingerprint_url
    return {"entry": entry, "evidence": evidence, "served": fingerprint, "probe": reasoning["observed"]}


def _probe_reasoning(service, timeout):
    """Where this backend puts its reasoning, and what that costs before an answer.

    Three outcomes, and each wants a different field. Reasoning into a separate
    `reasoning_content` leaves `content` empty and nothing to parse, which
    `chatTemplateKwargs` turns off. Reasoning into visible content spends the
    output budget before the answer starts, which `outputHeadroom` pays for. No
    reasoning at all needs neither.
    """
    entry, evidence, observed = {}, {}, {}
    try:
        content, record = forge_llm.call(
            service, PROBE_MESSAGES, temperature=0, max_tokens=2048, cache_prompt=False, background=False, env={},
            task="eval:add-model", timeout=timeout,
        )
    except (forge_llm.ChatError, OSError, InterruptedError) as error:
        observed["error"] = f"{type(error).__name__}: {error}"
        return {"entry": entry, "evidence": evidence, "observed": observed}

    hidden = record.get("hiddenTokens") or 0
    observed.update(
        {
            "content": content[:400],
            "answeredCorrectly": "150" in content,
            "generatedTokens": record.get("generatedTokens"),
            "hiddenTokens": hidden,
            "reasoned": record.get("reasoned"),
            "elapsedMs": record.get("elapsedMs"),
        }
    )
    if not content.strip() and (record.get("generatedTokens") or 0) > 0:
        entry["chatTemplateKwargs"] = {"enable_thinking": False}
        evidence["chatTemplateKwargs"] = (
            f"the probe generated {record['generatedTokens']} tokens and returned empty content: "
            f"this backend reasons into a field the client does not read"
        )
        return {"entry": entry, "evidence": evidence, "observed": observed}

    if record.get("reasoned") and hidden > forge_llm.HIDDEN_TOKEN_MARGIN:
        # The probe decides *whether* to set headroom. It deliberately does not
        # decide how much. Sizing from a short question is exactly how this went
        # wrong before: two probes at ~1,900 tokens produced a headroom of 3,000,
        # and real cases then spent 3,500-9,938, truncating items in four of
        # twelve and scoring them as model failures. This probe spends a few
        # hundred tokens on one arithmetic question, so extrapolating from it
        # would be worse still.
        #
        # So the floor is the largest requirement actually measured on this
        # stack, and the multiplier only matters for a backend that reasons far
        # more than the one this was calibrated against. A cap costs nothing
        # unless it is reached.
        entry["outputHeadroom"] = max(MEASURED_REASONING_HEADROOM, int(hidden * 12 / 1000 + 1) * 1000)
        evidence["outputHeadroom"] = (
            f"this backend reasons in visible content — the probe spent {hidden} tokens doing it. "
            f"The value is a floor taken from the largest requirement measured on this stack "
            f"({MEASURED_REASONING_HEADROOM}), not a measurement of this model: a one-question probe "
            f"cannot size a real case. Raise it if any case reports finishReason: length"
        )
    return {"entry": entry, "evidence": evidence, "observed": observed}


ENTRY_ORDER = (
    "label", "url", "model", "contextTokens", "chatTemplateKwargs", "outputHeadroom",
    "tier", "family", "sizeGiB", "coResident", "fingerprintUrl",
    "expectParams", "expectQuant", "expectModelPath", "notes",
)


def render_entry(model_id, entry, evidence, observed):
    ordered = {key: entry[key] for key in ENTRY_ORDER if key in entry}
    ordered.update({key: value for key, value in entry.items() if key not in ordered})
    lines = [f'"{model_id}": {json.dumps(ordered, indent=2, ensure_ascii=False)}', "", "read from the endpoint:"]
    for field, why in sorted(evidence.items()):
        lines.append(f"  {field}: {why}")
    if observed.get("error"):
        lines.append(f"\nthe generation probe failed: {observed['error']}")
        lines.append("chatTemplateKwargs and outputHeadroom could not be determined; `doctor` before running.")
    elif observed:
        verdict = "correct" if observed.get("answeredCorrectly") else "WRONG (expected 150)"
        lines.append(
            f"\nprobe: answered {verdict} in {observed.get('elapsedMs')} ms, "
            f"{observed.get('generatedTokens')} tokens generated, {observed.get('hiddenTokens')} of them hidden"
        )
    missing = [name for name in ("expectParams", "expectQuant", "expectModelPath") if name not in entry]
    if len(missing) == 3:
        lines.append("\nNo identity claim could be read here, so nothing will confirm what this entry serves.")
    return "\n".join(lines)


def write_entry(model_id, entry):
    """Add or replace an entry in models.json, preserving the rest of the file."""
    path = EVALS_ROOT / "models.json"
    document = harness.load_json(path)
    existing = document["models"].get(model_id, {})
    # Anything a human wrote and a probe cannot re-derive is kept: the label, the
    # notes explaining why the entry exists, and the tier it stands in for.
    merged = {**existing, **entry}
    for field in ("label", "notes", "tier", "family", "coResident"):
        if existing.get(field) is not None:
            merged[field] = existing[field]
    merged.setdefault("label", model_id)
    document["models"][model_id] = {key: merged[key] for key in ENTRY_ORDER if key in merged} | {
        key: value for key, value in merged.items() if key not in ENTRY_ORDER
    }
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path

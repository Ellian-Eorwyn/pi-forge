#!/usr/bin/env python3
"""How fast a model is, at the prompt sizes the suite actually uses.

Half of a routing decision. "The 4B passed this case" is not an argument for
moving a stage onto it; "passed it and is four times faster" is, and "passed it
and is slower" ends the conversation. The cases measure the first half and
nothing measured the second.

Separate from `run` because it is a different question with a different shape:
no fixtures, no gates, no scoring — just prefill and decode rates at three
sizes, from the timings llama.cpp already returns on every response.
"""

import statistics
import sys
import time
from pathlib import Path

EVALS_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(EVALS_ROOT))

import harness  # noqa: E402

# The three sizes the suite works at: a classification-shaped prompt, a
# document-shaped one, and a corpus-shaped one. Taken from real fixtures rather
# than generated filler, because prefill speed depends on what is being read
# only through its token count, but a synthetic prompt would not survive review
# as evidence about real work.
SIZES = (("small", 1_000), ("document", 16_000), ("corpus", 60_000))

# Long enough to measure a rate rather than a startup transient, short enough
# that a slow model does not turn this into a second suite run.
GENERATE_TOKENS = 200

SAMPLES = 2


def _prompt(target_tokens):
    """A prompt of about the requested size, built from frozen fixtures."""
    budget = int(target_tokens * harness.forge_llm.PROMPT_CHARACTERS_PER_TOKEN)
    parts, used = [], 0
    for path in sorted((harness.FROZEN).glob("*.md")):
        body = path.read_text(encoding="utf-8")
        parts.append(body[: budget - used])
        used += min(len(body), budget - used)
        if used >= budget:
            break
    if used < budget:
        raise harness.EvalError(f"not enough frozen text for a {target_tokens:,}-token prompt; run `freeze` first")
    return "".join(parts)


def measure(model_id, timeout=600.0):
    service = harness.resolve_model(model_id)
    fingerprint = harness.served_fingerprint(service)
    problems = harness.check_served(service, fingerprint)
    if problems:
        raise harness.EvalError(problems[0])

    rows = []
    for label, size in SIZES:
        ceiling = service["contextTokens"] - (service.get("outputHeadroom") or 0) - GENERATE_TOKENS
        if size > ceiling:
            rows.append({"size": label, "promptTokens": size, "skipped": f"over this model's {ceiling:,}-token room"})
            continue
        prefill, decode, elapsed = [], [], []
        for sample in range(SAMPLES):
            messages = [
                # The nonce defeats the prefix cache. A cached prefill measures
                # the cache, which is worth knowing and is not what this asks.
                {"role": "system", "content": f"Reply with a list of {GENERATE_TOKENS // 4} colours. Run {sample}-{time.time()}."},
                {"role": "user", "content": _prompt(size)},
            ]
            started = time.monotonic()
            try:
                _content, record = harness.forge_llm.call(
                    service, messages, temperature=0, max_tokens=GENERATE_TOKENS,
                    cache_prompt=False, background=False, env={}, task="eval:throughput", timeout=timeout,
                )
            except (harness.forge_llm.ChatError, OSError, InterruptedError) as error:
                rows.append({"size": label, "promptTokens": size, "error": f"{type(error).__name__}: {error}"})
                break
            elapsed.append(time.monotonic() - started)
            if record.get("promptTokens") and record.get("prefillMs"):
                prefill.append(record["promptTokens"] / (record["prefillMs"] / 1000))
            if record.get("generatedTokens") and record.get("generationMs"):
                decode.append(record["generatedTokens"] / (record["generationMs"] / 1000))
        else:
            rows.append({
                "size": label,
                "promptTokens": size,
                "prefillTokensPerSecond": round(statistics.median(prefill), 1) if prefill else None,
                "decodeTokensPerSecond": round(statistics.median(decode), 1) if decode else None,
                "secondsPerCall": round(statistics.median(elapsed), 2),
            })
    return {"model": model_id, "label": service["label"], "served": fingerprint, "sizes": rows}


def render(result):
    lines = [f"{result['label']}  ({result['model']})", ""]
    lines.append(f"{'prompt':<10} {'tokens':>8}  {'prefill tok/s':>13}  {'decode tok/s':>12}  {'s/call':>7}")
    lines.append(f"{'-' * 10} {'-' * 8}  {'-' * 13}  {'-' * 12}  {'-' * 7}")
    for row in result["sizes"]:
        if row.get("skipped") or row.get("error"):
            lines.append(f"{row['size']:<10} {row['promptTokens']:>8,}  {row.get('skipped') or row['error']}")
            continue
        lines.append(
            f"{row['size']:<10} {row['promptTokens']:>8,}  {row['prefillTokensPerSecond'] or 0:>13,.0f}  "
            f"{row['decodeTokensPerSecond'] or 0:>12,.1f}  {row['secondsPerCall']:>7.2f}"
        )
    lines.append("")
    lines.append("Prefill is paid once per distinct prompt; a case whose items share a prefix pays it once.")
    return "\n".join(lines)

#!/usr/bin/env python3
"""How fast the local vision model turns an image into a short description.

The text throughput benchmark (`throughput.py`) never sends an image, so it says
nothing about the one cost that is unique to a multimodal request: encoding the
image through the mmproj before a single token is generated. That encode lands in
llama.cpp's `prompt_ms` (prefill) alongside the text, so the same timings the
server already returns answer "how long until it starts describing, and how fast
does it describe" — this just sends an image to get them.

It drives the exact path a skill would use — `forge_llm.image_message(...)` then
`forge_llm.call(...)` — so a green run here is also an end-to-end check that the
Python image helper builds a request the backend accepts.

Defaults to the `chat` service (the non-thinking primary, best for a quick
description) and the repo's `red-circle.png` fixture. Point it elsewhere with
`--service think`, `--chat-url`, `--model`, or `--image`.

    python3 forge/evals/image-describe/probe.py
    python3 forge/evals/image-describe/probe.py --image photo.jpg --runs 5
    python3 forge/evals/image-describe/probe.py --chat-url http://llms:8104/v1 --model chat-custom2

Needs the inference stack reachable (host `llms`); run it where that resolves.
"""

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "forge" / "lib"))

import forge_llm  # noqa: E402

DEFAULT_IMAGE = REPO_ROOT / "packages" / "ai" / "test" / "data" / "red-circle.png"
DEFAULT_PROMPT = "Briefly describe this image."


def _rate(tokens, millis):
    """Tokens per second, or None when either input is missing or zero."""
    if not tokens or not millis:
        return None
    return tokens / (millis / 1000)


def measure(image, prompt, *, service_name="chat", base_url=None, model=None, runs=3, max_tokens=128, timeout=600.0):
    """Send ``image`` ``runs`` times and collect per-call timings.

    Returns ``{service, image, runs: [...], description}``. Each run reuses the
    same message; ``cache_prompt=False`` forces a real prefill (and image encode)
    every time, so a warm second run does not flatter the number.
    """
    service = forge_llm.resolve_service(service_name, base_url=base_url, model=model, env=os.environ)
    message = forge_llm.image_message(prompt, image)

    rows = []
    description = None
    for index in range(runs):
        started = time.monotonic()
        content, record = forge_llm.call(
            service,
            [message],
            temperature=0,
            max_tokens=max_tokens,
            cache_prompt=False,
            background=False,
            env=os.environ,
            task="eval:image-describe",
            timeout=timeout,
        )
        wall_ms = (time.monotonic() - started) * 1000
        description = content
        rows.append({
            "run": index + 1,
            "promptTokens": record.get("promptTokens"),
            "generatedTokens": record.get("generatedTokens"),
            "prefillMs": record.get("prefillMs"),
            "generationMs": record.get("generationMs"),
            "elapsedMs": record.get("elapsedMs") or round(wall_ms),
            "prefillTokensPerSecond": _rate(record.get("promptTokens"), record.get("prefillMs")),
            "decodeTokensPerSecond": _rate(record.get("generatedTokens"), record.get("generationMs")),
        })
    return {
        "service": {"name": service["name"], "url": service["url"], "model": service["model"]},
        "image": str(image),
        "prompt": prompt,
        "runs": rows,
        "description": description,
    }


def _median(rows, key):
    values = [row[key] for row in rows if row.get(key) is not None]
    return statistics.median(values) if values else None


def render(result):
    service = result["service"]
    image_path = Path(result["image"])
    size_kb = image_path.stat().st_size / 1024 if image_path.exists() else 0
    rows = result["runs"]

    lines = [
        f"image-describe latency — {service['name']} ({service['model']} @ {service['url']})",
        f"image: {result['image']} ({size_kb:,.1f} KB)   prompt: {result['prompt']!r}",
        "",
        f"{'run':>3}  {'prefill≈TTFT(ms)':>16}  {'decode(ms)':>10}  {'decode tok/s':>12}  {'total(ms)':>9}  {'out tok':>7}",
        f"{'-' * 3}  {'-' * 16}  {'-' * 10}  {'-' * 12}  {'-' * 9}  {'-' * 7}",
    ]
    for row in rows:
        lines.append(
            f"{row['run']:>3}  {_fmt(row['prefillMs'], ',.0f'):>16}  {_fmt(row['generationMs'], ',.0f'):>10}  "
            f"{_fmt(row['decodeTokensPerSecond'], ',.1f'):>12}  {_fmt(row['elapsedMs'], ',.0f'):>9}  "
            f"{_fmt(row['generatedTokens'], ',.0f'):>7}"
        )
    if len(rows) > 1:
        lines.append(f"{'-' * 3}  {'-' * 16}  {'-' * 10}  {'-' * 12}  {'-' * 9}  {'-' * 7}")
        lines.append(
            f"{'med':>3}  {_fmt(_median(rows, 'prefillMs'), ',.0f'):>16}  {_fmt(_median(rows, 'generationMs'), ',.0f'):>10}  "
            f"{_fmt(_median(rows, 'decodeTokensPerSecond'), ',.1f'):>12}  {_fmt(_median(rows, 'elapsedMs'), ',.0f'):>9}  "
            f"{_fmt(_median(rows, 'generatedTokens'), ',.0f'):>7}"
        )
    lines.append("")
    lines.append("prefill (≈ time to first token) includes encoding the image through the mmproj.")
    lines.append("")
    lines.append("description:")
    lines.append((result["description"] or "").strip() or "(empty)")
    return "\n".join(lines)


def _fmt(value, spec):
    return format(value, spec) if isinstance(value, (int, float)) else "—"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image", type=Path, default=DEFAULT_IMAGE, help=f"image file (default: {DEFAULT_IMAGE})")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help=f"prompt text (default: {DEFAULT_PROMPT!r})")
    parser.add_argument("--service", default="chat", choices=("chat", "think"), help="named service (default: chat)")
    parser.add_argument("--chat-url", dest="base_url", default=None, help="override the service base URL")
    parser.add_argument("--model", default=None, help="override the served model id")
    parser.add_argument("--runs", type=int, default=3, help="number of calls to time (default: 3)")
    parser.add_argument("--max-tokens", type=int, default=128, help="output token cap (default: 128)")
    parser.add_argument("--timeout", type=float, default=600.0, help="per-call timeout seconds (default: 600)")
    parser.add_argument("--json", action="store_true", help="emit the raw result as JSON")
    args = parser.parse_args(argv)

    if not args.image.exists():
        parser.error(f"image not found: {args.image}")

    try:
        result = measure(
            args.image, args.prompt,
            service_name=args.service, base_url=args.base_url, model=args.model,
            runs=args.runs, max_tokens=args.max_tokens, timeout=args.timeout,
        )
    except forge_llm.ContextBudgetError as error:
        print(f"prompt did not fit: {error}", file=sys.stderr)
        return 1
    except (forge_llm.ChatError, OSError, InterruptedError) as error:
        print(f"probe failed: {type(error).__name__}: {error}", file=sys.stderr)
        print("Is the inference stack reachable? This needs host `llms` (or --chat-url).", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

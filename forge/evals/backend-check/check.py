#!/usr/bin/env python3
"""Stand-alone backend check for the local llama.cpp stack.

Answers three questions about a chat endpoint without importing anything else in
this repo:

  1. What is actually loaded?  (gguf path, params, quant, context) and is
     speculative decoding / MTP on — read from the stack manager and the model
     metadata port, not guessed from a label.
  2. How fast is it?  Prefill and decode tokens/second from llama.cpp's own
     `timings`, plus the MTP draft-acceptance rate, so a spec-decode change shows
     up directly.
  3. Does it still behave?  A small abstention smoke test — answer from a source,
     decline when the source is silent, and don't confabulate a fact that does
     not exist. Data lives in testset.json next to this file.

Why this exists as its own thing. The eval suite (`forge/evals/run.py`) resolves
a suite by importing every case, so one case whose module raises at build time
takes the whole runner down with it, and its model registry pins a gguf path it
can only verify through a stack-state URL that has been wrong before. Neither can
happen here: this file imports nothing from the suite, takes every endpoint as an
argument with a sane default, and *finds* the stack API by trying the likely
ports rather than trusting one baked-in address. If it cannot read the path, it
says so loudly instead of passing an unchecked claim.

    python3 check.py                 # backend identity + speed + smoke test
    python3 check.py backend         # identity + speed only
    python3 check.py test            # smoke test only
    python3 check.py backend --expect-path .../Qwen3.8-27B-Q6_K.gguf   # assert

Endpoints (defaults match the current deployment; override for any other):
    --chat-url   http://llms:8004/v1     where completions are served
    --meta-url   http://llms:8010/v1     where /v1/models reports params & quant
    --stack-url  http://llms:8077        stack manager /api/config (paths, spec)
    --model      chat                    model id as the endpoint serves it
    --prefix     CHAT_PRIMARY            which backend's config keys to read

Exit status is 1 when the endpoint is unreachable or an --expect-path assertion
fails; the smoke-test numbers are reported, never a reason to fail the process.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

# Stack-manager ports to try when --stack-url does not answer /api/config. The
# suite's default was :8078 while the live API was :8077; rather than inherit one
# fixed guess, probe both and report which answered.
STACK_PORT_FALLBACKS = (8077, 8078)


# --------------------------------------------------------------------------- io

def _get_json(url, timeout=15):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)


def _post_json(url, body, timeout=180):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _chat_completions_url(chat_url):
    base = chat_url.rstrip("/")
    return base if base.endswith("/chat/completions") else base + "/chat/completions"


def _models_url(base_url):
    base = base_url.rstrip("/")
    return base if base.endswith("/models") else base + "/models"


# --------------------------------------------------------------- backend identity

def resolve_stack_config(stack_url):
    """The stack manager's /api/config, and the URL it actually came from.

    Tries the given URL first, then the known fallback ports on the same host, so
    a stale --stack-url degrades to a warning and a working address rather than a
    silent no-op. Returns (config_or_None, url_or_None).
    """
    candidates = [stack_url]
    host = re.sub(r"^https?://", "", stack_url).split(":")[0].split("/")[0]
    for port in STACK_PORT_FALLBACKS:
        candidates.append(f"http://{host}:{port}")
    seen = set()
    for base in candidates:
        base = base.rstrip("/")
        if base in seen:
            continue
        seen.add(base)
        try:
            cfg = _get_json(base + "/api/config", timeout=8)
            if isinstance(cfg, dict) and cfg:
                return cfg, base
        except (urllib.error.URLError, OSError, ValueError):
            continue
    return None, None


def backend_identity(meta_url, model, stack_url, prefix):
    """What is loaded behind the endpoint, from metadata and stack config."""
    ident = {
        "model": model,
        "paramsB": None,
        "quant": None,
        "ctx": None,
        "sizeGiB": None,
        "ggufPath": None,
        "specMethod": None,
        "draftPath": None,
        "mtp": None,
        "stackUrl": None,
        "warnings": [],
    }

    # Params / quant / context from the metadata port (the serving port is a bare
    # proxy that reports an id and nothing else).
    try:
        listing = _get_json(_models_url(meta_url), timeout=10)
        entry = next((m for m in listing.get("data", []) if m.get("id") == model), None)
        if entry is None and listing.get("data"):
            entry = listing["data"][0]
            ident["warnings"].append(
                f"metadata port has no id {model!r}; read {entry.get('id')!r} instead"
            )
        meta = (entry or {}).get("meta", {})
        if meta:
            if meta.get("n_params"):
                ident["paramsB"] = round(meta["n_params"] / 1e9, 2)
            ident["quant"] = meta.get("ftype")
            ident["ctx"] = meta.get("n_ctx")
            if meta.get("size"):
                ident["sizeGiB"] = round(meta["size"] / 1024**3, 2)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        ident["warnings"].append(f"could not read metadata from {meta_url}: {exc}")

    # gguf path and spec-decode config from the stack manager.
    cfg, used = resolve_stack_config(stack_url)
    ident["stackUrl"] = used
    if cfg is None:
        ident["warnings"].append(
            f"no stack /api/config at {stack_url} or fallbacks {STACK_PORT_FALLBACKS}; "
            "gguf path and MTP state are UNVERIFIED"
        )
    else:
        if used != stack_url.rstrip("/"):
            ident["warnings"].append(f"--stack-url {stack_url} did not answer; used {used}")
        ident["ggufPath"] = cfg.get(f"{prefix}_MODEL_PATH")
        method = cfg.get(f"{prefix}_SPEC_METHOD")
        draft = cfg.get(f"{prefix}_SPEC_DRAFT_MODEL_PATH") or ""
        ident["specMethod"] = method
        ident["draftPath"] = draft or None
        if method is not None:
            ident["mtp"] = bool(method) and method.lower() != "off"
    return ident


# ------------------------------------------------------------------------ speed

def speed_probe(chat_url, model):
    """Prefill and decode rates, plus MTP acceptance, from llama.cpp timings.

    Two calls: a long prompt with a tiny answer isolates prefill; a short prompt
    with a long answer isolates decode. `draft_n` / `draft_n_accepted` in the
    decode call's timings report whether the MTP draft is being accepted.
    """
    url = _chat_completions_url(chat_url)
    out = {"prefillTokPerSec": None, "decodeTokPerSec": None,
           "draftN": None, "draftAccepted": None, "acceptRate": None, "notes": []}

    # Decode: predictable long generation so any spec-decode has something to
    # accept, temperature 0 for repeatability.
    decode_body = {
        "model": model, "temperature": 0, "max_tokens": 512, "stream": False,
        "messages": [{"role": "user", "content":
                      "Count from 1 to 200, writing each number as an English word "
                      "on its own line. Output only the list."}],
    }
    try:
        resp = _post_json(url, decode_body, timeout=180)
        tim = resp.get("timings") or {}
        out["decodeTokPerSec"] = tim.get("predicted_per_second")
        if tim.get("draft_n") is not None:
            out["draftN"] = tim.get("draft_n")
            out["draftAccepted"] = tim.get("draft_n_accepted")
            if tim.get("draft_n"):
                out["acceptRate"] = round(tim["draft_n_accepted"] / tim["draft_n"], 3)
        if out["decodeTokPerSec"] is None:
            usage = resp.get("usage", {})
            out["notes"].append("no timings block; decode rate unavailable "
                                 f"(generated {usage.get('completion_tokens')} tokens)")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        out["notes"].append(f"decode probe failed: {exc}")

    # Prefill: a large input, a one-token answer. The filler is repeated distinct
    # lines so it is not trivially cached away.
    filler = "\n".join(f"Line {i}: the quick brown fox jumps over the lazy dog." for i in range(1, 900))
    prefill_body = {
        "model": model, "temperature": 0, "max_tokens": 1, "stream": False,
        "messages": [{"role": "user", "content":
                      filler + "\n\nReply with the single word: ok."}],
    }
    try:
        resp = _post_json(url, prefill_body, timeout=180)
        tim = resp.get("timings") or {}
        out["prefillTokPerSec"] = tim.get("prompt_per_second")
        out["prefillTokens"] = tim.get("prompt_n")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        out["notes"].append(f"prefill probe failed: {exc}")

    return out


# ------------------------------------------------------------- abstention scoring

def _normalize(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _answer_correct(answer, accept):
    hay = _normalize(answer)
    for form in accept:
        if isinstance(form, (list, tuple)):
            if all(_normalize(term) in hay for term in form):
                return True
        elif _normalize(form) and _normalize(form) in hay:
            return True
    return False


def _abstained(answer, flag_value, abstentions):
    ans = str(answer or "").strip().lower()
    if not ans:
        return False
    if isinstance(flag_value, bool) and not flag_value:
        return True
    return any(tok in ans for tok in abstentions)


def _parse_reply(content):
    """The model's JSON object, tolerant of a stray ```json fence or prose tail."""
    if content is None:
        return None
    text = content.strip()
    fence = re.search(r"\{.*\}", text, re.DOTALL)
    if fence:
        text = fence.group(0)
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        return None


def score_item(item, reply, flag_field, abstentions):
    """One question -> correct / incorrect / abstained, AA-Omniscience style.

    Correct is +1, a confident wrong answer is -1, declining is 0. `incorrect`
    is the only outcome that counts as confabulation (the gate that matters).
    """
    if reply is None:
        return {"outcome": "malformed", "index": 0.0, "confabulated": False,
                "answer": None, "note": "reply was not JSON"}
    answer = str(reply.get("answer") or "").strip()
    if not answer:
        return {"outcome": "malformed", "index": 0.0, "confabulated": False,
                "answer": None, "note": "no answer field"}

    answerable = item["answerable"]
    declined = _abstained(answer, reply.get(flag_field), abstentions)
    if declined:
        outcome = "abstained" if answerable else "correct"
    elif not answerable:
        outcome = "incorrect"
    else:
        outcome = "correct" if _answer_correct(answer, item["accept"]) else "incorrect"

    index = 1.0 if outcome == "correct" else -1.0 if outcome == "incorrect" else 0.0
    note = ""
    if outcome == "incorrect" and not answerable:
        note = f"confabulated {answer[:80]!r} where the correct reply was to decline"
    elif outcome == "incorrect":
        note = f"answered {answer[:80]!r}, expected one of {item['accept']}"
    elif outcome == "abstained":
        note = "declined a question it should have answered"
    return {"outcome": outcome, "index": index, "confabulated": outcome == "incorrect",
            "answer": answer, "note": note}


def run_smoke(chat_url, model, testset):
    url = _chat_completions_url(chat_url)
    abstentions = tuple(testset["abstentions"])
    results = {}
    for name in ("grounded", "closed_book"):
        block = testset[name]
        flag_field = block["flag_field"]
        system = block["system"]
        source = block.get("source")
        rows = []
        for item in block["items"]:
            if source:
                user = f"Source:\n\n{source}\n\nQuestion: {item['question']}"
            else:
                user = item["question"]
            body = {"model": model, "temperature": 0, "max_tokens": 256, "stream": False,
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": user}]}
            try:
                resp = _post_json(url, body, timeout=120)
                content = resp["choices"][0]["message"]["content"]
            except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError) as exc:
                rows.append({"id": item["id"], **score_item(item, None, flag_field, abstentions),
                             "note": f"request failed: {exc}"})
                continue
            scored = score_item(item, _parse_reply(content), flag_field, abstentions)
            rows.append({"id": item["id"], **scored})
        results[name] = rows
    return results


# ---------------------------------------------------------------------- render

def _fmt(value, suffix="", nd=1):
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{nd}f}{suffix}"
    return f"{value}{suffix}"


def render_backend(ident, speed):
    lines = ["backend"]
    lines.append(f"  model id      {ident['model']}")
    lines.append(f"  gguf          {ident['ggufPath'] or 'UNVERIFIED'}")
    params = _fmt(ident["paramsB"], "B", 2) if ident["paramsB"] else "n/a"
    lines.append(f"  params/quant  {params}  {ident['quant'] or ''}".rstrip())
    ctx = f"{ident['ctx']:,}" if ident["ctx"] else "n/a"
    size = _fmt(ident["sizeGiB"], " GiB", 2) if ident["sizeGiB"] else "n/a"
    lines.append(f"  context/size  {ctx} tok  {size}")
    if ident["mtp"] is None:
        mtp = "UNVERIFIED"
    elif ident["mtp"]:
        draft = ident["draftPath"] or "built-in MTP head (no external draft)"
        mtp = f"ON  — method={ident['specMethod']}, draft={draft}"
    else:
        mtp = f"off — method={ident['specMethod']}"
    lines.append(f"  spec-decode   {mtp}")
    lines.append(f"  stack config  {ident['stackUrl'] or 'not reachable'}")
    lines.append("")
    lines.append("speed  (llama.cpp timings)")
    lines.append(f"  prefill       {_fmt(speed['prefillTokPerSec'], ' tok/s')}"
                 + (f"  over {speed.get('prefillTokens')} prompt tok" if speed.get("prefillTokens") else ""))
    lines.append(f"  decode        {_fmt(speed['decodeTokPerSec'], ' tok/s')}")
    if speed["draftN"]:
        lines.append(f"  MTP accept    {speed['draftAccepted']}/{speed['draftN']} "
                     f"({_fmt((speed['acceptRate'] or 0) * 100, '%', 0)}) draft tokens accepted")
    elif speed["draftN"] == 0:
        lines.append("  MTP accept    0 draft tokens — spec-decode idle on this prompt")
    for note in speed["notes"]:
        lines.append(f"  note: {note}")
    for warn in ident["warnings"]:
        lines.append(f"  warning: {warn}")
    return "\n".join(lines)


def render_smoke(results):
    lines = ["smoke test  (abstention: answer from source, decline what is not there)"]
    total_index = 0.0
    total_items = 0
    total_confab = 0
    for name, rows in results.items():
        counts = {"correct": 0, "incorrect": 0, "abstained": 0, "malformed": 0}
        idx = 0.0
        for r in rows:
            counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
            idx += r["index"]
        n = len(rows)
        confab = counts["incorrect"]
        total_index += idx
        total_items += n
        total_confab += confab
        omniscience = idx / n if n else 0.0
        lines.append(
            f"  {name:12s}  correct {counts['correct']}/{n}   declined {counts['abstained']}   "
            f"confabulated {confab}   malformed {counts['malformed']}   index {omniscience:+.2f}")
        for r in rows:
            if r["outcome"] in ("incorrect", "malformed"):
                lines.append(f"       - [{r['id']}] {r['outcome']}: {r['note']}")
    overall = total_index / total_items if total_items else 0.0
    lines.append(f"  {'overall':12s}  {total_items} items   confabulations {total_confab}   "
                 f"omniscience index {overall:+.2f}")
    return "\n".join(lines), total_confab


# ------------------------------------------------------- reasoning_effort sweep

REASONING_LEVELS = ("xhigh", "medium", "low", "none")


def reasoning_sweep(chat_url, model, levels=REASONING_LEVELS):
    """Confirm the endpoint actually honours `reasoning_effort` (Qwen 3.8+).

    A template that does not read the field discards it silently — no error, the
    setting simply does nothing — so the only proof is the reasoning trace. The
    contract: `none` must be empty, and a steered level must not be. The visible
    answer is not the signal, because a shorter chain often yields the same reply.
    """
    url = _chat_completions_url(chat_url)
    prompt = ("A bat and a ball cost $1.10 together. The bat costs $1.00 more than "
              "the ball. How much is the ball? Answer with just the amount.")
    rows = {}
    for eff in levels:
        body = {"model": model, "reasoning_effort": eff, "temperature": 0,
                "max_tokens": 8000,
                "messages": [{"role": "user", "content": prompt}]}
        try:
            resp = _post_json(url, body, timeout=120)
            msg = resp["choices"][0]["message"]
            rows[eff] = {"http": 200,
                         "reasoningChars": len(msg.get("reasoning_content") or ""),
                         "answer": (msg.get("content") or "").strip()[:32]}
        except urllib.error.HTTPError as exc:
            rows[eff] = {"http": exc.code, "reasoningChars": None,
                         "answer": exc.read()[:80].decode("utf-8", "replace")}
        except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError) as exc:
            rows[eff] = {"http": None, "reasoningChars": None, "answer": f"ERR {exc}"}

    chars = {e: rows[e]["reasoningChars"] for e in rows}
    if any(v is None for v in chars.values()):
        verdict = ("ERROR", "a level did not return a reasoning trace — see the HTTP column")
    elif chars.get("none") != 0:
        verdict = ("FAIL", f"none produced {chars.get('none')} reasoning chars — the field is not "
                           "reaching the shaping layer (wrong port, or a template that ignores it)")
    elif max(chars.values()) == 0:
        verdict = ("FAIL", "every level produced 0 reasoning — this port strips reasoning_effort "
                           "(e.g. :8004) or its template discards it; use the thinking port :8008")
    else:
        verdict = ("PASS", "none is silent and steered levels reason — reasoning_effort is honoured")
    return {"rows": rows, "verdict": verdict}


def render_sweep(sweep, chat_url, model):
    lines = [f"reasoning_effort sweep  ({model} @ {chat_url})",
             "  (reasoning_content length is the signal; the answer is not)"]
    for eff, r in sweep["rows"].items():
        rc = "  n/a" if r["reasoningChars"] is None else f"{r['reasoningChars']:5d} chars"
        lines.append(f"  {eff:7s} HTTP{r['http']}   reasoning {rc}   answer={r['answer']!r}")
    status, why = sweep["verdict"]
    lines.append(f"  verdict: {status} — {why}")
    return "\n".join(lines)


# ------------------------------------------------------------------------ main

def load_testset(path):
    with open(path or os.path.join(HERE, "testset.json")) as fh:
        return json.load(fh)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Stand-alone backend check for the local chat endpoint.")
    parser.add_argument("mode", nargs="?", default="all", choices=["all", "backend", "test", "sweep"])
    parser.add_argument("--chat-url", default=os.environ.get("FORGE_CHAT_URL", "http://llms:8004/v1"))
    parser.add_argument("--meta-url", default=os.environ.get("FORGE_META_URL", "http://llms:8010/v1"))
    parser.add_argument("--stack-url", default=os.environ.get("FORGE_STACK_URL", "http://llms:8077"))
    parser.add_argument("--model", default=os.environ.get("FORGE_CHAT_MODEL", "chat"))
    parser.add_argument("--prefix", default="CHAT_PRIMARY",
                        help="stack config key prefix for this backend (e.g. CHAT_PRIMARY, TASK)")
    parser.add_argument("--expect-path", help="fail if the served gguf path is not this")
    parser.add_argument("--testset", help="path to testset.json (default: alongside this script)")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON instead of text")
    args = parser.parse_args(argv)

    exit_code = 0
    payload = {}

    if args.mode in ("all", "backend"):
        ident = backend_identity(args.meta_url, args.model, args.stack_url, args.prefix)
        speed = speed_probe(args.chat_url, args.model)
        payload["backend"] = ident
        payload["speed"] = speed
        if not args.json:
            print(render_backend(ident, speed))
            print()
        # An endpoint that answered neither metadata nor a speed probe is down.
        if ident["paramsB"] is None and speed["decodeTokPerSec"] is None:
            print(f"error: {args.chat_url} is not answering", file=sys.stderr)
            exit_code = 1
        if args.expect_path:
            served = ident["ggufPath"]
            if served is None:
                print(f"error: --expect-path given but the served path is UNVERIFIED "
                      f"(no stack config at {args.stack_url})", file=sys.stderr)
                exit_code = 1
            elif os.path.basename(served) != os.path.basename(args.expect_path) and served != args.expect_path:
                print(f"error: expected {args.expect_path}, serving {served}", file=sys.stderr)
                exit_code = 1

    if args.mode in ("all", "test"):
        testset = load_testset(args.testset)
        results = run_smoke(args.chat_url, args.model, testset)
        payload["smoke"] = results
        if not args.json:
            text, _ = render_smoke(results)
            print(text)

    if args.mode == "sweep":
        # Reasoning is a thinking-port property; default the sweep to the A/B
        # port rather than the no-think chat port, unless the user aimed it.
        chat_url, model = args.chat_url, args.model
        if chat_url == "http://llms:8004/v1" and model == "chat":
            chat_url, model = "http://llms:8008/v1", "code"
            if not args.json:
                print(f"(no --chat-url given; sweeping the thinking A/B port {chat_url} / {model})\n")
        sweep = reasoning_sweep(chat_url, model)
        payload["sweep"] = sweep
        if not args.json:
            print(render_sweep(sweep, chat_url, model))
        if sweep["verdict"][0] != "PASS":
            exit_code = 1

    if args.json:
        print(json.dumps(payload, indent=2))

    return exit_code


if __name__ == "__main__":
    sys.exit(main())

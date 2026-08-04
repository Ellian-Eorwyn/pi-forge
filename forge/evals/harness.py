#!/usr/bin/env python3
"""Shared machinery for the model evaluation suite.

The suite answers one question: which skill stages can be moved to a different
model. So it drives the stages themselves — each case imports the skill it is
testing, builds the prompt with that skill's own builder, and scores the reply
with that skill's own gate. Nothing here reimplements a prompt or a check. A
case that passed its gate here is a case production would have accepted.

Three things follow from that:

- Cases import skill scripts by path, because their filenames are hyphenated and
  are not importable modules. This is the same reason ``pytest.ini`` sets
  ``--import-mode=importlib``.
- Fixtures are frozen copies of real vault notes, pinned by sha256. The vault
  moves; a benchmark that moves with it compares nothing.
- No case touches the embeddings endpoint. Measured on this deployment, `embed`
  and `task` are members of one router with ``MODEL_ROUTER_MAX=1``, so a case
  that interleaved the two would pay a ~6s model swap per item and would be
  timing the router rather than the model.
"""

import hashlib
import importlib.util
import json
import os
import random
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

EVALS_ROOT = Path(__file__).resolve().parent
FORGE_ROOT = EVALS_ROOT.parent
LIB = FORGE_ROOT / "lib"
SKILLS = FORGE_ROOT / "skills"
FROZEN = EVALS_ROOT / ".frozen"
RESULTS = EVALS_ROOT / "results"

sys.path.insert(0, str(LIB))

import forge_llm  # noqa: E402
import run_state  # noqa: E402

DEFAULT_VAULT = Path.home() / "Documents" / "Obsidian" / "Loom"

# How much of a failed reply to keep for inspection.
RAW_KEPT_CHARACTERS = 4000

# Freezing refuses these outright. The suite needs real notes, and the vault
# holds material that must never become a test artifact even in a gitignored
# directory: clinical records, therapy sessions, and — under Licenses — live
# software activation keys. A deny-list is cheap; remembering every time is not.
DENIED_PREFIXES = (
    "01 Personal/1.02 Therapy",
    "01 Personal/1.09 Context",
    "07 Administration/7.01 Health",
    "04 Technology/4.99 Licenses",
    "10 Sources/10.03 Transcript/Personal",
    "10 Sources/10.03 Transcript/Administration",
    ".vault-transcripts/duplicates",
)


class EvalError(RuntimeError):
    """The suite cannot run as configured, and saying why is the useful answer."""


# ---------------------------------------------------------------------------
# Loading skills and configuration


_LOADED = {}


def load_skill(name):
    """Import a skill's script by path and cache it.

    ``load_skill("vault-capture")`` returns the module for
    ``forge/skills/vault-capture/scripts/vault-capture.py``.
    """
    if name in _LOADED:
        return _LOADED[name]
    script = SKILLS / name / "scripts" / f"{name}.py"
    if not script.exists():
        raise EvalError(f"no script for skill {name!r} at {script}")
    spec = importlib.util.spec_from_file_location(f"eval_skill_{name.replace('-', '_')}", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _LOADED[name] = module
    return module


def load_lib(name):
    """Import a shared library module from ``forge/lib`` by name."""
    if name in _LOADED:
        return _LOADED[name]
    spec = importlib.util.spec_from_file_location(name, LIB / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _LOADED[name] = module
    return module


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def models():
    return load_json(EVALS_ROOT / "models.json")["models"]


def fixtures():
    return load_json(EVALS_ROOT / "fixtures.json")["fixtures"]


def expectations(case_id):
    path = EVALS_ROOT / "expectations" / f"{case_id}.json"
    if not path.exists():
        return {}
    return load_json(path)


def resolve_model(model_id):
    """Build a ``forge_llm`` service dict for one entry in ``models.json``.

    The eval never reads the user's ``connectedServices``: a suite whose results
    depend on local settings is not comparable between runs or between machines.
    """
    entry = models().get(model_id)
    if entry is None:
        raise EvalError(f"unknown model {model_id!r}; known: {', '.join(sorted(models()))}")
    service = forge_llm.resolve_service("chat", base_url=entry["url"], model=entry["model"], env={}, settings={})
    service["contextTokens"] = entry.get("contextTokens") or forge_llm.SLOT_CONTEXT_TOKENS
    service["chatTemplateKwargs"] = entry.get("chatTemplateKwargs")
    # Scheduling claims a background slot and yields to interactive turns. That
    # is right for a skill sharing the GPU with a live session and wrong here:
    # a preempted call would land in the results as a model failure.
    service["scheduling"] = {**service["scheduling"], "enabled": False}
    service["label"] = entry.get("label", model_id)
    service["id"] = model_id
    # Extra output tokens this model needs before it starts answering. A
    # reasoning backend spends them on reasoning, and on this deployment it does
    # so in *visible* content — there is no think block to strip, so a case
    # budget sized for the answer alone gets a reply that is all preamble and
    # ends mid-thought. Measured on :8008: ~1,900 tokens regardless of task
    # size, which is why this is added rather than multiplied.
    #
    # Production sets no max_tokens at all on the think tier. The cap here is a
    # safety rail against a runaway repetition loop, not a claim about fidelity.
    service["outputHeadroom"] = int(entry.get("outputHeadroom") or 0)
    # Optional identity assertion. Several entries can share one endpoint when a
    # router swaps the weights behind it, and nothing in a request says which is
    # loaded — so the entry states what it expects and `check_served` compares.
    service["expectParams"] = entry.get("expectParams")
    service["expectModelPath"] = entry.get("expectModelPath")
    service["fingerprintUrl"] = entry.get("fingerprintUrl")
    if entry.get("apiKeyEnv"):
        service["apiKey"] = os.environ.get(entry["apiKeyEnv"])
        if not service["apiKey"]:
            raise EvalError(f"model {model_id!r} needs {entry['apiKeyEnv']} in the environment")
    return service


# ---------------------------------------------------------------------------
# Fixtures


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def vault_root(explicit=None):
    root = Path(explicit or os.environ.get("FORGE_EVAL_VAULT") or DEFAULT_VAULT).expanduser()
    if not root.is_dir():
        raise EvalError(f"vault not found at {root}; pass --vault or set FORGE_EVAL_VAULT")
    return root


def excerpt(text, spec):
    """Apply a fixture's excerpt rule, so a 20k-word report becomes a fixture.

    Excerpting at freeze time rather than at run time keeps the sha meaningful:
    what the model saw is exactly what is on disk.
    """
    mode = (spec or {}).get("mode", "full")
    if mode == "full":
        return text
    if mode == "head":
        return text[: spec["chars"]]
    if mode == "lines":
        lines = text.splitlines()
        return "\n".join(lines[spec.get("start", 0) : spec.get("end", len(lines))])
    if mode == "body":
        # Everything after the frontmatter block, which is what a classifier or
        # a cleanup pass is given.
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end != -1:
                return text[end + 4 :].lstrip("\n")
        return text
    raise EvalError(f"unknown excerpt mode {mode!r}")


def freeze(vault=None, check=False, repin=False):
    """Materialize every fixture into ``.frozen/``, verifying pinned hashes.

    Returns a list of ``(fixture_id, status, detail)``. ``check`` reports drift
    without writing, which is what ``run`` uses as a precondition. ``repin``
    accepts the vault's current content as the new baseline — a deliberate act,
    because it makes every earlier result incomparable with every later one.
    """
    root = vault_root(vault)
    report = []
    repinned = {}
    if not check:
        FROZEN.mkdir(parents=True, exist_ok=True)
    for fixture_id, spec in sorted(fixtures().items()):
        relative = spec["path"]
        denied = next((prefix for prefix in DENIED_PREFIXES if relative.startswith(prefix)), None)
        if denied:
            report.append((fixture_id, "refused", f"{relative} is under the denied prefix {denied!r}"))
            continue
        source = root / relative
        if not source.exists():
            report.append((fixture_id, "missing", f"no such note: {relative}"))
            continue
        raw = source.read_text(encoding="utf-8")
        digest = sha256_text(raw)
        pinned = spec.get("sha256")
        if pinned and pinned != digest:
            if repin:
                repinned[fixture_id] = digest
                report.append((fixture_id, "repinned", f"{relative} {pinned[:12]} -> {digest[:12]}"))
            else:
                report.append((fixture_id, "drifted", f"{relative} is now {digest[:12]}, pinned at {pinned[:12]}"))
                if check:
                    continue
        target = FROZEN / f"{fixture_id}.md"
        content = excerpt(raw, spec.get("excerpt"))
        if check:
            status = "ok" if target.exists() and target.read_text(encoding="utf-8") == content else "stale"
            report.append((fixture_id, status, relative))
            continue
        target.write_text(content, encoding="utf-8")
        if not (repin and fixture_id in repinned):
            report.append((fixture_id, "pinned" if pinned == digest else "written", f"{relative} ({digest[:12]})"))
    if repinned:
        path = EVALS_ROOT / "fixtures.json"
        document = load_json(path)
        for fixture_id, digest in repinned.items():
            document["fixtures"][fixture_id]["sha256"] = digest
        path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def frozen_text(fixture_id):
    path = FROZEN / f"{fixture_id}.md"
    if not path.exists():
        raise EvalError(f"fixture {fixture_id!r} is not frozen; run `run.py freeze` first")
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Cases


def case_ids():
    """Every case module. A leading underscore marks shared helpers, not cases."""
    return sorted(
        path.stem.replace("_", "-") for path in (EVALS_ROOT / "cases").glob("*.py") if not path.stem.startswith("_")
    )


def load_case(case_id):
    path = EVALS_ROOT / "cases" / f"{case_id.replace('-', '_')}.py"
    if not path.exists():
        raise EvalError(f"unknown case {case_id!r}; known: {', '.join(case_ids())}")
    # Cases live outside a package and import `_common` as a sibling.
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(f"eval_case_{case_id.replace('-', '_')}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def served_fingerprint(service):
    """What the endpoint is actually serving, recorded alongside every result.

    A model id in ``models.json`` is a label someone typed; the weights behind a
    port can be swapped without it. This asks the server. Not every profile
    answers with metadata — the proxy ports return an id and nothing else — so
    whatever is missing is recorded as missing rather than guessed.

    Written after a run labelled `task-9b` turned out to have been served by a
    4B, with no way to tell after the fact.
    """
    # A proxy port answers /v1/models with an id and nothing else. `fingerprintUrl`
    # points at somewhere that knows more — usually the backend the proxy fronts,
    # which serves the same ids with full metadata attached.
    root = (service.get("fingerprintUrl") or service["url"]).rsplit("/chat/completions", 1)[0].rstrip("/")
    if root.endswith("/v1") is False and "/v1" not in root:
        root = f"{root}/v1"
    fingerprint = {"checkedAt": run_state.utc_now(), "readFrom": root}
    try:
        with urllib.request.urlopen(f"{root}/models", timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError) as error:
        return {**fingerprint, "error": str(error)}
    entry = next((item for item in payload.get("data") or [] if item.get("id") == service["model"]), None)
    if entry is None:
        return {**fingerprint, "error": f"{service['model']!r} is not served at {root}"}

    for key in ("architecture", "source", "aliases", "tags"):
        if entry.get(key):
            fingerprint[key] = entry[key]
    status = entry.get("status") or {}
    if status.get("value"):
        fingerprint["state"] = status["value"]
    if status.get("preset"):
        fingerprint["preset"] = status["preset"]

    meta = entry.get("meta") or {}
    if meta:
        fingerprint["meta"] = dict(meta)
        if isinstance(meta.get("n_params"), (int, float)):
            fingerprint["paramsB"] = round(meta["n_params"] / 1e9, 2)
        if isinstance(meta.get("size"), (int, float)):
            fingerprint["sizeGiB"] = round(meta["size"] / 2**30, 2)
        if meta.get("ftype"):
            fingerprint["quant"] = meta["ftype"]

    # The launch argv is the whole configuration, and llama-server reports it on
    # the router ports. Keeping it verbatim means a result can be re-read years
    # later against a stack that has moved on; the parsed view is for reading.
    args = [value for value in (status.get("args") or []) if isinstance(value, str)]
    if args:
        fingerprint["args"] = args
        fingerprint["settings"] = _parse_llama_args(args)
        weights = [value for value in args if value.endswith(".gguf") and "mmproj" not in value]
        if weights:
            fingerprint["modelPath"] = weights[0]
            fingerprint["modelFile"] = weights[0].rsplit("/", 1)[-1]
            # The quant is in the filename when the metadata does not carry it,
            # and the filename is where a human would look for it anyway.
            fingerprint.setdefault("quant", _quant_from_filename(fingerprint["modelFile"]))
    return fingerprint


def _parse_llama_args(args):
    """``--ctx-size 32769 --jinja`` -> ``{"ctx-size": "32769", "jinja": True}``."""
    settings = {}
    index = 1 if args and not args[0].startswith("-") else 0  # argv[0] is the binary
    while index < len(args):
        token = args[index]
        if not token.startswith("--"):
            index += 1
            continue
        name = token[2:]
        following = args[index + 1] if index + 1 < len(args) else None
        if following is None or following.startswith("--"):
            settings[name] = True
            index += 1
        else:
            settings[name] = following
            index += 2
    return settings


def _quant_from_filename(filename):
    """``Qwen3.5-4B-UD-Q6_K_XL.gguf`` -> ``Q6_K_XL``."""
    stem = filename.removesuffix(".gguf")
    for part in reversed(stem.split("-")):
        if part[:1].upper() == "Q" and any(character.isdigit() for character in part):
            return part
        if part.upper() in {"F16", "BF16", "F32"}:
            return part.upper()
    return None


def requested_settings(service):
    """What this run asked the endpoint for, as distinct from how it is configured.

    `models.json` is edited between runs, so a result that only records the
    server's own config cannot answer "was thinking on when we measured this".
    Both halves have to travel with the numbers.
    """
    return {
        "model": service["model"],
        "chatTemplateKwargs": service.get("chatTemplateKwargs"),
        "contextTokens": service.get("contextTokens"),
        "outputHeadroom": service.get("outputHeadroom") or 0,
        "temperature": 0,
        "backgroundScheduling": bool(service.get("scheduling", {}).get("enabled")),
    }


VARIANTS = EVALS_ROOT / "variants"
BASE_VARIANT = "base"


def load_variant(name):
    """A declarative patch tested against a case without editing the skill.

    That separation is the point: a variant asks "would this prompt be better"
    and gets an answer *before* anything in `forge/skills` changes. Production
    stays untouched until the numbers justify moving it.
    """
    if not name or name == BASE_VARIANT:
        return None
    path = VARIANTS / f"{name}.json"
    if not path.exists():
        known = sorted(p.stem for p in VARIANTS.glob("*.json")) if VARIANTS.is_dir() else []
        raise EvalError(f"unknown variant {name!r}; known: {', '.join(known) or 'none'}")
    variant = load_json(path)
    variant.setdefault("name", name)
    return variant


def variant_applies(variant, case_id):
    """Whether a variant touches this case. An `applies` list scopes it; absent means all."""
    if not variant:
        return False
    scope = variant.get("applies")
    return case_id in scope if scope else True


def apply_variant(items, variant, case_id):
    """Patch built items in place of the case doing it.

    Post-processing the item list rather than parameterising twelve `items()`
    functions: every patch a real experiment has needed so far — response format,
    token budget, temperature, a prompt suffix or replacement — is a field on the
    item the case already produced.
    """
    if not variant_applies(variant, case_id):
        return items
    patch = variant.get("patch") or {}
    patched = []
    for item in items:
        entry = dict(item)
        for key in ("response_format", "max_tokens", "temperature"):
            if key in patch:
                entry[key] = patch[key]
        suffix, replace = patch.get("systemSuffix"), patch.get("systemReplace")
        strip = patch.get("systemStrip")
        if suffix or replace or strip:
            entry["messages"] = _patch_system(entry["messages"], suffix, replace, strip, case_id, variant)
        patched.append(entry)
    return patched


def _patch_system(messages, suffix, replace, strip, case_id, variant):
    updated = []
    changed = False
    for message in messages:
        if message.get("role") != "system" or changed:
            updated.append(message)
            continue
        content = message["content"]
        if replace is not None:
            content = replace
        if strip is not None:
            if strip not in content:
                # Refused rather than silently no-op: a variant that believes it
                # removed a clause and did not would be measured as "the change
                # had no effect", which is the wrong conclusion entirely.
                raise EvalError(
                    f"variant {variant['name']!r} strips text that is not in {case_id}'s system prompt: {strip[:60]!r}"
                )
            content = content.replace(strip, "")
        if suffix:
            content = f"{content}\n{suffix}"
        updated.append({**message, "content": content})
        changed = True
    return updated


def undecided_cases(documents, baseline_documents=None, tolerance=0.15, floor=0.6):
    """Cases whose verdict a single item could move, and which therefore need repeating.

    Computed rather than listed, because a hand-maintained set of "decisive
    cases" is stale the moment results shift. A case qualifies when one item
    either way would cross a threshold the routing recommendation reads:

    - it sits within one item of the baseline (so the comparison is a coin flip),
    - or within one item of the tolerance or floor boundary,
    - or it already has an unstable item, which settles the matter.
    """
    undecided = []
    for case_id, document in documents.items():
        summary = document["summary"]
        count = summary["items"]
        if not count:
            continue
        if summary.get("stability", {}).get("unstableIds"):
            undecided.append(case_id)
            continue
        step = 1 / count  # what one item is worth
        rate = summary["passRate"]
        near_floor = abs(rate - floor) <= step
        base = (baseline_documents or {}).get(case_id)
        near_baseline = False
        if base:
            delta = rate - base["summary"]["passRate"]
            near_baseline = abs(delta) <= step or abs(abs(delta) - tolerance) <= step
        if near_floor or near_baseline:
            undecided.append(case_id)
    return sorted(undecided)


def check_served(service, fingerprint=None):
    """Complain if the endpoint is not serving what the entry claims.

    Returns a list of problems. Silence means either that it matches or that the
    entry made no claim — the difference is visible in `served` on the result.
    """
    found = fingerprint if fingerprint is not None else served_fingerprint(service)
    problems = []
    if found.get("error"):
        return [f"could not read what {service['id']!r} is serving: {found['error']}"]
    expected_params = service.get("expectParams")
    actual_params = found.get("n_params")
    if expected_params and actual_params:
        # Quantization and vocab size move the count a little; a different model
        # moves it a lot. Ten percent separates the two without false alarms.
        if abs(actual_params - expected_params) > 0.1 * expected_params:
            problems.append(
                f"{service['id']!r} expects ~{expected_params / 1e9:.1f}B parameters but the endpoint is "
                f"serving {actual_params / 1e9:.1f}B — the weights behind {service['url']} are not the ones this entry names"
            )
    expected_path = service.get("expectModelPath")
    actual_path = found.get("modelPath")
    if expected_path and actual_path and expected_path != actual_path:
        problems.append(f"{service['id']!r} expects {expected_path}, endpoint is serving {actual_path}")
    return problems


def run_case(case_id, service, repeat=1, progress=None, variant=None):
    """Run one case against one service and return its result document."""
    case = load_case(case_id)
    started = time.monotonic()
    fingerprint = served_fingerprint(service)
    items = apply_variant(case.items(), variant, case_id)
    results = []
    for index, item in enumerate(items, start=1):
        for attempt in range(1, repeat + 1):
            if progress:
                suffix = f" (repeat {attempt}/{repeat})" if repeat > 1 else ""
                progress(f"  {case_id} [{index}/{len(items)}] {item['id']}{suffix}")
            results.append(_run_item(case, item, service, attempt))
    elapsed = int((time.monotonic() - started) * 1000)
    return {
        "case": case_id,
        "dimension": getattr(case, "DIMENSION", "unspecified"),
        "skill": getattr(case, "SKILL", None),
        "judged": bool(getattr(case, "JUDGE", False)),
        "model": service["id"],
        "modelLabel": service["label"],
        "endpoint": service["url"],
        # Three separate things, because they can disagree and the disagreement
        # is the interesting part: what the server was configured to do
        # (`served`), what this run asked it to do (`requested`), and what it
        # actually did (`observed` in the summary).
        "served": fingerprint,
        "variant": (variant or {}).get("name", BASE_VARIANT),
        "variantApplied": variant_applies(variant, case_id),
        "requested": requested_settings(service),
        "repeat": repeat,
        "elapsedMs": elapsed,
        "items": results,
        "summary": summarize_items(results),
    }


def _run_item(case, item, service, attempt):
    """One model call plus its scoring, with every failure recorded rather than raised.

    A transport error, a context overrun, and an unparseable reply are all
    results: "this model could not do this" is exactly what the suite measures.
    """
    record = None
    content = None
    started = time.monotonic()
    try:
        content, record = forge_llm.call(
            service,
            item["messages"],
            temperature=0,
            max_tokens=_output_budget(item, service),
            response_format=item.get("response_format"),
            cache_prompt=True,
            background=False,
            api_key=service.get("apiKey"),
            task=f"eval:{item['id']}",
            env={},
        )
    except forge_llm.ContextBudgetError as error:
        return _failed_item(item, attempt, "context-budget", str(error), started)
    except (forge_llm.ChatError, OSError, InterruptedError) as error:
        return _failed_item(item, attempt, type(error).__name__, str(error), started)

    scored = _score(case, item, content, record)

    # Several skills give the model one corrective retry that names the check it
    # broke, and refuse the result only if that fails too. A case that models the
    # repair reports both numbers: first-shot quality is what a model is like,
    # and post-repair is what the pipeline would actually deliver. The retry only
    # fires on a failure, so the cost is bounded by how badly the run is going.
    repaired = None
    repair = getattr(case, "repair", None)
    if repair is not None and not scored.get("ok") and content is not None:
        messages = None
        try:
            messages = repair(item, content, scored)
        except Exception:
            messages = None
        if messages:
            try:
                retry_content, retry_record = forge_llm.call(
                    service,
                    messages,
                    temperature=0,
                    max_tokens=_output_budget(item, service),
                    response_format=item.get("response_format"),
                    cache_prompt=True,
                    background=False,
                    api_key=service.get("apiKey"),
                    task=f"eval:{item['id']}:repair",
                    env={},
                )
            except (forge_llm.ChatError, OSError, InterruptedError):
                repaired = {"ok": False, "note": "the repair call failed"}
            else:
                retry_scored = _score(case, item, retry_content, retry_record)
                repaired = {
                    "ok": bool(retry_scored.get("ok")),
                    "gates": retry_scored.get("gates", {}),
                    "notes": retry_scored.get("notes", []),
                    "output": retry_scored.get("output"),
                    "telemetry": _telemetry(retry_record),
                }

    return {
        "id": item["id"],
        "attempt": attempt,
        "ok": bool(scored.get("ok")),
        "gates": scored.get("gates", {}),
        "metrics": {**scored.get("metrics", {}), **({"repairedOk": 1.0 if repaired["ok"] else 0.0} if repaired else {})},
        "notes": scored.get("notes", []),
        "output": scored.get("output"),
        # Every failure keeps its reply. A model that returned `[]` to a real
        # report and a model that returned malformed JSON both read as "failed"
        # in the summary, and only the text tells them apart. Truncated to keep
        # a long cleanup from dominating the results file.
        "raw": (content or "")[:RAW_KEPT_CHARACTERS] if (scored.get("keepRaw") or not scored.get("ok")) else None,
        "repaired": repaired,
        "telemetry": _telemetry(record),
    }


def _output_budget(item, service):
    """A case's output budget plus whatever this model spends before answering.

    The case sizes the budget for the answer, because that is a property of the
    task. The headroom is a property of the model, so the two are added rather
    than one being written into the other.
    """
    budget = item.get("max_tokens")
    if not budget:
        return None
    return budget + (service.get("outputHeadroom") or 0)


def _score(case, item, content, record):
    """Every scorer takes ``(item, content, record)``; ``record`` carries the
    telemetry, which several cases need to tell "could not" from "ran out of
    output budget"."""
    try:
        return case.score(item, content, record)
    except Exception as error:  # a scorer must never take the whole run down
        return {
            "ok": False,
            "gates": {"scorer": False},
            "notes": [f"scorer raised {type(error).__name__}: {error}"],
            "keepRaw": True,
        }


def _failed_item(item, attempt, kind, detail, started):
    return {
        "id": item["id"],
        "attempt": attempt,
        "ok": False,
        "gates": {"reachable": False},
        "metrics": {},
        "notes": [f"{kind}: {detail}"],
        "output": None,
        "raw": None,
        "telemetry": {"elapsedMs": int((time.monotonic() - started) * 1000), "failure": kind},
    }


def _telemetry(record):
    if not record:
        return {}
    keep = (
        "promptTokens",
        "cachedTokens",
        "generatedTokens",
        "hiddenTokens",
        "prefillMs",
        "generationMs",
        "elapsedMs",
        "finishReason",
        "reasoned",
    )
    return {key: record.get(key) for key in keep}


def stability_by_item(items):
    """Group attempt rows by fixture id: ``{id: {attempts, passed, stable, severity}}``.

    Severity is derived, not read from the row. ``ok`` already means "every gate
    passed", so a failed attempt *is* a gated failure by definition — and
    deriving it means results written before the field existed classify
    correctly instead of silently counting their failures as clean.
    """
    grouped = {}
    for item in items:
        entry = grouped.setdefault(item["id"], {"id": item["id"], "attempts": 0, "passed": 0})
        entry["attempts"] += 1
        entry["passed"] += 1 if item["ok"] else 0
    for entry in grouped.values():
        entry["stable"] = entry["passed"] in (0, entry["attempts"])
        # Any attempt failing makes this a gated item: one that fabricated once
        # and behaved twice is a fabricating item.
        entry["severity"] = "gated" if entry["passed"] < entry["attempts"] else None
    return grouped



def summarize_items(items):
    """Roll per-item results into the numbers the report compares.

    ``items`` is one row per *attempt*, so a case run three times has three rows
    per fixture. They are grouped back by id here, because the question a routing
    decision asks is "does this item pass", not "did this attempt pass". Measured
    on this stack, 8 of 12 cases moved between two runs of the same model — an
    item that passes twice and fails once is not a 67% item, it is an unstable
    one, and the report has to be able to say so.
    """
    if not items:
        return {}
    per_item = stability_by_item(items)
    stable = [entry for entry in per_item.values() if entry["stable"]]
    passing = [entry for entry in per_item.values() if entry["passed"] == entry["attempts"]]
    gate_names = sorted({name for item in items for name in item.get("gates", {})})
    gates = {}
    for name in gate_names:
        seen = [item["gates"][name] for item in items if name in item.get("gates", {})]
        gates[name] = {"passed": sum(1 for value in seen if value), "of": len(seen)}
    metric_names = sorted({name for item in items for name in item.get("metrics", {})})
    metrics = {}
    for name in metric_names:
        values = [
            item["metrics"][name]
            for item in items
            if isinstance(item.get("metrics", {}).get(name), (int, float)) and not isinstance(item["metrics"][name], bool)
        ]
        if values:
            metrics[name] = {"mean": round(statistics.fmean(values), 4), "min": min(values), "max": max(values)}
    tokens = [item["telemetry"].get("generatedTokens") or 0 for item in items]
    hidden = sorted(item["telemetry"].get("hiddenTokens") or 0 for item in items)
    elapsed = [item["telemetry"].get("elapsedMs") or 0 for item in items]
    reasoned = sum(1 for item in items if item["telemetry"].get("reasoned"))
    return {
        # Counted in fixtures, not attempts. Repeats used to inflate this, so a
        # case run three times reported 9 items and a pass rate averaged across
        # attempts — which quietly turned an unstable item into a partial pass.
        "items": len(per_item),
        "passed": len(passing),
        "passRate": round(len(passing) / len(per_item), 4),
        "attempts": len(items),
        "stability": {
            "stableItems": len(stable),
            "ofItems": len(per_item),
            "unstableIds": sorted(entry["id"] for entry in per_item.values() if not entry["stable"]),
        },
        # What the deterministic checks caught. The other two severities need a
        # grader and are computed at report time by `report.severity_for`.
        "gatedFailures": sum(1 for entry in per_item.values() if entry["severity"] == "gated"),
        "gates": gates,
        "metrics": metrics,
        "generatedTokens": sum(tokens),
        "hiddenTokens": sum(hidden),
        # Whether the model actually reasoned, which neither the server's config
        # nor the request can be trusted to answer: a flag can be set and not
        # honoured, and was. The median matters more than the total — one item
        # with a long reply trips the visible-content estimator, so a handful of
        # outliers against a median of zero is noise, not thinking.
        "observedThinking": {
            "reasonedItems": reasoned,
            "ofItems": len(items),
            # A true median, averaging the two middles on an even count. Taking
            # the upper middle makes a two-item case report its maximum as its
            # median, which turns one long reply into apparent thinking.
            "hiddenTokensMedian": round(statistics.median(hidden)),
            "hiddenTokensMax": hidden[-1],
        },
        "elapsedMs": sum(elapsed),
    }


# ---------------------------------------------------------------------------
# Results on disk


def results_dir(model_id, variant=BASE_VARIANT):
    return RESULTS / model_id / (variant or BASE_VARIANT)


def write_result(document):
    directory = results_dir(document["model"], document.get("variant"))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{document['case']}.json"
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def read_results(model_id, only=None, variant=BASE_VARIANT):
    directory = results_dir(model_id, variant)
    if not directory.is_dir():
        return {}
    documents = {}
    for path in sorted(directory.glob("*.json")):
        if only and path.stem not in only:
            continue
        documents[path.stem] = load_json(path)
    return documents


def stable_shuffle(values, seed):
    """Shuffle reproducibly, so a re-run of `judge` relabels nothing."""
    ordered = list(values)
    random.Random(seed).shuffle(ordered)
    return ordered

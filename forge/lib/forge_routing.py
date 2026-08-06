#!/usr/bin/env python3
"""Which inference service each stage of work runs on.

A skill used to bind one service per *command*: ``parse_args`` resolved ``chat``
and every stage in the run shared it. The measurements in ``forge/evals`` are
per *stage*, and several of them point opposite directions inside a single
command — ``vault-transcripts`` wants the small model for cleaning a four-speaker
meeting and the thinking model for cleaning a single-speaker memo, and those are
two calls in the same run.

So routing is keyed on the stage label skills already pass to ``forge_llm.call``
as ``task=``. That label was being journaled and otherwise ignored; here it
decides where the call goes.

Three rules govern the table, and they are the report's rules:

- Where a model is **better**, route there even when it is slower. The two 27B
  profiles are one set of weights behind a proxy, so moving between them costs
  latency and nothing else — no swap, no reload.
- Where capability **ties**, take the faster profile. This is why several stages
  stay on ``chat`` against a thinking model that matched them: matching is not a
  reason to spend 4.6x the time.
- Where nothing cleared, stay on the baseline and say so.

Anything not named here runs on ``chat``. That is deliberate: a stage nobody has
measured is a stage with no evidence behind moving it.
"""

import forge_llm

# Stage label -> service name. The label is the `task=` a call site already
# passes. Every entry carries the measurement that put it there, because a
# routing decision without its evidence is one nobody can revisit.
STAGE_SERVICES = {
    # --- to the small tier -------------------------------------------------
    # Faithful cleanup of diarized speech. 7/8 against the baseline's 1/8 with
    # no silent failures; 0.12 invented words per item, 0.99 rare-word
    # retention, 0.97 word ratio — it barely touches the text, which is what
    # this stage is for. The thinking model scores 0/8 here: it rewrites and
    # compresses four-speaker material, dropping a quarter of it.
    #
    # Only reached for multi-speaker material; `clean_one_chunk` routes per
    # chunk, because the single-speaker case wants the opposite model.
    "clean-transcript-chunk-multi": "task",
    "clean-transcript-chunk-multi-repair": "task",
    # Yes/no pair judgment. 16/16 against 14/16, no false negatives, and 3.4x
    # faster in absolute terms (0.52s against 1.75s per pair). The shape the
    # small model is good at: the answer is a decision about the input, not
    # prose composed from it.
    "connection-judgment": "task",
    # --- to the thinking tier ----------------------------------------------
    # Single-speaker cleanup. 8/8 against 2/8, and invented words fall from
    # 4.38 per item to 0.00. The small model is blocked here by a silent
    # failure — output that passed every deterministic check and was still
    # graded unfaithful — which is the one failure nothing downstream catches.
    # +54s per chunk, and worth it.
    "clean-transcript-chunk-single": "think",
    "clean-transcript-chunk-single-repair": "think",
    # How many notes a braindump is. 7/8 against 4/8, and the baseline carries a
    # silent failure on `raw-asr-piforge` that the thinking model does not.
    # Better on both instruments. +55s per braindump, on a stage that runs a
    # handful of times a day.
    "split-braindump": "think",
}

# Stages measured and deliberately left on `chat`, with the reason. Nothing
# reads this at runtime — it exists so that "not in the table" and "measured,
# and the answer was no" are different states in the file a person reads.
#
# Every key is the label a call site actually passes as `task=`, checked by
# `test_forge_routing.py`. That check exists because seven of these once named the
# eval case instead: `verify-packet` for what `forge_verify` passes as `verify`,
# `clean-document-chunk` for `document-ingest`'s `clean-chunk`, `ground-draft`
# for `vault-capture`'s `draft-note`. A reader took the record as read; an
# override written against one of those names would have parsed, validated, and
# done nothing, because `service_name_for` looks up the call site's string.
STAGES_HELD_ON_CHAT = {
    "classify-note": (
        "the thinking profile is better on the stage in isolation — 5/8 against 3/8, no silent "
        "failures — but `vault-organizer` classifies, verifies and escalates in one pipeline, and "
        "verification already runs on `think`. Routing classification there too would leave the "
        "same profile reviewing its own work, which is not what `verifier-seeded` measured and is "
        "not a trade the report can price. The gate improvement is also thin: 5/8 barely clears the "
        "0.6 floor, at +36s per note — about five hours on a 500-note vault. "
        "`variants/classify-per-property.json` and `classify-needs-review.json` test whether the "
        "real problem (0.85 per-property accuracy compounding across an all-or-nothing gate, and a "
        "`needsReview` flag no model ever sets) is fixable in the prompt on either model. Run those "
        "first; if they do not close it, revisit this with the pipeline in view rather than the stage."
    ),
    "summarize-transcript": "thinking ties on gates (8/8) and carries 2 silent failures; the small model 3",
    "draft-note": (
        "measured as the `grounding-draft` case: every candidate either gate-blocked or unstable "
        "across repeats. Note that case's scorer raised on every item before commit 1d0eac08f, so "
        "re-run it before treating this entry as current."
    ),
    "clean-chunk": "measured as `doc-cleanup-ocr`: no candidate cleared; thinking flipped on 4 items between attempts",
    "verify": (
        "measured as `verifier-seeded`: all three tie (7/8, 6/8, 7/8; precision 1.00/0.92/1.00), so "
        "the case cannot tell them apart — that is a statement about the case, not the models. "
        "Verification stays on `think` until `verifier-seeded` is strengthened. The small model "
        "stays out regardless: 0.25 false flags each buy a per-item escalation, the most expensive "
        "call in the pipeline."
    ),
    "verify-repair": "the corrective retry of `verify`, and it goes wherever `verify` goes",
}

# Capabilities the suite measures that no production stage corresponds to. These
# were in STAGES_HELD_ON_CHAT, which made the table read as if a call site
# somewhere passed `meeting-brief` as a stage. None does. They are kept because
# they say what a tier is *for* — the evidence behind the tier shapes described
# in `docs/skill-architecture.md` — and losing that would cost more than the
# confusion of filing them as stages did.
CAPABILITIES_MEASURED = {
    "summarize-report": "summarizing a report document: gates tie, silent failures do not",
    "meeting-brief": "synthesis over a whole meeting: small model 2/8 with 0.11 fact recall; thinking carries a silent failure",
    "enumerate-items": "breadth: small model 1/8 against 3/8, and slower per call on this prompt size",
    "abstention-grounded": "answering from a source: thinking ties exactly (12/12); the tie rule takes 5.8s over 14.7s",
}

# Which eval case measured each routed stage, and which model tier each service
# stands for. Together these are the join between this table and the evidence for
# it: `forge/evals/tests/test_evals.py` refuses a stage routed somewhere the
# latest report does not support, so a routing decision cannot outlive its
# measurement. A stage with no case here is one nothing has measured — which is
# why it is not in STAGE_SERVICES either.
STAGE_EVAL_CASES = {
    "clean-transcript-chunk-multi": "transcript-cleanup-meeting",
    "clean-transcript-chunk-multi-repair": "transcript-cleanup-meeting",
    "clean-transcript-chunk-single": "transcript-cleanup-memo",
    "clean-transcript-chunk-single-repair": "transcript-cleanup-memo",
    "connection-judgment": "connection-judgment",
    "split-braindump": "braindump-split",
    "classify-note": "classify-notes",
}

# `tier` as `models.json` spells it, per service name.
SERVICE_TIERS = {"chat": "bulk", "think": "verify", "task": "small"}

DEFAULT_SERVICE = "chat"

RESOLVERS = {
    "chat": lambda **kwargs: forge_llm.resolve_service("chat", **kwargs),
    "think": forge_llm.resolve_think_or_chat,
    "task": forge_llm.resolve_task_or_chat,
}


def routing_overrides(env=None, settings=None):
    """Per-stage overrides from ``connectedServices.routing``."""
    if settings is None:
        settings = forge_llm.load_connected_services(env).get("routing")
    return settings if isinstance(settings, dict) else {}


RUN_OVERRIDES_ATTRIBUTE = "_forge_routing_overrides"


def disable_unreachable(args, stages, timeout=30.0, env=None, settings=None):
    """Probe each service these stages route to, and pin the dead ones to ``chat``.

    Routing a bulk stage onto ``think`` introduced a failure mode the old
    arrangement did not have. Before, ``think`` carried only verification, and an
    unreachable one degraded politely — the run finished and the report said the
    work was not verified. Now a routed stage would hit that same dead endpoint
    mid-run and take the whole run down, which is a worse outcome than the
    slightly-worse model it was routed away from.

    So an unreachable target is treated the way an unreachable verifier already
    is: fall back, finish the work, and say plainly in the report that it ran
    somewhere other than intended. One probe per distinct service per run, at the
    start, where a warning is still actionable.

    Returns the warnings; the overrides are stashed on ``args`` so call sites
    need not thread them.
    """
    wanted = {}
    for stage in stages:
        name = service_name_for(stage, env=env, routing=None if settings is None else settings)
        if name != DEFAULT_SERVICE:
            wanted.setdefault(name, []).append(stage)
    if not wanted:
        return []

    warnings, overrides = [], dict(getattr(args, RUN_OVERRIDES_ATTRIBUTE, None) or {})
    for name, affected in wanted.items():
        service = service_for(affected[0], args, env=env, settings=settings)
        if service.get("fallback") or service.get("pinned"):
            # Already resolving to chat for a reason the resolver has recorded.
            continue
        probe = forge_llm.service_doctor(service, timeout=timeout, env=env)
        if probe.get("reachable"):
            continue
        for stage in affected:
            overrides[stage] = DEFAULT_SERVICE
        warnings.append(
            f"the `{name}` service is unreachable ({probe.get('detail') or 'no response'}), so "
            f"{', '.join(sorted(affected))} ran on `chat` instead. That is the fallback, not the measured choice."
        )
    if overrides:
        try:
            setattr(args, RUN_OVERRIDES_ATTRIBUTE, overrides)
        except AttributeError:
            pass
    return warnings


def service_name_for(stage, override=None, env=None, routing=None, args=None):
    """The service a stage should run on, before any of it is resolved."""
    if override:
        return override
    pinned = (getattr(args, RUN_OVERRIDES_ATTRIBUTE, None) or {}).get(stage) if args is not None else None
    if pinned in RESOLVERS:
        return pinned
    configured = routing_overrides(env=env, settings=routing).get(stage)
    if configured in RESOLVERS:
        return configured
    return STAGE_SERVICES.get(stage, DEFAULT_SERVICE)


def pinned_to_one_endpoint(args):
    """Whether this command was pointed at one server and no other.

    ``--base-url`` means "run this against this endpoint". If routing then sent a
    stage to `think` or `task`, those would resolve from settings and the run
    would quietly reach a server the caller never named — the built-in :8008, say,
    on a machine that only serves one. Naming one endpoint has to keep meaning
    what it has always meant, so routing yields to it.

    Passing ``--think-url`` (or ``--task-url``) alongside says the opposite: the
    caller knows about the split and has supplied both sides, so routing applies.
    """
    if args is None:
        return False
    named_chat = getattr(args, "base_url_provided", False)
    named_other = bool(getattr(args, "think_url", None) or getattr(args, "task_url", None))
    return named_chat and not named_other


def service_for(stage, args=None, override=None, env=None, settings=None, routing=None):
    """Resolve the service for ``stage``.

    Precedence is explicit argument, then an endpoint the command was pinned to,
    then ``connectedServices.routing``, then the table, then ``chat``.

    A target that is unconfigured or disabled resolves to ``chat`` and the
    returned service says so under ``fallback`` — the resolvers already work that
    way, and this preserves it rather than hiding it. A stage that quietly ran
    somewhere other than where it was routed is the failure this module exists to
    prevent, so the caller is always able to journal what actually happened.
    """
    name = service_name_for(stage, override=override, env=env, routing=routing, args=args)
    if name != DEFAULT_SERVICE and override is None and pinned_to_one_endpoint(args):
        service = forge_llm.service_from_args(args, "chat", env=env, settings=settings)
        return {**service, "stage": stage, "routedTo": name, "fallback": "chat", "pinned": True}
    if args is None or name not in forge_llm.SERVICE_ARGUMENT_NAMES:
        service = RESOLVERS.get(name, RESOLVERS[DEFAULT_SERVICE])(env=env, settings=settings)
        return {**service, "stage": stage, "routedTo": name}
    # A command that named an endpoint for this service keeps it — `--think-url`
    # has to reach a think-routed stage, not just the verifier it was added for —
    # and `service_from_args` caches the resolution for the hot loops.
    service = forge_llm.service_from_args(args, name, env=env, settings=settings)
    if name != DEFAULT_SERVICE and not (service["enabled"] and service["url"]):
        # Disabled tiers degrade toward the 27B, exactly as the resolvers do.
        service = {
            **forge_llm.service_from_args(args, "chat", env=env, settings=settings),
            "name": name,
            "fallback": "chat",
        }
    return {**service, "stage": stage, "routedTo": name}


def routing_record(service):
    """What to journal about where a call went, in one flat dict.

    ``routedTo`` is where the table sent it; ``name`` and ``fallback`` are where
    it landed. When those disagree the stage ran somewhere other than intended,
    and the run should be readable as such months later.
    """
    return {
        "stage": service.get("stage"),
        "routedTo": service.get("routedTo"),
        "ranOn": service.get("fallback") or service.get("name"),
        "url": service.get("url"),
        "model": service.get("model"),
    }

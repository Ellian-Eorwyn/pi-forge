#!/usr/bin/env python3
"""Researched schema proposals: what a field's practice would mean for this vault.

`vault-organizer` files notes into the schema that exists. This skill proposes
what the schema should be, for a kind of thing the vault does not yet know how
to hold.

The hard part is not drafting rows. It is that the model doing it is a local
non-thinking one, and `docs/service-split-handoff.md` §2.1 measured exactly how
that fails: asked an open question it answers with whatever the prompt made
salient and silently omits the rest -- four categories where a reasoning model
found eight. Left alone it would propose "identification, habitat, notes" for
any subject on earth and never think about naming authority, provenance,
condition scales, or the split between a reference card and a dated record.

So four things carry the weight, and none of them is the model being clever:

**The enumeration is shipped.** `references/catalog-dimensions.json` lists what
any catalog has to decide. The model says which apply and what this field calls
them; it never generates the list, and the prompt names the dimensions that get
skipped rather than only the full set, because naming the omission is what moved
the numbers.

**The practice comes off the network.** A field's real conventions are thin in a
local model's weights. Research runs through `web-research deep`, and a practice
with no claim id and no archived quote cannot be cited by a proposal. With no
network the run proposes nothing and says so -- a schema drafted from the
weights alone is the failure this skill exists to prevent.

**The moves are a closed set.** Reconciliation picks from eleven named moves, not
from an open design space. `approved-property` is deliberately not among them:
the vault's property list is global and closed, so a new property is inherited by
every note type and a nested one is stripped on the next filing pass. A field
that wants one gets `refused` with the argument attached, for the owner to weigh.

**Nothing is proposed that has not been proved.** Every schema move is applied to
a candidate copy of the schema note, which is reparsed, revalidated, and
re-drift-checked against the real vault before anyone reads it. Numbers come from
`free_numbers`, never from the model. This is the same principle as the quote
check in `literature-extraction`: a deterministic check beats a prompt rule.

Applying is per-id and additive only. The schema note is the owner's file; this
adds rows to it and can do nothing else to it -- no edit, no removal, no
renumber, and nothing at all in **Approved properties** or the **Project
registry**.
"""

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
import forge_llm
import forge_verify
import run_state
import vault_profile
import vault_wiki
from vault_schema import (
    UserError,
    candidate_schema_text,
    check_schema_drift,
    compile_destination,
    compiled_schema_for,
    drift_counts,
    free_numbers,
    parse_frontmatter,
    parse_schema_note,
    relative_path,
    require_safe_label,
    resolve_schema_path,
    selected_notes,
    sha256_text,
    split_frontmatter,
    validate_derived_paths,
)

WORKFLOW = "vault-curator"
STATE_DIR = ".vault-curator"
PROMPT_VERSION = "vault-curator-v1"
PHASES = ("frame", "research", "practices", "survey", "reconcile", "draft", "validate", "verify", "report")

WEB_TIMEOUT = 1800.0
MAX_QUERIES = 12
MAX_CLAIMS_PER_CALL = 60
CLAIM_EXCERPT_CHARS = 400

# A first guess, and the constant most likely to move. Below it a new area is
# proposed as a topic hub rather than a subdomain, because the schema's own
# change policy says a recurring area should usually begin as one and be
# promoted only when it is useful as a *storage boundary*. Volume is the
# evidence for that -- except when the field keeps dated event records, which
# accumulate by construction. Both reasons are reported.
SUBDOMAIN_NOTE_THRESHOLD = 12

# Length budgets, taken from vault-wiki's measured ones so a drafted kind spec
# is in the same register as the shipped ten. See
# forge/skills/vault-wiki/references/wiki-note-format.md.
MAX_DEFINITION_CHARS = 180
MAX_GUIDANCE_CHARS = 320
MAX_LEAD_CHARS = 400
MAX_KIND_SECTIONS = 8

MOVES = {
    "already-covered": "The vault already expresses this. Nothing to add.",
    "topic-hub": "A non-routing hub note, the way the schema says a new area should usually begin.",
    "note-type": "One bullet under Note types.",
    "domain": "One row in Domains, for records that are not reference cards.",
    "subdomain": "One row under Subdomains for an existing domain.",
    "source-kind": "One row in Source kinds, when the field's records are external sources.",
    "capture-type": "One bullet in Capture types, for a new way material enters the vault.",
    "body-table": "A managed table section on an existing wiki kind, the way Phenology is.",
    "wiki-kind": "A new wiki kind: section spec, template, and source policy.",
    "naming": "A note-title convention, applied by hand.",
    "refused": "The field wants something this vault's design does not offer.",
}
SCHEMA_MOVES = ("note-type", "domain", "subdomain", "source-kind", "capture-type")
REPO_MOVES = ("body-table", "wiki-kind")
INERT_MOVES = ("already-covered", "refused", "naming", "topic-hub")

# What the model is told it cannot have, by name. Left unsaid, a model asked for
# "good metadata" proposes a frontmatter property every time.
REFUSALS = {
    "approved-property": (
        "The approved-property list is global and closed: a new property is inherited by every "
        "note type in the vault, and a nested value is stripped the next time a note is filed. "
        "Structured per-record data goes in a managed body table instead."
    ),
    "project": (
        "Registering a project is a judgment about whether grouping files physically is useful, "
        "not a fact about a field."
    ),
    "renumber": "Moving an existing number is `vault-organizer renumber`, and it is the owner's call.",
    "edit": "Changing an existing row's label or definition is the owner's edit, never a proposal.",
}


# --------------------------------------------------------------------------- #
# Prompts
#
# Module constants, byte-stable across every call in a run, so the server's
# prefix cache stays warm. Per-item variation belongs in the user message.
# --------------------------------------------------------------------------- #

FRAME_SYSTEM = """You are scoping a research brief about how a field keeps records.

You are given a subject someone wants to catalogue, and a fixed list of dimensions every
catalogue has to settle. Decide which dimensions this subject's field actually has practice
about, and what that field calls each one.

Return JSON only:
{"field": "the established field or discipline this belongs to, in three words or fewer",
 "cluster": "one cluster id from the clusters given, or general",
 "dimensions": [{"id": "<dimension id>", "applies": true, "term": "what this field calls it",
                 "why": "one clause"}],
 "queries": ["up to four search queries that would find this field's published practice"]}

Rules:
- Include every dimension id you were given, with applies true or false. Do not invent ids.
- A dimension the field genuinely has no practice about is applies false with a one-clause why.
  That is a legitimate and useful answer.
- "term" is what a practitioner would say, not a restatement of the dimension label.
- Queries name the field and the practice, not the vault. Never mention Obsidian or a schema."""

PRACTICE_SYSTEM = """You are reading research findings and extracting one field's established practice.

You are given a dimension of cataloguing, and a list of claims from a research run. Each
claim carries an id and the text of what was found. Report only what the claims support.

Return JSON only:
{"practice": "one or two sentences stating what practitioners in this field actually do about
              this dimension, or an empty string if the claims do not establish it",
 "claims": ["claim ids that support it"],
 "standard": "the named standard, code, or authority if the claims name one, else empty",
 "confidence": "high | medium | low"}

Rules:
- An empty practice with an empty claims list is the right answer when the research did not
  reach this dimension. Say nothing rather than filling the gap from memory.
- Never cite a claim id you were not given.
- Do not describe what the field *should* do. Report what the claims say it does."""

RECONCILE_SYSTEM = """You are deciding how one piece of a field's practice fits into an existing vault.

You are given: the practice, the vault's current schema (its note types, domains, subdomains,
and the routes they compile to), and a closed list of moves. Choose exactly one move.

Return JSON only:
{"move": "<one move id>", "reason": "one sentence naming the practice and the move",
 "value": "the controlled value, lowercase with hyphens, or empty",
 "label": "the folder label in Title Case, or empty",
 "definition": "one sentence defining the value, for the schema note",
 "domain": "the parent domain value when the move is subdomain, else empty",
 "kind": "the wiki kind when the move is body-table, else empty",
 "heading": "the section heading when the move is body-table, else empty"}

Work through the moves in order and ask whether each one fits before settling. Do not stop at
subdomain and refused: the answer is often already-covered, and often a body-table or a
topic-hub rather than any change to the schema at all.

Rules:
- already-covered is the most common correct answer. A vault with a `place` note type and a
  `related` property already expresses most of what a field means by "location" and "see also".
- A new area begins as a topic-hub unless the field's records accumulate: choose domain or
  subdomain only when this practice produces many records over time.
- Structured per-record data is a body-table, never a property. There is no move that adds a
  frontmatter property, and asking for one is refused.
- Never propose a number. Numbers are assigned by the tool.
- refused is correct when the field genuinely needs something not on the list. Say what and why
  in the reason; the owner decides."""

KIND_SYSTEM = """You are writing the section specification for a new kind of reference card.

A reference card defines a thing so other notes can link to it. You are given the field, the
kind, and what the research established about how practitioners describe one. Produce the
sections a generator will own.

Return JSON only:
{"lead_guidance": "how to write the one-or-two-sentence definition at the top",
 "sections": [{"id": "snake_case", "heading": "Title Case", "fill": "bullets | prose | table",
               "guidance": "what goes here, addressed to the writer",
               "optional": false, "owner": false}]}

Rules:
- Between three and six sections. A card that needs more than that is a document, not a card.
- Each section is something a source can be cited for. A section whose content could only come
  from the owner's own hands or judgment is owner true, and gets no guidance about content.
- fill bullets for lists of discrete facts, prose for one or two lines, table for repeating
  region-scoped or per-instance rows.
- Do not include Sources, Evidence, Provenance, or Notes. Those are added for you.
- guidance is prompt text sent verbatim to the drafting model. Write it as an instruction."""

VERIFY_SYSTEM = """You are reviewing proposed changes to a personal vault's organizing schema.

Each item carries the field practice it implements, the claims that established that practice,
the move chosen, and the exact row or specification that would be added. Flag an item only when
one of these is true:

- the proposal does not implement the practice it cites, or cites a practice the claims do not
  support;
- the value, label, or definition says something different from the practice;
- the move is wrong in a way that matters: a reference card filed as an event record or the
  reverse, or a schema row for something the vault already expresses.

A defensible design is ok even if you would have named it differently; taste is not an error.
A definition that is terse is ok. A move you would have made one level higher or lower is ok
unless the practice contradicts it. Do not flag an item for being modest in scope."""


# --------------------------------------------------------------------------- #
# Tool contract
# --------------------------------------------------------------------------- #


def structured(status, artifacts=None, warnings=None, errors=None, data=None):
    return {
        "status": status,
        "artifacts": artifacts or [],
        "warnings": warnings or [],
        "errors": errors or [],
        "data": data,
    }


def error_entry(code, message):
    return {"code": code, "message": message}


def print_json(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def progress(message):
    print(message, file=sys.stderr, flush=True)


def skill_root():
    return Path(__file__).resolve().parents[1]


def references_root():
    return skill_root() / "references"


def wiki_references_root():
    """vault-wiki's references, which own the kind specs, templates, and source policy.

    Read rather than copied: a proposal for a new wiki kind has to agree with the
    ten that exist, and a second copy of the vocabulary is a second thing to drift.
    """
    return skill_root().parent / "vault-wiki" / "references"


def web_research_script():
    candidate = skill_root().parent / "web-research" / "scripts" / "web-research.mjs"
    return candidate if candidate.is_file() else None


def load_json(path, label):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise UserError(f"could not read {label} from {path}: {error}") from error


def load_dimensions():
    raw = load_json(references_root() / "catalog-dimensions.json", "catalog dimensions")
    dimensions = raw.get("dimensions")
    if not isinstance(dimensions, list) or not dimensions:
        raise UserError("catalog-dimensions.json has no dimensions")
    return raw


def load_catalog_sources():
    return load_json(references_root() / "catalog-sources.json", "catalog sources")


# --------------------------------------------------------------------------- #
# Run directories
# --------------------------------------------------------------------------- #


def unique_run_directory(base):
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    candidate = base / stamp
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = base / f"{stamp}-{suffix}"
    (candidate / "backup").mkdir(parents=True)
    (candidate / "patches").mkdir(parents=True)
    return candidate


def resolve_run_directory(vault, raw):
    if raw:
        run_dir = Path(raw).expanduser()
        if not run_dir.is_absolute():
            run_dir = (vault / run_dir).resolve()
        if not run_dir.is_dir():
            raise UserError(f"run directory does not exist: {run_dir}")
        return run_dir
    base = vault / STATE_DIR / "runs"
    runs = sorted((path for path in base.glob("*") if path.is_dir()), key=lambda path: path.name)
    if not runs:
        raise UserError(f"no runs under {base}; start one with `propose`")
    return runs[-1]


def decisions_path(vault):
    return vault / STATE_DIR / "decisions.jsonl"


def record_decision(vault, key, outcome, proposal_id):
    path = decisions_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    run_state.append_jsonl_fsync(
        path, {"at": run_state.utc_now(), "key": key, "outcome": outcome, "id": proposal_id}
    )


def settled_keys(vault):
    """Proposal keys the owner has already accepted or rejected, so they are not re-proposed."""
    path = decisions_path(vault)
    if not path.is_file():
        return {}
    rows, _ = run_state.read_jsonl_recover_tail(path)
    return {row["key"]: row["outcome"] for row in rows if row.get("key")}


# --------------------------------------------------------------------------- #
# Services
# --------------------------------------------------------------------------- #


def resolve_services(args):
    chat = forge_llm.resolve_service("chat", base_url=args.chat_url, model=args.chat_model)
    think = forge_llm.resolve_service("think", base_url=args.think_url, model=args.think_model)
    return chat, think


def call_json(service, system, payload, warnings, label, background=False):
    """One bounded call returning parsed JSON, or None with a warning."""
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    try:
        value, _record = forge_llm.call_json_with_retry(
            service, messages, temperature=0, background=background, task=f"{WORKFLOW}:{label}"
        )
    except (forge_llm.ChatError, ValueError) as error:
        warnings.append(f"{label}: {error}")
        return None
    if not isinstance(value, dict):
        warnings.append(f"{label}: model returned {type(value).__name__}, expected an object")
        return None
    return value


# --------------------------------------------------------------------------- #
# Phase: frame
# --------------------------------------------------------------------------- #


def cluster_for(catalog_sources, cluster_id, subject):
    clusters = catalog_sources.get("clusters", [])
    by_id = {entry["id"]: entry for entry in clusters}
    if cluster_id in by_id:
        return by_id[cluster_id]
    haystack = subject.lower()
    for entry in clusters:
        for match in entry.get("matches", []):
            if match != "*" and match in haystack:
                return entry
    return by_id.get("general", clusters[-1] if clusters else {"id": "general", "sources": []})


def build_queries(brief, catalog_sources, cluster):
    """The model's queries, plus templated and site-restricted ones it would not think of."""
    queries = []
    for query in brief.get("queries", [])[:4]:
        if isinstance(query, str) and query.strip():
            queries.append(query.strip())
    subject = brief["subject"]
    for template in catalog_sources.get("generic_queries", []):
        queries.append(template.replace("{subject}", subject))
    for source in cluster.get("sources", [])[:3]:
        site = source.get("site")
        if site:
            queries.append(f"site:{site} {subject} record fields")
    seen = []
    for query in queries:
        if query not in seen:
            seen.append(query)
    return seen[:MAX_QUERIES]


def frame_brief(service, entry, dimensions_doc, catalog_sources, warnings):
    payload = {
        "subject": entry["subject"],
        "entryPoint": entry["entry_point"],
        "evidence": entry.get("evidence", []),
        "clusters": [
            {"id": cluster["id"], "label": cluster["label"]} for cluster in catalog_sources.get("clusters", [])
        ],
        "dimensions": [
            {"id": item["id"], "label": item["label"], "question": item["question"]}
            for item in dimensions_doc["dimensions"]
        ],
        "enumerationClause": dimensions_doc["enumeration_clause"],
    }
    answer = call_json(service, FRAME_SYSTEM, payload, warnings, "frame") or {}
    known = {item["id"]: item for item in dimensions_doc["dimensions"]}
    chosen = []
    seen = set()
    for row in answer.get("dimensions") or []:
        if not isinstance(row, dict):
            continue
        identifier = row.get("id")
        if identifier not in known or identifier in seen:
            continue
        seen.add(identifier)
        chosen.append(
            {
                "id": identifier,
                "label": known[identifier]["label"],
                "question": known[identifier]["question"],
                "often_missed": bool(known[identifier].get("often_missed")),
                "vault_note": known[identifier].get("vault_note", ""),
                "applies": bool(row.get("applies", True)),
                "term": str(row.get("term", ""))[:120],
                "why": str(row.get("why", ""))[:200],
            }
        )
    # A dimension the model dropped is not a dimension that does not apply. The
    # whole point of shipping the list is that omission is the failure mode, so
    # anything missing comes back as applicable and unnamed.
    for identifier, item in known.items():
        if identifier in seen:
            continue
        warnings.append(f"frame: model omitted dimension '{identifier}'; kept as applicable")
        chosen.append(
            {
                "id": identifier,
                "label": item["label"],
                "question": item["question"],
                "often_missed": bool(item.get("often_missed")),
                "vault_note": item.get("vault_note", ""),
                "applies": True,
                "term": "",
                "why": "restored: the model did not answer for this dimension",
            }
        )
    chosen.sort(key=lambda row: [item["id"] for item in dimensions_doc["dimensions"]].index(row["id"]))
    cluster = cluster_for(catalog_sources, answer.get("cluster"), entry["subject"])
    brief = {
        "subject": entry["subject"],
        "entryPoint": entry["entry_point"],
        "refine": entry.get("refine"),
        "evidence": entry.get("evidence", []),
        "field": str(answer.get("field", ""))[:80] or entry["subject"],
        "cluster": cluster.get("id", "general"),
        "clusterSources": cluster.get("sources", []),
        "dimensions": chosen,
        "queries": [],
        "promptVersion": PROMPT_VERSION,
    }
    brief["queries"] = build_queries({**brief, "queries": answer.get("queries") or []}, catalog_sources, cluster)
    return brief


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


def organizer_suggestions(vault, limit=40):
    """Schema pressure the organizer already recorded, newest run first.

    Deterministic: the clustering was done by the organizer, and this reads its
    artifacts rather than re-deriving them.
    """
    base = vault / ".vault-organizer" / "runs"
    if not base.is_dir():
        return []
    evidence = []
    for run_dir in sorted((path for path in base.glob("*") if path.is_dir()), reverse=True):
        plan = run_dir / "plan.json"
        if not plan.is_file():
            continue
        try:
            data = json.loads(plan.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for row in data.get("schemaSuggestions") or data.get("schema_suggestions") or []:
            text = row.get("suggestion") if isinstance(row, dict) else row
            if isinstance(text, str) and text.strip():
                evidence.append({"kind": "suggestion", "text": text.strip(), "run": run_dir.name})
        for finding in data.get("schemaDrift") or data.get("drift") or []:
            if isinstance(finding, dict) and finding.get("severity") in {"high", "medium"}:
                evidence.append(
                    {
                        "kind": "drift",
                        "text": f"{finding.get('kind')}: {finding.get('detail', finding.get('path', ''))}",
                        "run": run_dir.name,
                    }
                )
        if evidence:
            break
    return evidence[:limit]


def resolve_entry(args, vault, schema):
    chosen = [name for name in ("subject", "from_vault", "refine") if getattr(args, name, None)]
    if len(chosen) != 1:
        raise UserError("give exactly one of --subject, --from-vault, or --refine")
    if args.subject:
        return {"entry_point": "subject", "subject": args.subject.strip(), "evidence": []}
    if args.from_vault:
        evidence = organizer_suggestions(vault)
        if not evidence:
            raise UserError(
                "no schema suggestions or drift findings in any vault-organizer run; "
                "run `vault-organizer vault` first, or name a subject with --subject"
            )
        subject = "; ".join(row["text"] for row in evidence[:6])
        return {"entry_point": "from-vault", "subject": subject, "evidence": evidence}
    route = args.refine.strip()
    domain, _, subdomain = route.partition("/")
    if domain not in schema["domains"]:
        raise UserError(f"--refine names unknown domain '{domain}'")
    if subdomain and subdomain not in schema["subdomains"].get(domain, {}):
        raise UserError(f"--refine names unknown subdomain '{subdomain}' under '{domain}'")
    definition = (
        schema["subdomains"][domain][subdomain]["definition"]
        if subdomain
        else schema["domains"][domain]["definition"]
    )
    return {
        "entry_point": "refine",
        "subject": definition or route,
        "refine": {"domain": domain, "subdomain": subdomain or None},
        "evidence": [{"kind": "route", "text": f"{route}: {definition}", "run": ""}],
    }


# --------------------------------------------------------------------------- #
# Phase: research
# --------------------------------------------------------------------------- #


def run_deep_research(brief, output_dir, timeout, warnings):
    """Invoke `web-research deep` and return its claim register, or None.

    Failure is never silent and never fatal-with-a-guess: the run continues to
    the report and proposes nothing, because a schema drafted out of the local
    model's weights is exactly what this skill exists to avoid.
    """
    register = output_dir / "claim_register.jsonl"
    if register.is_file():
        # A finished register is the answer. web-research resumes on its own
        # output directory, and re-running it to re-read a register it already
        # wrote spends fetches for nothing.
        progress("[research] reusing the claim register already in the run directory")
    else:
        script = web_research_script()
        if script is None:
            warnings.append("research: web-research is not installed beside this skill")
            return None
        queries = brief["queries"]
        if not queries:
            warnings.append("research: the brief produced no queries")
            return None
        arguments = [queries[0]]
        for query in queries[1:]:
            arguments.extend(["--query", query])
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            completed = subprocess.run(
                ["node", str(script), "deep", *arguments, "--output", str(output_dir)],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            warnings.append(f"research: could not run web-research ({error})")
            return None
        if not register.is_file():
            detail = (completed.stderr or completed.stdout or "").strip().splitlines()
            warnings.append(
                "research: web-research wrote no claim register"
                + (f" ({detail[-1][:200]})" if detail else "")
            )
            return None
    rows, _ = run_state.read_jsonl_recover_tail(register, repair=True)
    claims = []
    for row in rows:
        text = row.get("claim") or row.get("text") or row.get("statement") or ""
        if not isinstance(text, str) or not text.strip():
            continue
        claims.append(
            {
                "id": row.get("id") or f"claim-{len(claims) + 1:03d}",
                "text": text.strip()[:CLAIM_EXCERPT_CHARS],
                "sources": [str(value) for value in (row.get("sourceIds") or row.get("sources") or [])],
                "confidence": row.get("confidence", ""),
                "verification": row.get("verification", ""),
            }
        )
    if not claims:
        warnings.append("research: the deep run established no claims")
        return None
    return claims


def extract_practices(service, brief, claims, warnings):
    """One bounded call per applicable dimension. Claims are shared; the dimension varies."""
    practices = []
    applicable = [row for row in brief["dimensions"] if row["applies"]]
    for position, dimension in enumerate(applicable, start=1):
        payload = {
            "field": brief["field"],
            "subject": brief["subject"],
            "dimension": {
                "id": dimension["id"],
                "label": dimension["label"],
                "question": dimension["question"],
                "fieldTerm": dimension["term"],
            },
            "claims": claims[:MAX_CLAIMS_PER_CALL],
        }
        answer = call_json(service, PRACTICE_SYSTEM, payload, warnings, f"practice:{dimension['id']}")
        progress(f"[practices {position}/{len(applicable)}] {dimension['id']}")
        if not answer:
            continue
        practice = str(answer.get("practice", "")).strip()
        known = {claim["id"] for claim in claims}
        cited = [str(value) for value in (answer.get("claims") or []) if str(value) in known]
        if not practice:
            practices.append(
                {
                    "id": f"d-{dimension['id']}",
                    "dimension": dimension["id"],
                    "label": dimension["label"],
                    "practice": "",
                    "claims": [],
                    "standard": "",
                    "confidence": "none",
                    "note": "the research did not establish practice for this dimension",
                }
            )
            continue
        if not cited:
            # An uncited practice is the model answering from its weights, which
            # is the one thing this pipeline is built to refuse.
            warnings.append(f"practice:{dimension['id']}: dropped, no claim cited")
            practices.append(
                {
                    "id": f"d-{dimension['id']}",
                    "dimension": dimension["id"],
                    "label": dimension["label"],
                    "practice": "",
                    "claims": [],
                    "standard": "",
                    "confidence": "none",
                    "note": "the model stated a practice but cited no claim, so it was dropped",
                }
            )
            continue
        practices.append(
            {
                "id": f"d-{dimension['id']}",
                "dimension": dimension["id"],
                "label": dimension["label"],
                "practice": practice,
                "claims": cited,
                "standard": str(answer.get("standard", ""))[:120],
                "confidence": str(answer.get("confidence", "medium")),
                "note": "",
            }
        )
    return practices


# --------------------------------------------------------------------------- #
# Phase: survey
# --------------------------------------------------------------------------- #


def survey_vault(vault, schema_path, schema, entry):
    """The vault as it is: routes, free numbers, note counts, drift baseline.

    Deterministic and model-free. Everything a proposal is judged against comes
    from here, so a run can say exactly what the vault looked like when it was
    made.
    """
    counts = {}
    total = 0
    for path in selected_notes(vault, schema_path, "vault", None):
        try:
            split = split_frontmatter(path.read_bytes())
        except (OSError, UnicodeDecodeError):
            continue
        total += 1
        metadata = {} if split["malformed"] else parse_frontmatter(split["frontmatter_text"])
        domain = metadata.get("domain")
        if not domain:
            continue
        key = f"{domain}/{metadata.get('subdomain')}" if metadata.get("subdomain") else domain
        counts[key] = counts.get(key, 0) + 1

    routes = {}
    for value, domain in schema["domains"].items():
        routes[value] = str(compile_destination(schema, {"domain": value}))
        for subdomain in schema["subdomains"].get(value, {}):
            routes[f"{value}/{subdomain}"] = str(
                compile_destination(schema, {"domain": value, "subdomain": subdomain})
            )

    findings = check_schema_drift(vault, schema)
    refine = entry.get("refine") or {}
    refine_key = None
    if refine.get("domain"):
        refine_key = (
            f"{refine['domain']}/{refine['subdomain']}" if refine.get("subdomain") else refine["domain"]
        )
    return {
        "at": run_state.utc_now(),
        "noteCount": total,
        "notesByRoute": counts,
        "routes": routes,
        "types": sorted(schema["types"]),
        "statuses": sorted(schema["statuses"]),
        "captureTypes": sorted(schema["capture_types"]),
        "sourceKinds": sorted(schema["source_kinds"]),
        "properties": sorted(schema["properties"]),
        "domains": {
            value: {"label": entry_["label"], "definition": entry_["definition"]}
            for value, entry_ in schema["domains"].items()
        },
        "subdomains": {
            domain: {
                value: {"label": row["label"], "definition": row["definition"]}
                for value, row in rows.items()
            }
            for domain, rows in schema["subdomains"].items()
        },
        "wikiKinds": sorted(vault_wiki.WIKI_KINDS),
        "driftBaseline": sorted(finding["id"] for finding in findings),
        "driftCounts": drift_counts(findings),
        "refineRoute": refine_key,
        "refineNoteCount": counts.get(refine_key, 0) if refine_key else 0,
    }


# --------------------------------------------------------------------------- #
# Phase: reconcile
# --------------------------------------------------------------------------- #


def schema_summary(survey):
    """The compact schema view a reconcile call is handed. Small enough to repeat cheaply."""
    return {
        "noteTypes": survey["types"],
        "captureTypes": survey["captureTypes"],
        "sourceKinds": survey["sourceKinds"],
        "properties": survey["properties"],
        "domains": survey["domains"],
        "subdomains": survey["subdomains"],
        "wikiKinds": survey["wikiKinds"],
    }


def slug(value):
    text = re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower()).strip("-")
    return text[:40]


def title_label(value):
    text = re.sub(r"\s+", " ", str(value).strip())
    return text[:60]


def accumulating_field(practices):
    """True when the research established that this field keeps dated event records.

    The one reason to open a route for an area with no notes in it yet: records
    that accumulate need somewhere to accumulate. Without this the note-count
    rule would have refused the observations route that the natural-world
    feature turned on.
    """
    for row in practices:
        if row["dimension"] == "record_split" and row["practice"]:
            return True
    return False


def reconcile_practices(service, brief, practices, survey, warnings):
    summary = schema_summary(survey)
    moves = [{"id": key, "means": value} for key, value in MOVES.items()]
    refusals = [{"id": key, "why": value} for key, value in REFUSALS.items()]
    decisions = []
    live = [row for row in practices if row["practice"]]
    for position, row in enumerate(live, start=1):
        payload = {
            "field": brief["field"],
            "subject": brief["subject"],
            "dimension": {"id": row["dimension"], "label": row["label"]},
            "practice": row["practice"],
            "standard": row["standard"],
            "vaultNote": next(
                (item["vault_note"] for item in brief["dimensions"] if item["id"] == row["dimension"]), ""
            ),
            "schema": summary,
            "moves": moves,
            "notAvailable": refusals,
        }
        answer = call_json(service, RECONCILE_SYSTEM, payload, warnings, f"reconcile:{row['dimension']}")
        progress(f"[reconcile {position}/{len(live)}] {row['dimension']}")
        if not answer:
            continue
        move = str(answer.get("move", "")).strip()
        if move not in MOVES:
            warnings.append(f"reconcile:{row['dimension']}: unknown move '{move}', recorded as refused")
            move = "refused"
        decisions.append(
            {
                "practice": row["id"],
                "dimension": row["dimension"],
                "move": move,
                "reason": str(answer.get("reason", ""))[:300],
                "value": slug(answer.get("value", "")),
                "label": title_label(answer.get("label", "")),
                "definition": str(answer.get("definition", "")).strip()[:MAX_DEFINITION_CHARS],
                "domain": str(answer.get("domain", "")).strip(),
                "kind": str(answer.get("kind", "")).strip(),
                "heading": title_label(answer.get("heading", "")),
            }
        )
    return decisions


def demote_thin_routes(decisions, survey, practices, warnings):
    """Apply the schema's own "begin as a topic hub" rule, with its evidence.

    The schema note says a recurring area should usually start as a topic hub and
    be promoted only when it is useful as a storage boundary. Volume is the
    evidence for that, except for a field whose records accumulate by
    construction. Both reasons are recorded so the report can say which applied.
    """
    accumulates = accumulating_field(practices)
    for decision in decisions:
        if decision["move"] not in {"domain", "subdomain"}:
            continue
        key = (
            f"{decision['domain']}/{decision['value']}"
            if decision["move"] == "subdomain" and decision["domain"]
            else decision["value"]
        )
        existing = survey["notesByRoute"].get(key, 0)
        if existing >= SUBDOMAIN_NOTE_THRESHOLD:
            decision["routeEvidence"] = f"{existing} notes already carry this route"
            continue
        if accumulates:
            decision["routeEvidence"] = (
                "the field keeps dated records, which accumulate, so the route is opened before "
                "the notes exist"
            )
            continue
        decision["routeEvidence"] = (
            f"only {existing} notes would fall here and the field's records do not accumulate, "
            f"so it begins as a topic hub (threshold {SUBDOMAIN_NOTE_THRESHOLD})"
        )
        warnings.append(
            f"reconcile:{decision['dimension']}: {decision['move']} demoted to topic-hub, {decision['routeEvidence']}"
        )
        decision["demotedFrom"] = decision["move"]
        decision["move"] = "topic-hub"
    return decisions


# --------------------------------------------------------------------------- #
# Phase: draft — wiki kind specs and their templates
# --------------------------------------------------------------------------- #

RESERVED_SECTION_HEADINGS = {"sources", "evidence", "provenance", "notes"}


def draft_kind_spec(service, brief, decision, practices, warnings):
    payload = {
        "field": brief["field"],
        "kind": decision["value"],
        "subject": brief["subject"],
        "practices": [
            {"dimension": row["dimension"], "practice": row["practice"]} for row in practices if row["practice"]
        ],
        "budgets": {
            "maxSections": MAX_KIND_SECTIONS,
            "maxGuidanceChars": MAX_GUIDANCE_CHARS,
            "maxLeadChars": MAX_LEAD_CHARS,
        },
    }
    answer = call_json(service, KIND_SYSTEM, payload, warnings, f"kind:{decision['value']}")
    if not answer:
        return None
    sections = []
    seen = set()
    for entry in answer.get("sections") or []:
        if not isinstance(entry, dict):
            continue
        identifier = slug(entry.get("id", "")).replace("-", "_")
        heading = title_label(entry.get("heading", ""))
        if not identifier or not heading or identifier in seen:
            continue
        if heading.strip().lower() in RESERVED_SECTION_HEADINGS:
            continue
        seen.add(identifier)
        fill = entry.get("fill", "prose")
        if fill not in vault_wiki.DRAFTED_FILL_MODES:
            fill = "prose"
        owner = bool(entry.get("owner"))
        section = {
            "id": identifier,
            "heading": heading,
            "fill": fill,
            "guidance": "" if owner else str(entry.get("guidance", ""))[:MAX_GUIDANCE_CHARS],
            "optional": bool(entry.get("optional")),
        }
        if owner:
            section["owner"] = True
        else:
            section["placeholder"] = identifier
        if fill == "bullets":
            section["max_bullets"] = 5
            section["max_chars"] = 180
        elif fill == "prose":
            section["max_chars"] = 320
        sections.append(section)
        if len(sections) >= MAX_KIND_SECTIONS:
            break
    if len(sections) < 3:
        warnings.append(f"kind:{decision['value']}: model produced {len(sections)} usable sections, need 3")
        return None
    spec = {
        "max_managed_chars": 2000,
        "lead_guidance": str(answer.get("lead_guidance", ""))[:MAX_GUIDANCE_CHARS],
        "sections": (
            [{"id": "_lead", "placeholder": "summary", "fill": "lead", "max_chars": MAX_LEAD_CHARS,
              "guidance": "The definition. One or two sentences."}]
            + sections
            + [
                {"id": "sources", "heading": "Sources", "placeholder": "sources", "fill": "links",
                 "guidance": "Deterministic. Rendered from the archived sources, never written by the model."},
                {"id": "notes", "heading": "Notes", "owner": True,
                 "guidance": "Owner-authored. Never written or read by the pipeline."},
                {"id": "_footnotes", "placeholder": "footnotes", "fill": "footnotes",
                 "guidance": "Deterministic. Rendered from the citations the draft actually used."},
            ]
        ),
    }
    return spec


def render_kind_template(kind, spec):
    """The template file a drafted spec implies, in the shape the shipped ten have.

    `template_spec_drift` then proves the two agree before either is proposed,
    which is the same check `vault-wiki doctor` runs on the installed copies.
    """
    lines = ["---"]
    for key, value in vault_wiki.TEMPLATE_FRONTMATTER.items():
        lines.append(f"{key}: {value}")
    lines.extend(["---", "", "# {{title}}", "", "> [!abstract]", "> {{summary}}", ""])
    for section in spec["sections"]:
        identifier = section["id"]
        if identifier == "_lead":
            continue
        if identifier == "_footnotes":
            lines.extend(["{{footnotes}}"])
            continue
        lines.extend([f"## {section['heading']}", ""])
        if section.get("owner"):
            lines.append("")
        else:
            lines.extend([f"{{{{{section['placeholder']}}}}}", ""])
        if identifier == "sources":
            lines.extend(["## Evidence", "", "{{evidence}}", "", "## Provenance", "", "{{provenance}}", ""])
    return "\n".join(lines).rstrip("\n") + "\n"


def validate_kind_draft(kind, spec, warnings):
    """Prove a drafted spec parses and that its template agrees, before proposing either."""
    try:
        validated = vault_wiki.validate_proposed_kind_spec(kind, spec, "<proposal>")
    except UserError as error:
        warnings.append(f"kind:{kind}: spec rejected ({error})")
        return None
    body = render_kind_template(kind, spec)
    errors = vault_wiki.template_spec_drift(body, validated, f"Wiki {kind.title()}.md")
    if errors:
        warnings.append(f"kind:{kind}: template disagrees with spec ({'; '.join(errors[:2])})")
        return None
    return {"spec": spec, "template": body, "validated": validated}


# --------------------------------------------------------------------------- #
# Phase: validate — the proof
# --------------------------------------------------------------------------- #


def insertion_for(decision, schema):
    """The additive schema edit a decision implies, with its number chosen by code."""
    move = decision["move"]
    value = decision["value"]
    definition = decision["definition"]
    if not value:
        return None, "the model gave no value"
    if move == "note-type":
        return {"kind": "bullet", "heading": "Note types", "value": value, "definition": definition}, None
    if move == "capture-type":
        return {"kind": "bullet", "heading": "Capture types", "value": value, "definition": definition}, None
    label = decision["label"] or value.replace("-", " ").title()
    try:
        require_safe_label(label, "proposal")
    except UserError as error:
        return None, str(error)
    if move == "domain":
        numbers = free_numbers(schema, {"kind": "domain"})
        if not numbers:
            return None, "every domain number from 1 to 99 is taken"
        return {
            "kind": "row",
            "table": "Domains",
            "cells": {"Value": value, "Number": numbers[0], "Label": label, "Definition": definition},
        }, None
    if move == "subdomain":
        domain = decision["domain"]
        if domain not in schema["domains"]:
            return None, f"unknown parent domain '{domain}'"
        numbers = free_numbers(schema, {"kind": "subdomain", "domain": domain})
        if not numbers:
            return None, f"every subdomain number under '{domain}' is taken"
        return {
            "kind": "row",
            "table": f"Subdomains/{domain}",
            "cells": {"Value": value, "Number": numbers[0], "Label": label, "Definition": definition},
        }, None
    if move == "source-kind":
        if not schema.get("sources_root"):
            return None, "this vault files sources by domain, so there is no Source kinds table to add to"
        numbers = free_numbers(schema, {"kind": "source_kind"})
        if not numbers:
            return None, "every source-kind number is taken"
        return {
            "kind": "row",
            "table": "Source kinds",
            "cells": {"Value": value, "Number": numbers[0], "Label": label, "Definition": definition},
        }, None
    return None, f"'{move}' is not a schema move"


def prove_candidate(vault, schema_text, insertions, baseline):
    """Apply insertions to a copy of the schema note and prove the result is legal.

    Returns ``(ok, detail)``. Everything here is the gate that lets a weak model
    drive: the candidate has to parse, compile to unique paths, and introduce no
    high-severity drift the vault did not already have.
    """
    try:
        candidate, _rendered = candidate_schema_text(schema_text, insertions)
    except UserError as error:
        return False, f"the rows do not apply cleanly: {error}"
    try:
        parsed = parse_schema_note(candidate)
    except UserError as error:
        return False, f"the candidate schema does not parse: {error}"
    try:
        validate_derived_paths(parsed)
    except UserError as error:
        return False, f"the candidate schema compiles to a colliding path: {error}"
    try:
        findings = check_schema_drift(vault, parsed)
    except UserError as error:
        return False, f"the candidate schema could not be drift-checked: {error}"
    introduced = [
        finding
        for finding in findings
        if finding["severity"] == "high" and finding["id"] not in baseline
    ]
    if introduced:
        detail = "; ".join(f"{item['kind']} at {item['path']}" for item in introduced[:2])
        return False, f"the candidate schema introduces high-severity drift: {detail}"
    return True, candidate


# --------------------------------------------------------------------------- #
# Proposals
# --------------------------------------------------------------------------- #


def proposal_key(brief, decision):
    return sha256_text(
        json.dumps(
            {"subject": brief["subject"], "move": decision["move"], "value": decision["value"]},
            sort_keys=True,
        )
    )[:16]


def build_proposals(vault, schema_path, schema, brief, decisions, practices, survey, kind_drafts, warnings):
    """Turn reconcile decisions into reviewable, proved proposals."""
    schema_text = schema_path.read_text(encoding="utf-8")
    baseline = set(survey["driftBaseline"])
    by_practice = {row["id"]: row for row in practices}
    settled = settled_keys(vault)

    proposals = []
    held = []
    accepted_insertions = []
    schema_index = 0
    repo_index = 0

    for decision in decisions:
        practice = by_practice.get(decision["practice"], {})
        key = proposal_key(brief, decision)
        base = {
            "key": key,
            "move": decision["move"],
            "dimension": decision["dimension"],
            "practice": practice.get("practice", ""),
            "standard": practice.get("standard", ""),
            "claims": practice.get("claims", []),
            "reason": decision["reason"],
            "routeEvidence": decision.get("routeEvidence", ""),
            "demotedFrom": decision.get("demotedFrom", ""),
        }
        if decision["move"] in INERT_MOVES:
            # A demoted route is the interesting half of "not proposed": the
            # reason it was demoted is the evidence, not the reason the model
            # gave for wanting it.
            detail = decision["reason"]
            if decision.get("demotedFrom"):
                detail = f"proposed as a {decision['demotedFrom']}, but {decision.get('routeEvidence', '')}"
            held.append({**base, "outcome": decision["move"], "detail": detail})
            continue
        if key in settled:
            held.append({**base, "outcome": "settled", "detail": f"already {settled[key]} in a previous run"})
            continue

        if decision["move"] in SCHEMA_MOVES:
            insertion, refusal = insertion_for(decision, schema)
            if insertion is None:
                held.append({**base, "outcome": "held", "detail": refusal})
                continue
            trial = accepted_insertions + [insertion]
            ok, detail = prove_candidate(vault, schema_text, trial, baseline)
            if not ok:
                held.append({**base, "outcome": "held", "detail": detail})
                continue
            accepted_insertions = trial
            schema_index += 1
            _, rendered = candidate_schema_text(schema_text, [insertion])
            proposals.append(
                {
                    **base,
                    "id": f"s-{schema_index:03d}",
                    "side": "schema",
                    "insertion": insertion,
                    "rendered": rendered[0],
                    "value": decision["value"],
                    "label": decision["label"],
                    "definition": decision["definition"],
                    "proved": True,
                }
            )
            continue

        if decision["move"] == "wiki-kind":
            draft = kind_drafts.get(decision["value"])
            if not draft:
                held.append({**base, "outcome": "held", "detail": "the kind specification could not be drafted"})
                continue
            repo_index += 1
            proposals.append(
                {
                    **base,
                    "id": f"r-{repo_index:03d}",
                    "side": "repo",
                    "value": decision["value"],
                    "label": decision["label"],
                    "spec": draft["spec"],
                    "template": draft["template"],
                    "proved": True,
                }
            )
            continue

        if decision["move"] == "body-table":
            kind = decision["kind"]
            if kind not in vault_wiki.WIKI_KINDS:
                held.append({**base, "outcome": "held", "detail": f"'{kind}' is not a wiki kind"})
                continue
            heading = decision["heading"] or decision["label"]
            if not heading:
                held.append({**base, "outcome": "held", "detail": "no section heading given"})
                continue
            repo_index += 1
            proposals.append(
                {
                    **base,
                    "id": f"r-{repo_index:03d}",
                    "side": "repo",
                    "kind": kind,
                    "heading": heading,
                    "section": {
                        "id": slug(heading).replace("-", "_"),
                        "heading": heading,
                        "placeholder": slug(heading).replace("-", "_"),
                        "fill": "table",
                        "optional": True,
                        "max_bullets": 12,
                        "guidance": decision["reason"][:MAX_GUIDANCE_CHARS],
                    },
                    "proved": True,
                }
            )

    # The whole accepted set has to hold together, not just each row against the
    # note it was proved on. Two rows can each be legal and collide with each other.
    if accepted_insertions:
        ok, detail = prove_candidate(vault, schema_text, accepted_insertions, baseline)
        if not ok:
            warnings.append(f"validate: the accepted rows do not hold together ({detail}); schema side dropped")
            for proposal in proposals:
                if proposal["side"] == "schema":
                    held.append({**proposal, "outcome": "held", "detail": detail})
            proposals = [proposal for proposal in proposals if proposal["side"] != "schema"]
            accepted_insertions = []
    return proposals, held, accepted_insertions


# --------------------------------------------------------------------------- #
# Phase: verify
# --------------------------------------------------------------------------- #


def verify_proposals(service, proposals, journal, warnings):
    if not proposals:
        return {}
    items = []
    for proposal in proposals:
        items.append(
            {
                "id": proposal["id"],
                "practice": proposal["practice"],
                "claims": proposal["claims"],
                "move": proposal["move"],
                "reason": proposal["reason"],
                "proposed": proposal.get("rendered")
                or proposal.get("heading")
                or json.dumps(proposal.get("spec", {}), ensure_ascii=False)[:1200],
            }
        )
    try:
        return forge_verify.verify_packets(
            service, VERIFY_SYSTEM, items, journal_path=journal, progress=progress
        )
    except forge_verify.VerificationError as error:
        warnings.append(f"verify: {error}")
        return {}


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def render_report(brief, practices, survey, proposals, held, verdicts, warnings, run_dir):
    lines = [f"# Schema proposal: {brief['subject']}", ""]
    lines.extend(
        [
            f"- Field: {brief['field']}",
            f"- Entry point: {brief['entryPoint']}",
            f"- Source cluster: {brief['cluster']}",
            f"- Vault: {survey['noteCount']} notes, drift baseline {survey['driftCounts']}",
            f"- Run: `{run_dir}`",
            "",
        ]
    )

    lines.extend(["## Field practice", ""])
    established = [row for row in practices if row["practice"]]
    missing = [row for row in practices if not row["practice"]]
    if not established:
        lines.extend(
            [
                "Nothing was established. No proposal can be made from a run with no field practice —",
                "a schema drafted from the local model's training data alone is what this skill refuses.",
                "",
            ]
        )
    for row in established:
        standard = f" ({row['standard']})" if row["standard"] else ""
        lines.append(f"- **{row['label']}**{standard} — {row['practice']} [{', '.join(row['claims'])}]")
    if missing:
        lines.extend(["", "Dimensions the research did not reach:", ""])
        for row in missing:
            lines.append(f"- {row['label']} — {row['note'] or 'no claims'}")
    lines.append("")

    lines.extend(["## Proposals", ""])
    if not proposals:
        lines.extend(["None survived validation.", ""])
    for proposal in proposals:
        verdict = verdicts.get(proposal["id"], {})
        flag = " — **flagged by review**" if verdict.get("verdict") == "flag" else ""
        lines.append(f"### `{proposal['id']}` {proposal['move']}{flag}")
        lines.append("")
        lines.append(f"{proposal['reason']}")
        lines.append("")
        if proposal.get("rendered"):
            lines.extend(["```markdown", proposal["rendered"], "```", ""])
        if proposal.get("routeEvidence"):
            lines.extend([f"Route evidence: {proposal['routeEvidence']}", ""])
        if proposal.get("spec"):
            headings = [
                section["heading"] for section in proposal["spec"]["sections"] if section.get("heading")
            ]
            lines.extend([f"Sections: {', '.join(headings)}", ""])
        if proposal.get("heading"):
            lines.extend([f"Adds `## {proposal['heading']}` to the `{proposal.get('kind')}` card.", ""])
        lines.extend([f"Implements: {proposal['practice']} [{', '.join(proposal['claims'])}]", ""])
        if flag:
            lines.extend([f"> Reviewer: {verdict.get('reason', '')}", ""])

    if held:
        lines.extend(["## Not proposed", ""])
        for item in held:
            lines.append(f"- **{item['move']}** ({item['dimension']}) — {item['detail']}")
        lines.append("")

    if warnings:
        lines.extend(["## Warnings", ""])
        for warning in warnings:
            lines.append(f"- {warning}")
        lines.append("")
    return "\n".join(lines) + "\n"


def render_handoff(brief, proposals, survey, run_dir):
    """The paste-ready migration doc, in the shape of docs/vault-schema-created-and-nature.md."""
    schema_side = [item for item in proposals if item["side"] == "schema"]
    repo_side = [item for item in proposals if item["side"] == "repo"]
    lines = [
        f"# Schema migration: {brief['subject']}",
        "",
        "Every row below was applied to a copy of the schema note and proved: it parses, it",
        "compiles to a unique folder path, and it introduces no high-severity drift this vault",
        "did not already have. The numbers were chosen from the free slots in each parent, not",
        "by a model.",
        "",
    ]
    if not schema_side and not repo_side:
        lines.extend(["Nothing to apply.", ""])
        return "\n".join(lines)

    if schema_side:
        lines.extend(
            [
                "## 1. The schema note",
                "",
                "`99 Meta/99.02 Schemas/0.00 Vault Schema.md`. Apply them with the tool, which",
                "backs the note up and re-checks the result before committing:",
                "",
                "```bash",
                "python3 forge/skills/vault-curator/scripts/vault-curator.py apply \\",
                f"    --vault <vault> --run {run_dir.name} --accept {','.join(item['id'] for item in schema_side)}",
                "```",
                "",
                "Or paste them by hand:",
                "",
            ]
        )
        for item in schema_side:
            lines.extend([f"**{item['move']}** — {item['reason']}", "", "```markdown", item["rendered"], "```", ""])

    if repo_side:
        lines.extend(
            [
                "## 2. The machine side",
                "",
                "These are repo files, not vault files. They are written under `patches/` in the run",
                "directory for review; nothing in this repo is edited by the tool.",
                "",
            ]
        )
        for item in repo_side:
            if item["move"] == "wiki-kind":
                lines.extend(
                    [
                        f"**A new wiki kind `{item['value']}`** — {item['reason']}",
                        "",
                        f"- `patches/{item['id']}-wiki-kinds-{item['value']}.json` — the section spec, "
                        "to merge into `forge/skills/vault-wiki/references/wiki-kinds.json`",
                        f"- `patches/{item['id']}-Wiki {item['value'].title()}.md` — the template, for "
                        "`forge/skills/vault-wiki/references/templates/`",
                        f"- `forge/lib/vault_wiki.py` still needs `{item['value']}` added to "
                        "`WIKI_KIND_SUBDOMAIN`, `WIKI_KIND_TYPE`, and `WIKI_TEMPLATE_NAMES` — three "
                        "lines the tool deliberately does not write.",
                        "",
                    ]
                )
            else:
                lines.extend(
                    [
                        f"**A `## {item['heading']}` table on `{item.get('kind')}` cards** — {item['reason']}",
                        "",
                        f"- `patches/{item['id']}-section-{slug(item['heading'])}.json` — the section spec",
                        "",
                    ]
                )

    lines.extend(
        [
            "## 3. Afterwards",
            "",
            "Editing the schema note changes `schema_hash`, which is part of the classification",
            "cache key. A plain whole-vault run after this re-derives every classification through",
            "the model — slow, and lossy, because every note it hedges on lands back in `00 Inbox`.",
            "Use `--reuse-frontmatter`, which validates existing values with no model call:",
            "",
            "```bash",
            "python3 forge/skills/vault-organizer/scripts/vault-organizer.py vault \\",
            "    --vault <vault> --reuse-frontmatter",
            "```",
            "",
            "Then check the folders agree with the rows:",
            "",
            "```bash",
            "python3 forge/skills/vault-organizer/scripts/vault-organizer.py drift --vault <vault>",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def write_patches(run_dir, proposals):
    written = []
    for proposal in proposals:
        if proposal["side"] != "repo":
            continue
        if proposal["move"] == "wiki-kind":
            spec_path = run_dir / "patches" / f"{proposal['id']}-wiki-kinds-{proposal['value']}.json"
            run_state.atomic_write_json(spec_path, {"kinds": {proposal["value"]: proposal["spec"]}})
            template_path = run_dir / "patches" / f"{proposal['id']}-Wiki {proposal['value'].title()}.md"
            run_state.atomic_write_text(template_path, proposal["template"])
            written.extend([str(spec_path), str(template_path)])
        elif proposal["move"] == "body-table":
            path = run_dir / "patches" / f"{proposal['id']}-section-{slug(proposal['heading'])}.json"
            run_state.atomic_write_json(path, {"kind": proposal["kind"], "section": proposal["section"]})
            written.append(str(path))
    return written


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def resolve_vault(raw):
    vault = Path(raw).expanduser().resolve()
    if not vault.is_dir():
        raise UserError(f"vault directory does not exist: {vault}")
    return vault


def load_schema(vault, raw_schema):
    """``(path, compiled schema, sha256)`` — the cache is keyed by that hash."""
    schema_path = resolve_schema_path(vault, raw_schema)
    schema, schema_hash = compiled_schema_for(vault, schema_path, cache_dir=vault / STATE_DIR / "cache")
    return schema_path, schema, schema_hash


def command_doctor(args):
    warnings = []
    errors = []
    checks = {}
    vault = resolve_vault(args.vault)
    try:
        schema_path, schema, _hash = load_schema(vault, args.schema)
        checks["schema"] = {
            "path": str(relative_path(vault, schema_path)),
            "domains": len(schema["domains"]),
            "types": len(schema["types"]),
        }
    except UserError as error:
        errors.append(error_entry("schema_unreadable", str(error)))
        print_json(structured("error", errors=errors, data={"checks": checks}))
        return 1

    findings = check_schema_drift(vault, schema)
    counts = drift_counts(findings)
    checks["drift"] = counts
    if counts.get("high"):
        warnings.append(
            f"{counts['high']} high-severity schema drift finding(s); resolve them with "
            "`vault-organizer drift` before proposing — a proposal on top of a collision cannot be proved"
        )

    for name, path in (
        ("dimensions", references_root() / "catalog-dimensions.json"),
        ("sources", references_root() / "catalog-sources.json"),
    ):
        try:
            data = load_json(path, name)
            checks[name] = len(data.get("dimensions") or data.get("clusters") or [])
        except UserError as error:
            errors.append(error_entry("references_unreadable", str(error)))

    script = web_research_script()
    checks["webResearch"] = str(script) if script else None
    if script is None:
        warnings.append(
            "web-research is not installed beside this skill; every run would refuse to propose"
        )
    else:
        try:
            completed = subprocess.run(
                ["node", str(script), "doctor", "--json"],
                capture_output=True, text=True, timeout=120, check=False,
            )
            checks["webResearchDoctor"] = completed.returncode == 0
            if completed.returncode != 0:
                warnings.append("web-research doctor exited non-zero; search or extraction may be down")
        except (OSError, subprocess.SubprocessError) as error:
            checks["webResearchDoctor"] = False
            warnings.append(f"could not run web-research doctor: {error}")

    chat, think = resolve_services(args)
    for label, service, expect_non_thinking in (("chat", chat, True), ("think", think, False)):
        report = forge_llm.service_doctor(service, expect_non_thinking=expect_non_thinking)
        checks[label] = report
        if not report.get("reachable"):
            warnings.append(f"the {label} endpoint at {service['url']} did not answer")
        elif expect_non_thinking and report.get("reasoned"):
            warnings.append(
                f"the {label} endpoint is reasoning; bulk drafting will be far slower and costlier"
            )

    profile_path, profile_warnings = vault_profile.resolve_profile_or_warn(vault)
    checks["ownerRecord"] = bool(profile_path)
    warnings.extend(profile_warnings)

    status = "error" if errors else "ok"
    print_json(structured(status, warnings=warnings, errors=errors, data={"checks": checks}))
    return 1 if errors else 0


def command_propose(args):
    warnings = []
    vault = resolve_vault(args.vault)
    schema_path, schema, schema_hash = load_schema(vault, args.schema)
    entry = resolve_entry(args, vault, schema)
    chat, think = resolve_services(args)

    dimensions_doc = load_dimensions()
    catalog_sources = load_catalog_sources()

    run_dir = resolve_run_directory(vault, args.run) if args.run else unique_run_directory(vault / STATE_DIR / "runs")
    input_config = {
        "promptVersion": PROMPT_VERSION,
        "entry": entry["entry_point"],
        "subject": entry["subject"],
        "schemaHash": schema_hash,
        "chatModel": chat["model"],
        "thinkModel": think["model"],
        "noWeb": bool(args.no_web),
    }
    # `--run` names the directory being resumed, so it is the one option that is
    # expected to differ between the run that created the state and the run that
    # continues it. Everything else must match: resuming with a different model,
    # entry point, or schema would silently mix two runs' assumptions.
    options = {key: value for key, value in vars(args).items() if key not in {"run", "command"}}
    configuration = {"workflow": WORKFLOW, "command": "propose", "input": input_config, "options": options}
    if args.run:
        state = run_state.load_run_state(run_dir, workflow=WORKFLOW)
        run_state.assert_compatible_run(state, configuration)
    else:
        run_state.initialize_run_state(
            run_dir,
            run_state.create_run_state(
                WORKFLOW, "propose", input_config, options, phase="frame", next_action="research",
            ),
        )

    with run_state.run_lock(vault / STATE_DIR):
        progress(f"[frame] {entry['subject'][:70]}")
        brief = frame_brief(chat, entry, dimensions_doc, catalog_sources, warnings)
        run_state.atomic_write_json(run_dir / "brief.json", brief)
        run_state.update_run_state(run_dir, lambda state: state.update({"phase": "research"}))

        claims = None
        if args.no_web:
            warnings.append("research: skipped by --no-web, so nothing can be proposed")
        else:
            research_dir = (
                Path(args.research_dir).expanduser().resolve() if args.research_dir else run_dir / "research"
            )
            progress(f"[research] {len(brief['queries'])} queries")
            claims = run_deep_research(brief, research_dir, args.research_timeout, warnings)
        if claims:
            run_state.atomic_write_json(run_dir / "claims.json", {"count": len(claims), "claims": claims})

        practices = []
        if claims:
            run_state.update_run_state(run_dir, lambda state: state.update({"phase": "practices"}))
            practices = extract_practices(chat, brief, claims, warnings)
        for row in practices:
            run_state.append_jsonl_fsync(run_dir / "practices.jsonl", row)

        run_state.update_run_state(run_dir, lambda state: state.update({"phase": "survey"}))
        progress("[survey] reading the vault")
        survey = survey_vault(vault, schema_path, schema, entry)
        run_state.atomic_write_json(run_dir / "survey.json", survey)

        decisions = []
        kind_drafts = {}
        if any(row["practice"] for row in practices):
            run_state.update_run_state(run_dir, lambda state: state.update({"phase": "reconcile"}))
            decisions = reconcile_practices(chat, brief, practices, survey, warnings)
            decisions = demote_thin_routes(decisions, survey, practices, warnings)
            for row in decisions:
                run_state.append_jsonl_fsync(run_dir / "moves.jsonl", row)

            run_state.update_run_state(run_dir, lambda state: state.update({"phase": "draft"}))
            for decision in decisions:
                if decision["move"] != "wiki-kind" or not decision["value"]:
                    continue
                spec = draft_kind_spec(chat, brief, decision, practices, warnings)
                if spec is None:
                    continue
                draft = validate_kind_draft(decision["value"], spec, warnings)
                if draft:
                    kind_drafts[decision["value"]] = draft

        run_state.update_run_state(run_dir, lambda state: state.update({"phase": "validate"}))
        proposals, held, insertions = build_proposals(
            vault, schema_path, schema, brief, decisions, practices, survey, kind_drafts, warnings
        )

        verdicts = {}
        if proposals and not args.no_verify:
            run_state.update_run_state(run_dir, lambda state: state.update({"phase": "verify"}))
            verdicts = verify_proposals(think, proposals, run_dir / "verified.jsonl", warnings)
        elif args.no_verify:
            warnings.append("verify: skipped by --no-verify; nothing in this run was reviewed")

        for proposal in proposals:
            verdict = verdicts.get(proposal["id"], {})
            proposal["verdict"] = verdict.get("verdict", "unreviewed")
            proposal["objection"] = verdict.get("reason", "")
        proposals.sort(key=lambda item: (item["verdict"] != "flag", item["id"]))

        run_state.update_run_state(run_dir, lambda state: state.update({"phase": "report"}))
        (run_dir / "proposals.jsonl").unlink(missing_ok=True)
        (run_dir / "proposals.jsonl").touch()
        for proposal in proposals:
            run_state.append_jsonl_fsync(run_dir / "proposals.jsonl", proposal)
        run_state.atomic_write_json(
            run_dir / "validation.json",
            {"insertions": insertions, "driftBaseline": survey["driftBaseline"], "held": held},
        )
        patches = write_patches(run_dir, proposals)
        report = render_report(brief, practices, survey, proposals, held, verdicts, warnings, run_dir)
        run_state.atomic_write_text(run_dir / "report.md", report)
        handoff = render_handoff(brief, proposals, survey, run_dir)
        run_state.atomic_write_text(run_dir / "handoff.md", handoff)
        run_state.update_run_state(
            run_dir,
            lambda state: state.update(
                {"phase": "complete", "nextAction": "review, then apply --accept <ids>"}
            ),
        )

    flagged = sum(1 for proposal in proposals if proposal["verdict"] == "flag")
    print_json(
        structured(
            "ok",
            artifacts=[str(run_dir / "report.md"), str(run_dir / "handoff.md"),
                       str(run_dir / "proposals.jsonl")] + patches,
            warnings=warnings,
            data={
                "run": str(run_dir),
                "subject": brief["subject"],
                "field": brief["field"],
                "dimensionsApplicable": sum(1 for row in brief["dimensions"] if row["applies"]),
                "practicesEstablished": sum(1 for row in practices if row["practice"]),
                "proposals": len(proposals),
                "schemaProposals": sum(1 for item in proposals if item["side"] == "schema"),
                "repoProposals": sum(1 for item in proposals if item["side"] == "repo"),
                "flagged": flagged,
                "held": len(held),
                "nextAction": f"review --run {run_dir}",
            },
        )
    )
    return 0


def load_proposals(run_dir):
    path = run_dir / "proposals.jsonl"
    if not path.is_file():
        raise UserError(f"no proposals.jsonl in {run_dir}")
    rows, _ = run_state.read_jsonl_recover_tail(path, repair=True)
    return rows


def command_review(args):
    vault = resolve_vault(args.vault)
    run_dir = resolve_run_directory(vault, args.run)
    proposals = load_proposals(run_dir)
    window = proposals[args.offset : args.offset + args.limit]
    print_json(
        structured(
            "ok",
            artifacts=[str(run_dir / "report.md"), str(run_dir / "handoff.md")],
            data={
                "run": str(run_dir),
                "total": len(proposals),
                "offset": args.offset,
                "shown": len(window),
                "proposals": window,
                "nextAction": "apply --accept <ids>",
            },
        )
    )
    return 0


def command_apply(args):
    warnings = []
    vault = resolve_vault(args.vault)
    schema_path, _schema, _hash = load_schema(vault, args.schema)
    run_dir = resolve_run_directory(vault, args.run)
    proposals = load_proposals(run_dir)
    by_id = {proposal["id"]: proposal for proposal in proposals}

    accepted = [value for value in (args.accept or "").split(",") if value.strip()]
    rejected = [value for value in (args.reject or "").split(",") if value.strip()]
    if not accepted and not rejected:
        raise UserError("apply needs --accept <ids> and/or --reject <ids>")
    unknown = [value for value in accepted + rejected if value.strip() not in by_id]
    if unknown:
        raise UserError(
            f"unknown proposal id(s): {', '.join(unknown)}; this run has {', '.join(sorted(by_id))}"
        )
    accepted = [value.strip() for value in accepted]
    rejected = [value.strip() for value in rejected]

    schema_text = schema_path.read_text(encoding="utf-8")
    baseline = set(json.loads((run_dir / "validation.json").read_text(encoding="utf-8"))["driftBaseline"])

    insertions = []
    repo_ids = []
    for proposal_id in accepted:
        proposal = by_id[proposal_id]
        if proposal["side"] == "schema":
            insertions.append(proposal["insertion"])
        else:
            repo_ids.append(proposal_id)

    operations = [
        {"id": proposal_id, "move": by_id[proposal_id]["move"], "rendered": by_id[proposal_id].get("rendered")}
        for proposal_id in accepted
    ]
    if args.dry_run:
        print_json(
            structured(
                "ok",
                warnings=warnings,
                data={"run": str(run_dir), "dryRun": True, "operations": operations},
            )
        )
        return 0

    written = []
    if insertions:
        # Re-prove against the note as it is *now*, not as it was when the run
        # was made: the owner may have edited it in between, and a row proved
        # against a stale note is not proved at all.
        ok, candidate = prove_candidate(vault, schema_text, insertions, baseline)
        if not ok:
            raise UserError(f"refusing to write: {candidate}")
        backup = run_dir / "backup" / schema_path.name
        shutil.copy2(schema_path, backup)
        handle, temporary = tempfile.mkstemp(dir=str(schema_path.parent), suffix=".tmp")
        os.close(handle)
        temporary_path = Path(temporary)
        try:
            temporary_path.write_text(candidate, encoding="utf-8")
            reread = temporary_path.read_text(encoding="utf-8")
            parse_schema_note(reread)
            os.replace(str(temporary_path), str(schema_path))
        except (OSError, UserError) as error:
            temporary_path.unlink(missing_ok=True)
            raise UserError(f"refusing to write: {error}") from error
        written.append(str(schema_path))

    for proposal_id in accepted:
        record_decision(vault, by_id[proposal_id]["key"], "accepted", proposal_id)
    for proposal_id in rejected:
        record_decision(vault, by_id[proposal_id]["key"], "rejected", proposal_id)
    for proposal_id in accepted + rejected:
        run_state.append_jsonl_fsync(
            run_dir / "apply-log.jsonl",
            {
                "at": run_state.utc_now(),
                "id": proposal_id,
                "outcome": "accepted" if proposal_id in accepted else "rejected",
                "move": by_id[proposal_id]["move"],
            },
        )

    if repo_ids:
        warnings.append(
            "repo-side proposals are patch files under the run's `patches/` directory; nothing in "
            "the repo was edited: " + ", ".join(repo_ids)
        )
    if insertions:
        warnings.append(
            "the schema note changed, so the classification cache is stale: refile with "
            "`vault-organizer vault --reuse-frontmatter`, then run `vault-organizer drift`"
        )

    print_json(
        structured(
            "ok",
            artifacts=written,
            warnings=warnings,
            data={
                "run": str(run_dir),
                "accepted": accepted,
                "rejected": rejected,
                "rowsWritten": len(insertions),
                "backup": str(run_dir / "backup" / schema_path.name) if insertions else None,
            },
        )
    )
    return 0


def command_status(args):
    vault = resolve_vault(args.vault)
    run_dir = resolve_run_directory(vault, args.run)
    state = run_state.load_run_state(run_dir, workflow=WORKFLOW)
    proposals = load_proposals(run_dir) if (run_dir / "proposals.jsonl").is_file() else []
    print_json(
        structured(
            "ok",
            data={
                "run": str(run_dir),
                "phase": state.get("phase"),
                "nextAction": state.get("nextAction"),
                "proposals": len(proposals),
                "flagged": sum(1 for item in proposals if item.get("verdict") == "flag"),
                "phases": list(PHASES),
            },
        )
    )
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser():
    parser = argparse.ArgumentParser(
        description="Research a field's cataloguing practice and propose what it means for this vault."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(target, with_services=True):
        target.add_argument("--vault", required=True, help="path to the Obsidian vault")
        target.add_argument("--schema", help="path to the schema note, if it is not the default")
        if with_services:
            target.add_argument("--chat-url")
            target.add_argument("--chat-model")
            target.add_argument("--think-url")
            target.add_argument("--think-model")

    doctor = sub.add_parser("doctor", help="check the schema, the references, the endpoints, and web-research")
    add_common(doctor)

    propose = sub.add_parser("propose", help="research a field and propose schema and machine-side changes")
    add_common(propose)
    propose.add_argument("--subject", help="what you want to catalogue")
    propose.add_argument("--from-vault", action="store_true", help="start from the organizer's schema pressure")
    propose.add_argument("--refine", help="an existing route to check against field practice, as domain[/subdomain]")
    propose.add_argument("--run", help="resume this run directory")
    propose.add_argument(
        "--research-dir",
        help="an existing web-research run to read the claim register from, instead of researching again",
    )
    propose.add_argument("--no-web", action="store_true", help="skip research; the run will propose nothing")
    propose.add_argument("--no-verify", action="store_true", help="skip the thinking-model review")
    propose.add_argument("--research-timeout", type=float, default=WEB_TIMEOUT)

    review = sub.add_parser("review", help="read proposals a page at a time")
    add_common(review, with_services=False)
    review.add_argument("--run")
    review.add_argument("--limit", type=int, default=10)
    review.add_argument("--offset", type=int, default=0)

    apply_parser = sub.add_parser("apply", help="write accepted rows into the schema note")
    add_common(apply_parser, with_services=False)
    apply_parser.add_argument("--run")
    apply_parser.add_argument("--accept", help="comma-separated proposal ids to apply")
    apply_parser.add_argument("--reject", help="comma-separated proposal ids to record as declined")
    apply_parser.add_argument("--dry-run", action="store_true")

    status = sub.add_parser("status", help="where a run got to")
    add_common(status, with_services=False)
    status.add_argument("--run")
    return parser


def run(argv):
    args = build_parser().parse_args(argv)
    handlers = {
        "doctor": command_doctor,
        "propose": command_propose,
        "review": command_review,
        "apply": command_apply,
        "status": command_status,
    }
    return handlers[args.command](args)


def main():
    try:
        return run(sys.argv[1:])
    except UserError as error:
        print_json(structured("error", errors=[error_entry("user_error", str(error))]))
        return 1
    except Exception as error:  # noqa: BLE001 - the contract requires a JSON result
        print_json(structured("error", errors=[error_entry("internal_error", str(error))]))
        return 1


if __name__ == "__main__":
    sys.exit(main())

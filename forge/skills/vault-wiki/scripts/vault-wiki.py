#!/usr/bin/env python3
"""Comprehensive, cited wiki entity notes for an Obsidian vault.

A wiki note is a reference card: it defines a thing so other notes can link to
it. The vault has 466 of them and almost all are stubs — every figure note is
link scaffolding with no sentence saying who the person is. This skill fills
them in from canonical sources and cites what it used.

It is the one pi-forge skill that writes into an existing note's *body*, so the
whole design is about narrowing that permission. The kind spec names the sections
a generator owns; ``vault_wiki.merge_sections`` rewrites only those, matched by
their visible heading; and ``assert_only_managed_changed`` re-reads the result
and refuses the note unless everything else survived byte for byte. ``## Notes``
is owner-authored and is never written or read.

The research is tiered because 466 full research runs is hours of fetching for
notes that want three sentences each. Per note: the ``chat`` service drafts, one
or two canonical sources ground and cite it, and the ``think`` service reviews
the batch in packets of twenty against the *archived source text* — a reviewer
shown only a paraphrase has nothing to check and rubber-stamps. Deterministic
checks run before either model, because a fabricated URL is caught for free by
comparing it against what was actually fetched.

Nothing is written until the user accepts a run. A note the reviewer flagged is
still proposed, but it has to be named by id — batch acceptance skips it.
"""

import argparse
import datetime
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
import forge_llm
import forge_verify
import run_state
import vault_voice
import vault_wiki
from vault_schema import (
    UserError,
    compiled_schema_for,
    parse_frontmatter,
    relative_path,
    resolve_schema_path,
    selected_notes,
    serialize_frontmatter,
    sha256_bytes,
    sha256_text,
    split_frontmatter,
)

WORKFLOW = "vault-wiki"
STATE_DIR = ".vault-wiki"
PROMPT_VERSION = "vault-wiki-v1"

DEFAULT_PLAN_BATCH = 20
DEFAULT_SOURCES_PER_NOTE = 2
DEFAULT_SEARCH_LIMIT = 6
# The drafter and the reviewer must see byte-identical source text. When the
# reviewer saw less, it flagged claims the drafter had genuine support for and
# said so ("the excerpt cuts off mid-sentence") — a false flag that costs a
# thinking escalation and teaches the operator to distrust the reviewer. Sharing
# one budget makes that class of disagreement impossible.
# SearXNG rate-limits a burst of queries and answers with zero results rather
# than an error, which reads exactly like "this subject has no source". Pacing the
# searches is what keeps that from silently emptying a run.
SEARCH_DELAY_SECONDS = 2.0
SOURCE_EXCERPT_CHARS = 4000
DRAFT_SOURCE_BUDGET = 8000
# Items this large hold ~3 to a packet rather than 20, so review costs roughly
# one call per three notes. Correct beats cheap here.
VERIFY_PACKET_CHARS = 30000
MIN_QUOTE_WORDS = 5
WEB_TIMEOUT = 180

QUOTE_RE = re.compile(r"[\"“]([^\"”\r\n]{12,400})[\"”]")
YEAR_RE = re.compile(r"\b(1[0-9]{3}|20[0-9]{2})\b")
WIKILINK_RE = re.compile(r"\[\[([^\]\r\n|]+)(?:\|[^\]\r\n]*)?\]\]")
BULLET_RE = re.compile(r"^\s*[-*+]\s+")


# --------------------------------------------------------------------------- #
# Output plumbing
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


def print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def progress(message):
    print(message, file=sys.stderr, flush=True)


def utc_timestamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def phase(run_dir, name, event=None):
    run_state.update_run_state(run_dir, lambda draft: draft.update({"phase": name}) or draft, event=event)


def skill_root():
    return Path(__file__).resolve().parents[1]


def kind_specs():
    return vault_wiki.load_kind_specs(skill_root() / "references" / "wiki-kinds.json")


# --------------------------------------------------------------------------- #
# Source policy
# --------------------------------------------------------------------------- #


def load_source_policy(path=None):
    location = Path(path) if path else skill_root() / "references" / "canonical-sources.json"
    try:
        raw = json.loads(location.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise UserError(f"could not read source policy from {location}: {error}") from error
    sources = raw.get("sources")
    if not isinstance(sources, list) or not sources:
        raise UserError(f"{location} has no 'sources' list")
    for entry in sources:
        for key in ("id", "label", "site", "authority"):
            if key not in entry:
                raise UserError(f"{location}: source entry missing '{key}'")
    return {
        "path": str(location),
        "topics": tuple(raw.get("topics") or ("general",)),
        "sources": sorted(sources, key=lambda item: item["authority"]),
        "sha256": sha256_text(location.read_text(encoding="utf-8")),
    }


def sources_for(policy, kind, topic):
    """Which sources to try for a note, best first."""
    selected = []
    for entry in policy["sources"]:
        kinds = entry.get("kinds") or ["*"]
        topics = entry.get("topics") or ["*"]
        if "*" not in kinds and kind not in kinds:
            continue
        if "*" not in topics and topic not in topics:
            continue
        selected.append(entry)
    if not selected:
        selected = [entry for entry in policy["sources"] if "*" in (entry.get("topics") or [])]
    return selected


# --------------------------------------------------------------------------- #
# Vault reading
# --------------------------------------------------------------------------- #


def note_index(vault, schema_path):
    """Basename -> metadata for every note, so links can be resolved and typed."""
    index = {}
    for path in selected_notes(vault, schema_path, "vault", None):
        try:
            split = split_frontmatter(path.read_bytes())
        except (OSError, UnicodeDecodeError):
            continue
        metadata = parse_frontmatter(split["frontmatter_text"]) if split["had_frontmatter"] and not split["malformed"] else {}
        index[path.stem.casefold()] = {
            "title": path.stem,
            "path": relative_path(vault, path),
            "type": metadata.get("type"),
            "domain": metadata.get("domain"),
            "subdomain": metadata.get("subdomain"),
        }
    return index


def related_links(metadata):
    values = metadata.get("related")
    if isinstance(values, str):
        values = [values]
    links = []
    for value in values or []:
        for match in WIKILINK_RE.finditer(str(value)):
            target = match.group(1).strip()
            if target and target not in links:
                links.append(target)
    return links


def missing_section_ids(body, spec):
    """Managed sections a note has no usable content for."""
    parsed = vault_wiki.parse_sections(body)
    _title, lead = vault_wiki.split_preamble(parsed["blocks"][0]["content"])
    present = {}
    for block in parsed["blocks"][1:]:
        identifier = vault_wiki.resolve_section_id(block["heading"], spec)
        if identifier:
            present[identifier] = "".join(block["content"]).strip()
    missing = []
    for section in spec["sections"]:
        if section["owner"] or section["id"] == vault_wiki.FOOTNOTES_SECTION:
            continue
        if section["id"] == vault_wiki.LEAD_SECTION:
            if not "".join(lead).strip():
                missing.append(section["id"])
            continue
        if not present.get(section["id"]):
            missing.append(section["id"])
    return missing


def select_notes(vault, schema, schema_path, kinds, titles, only_empty, limit, specs):
    """The notes a run will work on, in a stable order."""
    wanted = {value.casefold() for value in titles} if titles else None
    seen = set()
    items = []
    for kind in kinds:
        folder = vault / vault_wiki.wiki_kind_folder(schema, kind)
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.md")):
            if wanted is not None and path.stem.casefold() not in wanted:
                continue
            data = path.read_bytes()
            split = split_frontmatter(data)
            if not split["had_frontmatter"] or split["malformed"]:
                continue
            metadata = parse_frontmatter(split["frontmatter_text"])
            resolved = vault_wiki.kind_for_metadata(metadata) or kind
            if resolved != kind:
                continue
            missing = missing_section_ids(split["body"], specs[kind])
            if only_empty and not missing:
                continue
            relative = relative_path(vault, path)
            if relative in seen:
                continue
            seen.add(relative)
            items.append(
                {
                    "id": f"w-{len(items) + 1:03d}",
                    "path": relative,
                    "title": path.stem,
                    "kind": kind,
                    "missingSections": missing,
                    "sha256Before": sha256_bytes(data),
                    "relatedLinks": related_links(metadata),
                }
            )
            if limit and len(items) >= limit:
                return items
    return items


# --------------------------------------------------------------------------- #
# Web acquisition
# --------------------------------------------------------------------------- #


def web_research_script():
    candidate = skill_root().parent / "web-research" / "scripts" / "web-research.mjs"
    return candidate if candidate.is_file() else None


def run_web_research(command, arguments, output_dir, timeout=WEB_TIMEOUT):
    """Invoke web-research and return its run report, or None on any failure.

    Every failure degrades to "no source" rather than failing the run: a note
    without a source is reported and left uncited, which is a visible outcome. A
    fabricated citation would not be.
    """
    script = web_research_script()
    if script is None:
        return None
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["node", str(script), command, *arguments, "--output", str(output_dir)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    report = output_dir / "research_report.json"
    if not report.is_file():
        return None
    try:
        return json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


USER_AGENT = "pi-forge-vault-wiki/1 (+https://github.com/pi-forge)"
HTTP_TIMEOUT = 30
INDEX_MAX_AGE_SECONDS = 7 * 24 * 3600
ANCHOR_RE = re.compile(r"<a\s[^>]*href=\"([^\"#?]+)\"[^>]*>(.*?)</a>", re.S | re.I)
TAG_RE = re.compile(r"<[^>]+>")


CA_BUNDLE_CANDIDATES = (
    "/etc/ssl/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/usr/local/etc/openssl/cert.pem",
)
_TLS_CONTEXT = None
_HTTP_FAILURES = []


def tls_context():
    """A verifying TLS context, working around Python builds with no CA bundle.

    A macOS framework Python points OpenSSL at an `etc/openssl/cert.pem` that
    does not exist, so every HTTPS call fails verification while `curl` on the
    same machine succeeds. Certificate verification is never disabled to get
    around it — an unverified fetch is exactly what a citation must not rest on.
    Instead the first usable bundle wins: an operator-set SSL_CERT_FILE, then a
    system bundle, then certifi if it happens to be installed.
    """
    global _TLS_CONTEXT
    if _TLS_CONTEXT is not None:
        return _TLS_CONTEXT
    candidates = []
    for variable in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        if os.environ.get(variable):
            candidates.append(os.environ[variable])
    candidates.extend(CA_BUNDLE_CANDIDATES)
    for path in candidates:
        if path and os.path.isfile(path):
            try:
                _TLS_CONTEXT = ssl.create_default_context(cafile=path)
                return _TLS_CONTEXT
            except (OSError, ssl.SSLError):
                continue
    try:
        import certifi

        _TLS_CONTEXT = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        _TLS_CONTEXT = ssl.create_default_context()
    return _TLS_CONTEXT


def http_get(url, params=None):
    """One GET, returning text, or None on any failure.

    Forgiving by design: a resolver that cannot reach its source falls through to
    the next source rather than failing the run. But the reason is recorded — a
    silently swallowed TLS error is indistinguishable from "this subject has no
    source", which is the most misleading thing this pipeline could report.
    """
    target = url if not params else f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(target, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT, context=tls_context()) as response:
            return response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as error:
        _HTTP_FAILURES.append(f"{urllib.parse.urlsplit(target).netloc}: {type(error).__name__} {error}")
        return None


def http_failures():
    """Distinct outbound-request failures seen so far, newest last."""
    seen = {}
    for message in _HTTP_FAILURES:
        seen[message] = None
    return list(seen)


def http_json(url, params=None):
    raw = http_get(url, params)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def cached_text(cache_dir, key, loader, max_age=INDEX_MAX_AGE_SECONDS):
    """Read a cached document, fetching it once when absent or stale."""
    path = cache_dir / "index" / f"{sha256_text(key)[:16]}.txt"
    if path.is_file() and (time.time() - path.stat().st_mtime) < max_age:
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            pass
    fetched = loader()
    if fetched is None:
        # A stale copy beats nothing: an index that is a week old still resolves
        # every entry that existed a week ago.
        return path.read_text(encoding="utf-8") if path.is_file() else None
    run_state.atomic_write_text(path, fetched)
    return fetched


def anchor_pairs(html, href_pattern, base):
    """(title, absolute url) for every anchor whose href matches."""
    pattern = re.compile(href_pattern, re.I)
    pairs = []
    for href, label in ANCHOR_RE.findall(html or ""):
        if not pattern.search(href):
            continue
        text = re.sub(r"\s+", " ", TAG_RE.sub("", label)).strip()
        if text:
            pairs.append((text, urllib.parse.urljoin(base, href)))
    return pairs


def best_candidate(title, candidates):
    """The candidate whose own title names the subject best.

    Matching on the index's title rather than on a fetched page means an
    off-target hit costs nothing — it is rejected before anything is downloaded.
    Scored rather than first-wins, because a 2,511-entry index is alphabetical,
    not relevance-ordered: taking the first acceptable match handed a note about
    the two truths doctrine the SEP's entry on `truth`.
    """
    best = None
    for entry_title, url in candidates:
        score = title_match_score(subject_key(title), entry_title)
        if score > 0 and (best is None or score > best[0]):
            best = (score, {"url": url, "title": entry_title, "relevance": "about"})
    return best[1] if best else None


def web_cache_dir(vault):
    """Where fetched pages live, shared across runs.

    Deliberately not under the run directory. Each run gets a fresh one, so a
    per-run cache means a re-run re-fetches every page — slow, rude to the search
    instance, and the fastest way to get rate-limited into a run full of "no
    source resolved". A vault-level cache makes a second pass nearly free.
    """
    return vault / STATE_DIR / "cache" / "web"


def resolve_via_mediawiki(title, entry):
    """Ask a MediaWiki site's own search for the article."""
    api = (entry.get("resolve") or {}).get("api")
    if not api:
        return None
    payload = http_json(api, {"action": "opensearch", "search": subject_key(title), "limit": 6, "format": "json"})
    if not isinstance(payload, list) or len(payload) < 4:
        return None
    titles, urls = payload[1], payload[3]
    if not isinstance(titles, list) or not isinstance(urls, list):
        return None
    return best_candidate(title, list(zip(titles, urls)))


def resolve_via_index(cache_dir, title, entry):
    """Match against a source's own table of contents.

    Strictly better than searching for the site: the SEP publishes all 2,511 of
    its entries on one page, so one cached fetch resolves every lookup offline —
    and it finds entries a site-restricted web search misses outright, including
    `entries/madhyamaka/` and `entries/twotruths-india/`.
    """
    config = entry.get("resolve") or {}
    url = config.get("url")
    if not url:
        return None
    html = cached_text(cache_dir, url, lambda: http_get(url))
    if not html:
        return None
    return best_candidate(title, anchor_pairs(html, config.get("hrefPattern") or "", url))


def resolve_via_wordpress(title, entry):
    """Ask a WordPress site's REST search for the entry."""
    api = (entry.get("resolve") or {}).get("api")
    if not api:
        return None
    payload = http_json(api, {"search": subject_key(title), "per_page": 6})
    if not isinstance(payload, list):
        return None
    candidates = [
        (str(hit.get("title") or ""), str(hit.get("url") or ""))
        for hit in payload
        if isinstance(hit, dict) and hit.get("url")
    ]
    return best_candidate(title, candidates)


def resolve_source_url(cache_dir, title, entry, extra_terms=""):
    """Find a source's page for a subject, preferring the source's own lookup.

    Never guesses a URL. SEP entry slugs are topic-based rather than name-based,
    so there is no ``/entries/latour/`` to construct, and a guessed URL that 404s
    is indistinguishable from a real one that was never checked.

    Each source declares how it is queried. A native lookup — the site's own API
    or published index — is preferred over general web search for two reasons.
    It is unaffected when the search engines behind SearXNG rate-limit or CAPTCHA
    the instance, which silently empties an entire run. And it is more accurate:
    the site's own index knows its entry titles, so a wrong hit is rejected
    before anything is downloaded.
    """
    method = (entry.get("resolve") or {}).get("method", "search")
    if method == "mediawiki":
        return resolve_via_mediawiki(title, entry)
    if method == "index":
        return resolve_via_index(cache_dir, title, entry)
    if method == "wordpress":
        return resolve_via_wordpress(title, entry)
    return resolve_via_search(cache_dir, title, entry, extra_terms)


def resolve_via_search(cache_dir, title, entry, extra_terms=""):
    """Site-restricted general web search — the fallback for sources with no API.

    The bare title is tried before the disambiguator, because adding topic words
    makes the query *worse*: web-research picks its engines from the query text,
    so "Sheila Jasanoff science and technology studies scholar site:en.wikipedia.org"
    routes to arXiv and returns telescope papers, while the bare name returns her
    article first. The disambiguator is a fallback for genuinely ambiguous names,
    not a default.
    """
    queries = [f"{title} site:{entry['site']}"]
    if extra_terms.strip():
        queries.append(f"{title} {extra_terms.strip()} site:{entry['site']}")
    for query in queries:
        output = cache_dir / "search" / sha256_text(query)[:16]
        fresh = not (output / "research_report.json").is_file()
        if fresh:
            time.sleep(SEARCH_DELAY_SECONDS)
        report = run_web_research("search", [query, "--limit", str(DEFAULT_SEARCH_LIMIT)], output)
        if not report:
            continue
        if fresh and not (report.get("results") or []):
            # Zero results on a fresh query is as likely to be throttling as a
            # genuine absence, and a cached empty answer would poison every later
            # run. Drop it so the next pass asks again.
            shutil.rmtree(output, ignore_errors=True)
        site = entry["site"].casefold()
        for result in report.get("results") or []:
            url = result.get("url")
            domain = (result.get("domain") or "").casefold()
            if url and (domain == site or domain.endswith("." + site)):
                return {"url": url, "title": result.get("title"), "snippet": result.get("snippet")}
    return None


def subject_key(title):
    """The part of a note title that names the thing.

    This vault names notes `Canonical Name, Gloss` — "Śūnyatā, Emptiness",
    "Two Truths Doctrine, Saṃvṛti and Paramārtha" — so the text before the first
    comma is the name to match a source against.
    """
    return normalized(title.split(",")[0])


MIN_COVERAGE_MENTIONS = 3
TITLE_STOPWORDS = {"the", "a", "an", "of", "in", "and", "or", "on", "to", "for", "as", "at", "by", "its"}
TITLE_OVERLAP = 2 / 3


def significant_tokens(text):
    return [token for token in re.findall(r"[\w']+", normalized(text)) if token not in TITLE_STOPWORDS]


def title_match_score(subject, page_title):
    """How well a page title names the subject, from 0 to 1.

    Substring matching alone is too strict for real encyclopedia titles: the SEP
    calls its Buddhist two-truths entry "The Theory of Two Truths in India", which
    neither contains "Two Truths Doctrine" nor is contained by it. Word overlap
    keeps that entry while still rejecting "God and Other Ultimates" for "God
    Trick" (one word of two) and "Feminist Ethics" for "Alison Jaggar" (none).

    There is deliberately no "the page title is a substring of the subject" rule.
    It reads as generous and is actively wrong: it matched the SEP's entry on
    *truth* to a note about the two truths doctrine, because "truth" is a
    substring of "two truths doctrine".
    """
    key = normalized(subject)
    page = normalized(page_title or "")
    if not key or not page:
        return 0.0
    if key in page:
        # An exact naming beats any partial overlap, and the shorter the
        # surrounding title the more squarely the page is about the subject.
        return 1.0 if key == page else 0.9
    subject_tokens = significant_tokens(subject)
    if len(subject_tokens) < 2:
        return 0.0
    page_tokens = set(significant_tokens(page_title))
    shared = sum(1 for token in subject_tokens if token in page_tokens)
    ratio = shared / len(subject_tokens)
    return ratio if ratio >= TITLE_OVERLAP else 0.0


def source_relevance(title, page_title, text=""):
    """How strongly a fetched page bears on the subject: about, covers, or neither.

    Site-restricted search returns a page on the site, not a page on the subject:
    searching the SEP for "Bruno Latour" returns an entry on the phenomenology of
    information technology that merely cites him. Drafting from that produces
    confident prose whose citation does not support it, and a reviewer handed the
    same page cannot tell.

    An encyclopedia entry *about* X puts X in its title, which is the strong test.
    But many notes here are coined phrases — "God Trick", "Situated Knowledge" —
    that no encyclopedia titles an entry after, while a broader entry discusses
    them at length. Requiring a title match would leave those permanently
    unciteable, so a page that names the subject repeatedly counts as *covering*
    it. The distinction is recorded and shown to the drafter and the reviewer
    rather than smoothed away, because "the SEP entry on feminist epistemology
    discusses this" is a different claim from "the SEP has an entry on this".
    """
    key = subject_key(title)
    if not key:
        return None
    if title_match_score(key, page_title) > 0:
        return "about"
    if text and normalized(text).count(key) >= MIN_COVERAGE_MENTIONS:
        return "covers"
    return None


def fetch_source(cache_dir, url):
    output = cache_dir / "read" / sha256_text(url)[:16]
    report = run_web_research("read", [url, "--no-browser"], output)
    if not report:
        return None
    for reading in report.get("readings") or []:
        if reading.get("extractionMethod") != "failed" and (reading.get("text") or "").strip():
            return reading
    return None


def acquire_sources(args, run_dir, cache_dir, item, plan, policy, existing, rejected=None):
    """Archive up to --sources-per-note canonical sources for one note."""
    records = []
    # Several policy entries can point at the same site — three of them are the
    # SEP — so the same page resolves more than once and would be cited twice.
    claimed = set()
    for entry in sources_for(policy, item["kind"], plan.get("topic") or "general"):
        if len(records) >= args.sources_per_note:
            break
        located = resolve_source_url(cache_dir, item["title"], entry, plan.get("disambiguator") or "")
        if not located or located["url"] in claimed:
            continue
        claimed.add(located["url"])
        if located["url"] in existing:
            cached = existing[located["url"]]
            if source_relevance(item["title"], cached["title"], archived_text(run_dir, cached)):
                records.append(cached)
            continue
        reading = fetch_source(cache_dir, located["url"])
        if not reading:
            continue
        page_title = reading.get("title") or located.get("title") or ""
        text = reading["text"]
        # Judged on the page that actually arrived, never on the title the
        # resolver promised. A resolver hit can be a redirect to somewhere else
        # entirely: Wikipedia's "Situated knowledge" redirects to "Knowledge", and
        # trusting the index title drafted a note about situated knowledge from
        # the general article on knowledge. If the target genuinely discusses the
        # subject this still passes, as `covers`.
        relevance = source_relevance(item["title"], page_title, text)
        if relevance is None:
            if rejected is not None:
                rejected.append({"url": located["url"], "pageTitle": page_title, "policyId": entry["id"]})
            continue
        source_id = f"s-{sha256_text(located['url'])[:10]}"
        archive = run_dir / "sources" / f"{source_id}.txt"
        archive.parent.mkdir(parents=True, exist_ok=True)
        run_state.atomic_write_text(archive, text)
        record = {
            "sourceId": source_id,
            "url": reading.get("url") or located["url"],
            "title": reading.get("title") or located.get("title") or entry["label"],
            "label": entry["label"],
            "policyId": entry["id"],
            "site": entry["site"],
            "relevance": relevance,
            "chars": len(text),
            "sha256": sha256_text(text),
            "archivePath": str(archive.relative_to(run_dir)),
        }
        existing[record["url"]] = record
        records.append(record)
    return records


def archived_text(run_dir, record):
    path = run_dir / record["archivePath"]
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #

PLAN_SYSTEM = """You route wiki reference notes to the right research topic.

For each note you are given a title, its wiki kind, and whatever the note
already says. Return the topic area it belongs to and up to three search phrases
that would find an authoritative encyclopedia entry for it.

Return exactly one JSON object and nothing else:
{"notes": [{"id": "<id>", "topic": "<one topic from the list>", "disambiguator": "<optional few words that separate this subject from same-named others, or empty>", "queries": ["<phrase>"]}]}

Include one entry for every id you were given and no ids you were not given.
Pick the topic from the supplied list only. The disambiguator matters most for
people and for terms that exist in several fields: for a Sanskrit term name the
tradition, for a scholar name their field."""


def draft_system(spec, voice_segment=""):
    lines = [
        f"You write one wiki reference note of kind '{spec['kind']}' for a personal Obsidian vault.",
        "",
        "A wiki note defines a thing so other notes can link to it. It is a reference",
        "card, not an essay: dense, skimmable, and short. It records what the thing is,",
        "not the vault owner's opinion of it.",
        "",
        "Write only from the SOURCES supplied. If the sources do not support a claim,",
        "leave it out — an omission is correct, an invention is not. Never state a date,",
        "a number, or a title that no source gives.",
        "",
        "Each source carries a relevance. 'about' means the page is an entry on this",
        "subject. 'covers' means a broader entry discusses it in passing — draw only on",
        "the parts that actually address the subject, and do not generalize the wider",
        "entry's claims onto it.",
        "",
        "Cite with footnote markers. Put [^1] at the end of a sentence carrying a",
        "load-bearing or contestable claim, and only there; a card peppered with markers",
        "is unreadable. Always mark the opening definition at minimum. Every marker you",
        "use must appear in your citations list, and every citation must name one of the",
        "source ids you were given.",
        "",
        "One citation entry per source you actually cite — not one per sentence. Two",
        "entries naming the same source with the same locator are the same citation.",
        "",
        "Sections to write:",
    ]
    for section in spec["sections"]:
        if section["id"] not in model_section_ids(spec):
            continue
        name = "the opening definition" if section["id"] == vault_wiki.LEAD_SECTION else f"'{section['heading']}'"
        limits = []
        if section["max_bullets"]:
            limits.append(f"at most {section['max_bullets']} bullets")
        if section["max_chars"]:
            unit = "per bullet" if section["fill"] == "bullets" else "total"
            limits.append(f"at most {section['max_chars']} characters {unit}")
        if section["optional"]:
            limits.append("omit entirely if the sources do not support it")
        suffix = f" ({'; '.join(limits)})" if limits else ""
        lines.append(f"- {section['id']} — {name}: {section['guidance']}{suffix}")
    lines.extend(
        [
            "",
            "A bullet section's value is a JSON array of strings, one entry per bullet,",
            "with no leading '- '. A prose section's value is a single string. Never put a",
            "line break inside any string.",
            "",
            "Wikilink a [[Target]] only when it appears in allowedLinks. Never invent one.",
            "",
            "Return exactly one JSON object and nothing else:",
            '{"sections": {"<section id>": "<markdown>"}, "citations": [{"label": "1", "sourceId": "<id>", "locator": "<section or page, or empty>"}]}',
            "",
            "The request hands you sectionsToFill with one empty string per section.",
            "Replace every empty string with that section's content. Write a value for",
            "every id you were given — an id you skip leaves a hole in the note. Drop an",
            "id only when it is marked optional above and the sources genuinely do not",
            "support it, and never return an empty string in place of dropping it.",
        ]
    )
    if voice_segment:
        lines.extend(["", "VOICE POLICY FOR GENERATED SOURCE PROSE:", voice_segment])
    return "\n".join(lines)


VERIFY_SYSTEM = """You review drafted wiki reference notes against their sources.

Each item gives you a note title, the drafted sections, and an excerpt of the
archived text of every source the draft was allowed to use. Judge the draft
against that excerpt — not against your own knowledge of the subject.

A source marked `"relevance": "covers"` is a broader entry that discusses the
subject rather than an entry about it. Claims drawn from it must be ones the
excerpt makes about *this* subject, not ones it makes about its wider topic.

Flag an item when any of these is true:
- a factual claim, date, number, or work title is not supported by the excerpts;
- a claim generalizes a "covers" source's wider topic onto this subject;
- a footnote marker points at a source that does not support the sentence it ends;
- the note describes the vault owner's opinion rather than defining the thing;
- the opening definition does not actually define the subject;
- a section is padding: it repeats the lead or says nothing specific.

Do not flag an item because you would have written it differently, because it is
brief, or because the excerpt is truncated. Brevity is the goal here."""


def model_section_ids(spec):
    """Sections the model writes. Links, sources, and footnotes are derived."""
    return tuple(
        section["id"]
        for section in spec["sections"]
        if not section["owner"] and section["fill"] in ("lead", "prose", "bullets")
    )


# --------------------------------------------------------------------------- #
# Planning and drafting
# --------------------------------------------------------------------------- #


def plan_notes(args, service, items, run_dir, policy, specs, warnings):
    journal = run_dir / "plan.jsonl"
    done = {row["id"]: row for row in read_journal(journal)}
    pending = [item for item in items if item["id"] not in done]
    batches = [pending[index:index + args.plan_batch] for index in range(0, len(pending), args.plan_batch)]
    for position, batch in enumerate(batches, start=1):
        payload = {
            "topics": list(policy["topics"]),
            "notes": [
                {
                    "id": item["id"],
                    "title": item["title"],
                    "kind": item["kind"],
                    "currentText": item.get("leadPreview", ""),
                    "relatedLinks": item["relatedLinks"][:8],
                }
                for item in batch
            ],
        }
        progress(f"[plan {position}/{len(batches)}] {len(batch)} notes")
        value, _record = forge_llm.call_json_with_retry(
            service,
            [
                {"role": "system", "content": PLAN_SYSTEM},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0,
            timeout=args.request_timeout,
            task="wiki-plan",
            response_format={"type": "json_object"},
        )
        rows = value.get("notes") if isinstance(value, dict) else None
        by_id = {row.get("id"): row for row in rows or [] if isinstance(row, dict)}
        for item in batch:
            row = by_id.get(item["id"]) or {}
            topic = row.get("topic")
            if topic not in policy["topics"]:
                if topic:
                    warnings.append(f"{item['title']}: unknown topic {topic!r}; using 'general'")
                topic = "general"
            record = {
                "id": item["id"],
                "topic": topic,
                "disambiguator": (row.get("disambiguator") or "")[:120],
                "queries": [str(query)[:200] for query in (row.get("queries") or [])][:3],
            }
            run_state.append_jsonl_fsync(journal, record)
            done[item["id"]] = record
    return done


def source_excerpts(run_dir, sources):
    """The source text a note is judged on — shared by drafting and review.

    One function, one budget, so the reviewer can never be shown less than the
    drafter was.
    """
    excerpts = []
    budget = DRAFT_SOURCE_BUDGET
    for record in sources:
        text = archived_text(run_dir, record).strip()
        if not text:
            continue
        slice_size = min(SOURCE_EXCERPT_CHARS, budget)
        if slice_size <= 0:
            break
        excerpts.append(
            {
                "sourceId": record["sourceId"],
                "label": record["label"],
                "title": record["title"],
                "url": record["url"],
                # "about" means the page is an entry on this subject; "covers"
                # means a broader entry discusses it. Both are citeable, but only
                # the first supports "the standard reference for X says".
                "relevance": record.get("relevance") or "about",
                "text": text[:slice_size],
            }
        )
        budget -= slice_size
    return excerpts


def draft_note(args, service, spec, item, sources, run_dir, allowed_links, voice_segment):
    excerpts = source_excerpts(run_dir, sources)
    payload = {
        "title": item["title"],
        "kind": item["kind"],
        "existingNote": item.get("bodyPreview", ""),
        # A skeleton to fill, not a list to enumerate: the non-thinking service
        # reliably drops sections when asked to produce the keys itself. Bullet
        # sections are arrays because multi-line strings were the single biggest
        # source of unparseable responses — an unescaped newline mid-string loses
        # the whole note.
        "sectionsToFill": {
            identifier: ([] if vault_wiki.section_by_id(spec, identifier)["fill"] == "bullets" else "")
            for identifier in model_section_ids(spec)
        },
        "allowedLinks": allowed_links,
        "sources": excerpts,
    }
    if not excerpts:
        payload["note"] = "No sources were supplied. Write no footnote markers and return an empty citations list."
    messages = [
        {"role": "system", "content": draft_system(spec, voice_segment)},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    options = {
        "temperature": 0,
        "timeout": args.request_timeout,
        "task": "wiki-draft",
        "response_format": {"type": "json_object"},
    }
    try:
        value, _record = forge_llm.call_json_with_retry(service, messages, **options)
        if not isinstance(value, dict) or not isinstance(value.get("sections"), dict):
            raise forge_llm.ChatError("response had no sections object")
    except forge_llm.ChatError as error:
        # One corrective retry that shows the model its own failure. Drafting is a
        # single call per note, so without this a lone malformed response silently
        # costs a whole note.
        repair = [
            *messages,
            {
                "role": "user",
                "content": f"That response was unusable: {error}. Return corrected JSON only, "
                "as one object with a 'sections' object and a 'citations' array.",
            },
        ]
        value, _record = forge_llm.call_json_with_retry(service, repair, **{**options, "task": "wiki-draft-repair"})
        if not isinstance(value, dict) or not isinstance(value.get("sections"), dict):
            raise UserError(f"{item['title']}: draft response had no sections object") from error
    sections = {}
    for key, raw in value["sections"].items():
        if key not in model_section_ids(spec):
            continue
        text = coerce_section(raw)
        if text:
            sections[key] = tidy_footnote_markers(normalize_section(key, text, spec))
    dedupe_footnote_markers(sections)
    citations = []
    for entry in value.get("citations") or []:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or "").strip()
        source_id = str(entry.get("sourceId") or "").strip()
        if label and source_id:
            citations.append({"label": label, "sourceId": source_id, "locator": str(entry.get("locator") or "").strip()})
    return {"id": item["id"], "sections": sections, "citations": citations}


def coerce_section(raw):
    """Accept a bullet section as an array or, if the model regressed, a string."""
    if isinstance(raw, list):
        items = [str(entry).strip().lstrip("-*+ ").strip() for entry in raw]
        return "\n".join(f"- {entry}" for entry in items if entry)
    if isinstance(raw, str):
        return raw.strip()
    return ""


def tidy_footnote_markers(text):
    """Put every marker tight against the end of its sentence.

    Models emit "…Boulder [^1]." and "…knowledges. [^1]" interchangeably; the
    convention is "…Boulder.[^1]". Purely positional, so it cannot change which
    source a claim is attributed to.
    """
    text = re.sub(r"[ \t]*\[\^([^\]\s]+)\][ \t]*([.,;:!?])", r"\2[^\1]", text)
    return re.sub(r"[ \t]+\[\^([^\]\s]+)\]", r"[^\1]", text)


def dedupe_footnote_markers(sections):
    """Keep one marker per source per section.

    A note with a single source otherwise ends up with [^1] on every bullet,
    which is noise on a card meant to be skimmed in seconds: the reader learns
    nothing from the fourth repetition that the first did not tell them. Keeping
    the first occurrence attributes the section without decorating it, and
    ``## Sources`` still carries the full reference.
    """
    for identifier, text in list(sections.items()):
        seen = set()

        def keep_first(match):
            label = match.group(1)
            if label in seen:
                return ""
            seen.add(label)
            return match.group(0)

        sections[identifier] = re.sub(r"\[\^([^\]\s]+)\]", keep_first, text)


def normalize_section(identifier, text, spec):
    """Repair the one formatting miss worth repairing rather than rejecting.

    A bullet section returned as newline-separated lines has the right content
    and the wrong markers. Adding the markers is deterministic and loses nothing,
    where holding the note back would cost a whole draft over punctuation. A
    section returned as one long paragraph is *not* repaired — that is a content
    problem, and ``check_budgets`` still catches it.
    """
    try:
        section = vault_wiki.section_by_id(spec, identifier)
    except KeyError:
        return text
    if section["fill"] != "bullets":
        return text
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or any(BULLET_RE.match(line) for line in lines):
        return text
    if len(lines) == 1:
        return text
    return "\n".join(f"- {line}" for line in lines)


# --------------------------------------------------------------------------- #
# Deterministic rendering
# --------------------------------------------------------------------------- #


def render_sources_section(sources):
    return "\n".join(f"- [{record['label']}]({record['url']})" for record in sources)


def render_footnotes(citations, sources):
    by_id = {record["sourceId"]: record for record in sources}
    lines = []
    for citation in citations:
        record = by_id.get(citation["sourceId"])
        if not record:
            continue
        locator = f" {citation['locator']}" if citation["locator"] else ""
        lines.append(f"[^{citation['label']}]: {record['label']}, “{record['title']}”{locator}.")
    return "\n".join(lines)


def existing_link_targets(body, spec, section_id):
    parsed = vault_wiki.parse_sections(body)
    for block in parsed["blocks"][1:]:
        if vault_wiki.resolve_section_id(block["heading"], spec) == section_id:
            return [match.group(1).strip() for match in WIKILINK_RE.finditer("".join(block["content"]))]
    return []


def render_link_section(body, spec, section_id, derived):
    """Union of what the section already lists and what `related` implies.

    Additive on purpose. Several figure notes list more colleagues in the section
    than they carry in `related`, so replacing the section from frontmatter alone
    would quietly delete links.
    """
    existing = existing_link_targets(body, spec, section_id)
    ordered = list(existing)
    seen = {value.casefold() for value in ordered}
    for value in derived:
        if value.casefold() not in seen:
            ordered.append(value)
            seen.add(value.casefold())
    if not ordered:
        return None
    return "\n".join(f"- [[{value}]]" for value in sorted(ordered, key=str.casefold))


def link_sections_for(item, spec, body, index):
    """Split `related` into the kind's link sections by the target's note type."""
    filled = {}
    people, others = [], []
    for target in item["relatedLinks"]:
        entry = index.get(target.casefold())
        if entry is None:
            continue
        (people if entry.get("type") == "person" else others).append(target)
    buckets = {"colleague_thinkers": people, "associated_concepts": others}
    for section in spec["sections"]:
        if section["fill"] != "links" or section["id"] == "sources":
            continue
        derived = buckets.get(section["id"], others)
        rendered = render_link_section(body, spec, section["id"], derived)
        if rendered:
            filled[section["id"]] = rendered
    return filled


def build_filled(draft, item, spec, body, sources, index):
    filled = dict(draft["sections"])
    filled.update(link_sections_for(item, spec, body, index))
    if sources:
        filled["sources"] = render_sources_section(sources)
        footnotes = render_footnotes(draft["citations"], sources)
        if footnotes:
            filled[vault_wiki.FOOTNOTES_SECTION] = footnotes
    return filled


# --------------------------------------------------------------------------- #
# Deterministic checks
# --------------------------------------------------------------------------- #


def normalized(text):
    """Fold the punctuation variants that would otherwise cause false mismatches.

    En- and em-dashes matter: Wikipedia titles its article "Actor–network theory"
    with an en-dash where the vault note uses a hyphen, and without folding them
    the page reads as merely covering its own subject.
    """
    folded = (text or "").replace("’", "'").replace("“", '"').replace("”", '"')
    folded = folded.replace("–", "-").replace("—", "-").replace("‑", "-")
    return re.sub(r"\s+", " ", folded).casefold()


def prose_of(sections):
    return "\n".join(sections.values())


def check_draft(draft, item, spec, sources, source_texts, original_body, index, allow_uncited):
    """Everything checkable without a model. Cheap, exact, and it runs first."""
    problems = []
    sections = draft["sections"]
    prose = prose_of(sections)
    if not sections:
        problems.append("draft produced no sections")
    if vault_wiki.LEAD_SECTION in item["missingSections"] and vault_wiki.LEAD_SECTION not in sections:
        problems.append("note has no definition and the draft did not supply one")

    # A URL that was never fetched cannot appear. This is the anti-fabrication gate.
    known_urls = {record["url"] for record in sources}
    for match in re.finditer(r"https?://[^\s)\]]+", prose):
        if match.group(0).rstrip(".,;") not in known_urls:
            problems.append(f"cites a URL this run never fetched: {match.group(0)}")

    # Footnotes must be internally consistent and point at real sources.
    known_ids = {record["sourceId"] for record in sources}
    labels = {citation["label"] for citation in draft["citations"]}
    referenced = set(vault_wiki.footnote_references(prose))
    for label in sorted(referenced - labels):
        problems.append(f"footnote [^{label}] has no citation entry")
    # An unreferenced citation is pruned rather than fatal: it declares a source
    # the prose never leans on, so dropping the entry changes no claim. The
    # reverse — a marker with no entry — stays fatal, because that is a claim
    # pointing at nothing.
    for label in sorted(labels - referenced):
        problems.append(f"citation [^{label}] is never referenced in the prose")
    for citation in draft["citations"]:
        if citation["sourceId"] not in known_ids:
            problems.append(f"citation [^{citation['label']}] names unknown source {citation['sourceId']}")
    if referenced and not sources and not allow_uncited:
        problems.append("draft cites footnotes but no source was archived")

    if sources:
        # Grounded checks need something to ground against. Without sources these
        # would reject every draft on its first date, which is why an ungrounded
        # run is marked uncited and refused at apply instead of pretending to
        # verify: skipping a check is honest, faking one is not.
        haystack = normalized(" ".join(source_texts) + " " + original_body)

        for match in QUOTE_RE.finditer(prose):
            quote = match.group(1).strip()
            if len(quote.split()) < MIN_QUOTE_WORDS:
                continue
            if normalized(quote) not in haystack:
                problems.append(f"quotes text absent from every archived source: “{quote[:60]}…”")

        # Dates are the specifics a model invents most readily.
        for year in sorted(set(YEAR_RE.findall(prose))):
            if year not in haystack:
                problems.append(f"states year {year}, which no source and no existing text contains")

    problems.extend(check_budgets(sections, spec))
    return problems


def collapse_duplicate_citations(draft):
    """Merge citations that point at the same place under one label.

    A model handed two sources routinely emits several labels for the same one, so
    a note ends up with `[^1]` and `[^2]` rendering identical footnotes. To a
    reader that looks like a defect. Labels are only merged when both the source
    and the locator match, so `§2` and `§4` of one entry stay distinct.
    """
    canonical = {}
    remap = {}
    for citation in draft["citations"]:
        key = (citation["sourceId"], citation["locator"])
        if key in canonical:
            remap[citation["label"]] = canonical[key]
        else:
            canonical[key] = citation["label"]
    if not remap:
        return {}
    draft["citations"] = [c for c in draft["citations"] if c["label"] not in remap]
    for identifier, text in list(draft["sections"].items()):
        draft["sections"][identifier] = re.sub(
            r"\[\^([^\]\s]+)\]",
            lambda match: f"[^{remap.get(match.group(1), match.group(1))}]",
            text,
        )
    dedupe_footnote_markers(draft["sections"])
    return remap


def prune_unused_citations(draft):
    """Drop citation entries the prose never references.

    Models routinely declare a source they end up not leaning on. The entry
    supports no claim, so removing it is lossless — and it is the difference
    between a usable note and one held back over bookkeeping.
    """
    referenced = set(vault_wiki.footnote_references(prose_of(draft["sections"])))
    unused = [citation["label"] for citation in draft["citations"] if citation["label"] not in referenced]
    if unused:
        draft["citations"] = [citation for citation in draft["citations"] if citation["label"] in referenced]
    return unused


def drop_unresolved_links(sections, item, index):
    """Unwrap wikilinks with no note behind them, keeping the words.

    Obsidian renders an unresolved link as a dead end, but the sentence around it
    is usually fine. Unwrapping costs a link and keeps a note; rejecting the draft
    costs the note over one bracket pair.
    """
    dropped = []

    def unwrap(match):
        target = match.group(1).strip()
        if target.casefold() in index or target.casefold() == item["title"].casefold():
            return match.group(0)
        dropped.append(target)
        return match.group(1).split("|")[0].strip()

    for identifier, text in list(sections.items()):
        sections[identifier] = WIKILINK_RE.sub(unwrap, text)
    return dropped


def check_budgets(sections, spec):
    problems = []
    total = 0
    for identifier, text in sections.items():
        total += len(text)
        try:
            section = vault_wiki.section_by_id(spec, identifier)
        except KeyError:
            problems.append(f"draft wrote unknown section '{identifier}'")
            continue
        if section["fill"] == "bullets":
            bullets = [line for line in text.splitlines() if BULLET_RE.match(line)]
            if not bullets:
                problems.append(f"section '{identifier}' should be bullets but has none")
            if section["max_bullets"] and len(bullets) > section["max_bullets"]:
                problems.append(f"section '{identifier}' has {len(bullets)} bullets, over the {section['max_bullets']} budget")
            for bullet in bullets:
                if section["max_chars"] and len(bullet) > section["max_chars"]:
                    problems.append(f"section '{identifier}' has a bullet of {len(bullet)} chars, over the {section['max_chars']} budget")
                    break
        else:
            if any(BULLET_RE.match(line) for line in text.splitlines()):
                problems.append(f"section '{identifier}' should be prose but is bulleted")
            if section["max_chars"] and len(text) > section["max_chars"]:
                problems.append(f"section '{identifier}' is {len(text)} chars, over the {section['max_chars']} budget")
    if spec["max_managed_chars"] and total > spec["max_managed_chars"]:
        problems.append(f"drafted sections total {total} chars, over the {spec['max_managed_chars']} budget for this kind")
    return problems


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #


def verify_items(proposals, run_dir):
    items = []
    for proposal in proposals:
        excerpts = source_excerpts(run_dir, proposal["sources"])
        items.append(
            {
                "id": proposal["id"],
                "title": proposal["title"],
                "kind": proposal["kind"],
                "sections": proposal["draft"]["sections"],
                "citations": proposal["draft"]["citations"],
                "sources": excerpts,
            }
        )
    return items


def verify_proposals(args, proposals, run_dir, warnings):
    if args.no_verify or not proposals:
        return {}
    try:
        service = forge_llm.resolve_think_or_chat(base_url=args.think_url, model=args.think_model)
    except forge_llm.ChatError as error:
        warnings.append(f"verification skipped: {error}")
        return {}
    if service.get("fallback"):
        warnings.append("thinking service unavailable; review ran on the chat service")
    try:
        return forge_verify.verify_packets(
            service,
            VERIFY_SYSTEM,
            verify_items(proposals, run_dir),
            journal_path=run_dir / "verified.jsonl",
            budget_characters=VERIFY_PACKET_CHARS,
            timeout=args.request_timeout,
            progress=progress,
        )
    except (forge_verify.VerificationError, forge_llm.ChatError) as error:
        warnings.append(f"verification did not complete: {error}")
        return {}


# --------------------------------------------------------------------------- #
# Journals
# --------------------------------------------------------------------------- #


def read_journal(path):
    if not Path(path).is_file():
        return []
    rows, _warnings = run_state.read_jsonl_recover_tail(path, repair=True)
    return rows


# --------------------------------------------------------------------------- #
# expand
# --------------------------------------------------------------------------- #


def command_expand(args):
    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        raise UserError(f"vault root does not exist: {vault}")
    warnings = []
    specs = kind_specs()
    policy = load_source_policy(args.source_policy)
    kinds = parse_kinds(args.kind)
    schema_path = resolve_schema_path(vault, args.schema)
    schema, schema_hash = compiled_schema_for(vault, schema_path, cache_dir=vault / STATE_DIR / "cache")
    templates = vault_wiki.require_wiki_templates(vault, schema, kinds, specs=specs)
    voice_path = vault_voice.resolve_voice_path(vault, args.voice, disabled=args.no_voice)
    voice, voice_hash = vault_voice.compiled_voice_for(vault, voice_path, cache_dir=vault / STATE_DIR / "cache")

    index = note_index(vault, schema_path)
    items = select_notes(vault, schema, schema_path, kinds, parse_titles(args.titles), args.only_empty, args.limit, specs)
    if not items:
        return structured("ok", warnings=["no wiki notes matched the selection"], data={"selected": 0})

    configuration = {
        "vault": str(vault),
        "schemaSha256": schema_hash,
        "kinds": list(kinds),
        "promptVersion": PROMPT_VERSION,
        "policySha256": policy["sha256"],
        "templates": {kind: record["sha256"] for kind, record in templates.items()},
        "voice": vault_voice.voice_state(voice_path, voice_hash, vault_voice.CONTEXT_SOURCE),
    }
    options = {
        "onlyEmpty": args.only_empty,
        "limit": args.limit,
        "sourcesPerNote": args.sources_per_note,
        "noWeb": args.no_web,
        "noVerify": args.no_verify,
    }

    if args.run:
        run_dir = Path(args.run).expanduser().resolve()
        state = run_state.load_run_state(run_dir, workflow=WORKFLOW)
        run_state.assert_compatible_run(state, {"workflow": WORKFLOW, "command": "expand", "input": configuration, "options": options})
    else:
        run_dir = unique_run_directory(vault, "expand")
        run_dir.mkdir(parents=True, exist_ok=True)
        run_state.initialize_run_state(
            run_dir,
            run_state.create_run_state(
                WORKFLOW,
                "expand",
                configuration,
                options,
                items=[{"id": item["id"], "path": item["path"], "status": "pending"} for item in items],
                phase="selected",
            ),
        )
    run_state.atomic_write_json(run_dir / "selected.json", items)

    with run_state.run_lock(run_dir):
        return expand_body(args, run_dir, vault, schema, specs, templates, policy, voice, index, items, warnings)


def expand_body(args, run_dir, vault, schema, specs, templates, policy, voice, index, items, warnings):
    bodies = {}
    for item in items:
        split = split_frontmatter((vault / item["path"]).read_bytes())
        bodies[item["id"]] = split["body"]
        parsed = vault_wiki.parse_sections(split["body"])
        _title, lead = vault_wiki.split_preamble(parsed["blocks"][0]["content"])
        item["leadPreview"] = "".join(lead).strip()[:400]
        item["bodyPreview"] = split["body"][:1200]

    chat = forge_llm.resolve_service("chat", base_url=args.base_url, model=args.model)
    phase(run_dir, "planning")
    plans = plan_notes(args, chat, items, run_dir, policy, specs, warnings)

    phase(run_dir, "acquiring")
    source_journal = run_dir / "sources.jsonl"
    archived = {row["url"]: row for row in read_journal(source_journal)}
    note_sources = {}
    if args.no_web:
        warnings.append("--no-web: drafts are ungrounded and cannot be applied")
        note_sources = {item["id"]: [] for item in items}
    else:
        assigned = {row["id"]: row for row in read_journal(run_dir / "assigned-sources.jsonl")}
        for position, item in enumerate(items, start=1):
            if item["id"] in assigned:
                note_sources[item["id"]] = [archived[url] for url in assigned[item["id"]]["urls"] if url in archived]
                continue
            progress(f"[acquire {position}/{len(items)}] {item['title']}")
            before = set(archived)
            off_topic = []
            records = acquire_sources(
                args, run_dir, web_cache_dir(vault), item, plans.get(item["id"], {}), policy, archived, off_topic
            )
            for record in records:
                if record["url"] not in before:
                    run_state.append_jsonl_fsync(source_journal, record)
            run_state.append_jsonl_fsync(
                run_dir / "assigned-sources.jsonl",
                {"id": item["id"], "urls": [record["url"] for record in records], "offTopic": off_topic},
            )
            note_sources[item["id"]] = records
            if not records:
                detail = ""
                if off_topic:
                    detail = f" (discarded {len(off_topic)} page(s) not about the subject, e.g. “{off_topic[0]['pageTitle']}”)"
                warnings.append(f"{item['title']}: no canonical source resolved; held back{detail}")

    for failure in http_failures()[:4]:
        warnings.append(f"source lookup could not reach a host — {failure}")

    phase(run_dir, "drafting")
    draft_journal = run_dir / "drafts.jsonl"
    drafts = {row["id"]: row for row in read_journal(draft_journal)}
    allowed = sorted({entry["title"] for entry in index.values()})
    for position, item in enumerate(items, start=1):
        if item["id"] in drafts:
            continue
        spec = specs[item["kind"]]
        sources = note_sources.get(item["id"]) or []
        if not sources and not args.no_web:
            drafts[item["id"]] = {"id": item["id"], "sections": {}, "citations": [], "skipped": "no source"}
            run_state.append_jsonl_fsync(draft_journal, drafts[item["id"]])
            continue
        progress(f"[draft {position}/{len(items)}] {item['title']}")
        voice_segment = vault_voice.prompt_segment(
            voice,
            note_type=vault_wiki.WIKI_KIND_TYPE[item["kind"]],
            context_mode=vault_voice.CONTEXT_SOURCE,
            material=item["title"],
        )
        try:
            draft = draft_note(args, chat, spec, item, sources, run_dir, nearby_links(allowed, item, index), voice_segment)
        except (UserError, forge_llm.ChatError) as error:
            warnings.append(f"{item['title']}: draft failed ({error})")
            draft = {"id": item["id"], "sections": {}, "citations": [], "skipped": str(error)}
        drafts[item["id"]] = draft
        run_state.append_jsonl_fsync(draft_journal, draft)

    phase(run_dir, "checking")
    proposals = []
    check_journal = run_dir / "checks.jsonl"
    if check_journal.exists():
        check_journal.unlink()
    for item in items:
        draft = drafts.get(item["id"]) or {}
        if not draft.get("sections"):
            reason = draft.get("skipped") or "the draft returned no sections"
            warnings.append(f"{item['title']}: held back — {reason}")
            run_state.append_jsonl_fsync(check_journal, {"id": item["id"], "ok": False, "problems": [reason]})
            continue
        spec = specs[item["kind"]]
        sources = note_sources.get(item["id"]) or []
        body = bodies[item["id"]]
        texts = [archived_text(run_dir, record) for record in sources]
        dropped = drop_unresolved_links(draft["sections"], item, index)
        if dropped:
            warnings.append(f"{item['title']}: unlinked {len(dropped)} target(s) with no note: {', '.join(sorted(set(dropped))[:4])}")
        collapse_duplicate_citations(draft)
        unused = prune_unused_citations(draft)
        if unused:
            warnings.append(f"{item['title']}: dropped {len(unused)} declared citation(s) the prose never used")
        if sources and not draft["citations"]:
            # The note is still attributed by `## Sources`, so this is not worth
            # discarding a good draft over — but it must be said rather than
            # shipped quietly as though per-claim citation had happened.
            warnings.append(
                f"{item['title']}: no inline citation — the draft placed no footnote markers, "
                "so only the Sources list attributes it"
            )
        problems = check_draft(draft, item, spec, sources, texts, body, index, args.no_web)
        filled = build_filled(draft, item, spec, body, sources, index)
        merged = None
        if not problems:
            try:
                merged = vault_wiki.merge_sections(body, spec, filled)
                vault_wiki.assert_only_managed_changed(body, merged, spec)
            except vault_wiki.MergeError as error:
                problems.append(f"merge refused: {error}")
                merged = None
        run_state.append_jsonl_fsync(check_journal, {"id": item["id"], "ok": not problems, "problems": problems})
        if problems or merged is None:
            warnings.append(f"{item['title']}: held back — {problems[0] if problems else 'merge produced nothing'}")
            continue
        proposals.append(
            {
                "id": item["id"],
                "path": item["path"],
                "title": item["title"],
                "kind": item["kind"],
                "action": "update",
                "sha256Before": item["sha256Before"],
                "bodyAfter": merged,
                "draft": draft,
                "sources": sources,
                "uncited": not sources,
                "weakSources": bool(sources) and all(record.get("relevance") == "covers" for record in sources),
            }
        )

    phase(run_dir, "verifying")
    verdicts = verify_proposals(args, proposals, run_dir, warnings)
    for proposal in proposals:
        verdict = verdicts.get(proposal["id"]) or {}
        proposal["verdict"] = verdict.get("verdict", "unreviewed")
        proposal["reason"] = verdict.get("reason", "")
    proposals.sort(key=lambda item: (item["verdict"] != forge_verify.VERDICT_FLAG, item["id"]))

    manifest = [{key: value for key, value in proposal.items() if key != "draft"} for proposal in proposals]
    run_state.atomic_write_json(run_dir / "proposals.json", manifest)
    fingerprint = run_state.configuration_fingerprint(manifest)
    run_state.update_run_state(
        run_dir,
        lambda draft: draft.update(
            {
                "phase": "proposed",
                "status": "awaiting-review",
                "proposalsSha256": fingerprint,
                "nextAction": f"review the proposals, then apply --run {run_dir}",
            }
        )
        or draft,
        event={"type": "proposed", "count": len(proposals)},
    )
    flagged = [proposal["id"] for proposal in proposals if proposal["verdict"] == forge_verify.VERDICT_FLAG]
    return structured(
        "ok",
        artifacts=[str(run_dir / "proposals.json")],
        warnings=warnings,
        data={
            "runDirectory": str(run_dir),
            "selected": len(items),
            "proposed": len(proposals),
            "flagged": flagged,
            "heldBack": len(items) - len(proposals),
            "uncited": [proposal["id"] for proposal in proposals if proposal["uncited"]],
            "proposals": [
                {
                    "id": proposal["id"],
                    "title": proposal["title"],
                    "kind": proposal["kind"],
                    "path": proposal["path"],
                    "verdict": proposal["verdict"],
                    "reason": proposal["reason"],
                    "sections": sorted(proposal["draft"]["sections"]),
                    "weakSources": proposal["weakSources"],
                    "sources": [
                        f"{record['url']} ({record.get('relevance') or 'about'})" for record in proposal["sources"]
                    ],
                }
                for proposal in proposals
            ],
        },
    )


def nearby_links(allowed, item, index):
    """Link targets worth offering the model, capped so the prompt stays small."""
    targets = [value for value in item["relatedLinks"] if value.casefold() in index]
    remaining = [value for value in allowed if value not in targets]
    return targets + remaining[: max(0, 120 - len(targets))]


# --------------------------------------------------------------------------- #
# apply / revert
# --------------------------------------------------------------------------- #


def command_apply(args):
    vault = Path(args.vault).expanduser().resolve()
    run_dir = Path(args.run).expanduser().resolve() if args.run else None
    if run_dir is None:
        raise UserError("apply needs --run <run-directory>")
    state = run_state.load_run_state(run_dir, workflow=WORKFLOW)
    manifest = json.loads((run_dir / "proposals.json").read_text(encoding="utf-8"))
    if run_state.configuration_fingerprint(manifest) != state.get("proposalsSha256"):
        raise UserError("the reviewed proposal manifest has changed since the run; re-run expand")
    if str(vault) != state["input"]["vault"]:
        raise UserError(f"this run was produced against {state['input']['vault']}, not {vault}")

    by_id = {proposal["id"]: proposal for proposal in manifest}
    accepted = parse_ids(args.accept)
    rejected = parse_ids(args.reject)
    flagged = {proposal["id"] for proposal in manifest if proposal["verdict"] == forge_verify.VERDICT_FLAG}
    uncited = {proposal["id"] for proposal in manifest if proposal.get("uncited")}
    if args.accept_batch:
        batch = [proposal["id"] for proposal in manifest if proposal["id"] not in flagged and proposal["id"] not in uncited]
        accepted = sorted(set(accepted) | set(batch))
    unknown = sorted((set(accepted) | set(rejected)) - set(by_id))
    if unknown:
        raise UserError(f"unknown proposal ids: {', '.join(unknown)}")
    both = sorted(set(accepted) & set(rejected))
    if both:
        raise UserError(f"ids both accepted and rejected: {', '.join(both)}")
    still_uncited = sorted(set(accepted) & uncited)
    if still_uncited:
        raise UserError(
            f"refusing to apply uncited proposals: {', '.join(still_uncited)}; "
            "re-run without --no-web so the claims are grounded"
        )
    if not accepted and not rejected:
        raise UserError("apply needs --accept, --accept-batch, or --reject")

    specs = kind_specs()
    warnings = []
    applied = []
    results = {"updated": 0, "skipped": 0, "rejected": len(rejected)}
    for proposal_id in accepted:
        proposal = by_id[proposal_id]
        path = vault / proposal["path"]
        if not path.is_file():
            warnings.append(f"{proposal['path']} no longer exists; skipped")
            results["skipped"] += 1
            continue
        data = path.read_bytes()
        current = sha256_bytes(data)
        split = split_frontmatter(data)
        if current == sha256_text_of_body(split, proposal["bodyAfter"]):
            applied.append({"action": "already-applied", "id": proposal_id, "path": proposal["path"]})
            results["skipped"] += 1
            continue
        if current != proposal["sha256Before"]:
            warnings.append(f"{proposal['path']} changed since review; skipped")
            results["skipped"] += 1
            continue
        # Re-prove the ownership invariant against the bytes on disk, not against
        # what the run remembers seeing.
        try:
            vault_wiki.assert_only_managed_changed(split["body"], proposal["bodyAfter"], specs[proposal["kind"]])
        except vault_wiki.MergeError as error:
            warnings.append(f"{proposal['path']}: refused at apply ({error})")
            results["skipped"] += 1
            continue
        rebuilt = rebuild_note(split, proposal["bodyAfter"])
        if args.dry_run:
            applied.append({"action": "update", "id": proposal_id, "path": proposal["path"], "dryRun": True})
            results["updated"] += 1
            continue
        backup = run_dir / "backup" / proposal["path"]
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        atomic_write_bytes(path, rebuilt)
        run_state.append_jsonl_fsync(
            run_dir / "apply-log.jsonl",
            {
                "at": run_state.utc_now(),
                "operation": "merge_sections",
                "id": proposal_id,
                "path": proposal["path"],
                "sha256Before": current,
                "sha256After": sha256_bytes(rebuilt),
                "status": "ok",
            },
        )
        applied.append({"action": "update", "id": proposal_id, "path": proposal["path"]})
        results["updated"] += 1

    if not args.dry_run:
        run_state.update_run_state(
            run_dir,
            lambda draft: draft.update({"phase": "applied", "status": "complete"}) or draft,
            event={"type": "applied", "accepted": len(accepted), "rejected": len(rejected)},
        )
    return structured(
        "ok",
        artifacts=[str(run_dir / "apply-log.jsonl")] if not args.dry_run else [],
        warnings=warnings,
        data={
            "runDirectory": str(run_dir),
            "dryRun": args.dry_run,
            "accepted": accepted,
            "rejected": rejected,
            "results": results,
            "operations": applied,
        },
    )


def command_revert(args):
    vault = Path(args.vault).expanduser().resolve()
    run_dir = Path(args.run).expanduser().resolve() if args.run else None
    if run_dir is None:
        raise UserError("revert needs --run <run-directory>")
    run_state.load_run_state(run_dir, workflow=WORKFLOW)
    entries = [row for row in read_journal(run_dir / "apply-log.jsonl") if row.get("status") == "ok"]
    if not entries:
        return structured("ok", warnings=["this run wrote nothing to revert"], data={"restored": 0})
    warnings = []
    restored = []
    for row in reversed(entries):
        path = vault / row["path"]
        backup = run_dir / "backup" / row["path"]
        if not backup.is_file():
            warnings.append(f"{row['path']}: no backup in this run; skipped")
            continue
        if not path.is_file():
            warnings.append(f"{row['path']} no longer exists; skipped")
            continue
        current = sha256_bytes(path.read_bytes())
        if current == row["sha256Before"]:
            restored.append({"path": row["path"], "action": "already-reverted"})
            continue
        if current != row.get("sha256After"):
            warnings.append(f"{row['path']} was edited after this run applied; left alone")
            continue
        if args.dry_run:
            restored.append({"path": row["path"], "action": "restore", "dryRun": True})
            continue
        atomic_write_bytes(path, backup.read_bytes())
        run_state.append_jsonl_fsync(
            run_dir / "revert-log.jsonl",
            {"at": run_state.utc_now(), "path": row["path"], "restoredTo": row["sha256Before"], "status": "ok"},
        )
        restored.append({"path": row["path"], "action": "restore"})
    return structured(
        "ok",
        artifacts=[str(run_dir / "revert-log.jsonl")] if not args.dry_run else [],
        warnings=warnings,
        data={"runDirectory": str(run_dir), "dryRun": args.dry_run, "restored": len(restored), "operations": restored},
    )


def rebuild_note(split, body):
    """Reattach the original frontmatter and BOM to a rewritten body."""
    prefix = b"\xef\xbb\xbf" if split["had_bom"] else b""
    if not split["had_frontmatter"]:
        return prefix + body.encode("utf-8")
    return prefix + ("---\n" + split["frontmatter_text"] + "---\n" + body).encode("utf-8")


def sha256_text_of_body(split, body):
    return sha256_bytes(rebuild_note(split, body))


def atomic_write_bytes(path, data):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(dir=str(target.parent), delete=False)
    try:
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        Path(handle.name).replace(target)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------- #
# template-install / review / status / doctor
# --------------------------------------------------------------------------- #


def command_template_install(args):
    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        raise UserError(f"vault root does not exist: {vault}")
    specs = kind_specs()
    schema_path = resolve_schema_path(vault, args.schema)
    schema, _hash = compiled_schema_for(vault, schema_path, cache_dir=vault / STATE_DIR / "cache")
    folder = vault_wiki.template_folder(schema)
    source_dir = skill_root() / "references" / "templates"
    warnings = []
    operations = []
    for kind in vault_wiki.WIKI_KINDS:
        name = vault_wiki.WIKI_TEMPLATE_NAMES[kind]
        shipped = source_dir / name
        if not shipped.is_file():
            raise UserError(f"the skill is missing its own template copy: {shipped}")
        payload = shipped.read_bytes()
        destination = vault / folder / name
        action = "create"
        if destination.is_file():
            current = destination.read_bytes()
            if current == payload:
                operations.append({"kind": kind, "path": (folder / name).as_posix(), "action": "unchanged"})
                continue
            if not args.force:
                warnings.append(
                    f"{(folder / name).as_posix()} differs from the shipped template; "
                    "left alone (pass --force to overwrite)"
                )
                operations.append({"kind": kind, "path": (folder / name).as_posix(), "action": "refused"})
                continue
            action = "overwrite"
        drift = vault_wiki.template_spec_drift(
            split_frontmatter(payload)["body"], specs[kind], str(shipped)
        )
        if drift:
            raise UserError("shipped template disagrees with its spec: " + "; ".join(drift))
        if not args.dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(destination, payload)
        operations.append(
            {"kind": kind, "path": (folder / name).as_posix(), "action": action, "dryRun": args.dry_run}
        )
    return structured(
        "ok",
        warnings=warnings,
        data={
            "templateFolder": folder.as_posix(),
            "dryRun": args.dry_run,
            "written": sum(1 for entry in operations if entry["action"] in ("create", "overwrite")),
            "operations": operations,
        },
    )


def command_review(args):
    run_dir = Path(args.run).expanduser().resolve() if args.run else None
    if run_dir is None:
        raise UserError("review needs --run <run-directory>")
    run_state.load_run_state(run_dir, workflow=WORKFLOW)
    manifest = json.loads((run_dir / "proposals.json").read_text(encoding="utf-8"))
    start = max(0, args.offset)
    window = manifest[start:start + (args.limit or 10)]
    return structured(
        "ok",
        data={
            "runDirectory": str(run_dir),
            "total": len(manifest),
            "offset": start,
            "showing": len(window),
            "proposals": [
                {
                    "id": proposal["id"],
                    "title": proposal["title"],
                    "kind": proposal["kind"],
                    "path": proposal["path"],
                    "verdict": proposal["verdict"],
                    "reason": proposal["reason"],
                    "uncited": proposal.get("uncited", False),
                    "weakSources": proposal.get("weakSources", False),
                    "sources": [
                        f"{record['url']} ({record.get('relevance') or 'about'})" for record in proposal["sources"]
                    ],
                    "bodyAfter": proposal["bodyAfter"],
                }
                for proposal in window
            ],
        },
    )


def command_status(args):
    run_dir = Path(args.run).expanduser().resolve() if args.run else None
    if run_dir is None:
        raise UserError("status needs --run <run-directory>")
    state = run_state.load_run_state(run_dir, workflow=WORKFLOW)
    checks = read_journal(run_dir / "checks.jsonl")
    return structured(
        "ok",
        data={
            "runDirectory": str(run_dir),
            "phase": state.get("phase"),
            "status": state.get("status"),
            "nextAction": state.get("nextAction"),
            "counts": {
                "selected": len(read_journal(run_dir / "plan.jsonl")),
                "sources": len(read_journal(run_dir / "sources.jsonl")),
                "drafts": len(read_journal(run_dir / "drafts.jsonl")),
                "checksPassed": sum(1 for row in checks if row.get("ok")),
                "checksHeld": sum(1 for row in checks if not row.get("ok")),
                "verified": len(read_journal(run_dir / "verified.jsonl")),
                "applied": len(read_journal(run_dir / "apply-log.jsonl")),
                "reverted": len(read_journal(run_dir / "revert-log.jsonl")),
            },
        },
    )


def command_doctor(args):
    vault = Path(args.vault).expanduser().resolve()
    warnings = []
    errors = []
    data = {"vault": str(vault)}
    if not vault.is_dir():
        raise UserError(f"vault root does not exist: {vault}")

    specs = kind_specs()
    data["kinds"] = sorted(specs)
    source_dir = skill_root() / "references" / "templates"
    drift = []
    for kind, spec in specs.items():
        shipped = source_dir / vault_wiki.WIKI_TEMPLATE_NAMES[kind]
        if not shipped.is_file():
            drift.append(f"missing shipped template: {shipped}")
            continue
        drift.extend(vault_wiki.template_spec_drift(split_frontmatter(shipped.read_bytes())["body"], spec, str(shipped)))
    data["shippedTemplatesOk"] = not drift
    errors.extend(error_entry("template_spec_drift", message) for message in drift)

    policy = load_source_policy(args.source_policy)
    data["sourcePolicy"] = {"path": policy["path"], "sources": len(policy["sources"]), "topics": len(policy["topics"])}

    schema_path = resolve_schema_path(vault, args.schema)
    schema, schema_hash = compiled_schema_for(vault, schema_path, cache_dir=vault / STATE_DIR / "cache")
    data["schema"] = {"path": str(schema_path), "sha256": schema_hash, "hasWikiDomain": vault_wiki.WIKI_DOMAIN in schema["domains"]}
    installed = {}
    for kind, spec in specs.items():
        result = vault_wiki.inspect_wiki_template(
            vault, schema, kind, required_fields=spec["required_placeholders"], known_fields=spec["placeholders"]
        )
        problems = list(result["errors"])
        if result["ok"]:
            problems.extend(vault_wiki.template_spec_drift(result["body"], spec, result["path"]))
        installed[kind] = {"path": result["path"], "ok": not problems, "problems": problems}
    data["installedTemplates"] = installed
    if not all(entry["ok"] for entry in installed.values()):
        warnings.append("some wiki templates are missing or stale; run template-install")

    counts = {}
    for kind in vault_wiki.WIKI_KINDS:
        try:
            folder = vault / vault_wiki.wiki_kind_folder(schema, kind)
        except UserError as error:
            warnings.append(str(error))
            continue
        notes = sorted(folder.glob("*.md")) if folder.is_dir() else []
        thin = 0
        for path in notes:
            split = split_frontmatter(path.read_bytes())
            if split["malformed"]:
                continue
            if missing_section_ids(split["body"], specs[kind]):
                thin += 1
        counts[kind] = {"notes": len(notes), "incomplete": thin}
    data["wiki"] = counts

    # Every native resolver depends on outbound HTTPS from this interpreter, which
    # fails on a Python with no CA bundle even where curl succeeds. Probing one
    # source of each method makes that a diagnosis rather than a run full of
    # "no source resolved".
    probes = {}
    for source_id, label in (("wikipedia", "mediawiki"), ("sep", "index"), ("iep", "wordpress")):
        entry = next((item for item in policy["sources"] if item["id"] == source_id), None)
        if entry is None:
            continue
        resolved = resolve_source_url(web_cache_dir(vault), "Madhyamaka", entry)
        probes[label] = resolved["url"] if resolved else None
    data["resolvers"] = probes
    if not any(probes.values()):
        warnings.append("no native source resolver reached its site; expansion will find nothing")
    for failure in http_failures()[:4]:
        warnings.append(f"outbound request failed — {failure}")
        if "CERTIFICATE_VERIFY_FAILED" in failure:
            warnings.append(
                "this Python cannot verify TLS: no CA bundle was found. Set SSL_CERT_FILE to a "
                "bundle (on macOS, /etc/ssl/cert.pem) or install certifi"
            )

    script = web_research_script()
    data["webResearch"] = {"script": str(script) if script else None}
    if script is None:
        warnings.append("web-research script not found; expansion can only run with --no-web")
    elif not args.no_web:
        # A throttled SearXNG answers HTTP 200 with zero results, so it looks
        # healthy while every note in a run comes back "no source resolved". One
        # probe query turns that into a diagnosis instead of a mystery.
        probe = run_web_research(
            "search",
            ["Bruno Latour site:en.wikipedia.org", "--limit", "3"],
            web_cache_dir(vault) / "doctor" / utc_timestamp(),
            timeout=60,
        )
        found = len((probe or {}).get("results") or [])
        data["webResearch"]["probeResults"] = found
        if probe is None:
            warnings.append("the search probe did not complete; expansion will find no sources")
        elif not found:
            warnings.append(
                "the search backend returned HTTP 200 but zero results for a known-good query — "
                "its upstream engines are almost certainly throttled. Expansion would hold every "
                "note back as 'no source resolved'; wait for it to recover before a long run"
            )

    for name, url, model in (("chat", args.base_url, args.model), ("think", args.think_url, args.think_model)):
        try:
            service = forge_llm.resolve_service(name, base_url=url, model=model)
            report = forge_llm.service_doctor(service, expect_non_thinking=name == "chat")
            data[name] = report
            if name == "chat" and report.get("reasoned"):
                warnings.append("the chat endpoint is reasoning; bulk drafting will be far slower than expected")
        except forge_llm.ChatError as error:
            data[name] = {"ok": False, "error": str(error)}
            warnings.append(f"{name} service unreachable: {error}")

    return structured("ok" if not errors else "error", warnings=warnings, errors=errors, data=data)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def unique_run_directory(vault, command):
    base = vault / STATE_DIR / "runs"
    base.mkdir(parents=True, exist_ok=True)
    stamp = utc_timestamp()
    candidate = base / f"{stamp}-{command}"
    suffix = 2
    while candidate.exists():
        candidate = base / f"{stamp}-{command}-{suffix}"
        suffix += 1
    return candidate


def parse_kinds(value):
    if not value:
        return vault_wiki.DEFAULT_WIKI_KINDS
    kinds = []
    for part in str(value).split(","):
        name = part.strip()
        if not name:
            continue
        if name == "all":
            return vault_wiki.WIKI_KINDS
        if name not in vault_wiki.WIKI_KIND_SUBDOMAIN:
            raise UserError(f"unknown wiki kind: {name}")
        if name not in kinds:
            kinds.append(name)
    if not kinds:
        raise UserError("--kind selected no wiki kinds")
    return tuple(kinds)


def parse_titles(values):
    """Each --title is one whole title.

    Never comma-split: this vault names notes `Canonical Name, Gloss`, so
    splitting "Actor-Network Theory, ANT" would look for two notes that do not
    exist and silently skip the one that does.
    """
    return [value.strip() for value in (values or []) if value and value.strip()]


def parse_ids(value):
    if not value:
        return []
    return sorted({part.strip() for part in str(value).split(",") if part.strip()})


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Expand wiki entity notes from canonical sources.")
    parser.add_argument(
        "command",
        choices=["expand", "template-install", "review", "apply", "revert", "status", "doctor"],
    )
    parser.add_argument("--vault", required=True)
    parser.add_argument("--schema")
    parser.add_argument("--kind", help="comma-separated wiki kinds, or 'all' (default: concept,term)")
    parser.add_argument(
        "--title",
        dest="titles",
        action="append",
        help="restrict the run to this exact note title; repeat for more. Not comma-separated — vault titles contain commas.",
    )
    parser.add_argument("--only-empty", action="store_true", help="only notes missing a managed section")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--run")
    parser.add_argument("--accept")
    parser.add_argument("--accept-batch", action="store_true", help="accept every unflagged, cited proposal")
    parser.add_argument("--reject")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="template-install only: overwrite a modified template")
    parser.add_argument("--sources-per-note", type=int, default=DEFAULT_SOURCES_PER_NOTE)
    parser.add_argument("--plan-batch", type=int, default=DEFAULT_PLAN_BATCH)
    parser.add_argument("--source-policy")
    parser.add_argument("--no-web", action="store_true", help="draft without sources; the run cannot be applied")
    parser.add_argument("--no-verify", action="store_true")
    parser.add_argument("--voice")
    parser.add_argument("--no-voice", action="store_true")
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--think-url")
    parser.add_argument("--think-model")
    parser.add_argument("--request-timeout", type=float, default=forge_llm.DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit requires a positive integer")
    if args.sources_per_note < 1:
        parser.error("--sources-per-note requires a positive integer")
    if args.plan_batch < 1:
        parser.error("--plan-batch requires a positive integer")
    return args


COMMANDS = {
    "expand": command_expand,
    "template-install": command_template_install,
    "review": command_review,
    "apply": command_apply,
    "revert": command_revert,
    "status": command_status,
    "doctor": command_doctor,
}


def main(argv=None):
    args = parse_args(argv)
    try:
        print_json(COMMANDS[args.command](args))
    except UserError as error:
        print_json(structured("error", errors=[error_entry("user_error", str(error))]))
        return 2
    except ValueError as error:
        print_json(structured("error", errors=[error_entry("run_state_error", str(error))]))
        return 2
    except forge_llm.ChatError as error:
        print_json(structured("error", errors=[error_entry("chat_error", str(error))]))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

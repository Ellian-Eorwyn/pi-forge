"""The media provider registry.

The shape deliberately mirrors ``web-research/scripts/search-providers.mjs``: a
uniform per-provider interface, a base URL with a per-provider environment
override (which is also what makes a provider testable against a fixture),
declared capabilities, and selection driven by the medium being asked about.

Every claim below was checked against the live services on 2026-08-06 rather
than taken from documentation, because most of the documentation is wrong now:

**Keyless and working.** Open Library (books), MusicBrainz plus Cover Art Archive
(music), TVmaze (shows), Steam (PC games). These are the no-key tier, and the
skill is useful with no credentials at all.

**Needs a key, and there is no way around it.** TMDB answers 401 to an
unauthenticated request and is the only usable source for films — every keyless
alternative below is either gone or not about movies. IGDB answers 401 and wants
a Twitch client-credentials token, which is what reaches console and handheld
games that Steam cannot see.

**Deliberately absent, having been probed rather than assumed:**

- *Google Books* answers HTTP 429 to an anonymous request and the body names the
  reason: ``quota_limit_value: "0"``. The anonymous per-project daily quota is
  literally zero, so this is not a rate limit that clears — it is a closed door.
  This matters beyond this file: the Obsidian Book Search plugin queries Google
  Books, so its default configuration cannot work. Open Library is the book
  provider on both paths.
- *RAWG* answered HTTP 522 on three attempts spread over several minutes. A
  Cloudflare origin failure is the host being down, not us being throttled, and a
  provider that is intermittently absent is worse than one honestly missing.
- *BoardGameGeek's XML API* now answers 401. It was open for years, which is
  exactly why it needs recording: an implementation written from memory would
  reach for it first. Board games therefore have no free API and are entered by
  hand.

Re-verify before trusting any of this past ~2027; this space moved twice in the
eighteen months before it was written.
"""

import json
import os
import re
import time
from urllib.parse import quote, urlencode

from media_http import HostLimiter, MediaHTTPError, fetch_json

MEDIA_KINDS = ("book", "music", "game", "movie", "show")

# Per-provider base override, the layer that lets the suite point a provider at a
# fixture instead of the internet.
PROVIDER_ENV = {}


def env_var_for(provider_id):
    return PROVIDER_ENV.get(provider_id) or f"FORGE_MEDIA_{re.sub(r'[^A-Z0-9]+', '_', provider_id.upper())}_URL"


def provider_base(provider_id, flags=None, env=None):
    """Explicit flag, then environment, then the registry default."""
    env = env if env is not None else os.environ
    flags = flags or {}
    explicit = flags.get(f"{provider_id}_base")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip().rstrip("/")
    from_env = env.get(env_var_for(provider_id))
    if isinstance(from_env, str) and from_env.strip():
        return from_env.strip().rstrip("/")
    return (MEDIA_PROVIDERS.get(provider_id) or {}).get("base")


NO_AUTH = {"auth_required": False, "optional_auth": False}


def candidate(provider, external_id, title, *, year=None, creators=None, cover=None, url=None, detail=None):
    """One normalized search hit.

    ``detail`` is the provider's own fields, kept whole and unflattened, because
    the note's Details table is rendered from them and a field dropped here is a
    field the note can never carry.
    """
    return {
        "provider": provider,
        "externalId": str(external_id),
        "title": title,
        "year": year,
        "creators": list(creators or []),
        "cover": cover,
        "url": url,
        "detail": detail or {},
    }


def _year(value):
    if not value:
        return None
    match = re.search(r"(1[6-9]\d{2}|20\d{2}|21\d{2})", str(value))
    return int(match.group(1)) if match else None


def _normalize(text):
    return re.sub(r"[^a-z0-9 ]+", " ", str(text or "").casefold()).strip()


def _words(text):
    return [w for w in _normalize(text).split() if w]


def match_score(item, query, year_hint=None):
    """How well a candidate answers the query, decided without a model.

    Providers rank by their own relevance and it is routinely wrong for this
    purpose. Searching MusicBrainz for "Radiohead In Rainbows" puts *Vitamin
    String Quartet performs Radiohead's In Rainbows* first, and Steam answers
    "Hades" with *Hades II*. Both are defensible full-text matches and both are
    the wrong record, so the ordering is recomputed here.

    Two signals do nearly all the work: whether the query *contains* the
    candidate's title (a query naming artist and album contains the album, but
    not a longer title that merely mentions it), and whether the candidate's
    creator is named in the query. Deterministic, and it runs before anything is
    shown or sent to a model.

    The title bonus scales with how much of the query the title accounts for,
    while the creator bonus is flat, because the title is the more identifying
    half and a tie has to break toward it. MusicBrainz really does list an artist
    called "In Rainbows" — reggaeton remixes of Radiohead — with an album titled
    "Radiohead", so "Radiohead In Rainbows" matches two records with the artist
    and title exactly swapped. Weighting the title is what separates them.
    """
    query_words = _words(query)
    query_norm = " ".join(query_words)
    title_norm = _normalize(item.get("title"))
    title_words = _words(title_norm)
    score = 0.0

    if title_norm and title_norm == query_norm:
        score += 100
    elif title_words and _contains_phrase(query_words, title_words):
        # The query says everything the title says, plus context like the artist.
        # A longer title that merely embeds the query does not qualify, which is
        # what separates "In Rainbows" from "…performs Radiohead's In Rainbows".
        score += 45 + 15 * (len(title_words) / max(1, len(query_words)))
    elif title_norm and query_norm and (title_norm.startswith(query_norm) or query_norm.startswith(title_norm)):
        score += 25

    # Extra words in the title that the query never asked for are the sequel and
    # cover-version problem: "Hades II" against "Hades".
    surplus = max(0, len(title_words) - len(query_words))
    score -= min(20, 8 * surplus)

    for creator in item.get("creators") or []:
        creator_words = _words(creator)
        if creator_words and _contains_phrase(query_words, creator_words):
            score += 35
            break

    if year_hint and item.get("year"):
        distance = abs(int(item["year"]) - int(year_hint))
        score += 20 if distance == 0 else (8 if distance == 1 else -10)

    # The provider's own relevance breaks an exact tie and nothing more. Given
    # any weight it flips real differences: MusicBrainz scores the swapped
    # "Radiohead" record 75 against the real album's 66.
    score += float(item.get("detail", {}).get("providerScore") or 0) / 1000.0
    return score


def _contains_phrase(haystack_words, needle_words):
    n = len(needle_words)
    return n > 0 and any(haystack_words[i : i + n] == needle_words for i in range(len(haystack_words) - n + 1))


def rank_candidates(items, query, year_hint=None):
    """Best match first, stable within equal scores."""
    scored = [(match_score(item, query, year_hint), index, item) for index, item in enumerate(items)]
    scored.sort(key=lambda row: (-row[0], row[1]))
    ranked = []
    for score, _index, item in scored:
        item = dict(item)
        item["matchScore"] = round(score, 2)
        ranked.append(item)
    return ranked


# --------------------------------------------------------------------------
# Books - Open Library
# --------------------------------------------------------------------------

OPEN_LIBRARY_FIELDS = (
    "key,title,subtitle,author_name,first_publish_year,publisher,"
    "number_of_pages_median,isbn,cover_i,subject,language,ia"
)


def _open_library_search(query, context):
    url = "{base}/search.json?{qs}".format(
        base=context["base"],
        qs=urlencode({"q": query, "limit": context.get("limit", 5), "fields": OPEN_LIBRARY_FIELDS}),
    )
    payload, _meta = fetch_json(url, limiter=context["limiter"])
    results = []
    for doc in payload.get("docs") or []:
        work_key = doc.get("key") or ""
        cover_id = doc.get("cover_i")
        # Subjects arrive with machine facets mixed into human ones
        # ("form:novel", "nyt:combined-print-and-e-book-fiction=2020-10-04").
        # Keeping the faceted ones would put database bookkeeping in a note.
        subjects = [s for s in (doc.get("subject") or []) if ":" not in s and "=" not in s][:6]
        isbns = doc.get("isbn") or []
        results.append(
            candidate(
                "openlibrary",
                work_key.rsplit("/", 1)[-1],
                doc.get("title") or "",
                year=doc.get("first_publish_year"),
                creators=doc.get("author_name") or [],
                cover=f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg" if cover_id else None,
                url=f"https://openlibrary.org{work_key}" if work_key else None,
                detail={
                    "subtitle": doc.get("subtitle"),
                    "authors": doc.get("author_name") or [],
                    "firstPublished": doc.get("first_publish_year"),
                    "publisher": (doc.get("publisher") or [None])[0],
                    "pages": doc.get("number_of_pages_median"),
                    # 13-digit first: it is the current standard and the one a
                    # reader is most likely to be able to look up.
                    "isbn": next((i for i in isbns if len(i) == 13), isbns[0] if isbns else None),
                    "subjects": subjects,
                    "openLibraryKey": work_key,
                },
            )
        )
    return results


# --------------------------------------------------------------------------
# Music - MusicBrainz, covers from the Cover Art Archive
# --------------------------------------------------------------------------


def _musicbrainz_search(query, context):
    url = "{base}/ws/2/release-group/?{qs}".format(
        base=context["base"],
        qs=urlencode({"query": query, "fmt": "json", "limit": context.get("limit", 5)}),
    )
    payload, _meta = fetch_json(url, limiter=context["limiter"])
    results = []
    for group in payload.get("release-groups") or []:
        mbid = group.get("id")
        artists = [c.get("name") for c in (group.get("artist-credit") or []) if isinstance(c, dict) and c.get("name")]
        results.append(
            candidate(
                "musicbrainz",
                mbid,
                group.get("title") or "",
                year=_year(group.get("first-release-date")),
                creators=artists,
                # The Cover Art Archive redirects to the image; a release group
                # with no art 404s, which the note writer treats as "no cover"
                # rather than as a failure.
                cover=f"https://coverartarchive.org/release-group/{mbid}/front-500" if mbid else None,
                url=f"https://musicbrainz.org/release-group/{mbid}" if mbid else None,
                detail={
                    "artists": artists,
                    "released": group.get("first-release-date"),
                    "releaseType": group.get("primary-type"),
                    "secondaryTypes": group.get("secondary-types") or [],
                    "mbid": mbid,
                    "providerScore": group.get("score"),
                },
            )
        )
    return results


# --------------------------------------------------------------------------
# Shows - TVmaze
# --------------------------------------------------------------------------


def _tvmaze_search(query, context):
    url = f"{context['base']}/search/shows?{urlencode({'q': query})}"
    payload, _meta = fetch_json(url, limiter=context["limiter"])
    results = []
    for entry in (payload or [])[: context.get("limit", 5)]:
        show = entry.get("show") or {}
        network = (show.get("network") or show.get("webChannel") or {}).get("name")
        image = show.get("image") or {}
        results.append(
            candidate(
                "tvmaze",
                show.get("id"),
                show.get("name") or "",
                year=_year(show.get("premiered")),
                creators=[network] if network else [],
                cover=image.get("original") or image.get("medium"),
                url=show.get("url"),
                detail={
                    "premiered": show.get("premiered"),
                    "ended": show.get("ended"),
                    "status": show.get("status"),
                    "network": network,
                    "genres": show.get("genres") or [],
                    "runtime": show.get("averageRuntime") or show.get("runtime"),
                    "language": show.get("language"),
                    "imdb": (show.get("externals") or {}).get("imdb"),
                    "summary": _strip_tags(show.get("summary") or ""),
                },
            )
        )
    return results


def _strip_tags(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html)).strip()


# --------------------------------------------------------------------------
# Games - Steam (keyless, PC only)
# --------------------------------------------------------------------------


def _steam_search(query, context):
    """Two calls: the community app search, then the store's detail endpoint.

    The search endpoint returns only an appid and a name, which is not enough for
    a note, so each candidate is enriched. The enrichment is bounded by ``limit``
    on purpose -- the store endpoint is the slow one and a five-result search
    should not cost twenty round trips.
    """
    url = f"{context['base']}/actions/SearchApps/{quote(query)}"
    payload, _meta = fetch_json(url, limiter=context["limiter"])
    results = []
    for app in (payload or [])[: context.get("limit", 5)]:
        appid = app.get("appid")
        detail = _steam_detail(appid, context) if appid else {}
        released = (detail.get("release_date") or {}).get("date")
        developers = detail.get("developers") or []
        results.append(
            candidate(
                "steam",
                appid,
                detail.get("name") or app.get("name") or "",
                year=_year(released),
                creators=developers,
                cover=detail.get("header_image") or app.get("logo"),
                url=f"https://store.steampowered.com/app/{appid}" if appid else None,
                detail={
                    "released": released,
                    "developers": developers,
                    "publishers": detail.get("publishers") or [],
                    "genres": [g.get("description") for g in (detail.get("genres") or []) if g.get("description")],
                    "metacritic": (detail.get("metacritic") or {}).get("score"),
                    "platforms": sorted(k for k, v in (detail.get("platforms") or {}).items() if v),
                    "appid": appid,
                },
            )
        )
    return results


def _steam_detail(appid, context):
    store = context.get("steam_store_base") or "https://store.steampowered.com"
    try:
        payload, _meta = fetch_json(f"{store}/api/appdetails?{urlencode({'appids': appid})}", limiter=context["limiter"])
    except MediaHTTPError:
        # A missing detail record degrades the candidate; it does not fail the
        # search. The appid and name are still enough to identify the game.
        return {}
    entry = (payload or {}).get(str(appid)) or {}
    return entry.get("data") or {} if entry.get("success") else {}


# --------------------------------------------------------------------------
# Movies and shows - TMDB (key required)
# --------------------------------------------------------------------------

TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"


def _tmdb_search(query, context, kind="movie"):
    key = context.get("api_key")
    if not key:
        raise MediaHTTPError("no_key", "TMDB needs an API key; none is configured")
    # TMDB accepts both a v3 key as a query parameter and a v4 read token as a
    # bearer header. Which one the user pasted is not knowable from the value
    # alone, so the shorter v3 form is detected by length and the rest is sent
    # as a bearer token.
    headers = {}
    params = {"query": query, "include_adult": "false"}
    if len(key) > 40:
        headers["Authorization"] = f"Bearer {key}"
    else:
        params["api_key"] = key
    url = f"{context['base']}/3/search/{kind}?{urlencode(params)}"
    payload, _meta = fetch_json(url, limiter=context["limiter"], headers=headers)
    results = []
    for item in (payload.get("results") or [])[: context.get("limit", 5)]:
        is_movie = kind == "movie"
        title = item.get("title") if is_movie else item.get("name")
        released = item.get("release_date") if is_movie else item.get("first_air_date")
        poster = item.get("poster_path")
        results.append(
            candidate(
                "tmdb",
                item.get("id"),
                title or "",
                year=_year(released),
                creators=[],
                cover=f"{TMDB_IMAGE_BASE}{poster}" if poster else None,
                url=f"https://www.themoviedb.org/{kind}/{item.get('id')}",
                detail={
                    "released": released,
                    "overview": item.get("overview"),
                    "originalTitle": item.get("original_title") if is_movie else item.get("original_name"),
                    "originalLanguage": item.get("original_language"),
                    "tmdbId": item.get("id"),
                    "voteAverage": item.get("vote_average"),
                },
            )
        )
    return results


# --------------------------------------------------------------------------
# Games - IGDB (Twitch client-credentials token)
# --------------------------------------------------------------------------

IGDB_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
_IGDB_TOKEN_CACHE = {}


def igdb_token(client_id, client_secret, limiter=None):
    """A client-credentials token, cached for the run.

    IGDB tokens last about two months, but caching in-process rather than on disk
    keeps a credential out of the vault and off the filesystem. Re-minting once
    per run is cheap.
    """
    cached = _IGDB_TOKEN_CACHE.get(client_id)
    if cached and cached["expires_at"] > time.time() + 60:
        return cached["token"]
    body = urlencode(
        {"client_id": client_id, "client_secret": client_secret, "grant_type": "client_credentials"}
    ).encode()
    from media_http import fetch_text

    raw, _meta = fetch_text(IGDB_TOKEN_URL, limiter=limiter, method="POST", data=body,
                            headers={"Content-Type": "application/x-www-form-urlencoded"})
    payload = json.loads(raw)
    token = payload.get("access_token")
    if not token:
        raise MediaHTTPError("no_key", "Twitch returned no access token for the IGDB credentials")
    _IGDB_TOKEN_CACHE[client_id] = {"token": token, "expires_at": time.time() + float(payload.get("expires_in") or 0)}
    return token


def _igdb_search(query, context):
    key = context.get("api_key")
    if not key or ":" not in key:
        raise MediaHTTPError("no_key", "IGDB needs a `client_id:client_secret` pair; none is configured")
    client_id, client_secret = key.split(":", 1)
    token = igdb_token(client_id, client_secret, limiter=context["limiter"])
    from media_http import fetch_text

    # IGDB's query language is a POST body, not query parameters.
    limit = context.get("limit", 5)
    body = (
        f'search "{query}"; '
        "fields name,first_release_date,summary,cover.image_id,genres.name,"
        "involved_companies.company.name,involved_companies.developer,platforms.name,"
        f"aggregated_rating,url; limit {limit};"
    ).encode()
    raw, _meta = fetch_text(
        f"{context['base']}/v4/games",
        limiter=context["limiter"],
        method="POST",
        data=body,
        headers={"Client-ID": client_id, "Authorization": f"Bearer {token}", "Content-Type": "text/plain"},
    )
    results = []
    for game in json.loads(raw) or []:
        released = game.get("first_release_date")
        released_iso = time.strftime("%Y-%m-%d", time.gmtime(released)) if released else None
        developers = [
            c["company"]["name"]
            for c in (game.get("involved_companies") or [])
            if c.get("developer") and isinstance(c.get("company"), dict) and c["company"].get("name")
        ]
        image_id = (game.get("cover") or {}).get("image_id")
        results.append(
            candidate(
                "igdb",
                game.get("id"),
                game.get("name") or "",
                year=_year(released_iso),
                creators=developers,
                cover=f"https://images.igdb.com/igdb/image/upload/t_cover_big/{image_id}.jpg" if image_id else None,
                url=game.get("url"),
                detail={
                    "released": released_iso,
                    "developers": developers,
                    "genres": [g.get("name") for g in (game.get("genres") or []) if g.get("name")],
                    "platforms": [p.get("name") for p in (game.get("platforms") or []) if p.get("name")],
                    "rating": round(game["aggregated_rating"]) if game.get("aggregated_rating") else None,
                    "summary": game.get("summary"),
                    "igdbId": game.get("id"),
                },
            )
        )
    return results


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------

MEDIA_PROVIDERS = {
    "openlibrary": {
        "id": "openlibrary",
        "label": "Open Library",
        "base": "https://openlibrary.org",
        "media": ["book"],
        "authority": 1,
        "spacing_ms": 200,
        "capabilities": lambda: {
            **NO_AUTH,
            "rateLimit": "no published limit; probed 2026-08-06 with no key and no throttling",
            "fields": ["title", "authors", "firstPublished", "publisher", "pages", "isbn", "subjects", "cover"],
            "strengths": ["keyless", "work-level records", "covers by id", "editions across languages"],
            "limits": ["subject lists mix machine facets with human ones", "sparse on very recent titles"],
        },
        "search": _open_library_search,
    },
    "musicbrainz": {
        "id": "musicbrainz",
        "label": "MusicBrainz",
        "base": "https://musicbrainz.org",
        "media": ["music"],
        "authority": 1,
        # One request a second is the published limit and the service reports
        # remaining budget on every response, which the limiter records.
        "spacing_ms": 1100,
        "capabilities": lambda: {
            **NO_AUTH,
            "rateLimit": "1 req/s; publishes x-ratelimit-remaining, which is read rather than assumed",
            "fields": ["title", "artists", "released", "releaseType", "mbid", "cover"],
            "strengths": ["keyless", "authoritative release metadata", "covers via Cover Art Archive"],
            "limits": ["refuses a generic User-Agent", "release groups, not individual pressings"],
        },
        "search": _musicbrainz_search,
    },
    "tvmaze": {
        "id": "tvmaze",
        "label": "TVmaze",
        "base": "https://api.tvmaze.com",
        "media": ["show"],
        "authority": 1,
        "spacing_ms": 200,
        "capabilities": lambda: {
            **NO_AUTH,
            "rateLimit": "20 calls per 10 seconds, unauthenticated",
            "fields": ["name", "premiered", "ended", "status", "network", "genres", "runtime", "imdb", "cover"],
            "strengths": ["keyless", "running/ended status", "network and streaming channel", "imdb ids"],
            "limits": ["television only", "thin on non-English productions"],
        },
        "search": _tvmaze_search,
    },
    "steam": {
        "id": "steam",
        "label": "Steam",
        "base": "https://steamcommunity.com",
        "media": ["game"],
        "authority": 2,
        "spacing_ms": 300,
        "capabilities": lambda: {
            **NO_AUTH,
            "rateLimit": "no published limit; store detail endpoint is the slow half",
            "fields": ["name", "released", "developers", "publishers", "genres", "metacritic", "platforms", "cover"],
            "strengths": ["keyless", "Metacritic scores", "accurate release dates", "store art"],
            "limits": ["PC titles only", "no console, handheld, board or card games"],
        },
        "search": _steam_search,
    },
    "tmdb": {
        "id": "tmdb",
        "label": "TMDB",
        "base": "https://api.themoviedb.org",
        "media": ["movie", "show"],
        "authority": 1,
        "spacing_ms": 100,
        "capabilities": lambda: {
            "auth_required": True,
            "optional_auth": False,
            "rateLimit": "~40 req/s per IP; the key is not what is limited",
            "fields": ["title", "released", "overview", "originalTitle", "voteAverage", "poster"],
            "strengths": ["the only usable film source", "posters", "covers television too"],
            "limits": ["needs a free key", "attribution required by its terms"],
        },
        "search": lambda query, context: _tmdb_search(query, context, context.get("kind") or "movie"),
    },
    "igdb": {
        "id": "igdb",
        "label": "IGDB",
        "base": "https://api.igdb.com",
        "media": ["game"],
        "authority": 1,
        "spacing_ms": 300,
        "capabilities": lambda: {
            "auth_required": True,
            "optional_auth": False,
            "rateLimit": "4 req/s",
            "fields": ["name", "released", "developers", "genres", "platforms", "rating", "cover"],
            "strengths": ["console and handheld as well as PC", "platform lists", "cover art"],
            "limits": ["needs a Twitch client id and secret as `id:secret`", "token exchange before every run"],
        },
        "search": _igdb_search,
    },
}


def providers_for(medium, api_keys=None, flags=None, env=None):
    """The providers that serve ``medium``, best first, with the unusable ones named.

    Returns ``(usable, skipped)``. A provider needing a key it does not have is
    *skipped with a reason* rather than silently dropped, because "no results for
    Arrival" and "TMDB is not configured" are different answers and only one of
    them is the user's problem to fix.
    """
    api_keys = api_keys or {}
    usable, skipped = [], []
    for provider in sorted(MEDIA_PROVIDERS.values(), key=lambda p: (p["authority"], p["id"])):
        if medium not in provider["media"]:
            continue
        capabilities = provider["capabilities"]()
        if capabilities.get("auth_required") and not api_keys.get(provider["id"]):
            skipped.append(
                {
                    "provider": provider["id"],
                    "reason": "no_key",
                    "detail": f"{provider['label']} needs a key; set connectedServices.apiKeys.{provider['id']}",
                }
            )
            continue
        if not provider_base(provider["id"], flags, env):
            skipped.append({"provider": provider["id"], "reason": "no_base", "detail": "no base URL configured"})
            continue
        usable.append(provider)
    return usable, skipped


def search(medium, query, *, api_keys=None, limit=5, flags=None, env=None, limiter=None, kind=None, year_hint=None):
    """Ask every usable provider for ``medium`` until one answers with results.

    Providers are tried in authority order and the first non-empty answer wins.
    A provider that raises is recorded and the next one is tried: a film search
    should not fail because Steam is down.

    Results are re-ranked by ``match_score`` before they are returned, so what a
    caller sees first is the best match rather than whatever the provider's
    full-text index happened to favour.
    """
    api_keys = api_keys or {}
    limiter = limiter or HostLimiter()
    usable, skipped = providers_for(medium, api_keys, flags, env)
    attempts = []
    for provider in usable:
        context = {
            "base": provider_base(provider["id"], flags, env),
            "limiter": HostLimiter(spacing_ms=provider.get("spacing_ms", 0)) if limiter is None else limiter,
            "limit": limit,
            "api_key": api_keys.get(provider["id"]),
            "kind": kind or ("show" if medium == "show" else "movie"),
        }
        if provider["id"] == "tmdb":
            context["kind"] = "tv" if medium == "show" else "movie"
        try:
            results = provider["search"](query, context)
        except MediaHTTPError as exc:
            attempts.append({"provider": provider["id"], "ok": False, "error": exc.code, "detail": str(exc)[:200]})
            continue
        attempts.append({"provider": provider["id"], "ok": True, "results": len(results)})
        if results:
            ranked = rank_candidates(results, query, year_hint)
            return {"medium": medium, "query": query, "results": ranked, "attempts": attempts, "skipped": skipped}
    return {"medium": medium, "query": query, "results": [], "attempts": attempts, "skipped": skipped}

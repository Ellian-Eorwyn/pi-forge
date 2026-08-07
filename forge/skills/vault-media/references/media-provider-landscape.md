# The media provider landscape

Every claim here was checked against the live service on **2026-08-06** by
issuing the request and reading the response, not taken from documentation.
Most of the documentation in this space is wrong, and two of the providers a
reasonable implementer would reach for first are closed.

Re-verify before trusting any of this past ~2027.

## Working, keyless

| Provider | Medium | Endpoint | Observed |
| --- | --- | --- | --- |
| Open Library | books | `openlibrary.org/search.json?q=&fields=` | HTTP 200, no key, no throttling seen |
| MusicBrainz | music | `musicbrainz.org/ws/2/release-group/?query=&fmt=json` | HTTP 200; `x-ratelimit-limit: 1200`, `x-ratelimit-remaining: 190` |
| Cover Art Archive | music covers | `coverartarchive.org/release-group/<mbid>/front-500` | HTTP 200, `image/jpeg`, 117 KB |
| Open Library covers | book covers | `covers.openlibrary.org/b/id/<id>-L.jpg` | HTTP 200, `image/jpeg`, 49 KB |
| TVmaze | shows | `api.tvmaze.com/search/shows?q=` | HTTP 200; genres, network, premiered, IMDb id, poster |
| Steam | PC games | `steamcommunity.com/actions/SearchApps/<q>` then `store.steampowered.com/api/appdetails?appids=` | HTTP 200 both; Metacritic, genres, developer, header image |

MusicBrainz **refuses a generic User-Agent**. The contact address in it is what
lets an operator complain to a human instead of blocking the host.

MusicBrainz is also the only provider here that publishes its own remaining
budget. Read `x-ratelimit-remaining` rather than assuming the documented number —
recording what the service said is the difference between backing off because we
are near the limit and backing off because a doc said so.

## Working, key required

| Provider | Medium | Auth | Observed |
| --- | --- | --- | --- |
| TMDB | movies, shows | v3 key as `api_key=`, or v4 read token as `Authorization: Bearer` | HTTP 401 unauthenticated: `{"status_code":7,"status_message":"Invalid API key…"}` |
| IGDB | games | Twitch client-credentials; `Client-ID` + `Authorization: Bearer` | HTTP 401 unauthenticated, with a body that names all three common mistakes |

TMDB is free forever for non-commercial use, requires no card, and rate-limits by
IP rather than by key. It is **the only usable film source** — every keyless
alternative below is either closed or not about movies. Its terms require
attribution.

IGDB's key is a `client_id:client_secret` pair, exchanged for a token at
`id.twitch.tv/oauth2/token`. The token is cached in-process rather than on disk,
which keeps a credential out of the vault; re-minting once per run is cheap.

## Closed or dead — do not reach for these

- **Google Books.** HTTP 429 to an anonymous request, and the body says why:
  `"quota_limit_value": "0"`, `quota_limit: defaultPerDayPerProject`. The
  anonymous per-project daily quota is **zero**. This is not a rate limit that
  clears overnight; it is a closed door. Verified twice, with and without a
  custom User-Agent.

  This has a consequence outside this skill: the Obsidian **Book Search plugin
  queries Google Books**, so its default configuration cannot work. Open Library
  is the book provider on both the plugin path and this one.

- **RAWG.** HTTP 522 on three attempts spread over several minutes. A Cloudflare
  522 is an origin failure — the host is down, not throttling us. A provider that
  is intermittently absent is worse than one honestly missing.

- **BoardGameGeek XML API.** HTTP 401 with `Unauthorized. See
  https://boardgamegeek.com/using_the_xml_api`. This was open for years, which is
  exactly why it is worth recording: an implementation written from memory would
  reach for it first. **Board games have no free API** and are entered by hand.

- **Wikidata SPARQL** for games. Answers HTTP 200 but an exact-label query for a
  well-known title returned zero bindings; the `P31/P279*` class chain for video
  games does not reliably reach individual titles. Not a substitute for Steam or
  IGDB.

## Ranking: why provider order is not trusted

Providers rank for full-text relevance, which is routinely the wrong record for
this purpose. Two real cases, both from live responses:

- Steam answers **"Hades"** with *Hades II* first.
- MusicBrainz answers **"Radiohead In Rainbows"** with *Vitamin String Quartet
  performs Radiohead's In Rainbows* first (score 100), and — more awkwardly — its
  second hit is an album literally titled **"Radiohead"** by an artist literally
  called **"In Rainbows"** (reggaeton remixes of Radiohead, score 75). The real
  album scores 66, last of the three.

`media_providers.match_score` recomputes the order deterministically, before
anything is displayed or sent to a model. The title bonus scales with how much of
the query the title accounts for while the creator bonus is flat, which is what
resolves the swapped case toward the title; the provider's own score breaks exact
ties and nothing more, because given any real weight it flips that case back.

## TLS

A python.org framework build on macOS points `ssl.get_default_verify_paths()` at
a bundle that only exists once someone has run its
`Install Certificates.command`. On a machine where nobody has, **every** provider
fails with `CERTIFICATE_VERIFY_FAILED`, which reads exactly like four hosts being
down — they all answer fine to `curl`, which uses the system store.

`media_http.ssl_context()` therefore locates the trust store instead of assuming
one: `SSL_CERT_FILE`, then the default paths if they resolve to something that
exists, then certifi's bundle. Verification is never disabled; an unverified
fetch would let a proxy or a captive portal write note content. `doctor` reports
which store is in play.

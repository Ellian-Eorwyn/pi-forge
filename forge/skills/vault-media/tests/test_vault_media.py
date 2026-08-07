"""Tests for vault-media. No network: providers are exercised through fixtures.

The two that matter most are the fabrication gates. A rating the owner did not
give must not appear as a key, and a `thoughts` value the model paraphrased
rather than quoted must be dropped. Everything else in this skill is recoverable
by re-running it; those two write a false statement into a personal record under
the owner's name, and nobody would catch it a year later.
"""

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))

import media_http  # noqa: E402
import media_notes  # noqa: E402
import media_providers as mp  # noqa: E402

PROPERTY_ORDER = [
    "type", "status", "domain", "subdomain", "project", "parent", "people",
    "organization", "related", "source_kind", "capture_type", "date", "rating",
    "cover", "cssclasses",
]


def item(title, creators=(), year=None, provider="test", detail=None, external_id="1"):
    return mp.candidate(provider, external_id, title, year=year, creators=list(creators), detail=detail or {})


# ---------------------------------------------------------------------------
# Fabrication gates
# ---------------------------------------------------------------------------


def test_no_rating_means_no_rating_key():
    """The gate. A work the owner has not rated carries no `rating` at all.

    Not null, not zero, not the provider's average promoted into the owner's
    voice. Regression test on purpose: every provider hands back a score, and
    the tempting bug is to let one through.
    """
    _name, text = media_notes.build_note(
        medium="game", item=item("Hades", detail={"metacritic": 93}),
        property_order=PROPERTY_ORDER, lead="Hades is a 2020 game.",
    )
    frontmatter = text.split("---")[1]
    assert "rating" not in frontmatter
    # The critic score is still recorded, under a name that cannot be mistaken
    # for a personal verdict.
    assert "| Metacritic | 93 |" in text


def test_rating_is_written_when_given():
    _name, text = media_notes.build_note(
        medium="book", item=item("Piranesi"), property_order=PROPERTY_ORDER, lead=None, rating=9,
    )
    assert "rating: 9" in text.split("---")[1]


@pytest.mark.parametrize("bad", [0, 11, -3, 100])
def test_rating_out_of_range_refuses(bad):
    with pytest.raises(ValueError):
        media_notes.build_note(medium="book", item=item("X"), property_order=PROPERTY_ORDER, lead=None, rating=bad)


def test_thoughts_are_copied_not_rewritten():
    words = "The house is the whole point. I resisted the plot arriving."
    _name, text = media_notes.build_note(
        medium="book", item=item("Piranesi"), property_order=PROPERTY_ORDER, lead=None, thoughts=words,
    )
    assert words in text


def test_empty_thoughts_leaves_the_section_empty():
    _name, text = media_notes.build_note(
        medium="book", item=item("Piranesi"), property_order=PROPERTY_ORDER, lead=None,
    )
    body = text.split("## Thoughts", 1)[1].split("##", 1)[0]
    assert body.strip() == ""


# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def cli():
    """The CLI module, loaded by path because its filename is not an identifier."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("vault_media_cli", SCRIPTS / "vault-media.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ungrounded_names_and_years_are_caught(cli):
    record = {"title": "Arrival", "year": 2016, "creators": [], "detail": {}}
    caught = cli.ungrounded_terms("Arrival is a 2016 film directed by Denis Villeneuve, adapted from Ted Chiang.", record)
    assert "Villeneuve" in caught and "Denis" in caught and "Chiang" in caught
    assert "2016" not in caught  # the year is in the record, so it is grounded


def test_grounded_lead_passes_clean(cli):
    record = {"title": "Hades", "year": 2020, "creators": ["Supergiant Games"], "detail": {}}
    assert cli.ungrounded_terms("Hades is a 2020 game developed by Supergiant Games.", record) == set()


def test_sentence_initial_capital_is_not_a_proper_noun(cli):
    record = {"title": "Piranesi", "year": 2020, "creators": ["Susanna Clarke"], "detail": {}}
    assert cli.ungrounded_terms("Piranesi is a 2020 book by Susanna Clarke. Published widely.", record) == set()


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def test_exact_title_beats_its_own_sequel():
    """Steam answers "Hades" with Hades II first. The re-rank is what fixes it."""
    ranked = mp.rank_candidates(
        [item("Hades II", ["Supergiant Games"], 2025), item("Hades", ["Supergiant Games"], 2020)],
        "Hades",
    )
    assert ranked[0]["title"] == "Hades"


def test_swapped_artist_and_title_resolves_toward_the_title():
    """MusicBrainz lists an artist called "In Rainbows" with an album "Radiohead".

    Searching "Radiohead In Rainbows" matches both records with artist and title
    exactly reversed, and MusicBrainz scores the wrong one higher. Weighting the
    title match by how much of the query it accounts for is what separates them.
    """
    real = item("In Rainbows", ["Radiohead"], 2007, detail={"providerScore": 66})
    swapped = item("Radiohead", ["In Rainbows"], 2007, detail={"providerScore": 75})
    for query in ("Radiohead In Rainbows", "In Rainbows Radiohead"):
        ranked = mp.rank_candidates([swapped, real], query)
        assert ranked[0]["title"] == "In Rainbows", query


def test_cover_version_loses_to_the_original():
    original = item("In Rainbows", ["Radiohead"], 2007)
    cover = item("Vitamin String Quartet performs Radiohead's In Rainbows", ["Vitamin String Quartet"], 2009)
    ranked = mp.rank_candidates([cover, original], "Radiohead In Rainbows")
    assert ranked[0]["title"] == "In Rainbows"


def test_year_hint_separates_remakes():
    ranked = mp.rank_candidates(
        [item("Dune", ["Denis Villeneuve"], 2021), item("Dune", ["David Lynch"], 1984)],
        "Dune", year_hint=1984,
    )
    assert ranked[0]["year"] == 1984


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------


def test_keyed_provider_is_skipped_by_name_not_silently():
    usable, skipped = mp.providers_for("movie", api_keys={})
    assert usable == []
    assert [s["provider"] for s in skipped] == ["tmdb"]
    assert skipped[0]["reason"] == "no_key"
    assert "apiKeys.tmdb" in skipped[0]["detail"]


def test_keyed_provider_becomes_usable_with_a_key():
    usable, skipped = mp.providers_for("movie", api_keys={"tmdb": "x" * 12})
    assert [p["id"] for p in usable] == ["tmdb"]
    assert skipped == []


def test_games_fall_back_to_steam_without_igdb():
    usable, skipped = mp.providers_for("game", api_keys={})
    assert [p["id"] for p in usable] == ["steam"]
    assert [s["provider"] for s in skipped] == ["igdb"]


def test_every_medium_has_at_least_one_keyless_provider_except_movies():
    for medium in mp.MEDIA_KINDS:
        usable, _ = mp.providers_for(medium, api_keys={})
        if medium == "movie":
            assert usable == [], "movies have no keyless source; if this fails, one appeared"
        else:
            assert usable, f"{medium} lost its keyless provider"


# ---------------------------------------------------------------------------
# HTTP rules
# ---------------------------------------------------------------------------


def test_429_does_not_trip_the_breaker():
    """"Slow down" is not "go away".

    MusicBrainz answers 429 to anyone over one request a second. Counting that
    as a refusal would remove the only free music provider three albums into a
    run.
    """
    limiter = media_http.HostLimiter(breaker_threshold=2, sleep=lambda _s: None)
    for _ in range(5):
        limiter.defer("musicbrainz.org", 0)
    assert not limiter.tripped("musicbrainz.org")


def test_repeated_refusals_do_trip_the_breaker():
    limiter = media_http.HostLimiter(breaker_threshold=3, sleep=lambda _s: None)
    assert not limiter.refused("api.themoviedb.org")
    assert not limiter.refused("api.themoviedb.org")
    assert limiter.refused("api.themoviedb.org")
    assert limiter.tripped("api.themoviedb.org")


def test_rate_limit_budget_is_recorded_from_headers_not_assumed():
    limiter = media_http.HostLimiter()
    limiter.record_budget("musicbrainz.org", {"x-ratelimit-limit": "1200", "x-ratelimit-remaining": "190"})
    budget = limiter.budget("musicbrainz.org")
    assert budget["limit"] == 1200 and budget["remaining"] == 190


def test_a_host_with_no_budget_headers_reports_none():
    limiter = media_http.HostLimiter()
    limiter.record_budget("openlibrary.org", {"content-type": "application/json"})
    assert limiter.budget("openlibrary.org") is None


def test_ssl_context_verifies():
    assert media_http.ssl_context().verify_mode.name == "CERT_REQUIRED"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_pipes_in_a_title_do_not_break_the_details_table():
    rows = media_notes.details_table("book", item("A | B", detail={"authors": ["X | Y"]}))
    rendered = media_notes.render_details(rows)
    for line in rendered.splitlines()[2:]:
        assert line.count("|") - line.count("\\|") == 3


def test_absent_fields_are_omitted_not_rendered_empty():
    """A row reading "Director: —" asserts the record has no director."""
    rows = media_notes.details_table("book", item("X", detail={"authors": ["A"], "publisher": None, "pages": None}))
    labels = [label for label, _ in rows]
    assert "Author" in labels and "Publisher" not in labels and "Pages" not in labels


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Nier: Automata", "Nier Automata"),
        ("What/If", "WhatIf"),
        ("Who? Me!", "Who Me!"),
        ("[Rec]", "(Rec)"),
        ("A|B", "A-B"),
    ],
)
def test_filenames_stay_reachable_by_wikilink(raw, expected):
    """A name with `#^[]|` is unreachable by wikilink and will not sync to mobile."""
    assert media_notes.safe_filename(raw) == expected


def test_frontmatter_follows_the_schema_property_order():
    _name, text = media_notes.build_note(
        medium="movie", item=item("Arrival"), property_order=PROPERTY_ORDER, lead=None,
        rating=8, date="2026-08-04", parent="00 Movies",
    )
    keys = [line.split(":")[0] for line in text.split("---")[1].strip().splitlines() if not line.startswith(" ")]
    assert keys == sorted(keys, key=PROPERTY_ORDER.index)


def test_urls_are_quoted_in_frontmatter():
    _name, text = media_notes.build_note(
        medium="movie", item=item("Arrival", detail={}) | {"cover": "https://image.tmdb.org/t/p/w500/x.jpg"},
        property_order=PROPERTY_ORDER, lead=None,
    )
    assert 'cover: "https://image.tmdb.org/t/p/w500/x.jpg"' in text


def test_thoughts_are_not_wrapped_in_a_reflection_callout():
    """`reflection` means generated interpretation, and is explicitly not for
    anything the owner wrote. The owner's words go in plain prose."""
    _name, text = media_notes.build_note(
        medium="book", item=item("X"), property_order=PROPERTY_ORDER, lead=None, thoughts="Mine.",
    )
    assert "[!reflection]" not in text


# ---------------------------------------------------------------------------
# Backlog
# ---------------------------------------------------------------------------


def test_backlog_round_trips_and_ignores_the_header():
    rows = [media_notes.backlog_row(item("Dune", year=2021), "Villeneuve"),
            media_notes.backlog_row(item("Solaris", year=1972))]
    table = media_notes.render_backlog_table(rows)
    parsed = media_notes.parse_backlog_table(table)
    assert [p["cells"][0] for p in parsed] == ["Dune", "Solaris"]


def test_a_hand_written_backlog_row_survives_parsing():
    """The owner will add a line by hand and it has to still be there afterwards."""
    table = media_notes.render_backlog_table([media_notes.backlog_row(item("Dune", year=2021))])
    table += "\n| Stalker | 1979 | someone said so | |"
    parsed = media_notes.parse_backlog_table(table)
    assert [p["cells"][0] for p in parsed] == ["Dune", "Stalker"]

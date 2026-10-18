"""Instagram / X from the ufc.com athlete page JSON-LD (migration 032, R6).

The fixture below is the REAL structure ufc.com served for
/athlete/alexander-volkanovski on 18-oct-2026 (JSON-LD block trimmed of the
breadcrumb, footer anchors copied verbatim): the Person.sameAs lists the
fighter's own accounts, and the page footer links the UFC's. The whole point of
the parser is that the second set NEVER leaks into the first. No network, no DB.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from src.scrapers.ufc_social import (
    DB_CHECK_INSTAGRAM,
    DB_CHECK_X,
    SocialLinks,
    extract_social_links,
    normalize_instagram,
    normalize_x,
)

MIGRATION_032 = (
    Path(__file__).resolve().parents[1] / "db" / "migrations" / "032_fighter_social_links.sql"
)

# Verbatim from the ufc.com footer (alexander-volkanovski.html:3010 and :3025).
_FOOTER = """
<footer>
  <ul class="c-menu-footer">
    <li><a href="https://www.instagram.com/ufc/" class="c-menu-footer__link--level-2">Instagram</a></li>
    <li><a href="https://twitter.com/ufc" class="c-menu-footer__link--level-2">Twitter</a></li>
    <li><a href="https://www.facebook.com/ufc" class="c-menu-footer__link--level-2">Facebook</a></li>
    <li><a href="https://www.tiktok.com/@ufc" class="c-menu-footer__link--level-2">TikTok</a></li>
  </ul>
</footer>
"""


def _person(same_as=None, *, url="https://www.ufc.com/athlete/alexander-volkanovski", **extra):
    person = {
        "@type": "Person",
        "name": "Alexander Volkanovski",
        "alternateName": "The Great",
        "url": url,
        "birthDate": "1988-09-29",
    }
    if same_as is not None:
        person["sameAs"] = same_as
    person.update(extra)
    return person


def _jsonld(main_entity) -> str:
    doc = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "WebPage",
                "url": "https://www.ufc.com/athlete/alexander-volkanovski",
                "name": "Alexander Volkanovski",
                "mainEntity": main_entity,
            }
        ],
    }
    return f'<script type="application/ld+json">{json.dumps(doc, indent=4)}</script>'


_VOLK_SAME_AS = [
    "https://twitter.com/alexvolkanovski",
    "https://instagram.com/alexvolkanovski",
    "https://facebook.com/alexvolkanovski/",
    "https://www.ufcstore.com/en/alexander-volkanovski/ufc-fighters/"
    "t-3800955420+c-0182266627+z-9-3523068364?_s=bm-Alexander_Volkanovski-UFC.com-Shop-Athlete-2024",
]


def _page(head: str = "", body: str = "") -> BeautifulSoup:
    html = (
        f"<html><head>{head}</head><body>"
        f'<h1 class="hero-profile__name">Alexander Volkanovski</h1>{body}{_FOOTER}'
        "</body></html>"
    )
    return BeautifulSoup(html, "lxml")


# ------------------------------------------------------------- extraction


def test_real_volkanovski_block_reads_his_accounts_not_the_footer():
    links = extract_social_links(_page(_jsonld(_person(_VOLK_SAME_AS))))
    assert links == SocialLinks(
        instagram_url="https://www.instagram.com/alexvolkanovski/",
        x_url="https://x.com/alexvolkanovski",
    )


def test_footer_only_page_is_unknown_never_at_ufc():
    # 🪤 The trap: the footer links @ufc on EVERY page. With no Person block the
    # answer is "unknown" (None: touch nothing), never the UFC's accounts.
    assert extract_social_links(_page()) is None


def test_social_anchors_in_the_body_are_ignored():
    body = '<a href="https://instagram.com/someone_else">ig</a>'
    links = extract_social_links(_page(_jsonld(_person(["https://twitter.com/ghurricane"])), body))
    assert links == SocialLinks(instagram_url=None, x_url="https://x.com/ghurricane")


def test_block_without_a_network_means_delete_it():
    # Gerald Harris (retired): X only. instagram_url None = "ufc.com does not
    # list it", which the writer stores as NULL.
    links = extract_social_links(_page(_jsonld(_person(["https://twitter.com/ghurricane"]))))
    assert links is not None
    assert links.instagram_url is None
    assert links.x_url == "https://x.com/ghurricane"
    assert links.any


@pytest.mark.parametrize("same_as", [None, [], "", 7])
def test_block_without_same_as_is_known_and_empty(same_as):
    # The Person block is there but lists nothing: authoritative "no socials",
    # NOT "unknown". MEASURED 18-oct-2026: ufc.com omits the sameAs key for
    # fighters without accounts (Fred Ettish, Zane Frazier, Kevin Rosier, Jason
    # DeLucia). A template change dropping it for everyone is caught by the
    # run-level brake in enrich_athlete_stats, not here.
    links = extract_social_links(_page(_jsonld(_person(same_as))))
    assert links == SocialLinks(instagram_url=None, x_url=None)
    assert not links.any


def test_same_as_as_a_single_string():
    links = extract_social_links(_page(_jsonld(_person("https://instagram.com/rondarousey"))))
    assert links == SocialLinks("https://www.instagram.com/rondarousey/", None)


def test_first_valid_url_per_network_wins():
    same_as = [
        "https://twitter.com/Unavailable ",  # filler -> skipped, not taken
        "https://instagram.com/p/ABC123/",  # a post -> skipped
        "https://twitter.com/TouchNgo_",
        "https://instagram.com/rudeboy",
        "https://instagram.com/second_account",
    ]
    links = extract_social_links(_page(_jsonld(_person(same_as))))
    assert links == SocialLinks("https://www.instagram.com/rudeboy/", "https://x.com/TouchNgo_")


@pytest.mark.parametrize(
    "raw,field",
    [
        ("https://x.com/i/user/123456", "x_unparsed"),  # a URL shape we do not read
        ("https://twitter.com/JonnyBones/status/1", "x_unparsed"),
        ("https://twitter.com/", "x_unparsed"),
        ("https://instagram.com/jonnybones/profilecard/?igsh=abc", "instagram_unparsed"),
        ("https://instagram.com/p/ABC123/", "instagram_unparsed"),
        ("https://instagram.com/" + "a" * 31, "instagram_unparsed"),
    ],
)
def test_unreadable_url_on_the_network_is_kept_not_deleted(raw, field):
    # Listed on instagram.com / twitter.com / x.com but not readable: that is
    # not "ufc.com dropped it", so the writer must keep the stored link.
    links = extract_social_links(_page(_jsonld(_person([raw]))))
    assert links is not None and not links.any
    assert getattr(links, field) is True
    other = "instagram_unparsed" if field == "x_unparsed" else "x_unparsed"
    assert getattr(links, other) is False


@pytest.mark.parametrize(
    "raw",
    [
        "https://twitter.com/Unavailable ",
        "https://instagram.com/Unavailable",
        "https://instagram.com/ufc",  # the UFC's own account: "none of his own"
        "https://twitter.com/@ufc",
    ],
)
def test_explicit_filler_means_no_account_and_is_deleted(raw):
    links = extract_social_links(_page(_jsonld(_person([raw]))))
    assert links == SocialLinks(None, None)
    assert not links.instagram_unparsed and not links.x_unparsed


def test_unreadable_entry_does_not_hide_a_readable_one():
    same_as = ["https://instagram.com/p/ABC123/", "https://instagram.com/rudeboy"]
    links = extract_social_links(_page(_jsonld(_person(same_as))))
    assert links == SocialLinks("https://www.instagram.com/rudeboy/", None)
    assert links.instagram_unparsed is False


def test_other_networks_never_count_as_unparsed():
    same_as = ["https://facebook.com/alexvolkanovski/", "https://www.ufcstore.com/en/x"]
    links = extract_social_links(_page(_jsonld(_person(same_as))))
    assert links == SocialLinks(None, None)


def test_only_a_person_on_an_athlete_url_counts():
    # A Person that is not an ufc.com athlete page (or a non-Person node with
    # sameAs, like an Organization) must not be read as the fighter's socials.
    not_athlete = _person(["https://twitter.com/ufc_writer"], url="https://www.ufc.com/news/x")
    org = {"@type": "Organization", "url": "https://www.ufc.com/athlete/x", "sameAs": _VOLK_SAME_AS}
    other_host = _person(_VOLK_SAME_AS, url="https://evil.example/athlete/alexander-volkanovski")
    for node in (not_athlete, org, other_host):
        assert extract_social_links(_page(_jsonld(node))) is None


def test_search_page_without_person_is_unknown():
    # What an unknown slug redirects to: a WebPage, no mainEntity Person.
    doc = {"@context": "https://schema.org", "@graph": [{"@type": "WebPage", "name": "Search"}]}
    head = f'<script type="application/ld+json">{json.dumps(doc)}</script>'
    assert extract_social_links(_page(head)) is None


def test_broken_json_block_is_skipped_and_the_next_one_used():
    head = '<script type="application/ld+json">{not json</script>' + _jsonld(
        _person(["https://instagram.com/alexvolkanovski"])
    )
    links = extract_social_links(_page(head))
    assert links == SocialLinks("https://www.instagram.com/alexvolkanovski/", None)


def test_person_as_a_top_level_graph_node():
    doc = {"@context": "https://schema.org", "@graph": [_person(["https://twitter.com/TeamKhabib"])]}
    head = f'<script type="application/ld+json">{json.dumps(doc)}</script>'
    assert extract_social_links(_page(head)) == SocialLinks(None, "https://x.com/TeamKhabib")


# ---------------------------------------------------------- normalization


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://instagram.com/alexvolkanovski", "https://www.instagram.com/alexvolkanovski/"),
        ("https://www.instagram.com/alexvolkanovski/", "https://www.instagram.com/alexvolkanovski/"),
        ("https://instagram.com/TheNotoriousMMA", "https://www.instagram.com/thenotoriousmma/"),
        ("instagram.com/Some.User?hl=es", "https://www.instagram.com/some.user/"),
        ("http://m.instagram.com/hitman_att/", "https://www.instagram.com/hitman_att/"),
        ("https://instagram.com/@navajostirling", "https://www.instagram.com/navajostirling/"),
        ("https://instagr.am/jonnybones", "https://www.instagram.com/jonnybones/"),
        ("https://instagram.com/_u/jonnybones", "https://www.instagram.com/jonnybones/"),
        ("https://instagram.com/_u/", None),
        ("  https://instagram.com/georgesstpierre_  ", "https://www.instagram.com/georgesstpierre_/"),
        # A real fighter handle that merely CONTAINS "ufc" is fine.
        ("https://instagram.com/michelpereiraufc", "https://www.instagram.com/michelpereiraufc/"),
        ("https://instagram.com/ufc", None),  # the UFC itself, never a fighter
        ("https://www.instagram.com/ufcespanol/", None),
        ("https://instagram.com/Unavailable", None),
        ("https://instagram.com/p/ABC123/", None),  # a post
        ("https://instagram.com/reel/XYZ/", None),
        ("https://instagram.com/rudeboy/reels/", None),  # not a bare profile URL
        ("https://instagram.com/", None),
        ("https://instagram.com/.dotfirst", None),
        ("https://instagram.com/dot..double", None),
        ("https://instagram.com/" + "a" * 31, None),
        ("https://instagram.com.evil.com/alexvolkanovski", None),
        ("https://evil.com/instagram.com/alexvolkanovski", None),
        ("https://twitter.com/alexvolkanovski", None),  # wrong network
        ("javascript:alert(1)", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_instagram(raw, expected):
    assert normalize_instagram(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://twitter.com/alexvolkanovski", "https://x.com/alexvolkanovski"),
        ("https://twitter.com/@navajostirling", "https://x.com/navajostirling"),
        ("https://mobile.twitter.com/TeamKhabib", "https://x.com/TeamKhabib"),
        ("https://www.twitter.com/TouchNgo_?ref=br_rs", "https://x.com/TouchNgo_"),
        ("https://x.com/RondaRousey/", "https://x.com/RondaRousey"),
        ("https://twitter.com/#!/JonnyBones", "https://x.com/JonnyBones"),
        ("https://twitter.com/#!/intent/tweet", None),
        ("https://twitter.com/UfcPereira", "https://x.com/UfcPereira"),
        ("https://twitter.com/ufc", None),
        ("https://twitter.com/UFCEspanol", None),
        ("https://twitter.com/Unavailable ", None),
        ("https://twitter.com/intent/tweet?text=hi", None),
        ("https://twitter.com/RondaRousey/status/123", None),  # a tweet
        ("https://twitter.com/a234567890123456", None),  # 16 chars
        ("https://twitter.com/dot.not.allowed", None),
        ("https://x.com/", None),
        ("https://facebook.com/Unavailable  ", None),
        ("https://instagram.com/alexvolkanovski", None),  # wrong network
        ("ftp://twitter.com/alexvolkanovski", None),
        (None, None),
    ],
)
def test_normalize_x(raw, expected):
    assert normalize_x(raw) == expected


def _check_regex(column: str) -> re.Pattern:
    """The CHECK pattern migration 032 enforces for ``column``, as Python re."""
    sql = MIGRATION_032.read_text(encoding="utf-8")
    match = re.search(rf"{column} ~ '([^']+)'", sql)
    assert match, f"no CHECK pattern for {column} in {MIGRATION_032.name}"
    return re.compile(match.group(1))


@pytest.mark.parametrize(
    "raw",
    [
        "https://instagram.com/TheNotoriousMMA",
        "instagram.com/Some.User?hl=es",
        "https://instagram.com/" + "a_b.c" * 6,  # exactly 30 chars
        "https://m.instagram.com/@x",
    ],
)
def test_normalized_instagram_satisfies_the_db_check(raw):
    # A value the CHECK rejects would abort the whole sweep mid-run.
    value = normalize_instagram(raw)
    assert value is not None
    assert _check_regex("instagram_url").fullmatch(value)


@pytest.mark.parametrize(
    "raw",
    ["https://twitter.com/@navajostirling", "https://x.com/" + "A_9" * 5, "twitter.com/TouchNgo_"],
)
def test_normalized_x_satisfies_the_db_check(raw):
    value = normalize_x(raw)
    assert value is not None
    assert _check_regex("x_url").fullmatch(value)


def test_python_check_patterns_are_the_migration_ones():
    # ufc_social refuses to return a value its copy of the CHECK rejects; that
    # copy must stay byte-identical to the .sql or the guard is worthless.
    assert DB_CHECK_INSTAGRAM.pattern == _check_regex("instagram_url").pattern
    assert DB_CHECK_X.pattern == _check_regex("x_url").pattern


@pytest.mark.parametrize("handle", ["abc\n", "abc\r\n", "ab c", "abc\t"])
def test_handles_with_whitespace_inside_never_pass(handle, monkeypatch):
    # Python's ``$`` matches before a trailing newline and Postgres's does not:
    # a handle like "abc\n" must never reach the CHECK (it would abort the
    # sweep). urlsplit already strips \n/\r/\t; fullmatch is the second lock.
    from src.scrapers import ufc_social

    monkeypatch.setattr(ufc_social, "_profile_handle", lambda raw, hosts, reserved: handle)
    assert normalize_instagram("https://instagram.com/x") is None
    assert normalize_x("https://twitter.com/x") is None

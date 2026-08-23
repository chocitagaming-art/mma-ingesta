"""Fase 5 / BE2: official weigh-in results from ufc.com news articles.

Article discovery via ufc.com's server-rendered /search (anchors to /news/
slugs like "official-weigh-in-results-fight-night-fiziev-torres-baku", token-
guarded like wiki_bonuses), body parsing of the <h4> bout lines inside
.field--name-text blocks, corner matching at IDENTITY_THRESHOLD and the
ON CONFLICT (fight_id, fighter_id) DO UPDATE + COALESCE write policy.

HTML inline (structure copied from real 2026-06/07 fetches) + fakedb;
no network, no real DB.
"""

import re
from collections import Counter
from pathlib import Path

from bs4 import BeautifulSoup

from src.scrapers.weigh_ins import (
    EventFight,
    ParsedWeighIn,
    _event_city,
    _process_event,
    find_weigh_in_article,
    match_bout,
    parse_weigh_ins,
    upsert_weigh_in,
)

# --------------------------------------------------------------------- HTML

# Real search-result structure: solr-search items with plain /news/ anchors.
SEARCH_HTML = """
<html><body>
<div class="solr-search__result-item">
  <div class="view view-solr-search">
    <ul class="l-flex--2col-1to2">
      <li class="l-flex__item">
        <a class="c-card--grid-card-trending grid_card_image_text"
           href="/news/fight-fight-preview-ufc-baku-fiziev-torres-2026">
          Fight By Fight Preview | UFC Fight Night: Fiziev vs Torres
        </a>
      </li>
      <li class="l-flex__item">
        <a class="c-card--grid-card-trending grid_card_image_text"
           href="/news/official-weigh-results-kape-horiguchi-vegas-119">
          Weigh-in Official Weigh-In Results | UFC Fight Night: Kape vs Horiguchi
        </a>
      </li>
      <li class="l-flex__item">
        <a class="c-card--grid-card-trending grid_card_image_text"
           href="/news/official-weigh-in-results-fight-night-fiziev-torres-baku">
          Weigh-in Official Weigh-In Results | UFC Fight Night: Fiziev vs Torres
        </a>
      </li>
    </ul>
  </div>
</div>
</body></html>
"""

# Real article-body structure: h3 section headings, h4 bout lines (optionally
# prefixed "Main Event -"/"Co-Main Event -"), interleaved promo links, &nbsp;.
# The asterisk marks a missed weight (added here on Pereira).
ARTICLE_HTML = """
<html><body>
<div class="field field--name-text field--type-text-long field--label-hidden field__item">
<h3><strong>MAIN CARD</strong></h3>
<h4><strong>Main Event -</strong> Lightweight Bout: Rafael Fiziev (156) vs Manuel Torres (156)</h4>
<h4><strong>Co-Main Event -</strong> Middleweight Bout: Shara Magomedov (186) vs Michel Pereira (186.5)*</h4>
<h4>Lightweight Bout: Nazim Sadykhov (156) vs Matheus Camilo (156)&nbsp;</h4>
<p><a href="https://www.ufc.com/news/fight-fight-preview-ufc-baku-fiziev-torres-2026"><strong>Preview The Entire UFC Baku Fight Card Here</strong></a></p>
</div>
<div class="field field--name-text field--type-text-long field--label-hidden field__item">
<h3><strong>PRELIMS</strong></h3>
<h4>Welterweight Bout: Farman Hasanov (170.5) vs Eric Nolan (170.5)</h4>
</div>
</body></html>
"""


# ufc.com's search is an AND over the article TITLE, and ufc.com titles this
# card "UFC Abu Dhabi" while our events row calls it "UFC Fight Night" (real
# 2026-07-25 divergence). Every query carrying "fight night" therefore returns
# the empty page below, and only the unqualified query finds the article.
NO_RESULTS_HTML = """
<html><body><div class="solr-search__result-item"><p>No results</p></div></body></html>
"""

GENERIC_SEARCH_HTML = """
<html><body>
<div class="solr-search__result-item">
  <ul>
    <li><a href="/news/official-weigh-results-ufc-329-mcgregor-vs-holloway-2">
      Official Weigh-In Results | UFC 329</a></li>
    <li><a href="/news/official-weigh-results-ufc-abu-dhabi-ankalaev-guskov">
      Official Weigh-In Results | UFC Abu Dhabi</a></li>
  </ul>
</div>
</body></html>
"""

# Searching by CITY finds the card the name query cannot: ufc.com titles it
# "UFC Sacramento", but the SLUG still carries both surnames, which is what the
# token guard reads. Real article for the 1086 (2026-08-22).
CITY_SEARCH_HTML = """
<html><body>
<div class="solr-search__result-item">
  <ul>
    <li><a href="/news/official-weigh-results-ufc-sacramento-fight-night-hernandez-rodrigues">
      Official Weigh-In Results | UFC Sacramento</a></li>
  </ul>
</div>
</body></html>
"""

# A city hosts more than one card over time: "ufc abu dhabi" really returns two
# weigh-in articles (measured 2026-08-23). Only the token guard tells them apart.
TWO_CITY_ARTICLES_HTML = """
<html><body>
<div class="solr-search__result-item">
  <ul>
    <li><a href="/news/official-weigh-results-ufc-abu-dhabi-ankalaev-guskov">
      Official Weigh-In Results | UFC Abu Dhabi</a></li>
    <li><a href="/news/official-weigh-results-ufc-abu-dhabi-whittaker-de-ridder">
      Official Weigh-In Results | UFC Abu Dhabi</a></li>
  </ul>
</div>
</body></html>
"""


def _fetch(pages: dict):
    def fetch_html(url: str, params: dict | None = None) -> str:
        return pages[url]

    return fetch_html


def _fetch_by_query(pages: dict, calls: list | None = None):
    """fetch_html keyed on the search query, so tests can drive the fallback."""

    def fetch_html(url: str, params: dict | None = None) -> str:
        query = (params or {}).get("query", "")
        if calls is not None:
            calls.append(query)
        return pages[query]

    return fetch_html


# ----------------------------------------------------------------- discovery


def test_find_weigh_in_article_picks_the_token_matching_slug():
    fetch = _fetch({"https://www.ufc.com/search": SEARCH_HTML})
    url = find_weigh_in_article(fetch, "UFC Fight Night: Fiziev vs. Torres")
    # The Kape/Horiguchi weigh-in article shares zero significant tokens and
    # the preview article has no "weigh" in its slug: both rejected.
    assert url == "https://www.ufc.com/news/official-weigh-in-results-fight-night-fiziev-torres-baku"


def test_find_weigh_in_article_rejects_foreign_events():
    fetch = _fetch({"https://www.ufc.com/search": SEARCH_HTML})
    # "UFC 328" shares no headliner/number token with any weigh-in slug.
    assert find_weigh_in_article(fetch, "UFC 328: Chimaev vs Strickland") is None


def test_find_weigh_in_article_falls_back_when_the_event_name_diverges():
    """The name-qualified query finds nothing -> retry unqualified.

    Regression for 2026-07-25: our events row said "UFC Fight Night: Ankalaev
    vs. Guskov" and ufc.com titled the article "UFC Abu Dhabi", so the
    name-qualified search returned "No results" and the cron wrote zero rows
    while reporting success.
    """
    calls: list[str] = []
    fetch = _fetch_by_query(
        {
            "official weigh-in results UFC Fight Night: Ankalaev vs. Guskov": NO_RESULTS_HTML,
            "official weigh-in results": GENERIC_SEARCH_HTML,
        },
        calls,
    )
    url = find_weigh_in_article(fetch, "UFC Fight Night: Ankalaev vs. Guskov")
    assert url == "https://www.ufc.com/news/official-weigh-results-ufc-abu-dhabi-ankalaev-guskov"
    # The qualified query still runs first: it is the precise one when it works.
    assert calls[0] == "official weigh-in results UFC Fight Night: Ankalaev vs. Guskov"


def test_find_weigh_in_article_fallback_keeps_the_token_guard():
    """The wider candidate pool must not weld a foreign card's article on.

    The fallback only widens WHICH articles are considered; the >=2 shared
    significant tokens rule is what stops UFC 329's weights landing on our card.
    """
    fetch = _fetch_by_query(
        {
            "official weigh-in results UFC 331: Chimaev vs Strickland": NO_RESULTS_HTML,
            "official weigh-in results": GENERIC_SEARCH_HTML,
        }
    )
    assert find_weigh_in_article(fetch, "UFC 331: Chimaev vs Strickland") is None


def test_find_weigh_in_article_does_not_search_twice_when_the_first_query_hits():
    calls: list[str] = []
    fetch = _fetch_by_query(
        {"official weigh-in results UFC Fight Night: Fiziev vs. Torres": SEARCH_HTML},
        calls,
    )
    assert find_weigh_in_article(fetch, "UFC Fight Night: Fiziev vs. Torres") is not None
    assert len(calls) == 1


# ------------------------------------------------- discovery by city (2026-08-23)


def test_event_city_reads_the_city_from_real_locations():
    """The eight real `events.location` shapes in the DB on 2026-08-23.

    The city is the SECOND comma-part in every normal case. `Belgrade Arena,
    BG, Serbia` is the one that breaks it: the second part is a two-letter
    country-ish code, so the venue name carries the city instead.
    """
    assert _event_city("Golden 1 Center, Sacramento, CA, United States") == "Sacramento"
    assert _event_city("Etihad Arena, Abu Dhabi, United Arab Emirates") == "Abu Dhabi"
    assert _event_city("Paycom Center, Oklahoma City, OK, United States") == "Oklahoma City"
    assert _event_city("National Gymnastics Arena, Baku, Azerbaijan") == "Baku"
    assert _event_city("Meta APEX, Las Vegas, NV, United States") == "Las Vegas"
    assert _event_city("Xfinity Mobile Arena, Philadelphia, PA, United States") == "Philadelphia"
    # The awkward one: falls back to the venue with the venue-word stripped.
    assert _event_city("Belgrade Arena, BG, Serbia") == "Belgrade"


def test_event_city_is_safe_on_junk():
    assert _event_city(None) == ""
    assert _event_city("") == ""
    assert _event_city("   ") == ""
    assert _event_city("Arena") == ""


def test_find_weigh_in_article_falls_back_to_the_city():
    """Name query dies, city query hits. Regression for the 1086 (2026-08-22).

    ufc.com titled the card "Official Weigh-In Results | UFC Sacramento" — no
    surnames, no "Fight Night" — while our events row says "UFC Fight Night:
    Hernandez vs. Rodrigues". The search is an AND over the TITLE, so the
    name-qualified query returns "No results". Measured 2026-08-23: this hits
    5 of the 10 most recent cards, and the unqualified retry only lists ~4
    articles and ignores `page`, so it stops working within days.
    """
    calls: list[str] = []
    fetch = _fetch_by_query(
        {
            "official weigh-in results UFC Fight Night: Hernandez vs. Rodrigues": NO_RESULTS_HTML,
            "official weigh-in results ufc Sacramento": CITY_SEARCH_HTML,
        },
        calls,
    )
    url = find_weigh_in_article(
        fetch,
        "UFC Fight Night: Hernandez vs. Rodrigues",
        "Golden 1 Center, Sacramento, CA, United States",
    )
    assert url == (
        "https://www.ufc.com/news/"
        "official-weigh-results-ufc-sacramento-fight-night-hernandez-rodrigues"
    )
    # Name first (it is the precise one), then city. The generic never runs.
    assert calls == [
        "official weigh-in results UFC Fight Night: Hernandez vs. Rodrigues",
        "official weigh-in results ufc Sacramento",
    ]


def test_find_weigh_in_article_city_query_keeps_the_token_guard():
    """A city hosts many cards; the >=2-token guard is what stops the wrong one.

    Real case measured on 2026-08-23: searching "ufc abu dhabi" returns TWO
    weigh-in articles (Ankalaev/Guskov and Whittaker/De Ridder). Only the one
    sharing two significant tokens with the event may pass.
    """
    fetch = _fetch_by_query(
        {
            "official weigh-in results UFC Fight Night: Whittaker vs. De Ridder": NO_RESULTS_HTML,
            "official weigh-in results ufc Abu Dhabi": TWO_CITY_ARTICLES_HTML,
            "official weigh-in results": NO_RESULTS_HTML,
        }
    )
    url = find_weigh_in_article(
        fetch,
        "UFC Fight Night: Whittaker vs. De Ridder",
        "Etihad Arena, Abu Dhabi, United Arab Emirates",
    )
    assert url == (
        "https://www.ufc.com/news/official-weigh-results-ufc-abu-dhabi-whittaker-de-ridder"
    )


def test_find_weigh_in_article_falls_through_city_to_the_generic_retry():
    """City is tried BETWEEN name and generic, and never swallows the generic."""
    calls: list[str] = []
    fetch = _fetch_by_query(
        {
            "official weigh-in results UFC Fight Night: Ankalaev vs. Guskov": NO_RESULTS_HTML,
            "official weigh-in results ufc Abu Dhabi": NO_RESULTS_HTML,
            "official weigh-in results": GENERIC_SEARCH_HTML,
        },
        calls,
    )
    url = find_weigh_in_article(
        fetch,
        "UFC Fight Night: Ankalaev vs. Guskov",
        "Etihad Arena, Abu Dhabi, United Arab Emirates",
    )
    assert url == "https://www.ufc.com/news/official-weigh-results-ufc-abu-dhabi-ankalaev-guskov"
    assert calls == [
        "official weigh-in results UFC Fight Night: Ankalaev vs. Guskov",
        "official weigh-in results ufc Abu Dhabi",
        "official weigh-in results",
    ]


def test_find_weigh_in_article_without_location_behaves_exactly_as_before():
    """`location` is optional: no city query is attempted when it is missing.

    Keeps every existing caller and the five older discovery tests valid.
    """
    calls: list[str] = []
    fetch = _fetch_by_query(
        {
            "official weigh-in results UFC Fight Night: Ankalaev vs. Guskov": NO_RESULTS_HTML,
            "official weigh-in results": GENERIC_SEARCH_HTML,
        },
        calls,
    )
    url = find_weigh_in_article(fetch, "UFC Fight Night: Ankalaev vs. Guskov")
    assert url == "https://www.ufc.com/news/official-weigh-results-ufc-abu-dhabi-ankalaev-guskov"
    assert len(calls) == 2  # name, generic. No city in between.


# ------------------------------------------------------------------- parsing


def test_parse_weigh_ins_reads_bout_lines_across_blocks():
    entries = parse_weigh_ins(ARTICLE_HTML)
    assert len(entries) == 4
    main = entries[0]
    assert (main.red_name, main.red_lbs, main.red_missed) == ("Rafael Fiziev", 156.0, False)
    assert (main.blue_name, main.blue_lbs, main.blue_missed) == ("Manuel Torres", 156.0, False)
    # Prefixes ("Co-Main Event -") are swallowed; decimals and the missed-
    # weight asterisk are captured.
    comain = entries[1]
    assert comain.red_name == "Shara Magomedov"
    assert (comain.blue_name, comain.blue_lbs, comain.blue_missed) == ("Michel Pereira", 186.5, True)
    # The &nbsp; line and the PRELIMS block both parse; the promo link doesn't.
    assert entries[2].red_name == "Nazim Sadykhov"
    assert entries[3].blue_name == "Eric Nolan"


def test_parse_weigh_ins_empty_for_non_weigh_in_article():
    html = """
    <div class="field field--name-text"><p>In the co-main event, knockout artist
    Shara Magomedov faces Michel Pereira in a high-energy middleweight clash.</p></div>
    """
    assert parse_weigh_ins(html) == []


# Real 2026-08-14 body of UFC 330. Numbered PPVs carry title fights, and
# ufc.com writes those lines "<promotion> <division> Championship:" with NO
# "Bout" token — the shape every other line uses. Verbatim from
# /news/official-weigh-results-ufc-330-makhachev-vs-machado-garry.
TITLE_ARTICLE_HTML = """
<html><body>
<div class="field field--name-text field--type-text-long field--label-hidden field__item">
<h3><strong>MAIN CARD</strong></h3>
<h4><strong>Main Event -</strong> UFC Welterweight Championship: Islam Makhachev (170) vs Ian Machado Garry (170)</h4>
<h4><strong>Co-Main Event -</strong> UFC Strawweight Championship: Mackenzie Dern (115) vs Gillian Robertson (115)</h4>
<h4>Lightweight Bout: Jalin Turner (156) vs Kaue Fernandes (156)</h4>
</div>
</body></html>
"""


def test_parse_weigh_ins_reads_title_fight_lines():
    # The two championship lines were dropped in silence until 2026-08-15,
    # which is why no title fight has ever had a weigh-in row.
    entries = parse_weigh_ins(TITLE_ARTICLE_HTML)
    assert len(entries) == 3
    main = entries[0]
    assert (main.red_name, main.red_lbs) == ("Islam Makhachev", 170.0)
    assert (main.blue_name, main.blue_lbs) == ("Ian Machado Garry", 170.0)
    comain = entries[1]
    assert (comain.red_name, comain.red_lbs) == ("Mackenzie Dern", 115.0)
    assert (comain.blue_name, comain.blue_lbs) == ("Gillian Robertson", 115.0)
    assert entries[2].red_name == "Jalin Turner"


def test_parse_weigh_ins_counts_bout_shaped_lines_it_could_not_read():
    # THE POINT OF THIS TEST: the championship bug was invisible because a line
    # that does not match is skipped without a trace, so the job reported
    # "weigh_ins_written: 42, event_errors: 0" while losing four weights. A
    # line that LOOKS like a bout (two names, two parenthesised weights, "vs")
    # but does not parse has to leave a mark.
    html = """
    <div class="field field--name-text">
    <h4>Middleweight Skirmish - Someone New (185) vs Someone Else (185)</h4>
    </div>
    """
    counts: Counter = Counter()
    assert parse_weigh_ins(html, counts) == []
    assert counts["bout_shaped_lines_unparsed"] == 1


def test_parse_weigh_ins_does_not_count_promo_lines_as_lost():
    # The symmetric hole: if the alarm fires on ordinary prose it stops being
    # read. Promo copy has names and a "vs" but no parenthesised weights.
    html = """
    <div class="field field--name-text">
    <p><a href="/news/x">Preview: Makhachev vs Machado Garry, the entire card</a></p>
    <p>In the co-main, Dern faces Robertson (a rematch) in Philadelphia.</p>
    </div>
    """
    counts: Counter = Counter()
    assert parse_weigh_ins(html, counts) == []
    assert counts["bout_shaped_lines_unparsed"] == 0


def test_parse_weigh_ins_keeps_working_without_a_counter():
    # The counter is opt-in: existing callers pass nothing.
    assert len(parse_weigh_ins(TITLE_ARTICLE_HTML)) == 3


# ------------------------------------------------- the real article, and why
#
# 🪤 EVERY TEST ABOVE THIS LINE WAS GREEN ON 2026-08-15 WHILE TWO REAL BOUTS
# WERE BEING LOST AND THE TRIPWIRE REPORTED ZERO. They cannot see it: the HTML
# they parse was typed by the same hand that wrote the regex, so it can only
# contain the variants that hand already thought of. The four tests below run
# against the article as ufc.com actually served it, and none of them asks the
# module how many lines there are — they count independently and cross-check
# against a second source inside the same page.

_REAL_ARTICLE = (Path(__file__).parent / "fixtures" / "weigh_ins_oklahoma_city.html").read_text(
    encoding="utf-8"
)


def _looks_like_a_bout(text: str) -> bool:
    """Bout-shaped? Decided by scanning the string, NOT by a regex.

    Deliberately shares no structure with _BOUT_LINE_RE or _BOUT_SHAPE_RE: an
    oracle built from the same subpattern as the code it checks inherits its
    blind spots, which is exactly how the tripwire came to share the parser's.
    """
    if " vs " not in text.lower() and " vs. " not in text.lower():
        return False
    weights = 0
    for chunk in text.split("(")[1:]:
        inside = chunk.split(")")[0]
        digits = ""
        for ch in inside:
            if ch.isdigit() or ch == ".":
                digits += ch
            else:
                break
        if digits and 2 <= len(digits.split(".")[0]) <= 3:
            weights += 1
    return weights >= 2


def _body_lines(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    return [
        el.get_text(" ", strip=True).replace("\xa0", " ")
        for block in soup.select(".field--name-text")
        for el in block.find_all(["h4", "p", "li"])
    ]


def test_the_real_article_loses_no_bout_line_in_silence():
    # CONSERVATION. Whatever the parser cannot read has to show up in the
    # counter, so parsed + flagged always equals what is on the page. This is
    # the assertion that survives the next wording change too: it never names
    # the asterisk. Before the 2026-08-15 fix it read 10 + 0 against 12.
    lines = _body_lines(_REAL_ARTICLE)
    shaped = [line for line in lines if _looks_like_a_bout(line)]
    assert len(shaped) == 12, "the fixture should still hold 12 bout lines"

    counts: Counter = Counter()
    parsed = parse_weigh_ins(_REAL_ARTICLE, counts)

    assert len(parsed) + counts["bout_shaped_lines_unparsed"] == len(shaped)
    assert len(parsed) == 12


def test_the_footnotes_of_the_real_article_match_the_flagged_corners():
    # A SECOND ORACLE INSIDE THE SAME PAGE, written by a different part of the
    # CMS: every athlete over the limit gets a footnote naming them. The parser
    # cannot fake this one. Before the fix it was 2 footnotes against 0 corners.
    lines = _body_lines(_REAL_ARTICLE)
    footnotes = [line for line in lines if line.lstrip().startswith("*")]
    assert len(footnotes) == 2

    parsed = parse_weigh_ins(_REAL_ARTICLE)
    flagged = [
        name
        for entry in parsed
        for name, missed in ((entry.red_name, entry.red_missed), (entry.blue_name, entry.blue_missed))
        if missed
    ]
    assert len(flagged) == len(footnotes)
    # And it is the RIGHT athletes, not just the right count: a parser that
    # flagged two arbitrary corners would pass the length check alone.
    for footnote in footnotes:
        assert any(surname in footnote for surname in (n.split()[-1] for n in flagged))


def test_the_tripwire_sees_the_asterisk_inside_the_bracket():
    # THE BLIND SPOT ITSELF. Both regexes carried their own copy of
    # `\(\s*\d{2,3}(?:\.\d+)?\s*\)`, so "(185*)" broke the two at once and the
    # loss was mute. The keyword here is deliberately one the parser rejects, so
    # the assertion tests the TRIPWIRE and not the parser: narrow _BOUT_SHAPE_RE
    # back and this goes red even though every bout line still parses.
    for variant in ("(185*)", "(185**)", "(185)*", "(185)**"):
        html = f"""
        <div class="field field--name-text">
        <h4>Middleweight Skirmish - Someone New {variant} vs Someone Else (185)</h4>
        </div>
        """
        counts: Counter = Counter()
        assert parse_weigh_ins(html, counts) == []
        assert counts["bout_shaped_lines_unparsed"] == 1, f"mute on {variant}"


def test_catchweight_limit_is_never_read_as_an_athletes_weight():
    # "Catchweight Bout (130-lbs):" was lost whole because the bracket sits
    # between the keyword and the colon. The fix has a trap of its own: that
    # bracket holds a THIRD weight that belongs to nobody, so the assertion that
    # matters is not "the line parses" but "129.5 and 130 went to the right
    # corners and the limit went nowhere".
    html = """
    <div class="field field--name-text">
    <h4>Catchweight Bout (130-lbs): Allan Nascimento (129.5) vs Cody Durden (130)</h4>
    </div>
    """
    counts: Counter = Counter()
    entries = parse_weigh_ins(html, counts)
    assert len(entries) == 1
    assert (entries[0].red_name, entries[0].red_lbs) == ("Allan Nascimento", 129.5)
    assert (entries[0].blue_name, entries[0].blue_lbs) == ("Cody Durden", 130.0)
    assert counts["bout_shaped_lines_unparsed"] == 0


def test_parsed_names_never_carry_a_marker():
    # PURITY OF THE OUTPUT, and it never mentions asterisks either. A marker
    # glued to a name is worse than a lost line: fold() (matching.py:86) does
    # not strip it and fold_ratio still scores 0.9565, over the 0.92 identity
    # cutoff — so the only guard that could have caught it waves it through and
    # the row is written with missed_weight=FALSE. A wrong row, not a missing
    # one. This catches `*`, `†`, `#` and whatever ufc.com invents next.
    html = """
    <div class="field field--name-text">
    <h4>Lightweight Bout: *Farman Hasanov (172.5) vs Eric Nolan (170.5)</h4>
    </div>
    """
    for entry in parse_weigh_ins(html) + parse_weigh_ins(_REAL_ARTICLE):
        for name in (entry.red_name, entry.blue_name):
            assert re.fullmatch(r"[A-Za-zÀ-ÿ' .\-]+", name), f"marker left in {name!r}"


# ------------------------------------------------------------------ matching


FIGHTS = [
    EventFight(fight_id=11, red_id=1, blue_id=2, red_name="Rafael Fiziev", blue_name="Manuel Torres"),
    EventFight(fight_id=12, red_id=3, blue_id=None, red_name="Shara Magomedov", blue_name="Michel Pereira"),
]


def _entry(red, blue, red_lbs=156.0, blue_lbs=156.0, red_missed=False, blue_missed=False):
    return ParsedWeighIn(red, red_lbs, red_missed, blue, blue_lbs, blue_missed)


def test_match_bout_direct_and_swapped():
    fight, swapped = match_bout(FIGHTS, _entry("Rafael Fiziev", "Manuel Torres"))
    assert (fight.fight_id, swapped) == (11, False)
    # The article can list the corners in the opposite order.
    fight, swapped = match_bout(FIGHTS, _entry("Manuel Torres", "Rafael Fiziev"))
    assert (fight.fight_id, swapped) == (11, True)


def test_match_bout_requires_identity_threshold():
    # A different fighter never matches at 0.92 (weights link DB ids).
    assert match_bout(FIGHTS, _entry("Rafael Fiziev", "Someone Else")) is None


# --------------------------------------------------------------------- write


def test_upsert_weigh_in_conflict_policy(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    assert upsert_weigh_in(conn, 11, 1, 156.0, False) is True
    sql, params = conn.cursors[0].executed[0]
    flat = " ".join(sql.split())
    assert "INSERT INTO weigh_ins" in flat
    assert "ON CONFLICT (fight_id, fighter_id)" in flat
    # Never overwrite a stored weight/flag with NULL.
    assert "weight_lbs = COALESCE(EXCLUDED.weight_lbs, weigh_ins.weight_lbs)" in flat
    assert "missed_weight = COALESCE(%s, weigh_ins.missed_weight)" in flat
    assert params == (11, 1, 156.0, False, False)


# ------------------------------------------------------------------ pipeline


def _event_fights_rows():
    return [
        (11, 1, 2, "Rafael Fiziev", "Manuel Torres"),
        (12, 3, None, "Shara Magomedov", "Michel Pereira"),
    ]


def _pages():
    return {
        "https://www.ufc.com/search": SEARCH_HTML,
        "https://www.ufc.com/news/official-weigh-in-results-fight-night-fiziev-torres-baku": ARTICLE_HTML,
    }


def test_process_event_writes_matched_corners_only(fakedb):
    def responder(sql, params=None):
        if "FROM fights fi" in sql:
            return _event_fights_rows()
        return [(1,)]

    conn = fakedb.Connection(responder)
    counts: Counter = Counter()
    _process_event(
        conn, _fetch(_pages()), counts, 7, "UFC Fight Night: Fiziev vs. Torres", dry_run=False
    )
    inserts = [s for s in fakedb.mutating_statements(conn) if "INSERT INTO weigh_ins" in s]
    # Fiziev + Torres + Magomedov: Pereira's corner is unlinked (blue_id NULL)
    # and the Sadykhov and Hasanov bouts are not on this event's card.
    assert len(inserts) == 3
    assert counts["weigh_ins_written"] == 3
    assert counts["corners_unlinked"] == 1
    assert counts["bouts_unmatched"] == 2
    # The cancelled-bout filter guards the corner lookup.
    fights_sql = next(
        sql for cur in conn.cursors for sql, _ in cur.executed if "FROM fights fi" in sql
    )
    assert "status IS DISTINCT FROM 'cancelled'" in " ".join(fights_sql.split())


def test_process_event_dry_run_never_writes(fakedb):
    def responder(sql, params=None):
        if "FROM fights fi" in sql:
            return _event_fights_rows()
        return [(1,)]

    conn = fakedb.Connection(responder)
    counts: Counter = Counter()
    _process_event(
        conn, _fetch(_pages()), counts, 7, "UFC Fight Night: Fiziev vs. Torres", dry_run=True
    )
    assert fakedb.mutating_statements(conn) == []
    assert counts["bouts_parsed"] == 4

"""Fase 5 / BE2: official weigh-in results from ufc.com news articles.

Article discovery via ufc.com's server-rendered /search (anchors to /news/
slugs like "official-weigh-in-results-fight-night-fiziev-torres-baku", token-
guarded like wiki_bonuses), body parsing of the <h4> bout lines inside
.field--name-text blocks, corner matching at IDENTITY_THRESHOLD and the
ON CONFLICT (fight_id, fighter_id) DO UPDATE + COALESCE write policy.

HTML inline (structure copied from real 2026-06/07 fetches) + fakedb;
no network, no real DB.
"""

from collections import Counter

from src.scrapers.weigh_ins import (
    EventFight,
    ParsedWeighIn,
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

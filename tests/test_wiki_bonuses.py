"""BE3 (Fase 4): Wikipedia bonus awards (FOTN/POTN) — title resolution, section
parsing, fight/fighter matching and the SQL emitted into fight_bonuses.

The JSON fixtures mirror real MediaWiki API responses (formatversion=2) and the
HTML mirrors a real article's "Bonus awards" section (mw-heading wrapper, bold
runs, footnote refs). No network, no DB: fetch_json is injected and writes go
through the shared fakedb recorder.
"""

from src.scrapers import wiki_bonuses
from src.scrapers.wiki_bonuses import (
    EventFight,
    ParsedBonus,
    match_fotn_fight,
    match_potn_fighter,
    parse_bonus_awards,
    resolve_wiki_title,
)

# ------------------------------------------------------------------- fixtures

EVENT_NAME = "UFC 328: Chimaev vs. Strickland"

BONUS_HTML = """
<div class="mw-parser-output">
<div class="mw-heading mw-heading2"><h2 id="Bonus_awards">Bonus awards</h2></div>
<p>The following fighters received $50,000 bonuses.</p>
<ul>
<li><b>Fight of the Night:</b> <b>Khamzat Chimaev</b> vs. <b>Sean Strickland</b><sup>[a]</sup></li>
<li><b>Performance of the Night:</b> <b>Bo Nickal</b> and <b>Carlos Prates</b></li>
</ul>
<div class="mw-heading mw-heading2"><h2 id="Aftermath">Aftermath</h2></div>
<ul><li>Not a bonus line</li></ul>
</div>
"""

NO_BONUS_HTML = """
<div class="mw-parser-output">
<div class="mw-heading mw-heading2"><h2 id="Results">Results</h2></div>
<ul><li>Some result line</li></ul>
</div>
"""


def make_fetch_json(pages=None, search_results=(), page_html=None):
    """MediaWiki API stub: exact-title lookups against ``pages`` (a mapping of
    requested title -> resolved title, i.e. redirects already followed),
    ``list=search`` returning ``search_results`` titles, and ``action=parse``
    serving ``page_html`` per title. Records every call."""
    pages = pages or {}
    page_html = page_html or {}
    calls: list[dict] = []

    def fetch_json(params: dict) -> dict:
        calls.append(dict(params))
        if params.get("action") == "parse":
            return {"parse": {"title": params["page"], "text": page_html.get(params["page"], "")}}
        if params.get("list") == "search":
            return {"query": {"search": [{"title": t} for t in search_results]}}
        title = params["titles"]
        if title in pages:
            return {"query": {"pages": [{"pageid": 1, "title": pages[title]}]}}
        return {"query": {"pages": [{"title": title, "missing": True}]}}

    fetch_json.calls = calls
    return fetch_json


# ------------------------------------------------------------ title resolution


def test_resolve_title_exact_hit_follows_redirects():
    fetch = make_fetch_json(pages={EVENT_NAME: "UFC 328"})
    assert resolve_wiki_title(fetch, EVENT_NAME) == "UFC 328"
    assert len(fetch.calls) == 1
    assert fetch.calls[0]["redirects"] == "1"


def test_resolve_title_trims_subtitle_when_exact_misses():
    fetch = make_fetch_json(pages={"UFC 328": "UFC 328"})
    assert resolve_wiki_title(fetch, EVENT_NAME) == "UFC 328"
    assert [c["titles"] for c in fetch.calls] == [EVENT_NAME, "UFC 328"]


def test_resolve_title_search_fallback_validates_tokens():
    # Neither exact nor trimmed exists; search returns noise before the right
    # article. "Khamzat Chimaev" shares one surname token (not enough) and
    # "UFC 327" carries a contradicting card number (no overlap) -> only
    # "UFC 328" (tokens a subset of the event's) is accepted.
    fetch = make_fetch_json(
        search_results=["Khamzat Chimaev", "UFC 327: Other vs. Guy", "UFC 328"],
    )
    assert resolve_wiki_title(fetch, EVENT_NAME) == "UFC 328"


def test_resolve_title_search_accepts_headliner_surname_overlap():
    # Wikipedia titles fight nights by headliners; two shared surnames accept.
    fetch = make_fetch_json(search_results=["UFC Fight Night: Sterling vs. Zalal"])
    assert (
        resolve_wiki_title(fetch, "UFC Fight Night: Sterling vs Zalal")
        == "UFC Fight Night: Sterling vs. Zalal"
    )


def test_resolve_title_returns_none_when_nothing_validates():
    fetch = make_fetch_json(search_results=["List of UFC events", "Dana White"])
    assert resolve_wiki_title(fetch, EVENT_NAME) is None


# -------------------------------------------------------------------- parsing


def test_parse_bonus_awards_fotn_and_multiple_potn():
    bonuses = parse_bonus_awards(BONUS_HTML)
    assert bonuses == [
        ParsedBonus("FOTN", ("Khamzat Chimaev", "Sean Strickland")),
        ParsedBonus("POTN", ("Bo Nickal",)),
        ParsedBonus("POTN", ("Carlos Prates",)),
    ]


def test_parse_bonus_awards_potn_comma_and_list():
    html = """
    <h2>Bonus awards</h2>
    <ul>
      <li>Performance of the Night: Alpha One, Bravo Two and Charlie Three</li>
    </ul>
    """
    bonuses = parse_bonus_awards(html)
    assert [b.names[0] for b in bonuses] == ["Alpha One", "Bravo Two", "Charlie Three"]
    assert {b.bonus_type for b in bonuses} == {"POTN"}


def test_parse_bonus_awards_without_section_returns_empty():
    assert parse_bonus_awards(NO_BONUS_HTML) == []


def test_parse_bonus_awards_ignores_unknown_labels():
    html = """
    <h2>Bonus awards</h2>
    <ul>
      <li>Crowd Favorite: Somebody Else</li>
      <li>Fight of the Night: A Person vs. B Person</li>
    </ul>
    """
    bonuses = parse_bonus_awards(html)
    assert bonuses == [ParsedBonus("FOTN", ("A Person", "B Person"))]


# ------------------------------------------------------------------- matching


_FIGHTS = [
    EventFight(101, 1, 2, "Khamzat Chimaev", "Sean Strickland"),
    EventFight(102, 3, 4, "Bo Nickal", "Carlos Prates"),
    EventFight(103, None, 6, None, "Linked Blue"),  # historical row, red unlinked
]


def test_match_fotn_fight_either_corner_order():
    assert match_fotn_fight(_FIGHTS, "Khamzat Chimaev", "Sean Strickland") == 101
    assert match_fotn_fight(_FIGHTS, "Sean Strickland", "Khamzat Chimaev") == 101


def test_match_fotn_accepts_diacritic_drift_at_identity_threshold():
    fights = [EventFight(200, 9, 8, "Jiri Prochazka", "Petr Yan")]
    # fold() strips accents -> exact after folding, well above 0.92.
    assert match_fotn_fight(fights, "Jiří Procházka", "Petr Yan") == 200


def test_match_fotn_rejects_wrong_names():
    assert match_fotn_fight(_FIGHTS, "Khamzat Chimaev", "Bo Nickal") is None
    # 0.92 identity cutoff: a different person never matches.
    fights = [EventFight(200, 9, 8, "Joe Smith", "Petr Yan")]
    assert match_fotn_fight(fights, "John Smith", "Petr Yan") is None


def test_match_potn_fighter_resolves_corner_id():
    assert match_potn_fighter(_FIGHTS, "Bo Nickal") == 3
    assert match_potn_fighter(_FIGHTS, "Carlos Prates") == 4


def test_match_potn_skips_unlinked_corner():
    # Fight 103's blue corner is linked (id 6) but a name matching an UNLINKED
    # corner must never be guessed.
    assert match_potn_fighter(_FIGHTS, "Linked Blue") == 6
    assert match_potn_fighter(_FIGHTS, "Somebody Unknown") is None


def test_match_applies_scraped_name_alias():
    # Wikipedia says "Beatriz Mesquita"; the DB stores "Bia Mesquita"
    # (fold_ratio 0.79 < 0.92) -> only the NAME_ALIASES entry can bridge it.
    fights = [EventFight(300, 31, 32, "Bia Mesquita", "Some Opponent")]
    assert match_potn_fighter(fights, "Beatriz Mesquita") == 31
    assert match_fotn_fight(fights, "Beatriz Mesquita", "Some Opponent") == 300


def test_alias_is_additive_and_leaves_other_names_untouched():
    # A DB that stores the LONG form still matches the literal scraped name
    # (the alias is tried in addition, never instead).
    fights = [EventFight(301, 41, 42, "Beatriz Mesquita", "Some Opponent")]
    assert match_potn_fighter(fights, "Beatriz Mesquita") == 41
    # Names without an alias keep matching exactly as before.
    assert match_potn_fighter(_FIGHTS, "Bo Nickal") == 3
    assert match_potn_fighter(_FIGHTS, "Bia Mesquita") is None


# ---------------------------------------------------------------- db pipeline


_EVENT_ROWS = [(10, EVENT_NAME)]
# _get_event_fights row shape: (id, red_id, blue_id, red_name, blue_name)
# (names already COALESCEd with the linked fighters' names in SQL).
_FIGHT_ROWS = [
    (101, 1, 2, "Khamzat Chimaev", "Sean Strickland"),
    (102, 3, 4, "Bo Nickal", "Carlos Prates"),
]


def _responder(event_rows=_EVENT_ROWS, fight_rows=_FIGHT_ROWS, insert_result=((1,),)):
    def responder(sql, params=None):
        flat = " ".join(sql.split())
        if flat.startswith("SELECT") and "FROM events" in flat:
            if "WHERE id = %s" in flat:
                wanted = [r for r in event_rows if r[0] == params[0]]
                return wanted
            return list(event_rows)
        if flat.startswith("SELECT") and "FROM fights" in flat:
            return list(fight_rows)
        if "INSERT INTO fight_bonuses" in flat:
            return list(insert_result)
        return []

    return responder


def _wiki_fetch(html=BONUS_HTML):
    return make_fetch_json(pages={EVENT_NAME: EVENT_NAME}, page_html={EVENT_NAME: html})


def _bonus_inserts(conn):
    return [
        (" ".join(sql.split()), params)
        for cur in conn.cursors
        for sql, params in cur.executed
        if "INSERT INTO fight_bonuses" in sql
    ]


def test_backfill_inserts_fotn_and_potn_rows(fakedb):
    conn = fakedb.Connection(_responder())
    counts = wiki_bonuses.backfill(conn, _wiki_fetch())
    assert counts["events_targeted"] == 1
    assert counts["events_with_bonuses"] == 1
    assert counts["bonuses_parsed"] == 3
    assert counts["bonuses_inserted"] == 3
    assert counts["bonuses_unmatched"] == 0
    inserts = _bonus_inserts(conn)
    # FOTN carries fight_id (fighter_id NULL); POTN one row per fighter_id.
    assert [p for _sql, p in inserts] == [
        (10, "FOTN", 101, None),
        (10, "POTN", None, 3),
        (10, "POTN", None, 4),
    ]
    for sql, _p in inserts:
        assert "ON CONFLICT DO NOTHING" in sql  # idempotent re-runs
    assert conn.commits == 1  # one commit per event


def test_backfill_event_without_bonus_section_writes_nothing(fakedb):
    conn = fakedb.Connection(_responder())
    counts = wiki_bonuses.backfill(conn, _wiki_fetch(html=NO_BONUS_HTML))
    assert counts["no_bonus_section"] == 1
    assert counts["bonuses_parsed"] == 0
    assert fakedb.mutating_statements(conn) == []


def test_backfill_unmatched_bonus_is_counted_not_written(fakedb):
    # The article names fighters that do not match any corner of the event.
    html = """
    <h2>Bonus awards</h2>
    <ul>
      <li>Fight of the Night: Nobody Known vs. Also Unknown</li>
      <li>Performance of the Night: Bo Nickal</li>
    </ul>
    """
    conn = fakedb.Connection(_responder())
    counts = wiki_bonuses.backfill(conn, _wiki_fetch(html=html))
    assert counts["bonuses_unmatched"] == 1
    assert counts["bonuses_inserted"] == 1  # the POTN that did match
    assert [p for _sql, p in _bonus_inserts(conn)] == [(10, "POTN", None, 3)]


def test_backfill_matches_bonus_through_name_alias(fakedb):
    # End to end: the article awards "Beatriz Mesquita" a POTN and the event's
    # fight stores her as "Bia Mesquita" -> the alias resolves the fighter_id.
    html = """
    <h2>Bonus awards</h2>
    <ul>
      <li>Performance of the Night: Beatriz Mesquita</li>
    </ul>
    """
    fight_rows = [(150, 51, 52, "Bia Mesquita", "Some Opponent")]
    conn = fakedb.Connection(_responder(fight_rows=fight_rows))
    counts = wiki_bonuses.backfill(conn, _wiki_fetch(html=html))
    assert counts["bonuses_unmatched"] == 0
    assert counts["bonuses_inserted"] == 1
    assert [p for _sql, p in _bonus_inserts(conn)] == [(10, "POTN", None, 51)]


def test_backfill_dry_run_writes_nothing(fakedb):
    conn = fakedb.Connection(_responder())
    counts = wiki_bonuses.backfill(conn, _wiki_fetch(), dry_run=True)
    assert counts["bonuses_parsed"] == 3
    assert counts["bonuses_inserted"] == 0
    assert fakedb.mutating_statements(conn) == []
    assert conn.commits == 0
    assert conn.rollbacks == 1  # read-only snapshot released


def test_backfill_event_id_targets_that_event_only(fakedb):
    conn = fakedb.Connection(_responder())
    counts = wiki_bonuses.backfill(conn, _wiki_fetch(), event_id=10)
    assert counts["events_targeted"] == 1
    # Single-event mode looks the event up by id (idempotent re-run for a card
    # that already has bonuses), never by the has-no-bonuses lookback query.
    first_sql = " ".join(conn.cursors[0].executed[0][0].split())
    assert "WHERE id = %s" in first_sql
    assert counts["bonuses_inserted"] == 3


def test_target_query_shape(fakedb):
    conn = fakedb.Connection(_responder())
    rows = wiki_bonuses._get_target_events(conn, 30)
    sql, params = conn.cursors[0].executed[0]
    flat = " ".join(sql.split())
    assert "e.status = 'completed'" in flat
    assert "e.event_date >= CURRENT_DATE - %s" in flat
    # Events that already have bonuses are skipped (idempotent lookback).
    assert "NOT EXISTS" in flat and "fight_bonuses" in flat
    assert params == (30,)
    assert rows == [(10, EVENT_NAME)]


def test_event_fights_query_coalesces_names_from_linked_fighters(fakedb):
    conn = fakedb.Connection(_responder())
    wiki_bonuses._get_event_fights(conn, 10)
    flat = " ".join(conn.cursors[0].executed[0][0].split())
    # Historical ufcstats rows have NULL *_name columns: the linked fighters'
    # names must back them up so bonuses also match on completed imports.
    assert "COALESCE(fi.fighter_red_name, fr.name)" in flat
    assert "COALESCE(fi.fighter_blue_name, fb.name)" in flat

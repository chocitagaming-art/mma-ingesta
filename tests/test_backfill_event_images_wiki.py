"""Wikipedia poster fallback: the conservative title-match guard, infobox poster
extraction (de-thumb + reject icons/off-site), and the dry-run/apply loop.
No network, no DB."""

from datetime import date

from src.scrapers.backfill_event_images_wiki import (
    _dethumb,
    _headliner_surnames,
    _sequel_ordinal,
    poster_from_infobox,
    run,
    title_matches,
)

_POSTER = "https://upload.wikimedia.org/wikipedia/en/b/bd/UFC_FOX_KC.jpg"
_THUMB = (
    "https://upload.wikimedia.org/wikipedia/en/thumb/d/d7/"
    "Covington_vs._Woodley.jpeg/250px-Covington_vs._Woodley.jpeg"
)
_THUMB_ORIGINAL = (
    "https://upload.wikimedia.org/wikipedia/en/d/d7/Covington_vs._Woodley.jpeg"
)


# ---------------------------------------------------------------- title_matches


def test_title_matches_numbered_requires_exact_number():
    assert title_matches("UFC 100", "UFC 100") is True
    assert title_matches("UFC 100", "UFC 100 (disambiguation)") is True
    assert title_matches("UFC 100", "UFC 1000") is False
    assert title_matches("UFC 100", "UFC 200") is False


def test_title_matches_requires_two_headliner_tokens():
    assert title_matches(
        "UFC on FOX: Johnson vs. Reis", "UFC on Fox: Johnson vs. Reis"
    ) is True
    # Only one shared surname (a fighter's own article) -> rejected.
    assert title_matches("UFC on FOX: Johnson vs. Reis", "Demetrious Johnson") is False


def test_title_matches_rejects_unrelated_and_generic():
    assert title_matches("UFC Fight Night: Smith vs. Clark", "UFC Fight Night") is False
    assert title_matches("UFC Fight Night: Smith vs. Clark", "Anderson Silva") is False


def test_title_matches_multiword_surname_not_confused():
    # 'Dos Santos' shares two raw tokens with any other Dos Santos card, but the
    # discriminating surname (Lewis vs Blaydes) must still be present.
    assert title_matches(
        "UFC Fight Night: Blaydes vs. Dos Santos", "UFC Fight Night: Blaydes vs. dos Santos"
    ) is True
    assert title_matches(
        "UFC Fight Night: Lewis vs. Dos Santos", "UFC Fight Night: Blaydes vs. dos Santos"
    ) is False


def test_title_matches_rejects_fighter_article():
    # A fighter's own article (no 'UFC' in title) must never be taken as a poster.
    assert title_matches("UFC on FOX: Dos Santos vs Miocic", "Junior dos Santos") is False


def test_title_matches_sequel_ordinal_must_agree():
    assert title_matches(
        "UFC Fight Night: Condit vs Kampmann 2", "UFC Fight Night: Condit vs. Kampmann 2"
    ) is True
    # The original (no '2') must not take the rematch poster, and vice versa.
    assert title_matches(
        "UFC Fight Night: Condit vs Kampmann", "UFC Fight Night: Condit vs. Kampmann 2"
    ) is False
    assert title_matches(
        "UFC Fight Night: Belfort vs Henderson 2", "UFC Fight Night: Belfort vs. Henderson 3"
    ) is False


def test_title_matches_rejects_identical_surnames():
    # Both fighters nicknamed 'Cowboy' -> cannot be told apart from another Cowboy card.
    assert title_matches(
        "UFC Fight Night: Cowboy vs Cowboy", "UFC Fight Night: Cowboy vs. Gaethje"
    ) is False


def test_title_matches_non_vs_card_is_unmatchable():
    # 'Fight for the Troops' has no 'X vs Y' headliner to disambiguate the trilogy.
    assert title_matches(
        "UFC Fight Night: Fight for the Troops 2", "UFC: Fight for the Troops 3"
    ) is False


def test_title_matches_roman_and_arabic_sequel_agree():
    # 'Barao II' (roman) must match the article's 'Barão 2' (arabic), surname 'barao'.
    assert title_matches(
        "UFC on FOX: Dillashaw vs. Barao II", "UFC on Fox: Dillashaw vs. Barão 2"
    ) is True


def test_title_matches_event_article_without_ufc_prefix():
    # Some legit event articles omit 'UFC' in the title; surnames+ordinal still match.
    assert title_matches(
        "Ortiz vs Shamrock 3: The Final Chapter", "Ortiz vs. Shamrock 3: The Final Chapter"
    ) is True


def test_sequel_ordinal():
    assert _sequel_ordinal("UFC Fight Night: Condit vs Kampmann 2") == 2
    assert _sequel_ordinal("Belfort vs. Henderson III") == 3
    assert _sequel_ordinal("UFC Fight Night: Condit vs Kampmann") is None
    assert _sequel_ordinal("UFC Fight Night 31") is None  # not a sequel ordinal


def test_headliner_surnames():
    assert _headliner_surnames("UFC Fight Night: Lewis vs. Dos Santos") == ["lewis", "santos"]
    assert _headliner_surnames("UFC on Fox: Velasquez vs. dos Santos") == ["velasquez", "santos"]
    assert _headliner_surnames("UFC on FOX: Dillashaw vs. Barao II") == ["dillashaw", "barao"]
    assert _headliner_surnames("UFC Fight Night: Fight for the Troops 2") == []


# ----------------------------------------------------------------- _dethumb


def test_dethumb_turns_thumbnail_into_file():
    assert _dethumb(_THUMB) == _THUMB_ORIGINAL


def test_dethumb_leaves_plain_url():
    assert _dethumb(_POSTER) == _POSTER


# ------------------------------------------------------------ poster_from_infobox


def _infobox(img_html: str) -> str:
    return f'<html><body><table class="infobox"><tbody><tr><td>{img_html}</td></tr></tbody></table></body></html>'


def test_poster_from_infobox_returns_dethumbed_file():
    html = _infobox(f'<img src="{_THUMB}" width="250" height="366">')
    assert poster_from_infobox(html) == _THUMB_ORIGINAL


def test_poster_from_infobox_accepts_plain_file():
    html = _infobox(f'<img src="{_POSTER}" width="220">')
    assert poster_from_infobox(html) == _POSTER


def test_poster_from_infobox_protocol_relative_src():
    html = _infobox('<img src="//upload.wikimedia.org/wikipedia/en/a/ab/Card.jpg" width="250">')
    assert poster_from_infobox(html) == "https://upload.wikimedia.org/wikipedia/en/a/ab/Card.jpg"


def test_poster_from_infobox_rejects_svg_flag_icon():
    html = _infobox('<img src="//upload.wikimedia.org/wikipedia/commons/a/a4/Flag_of_the_United_States.svg" width="23">')
    assert poster_from_infobox(html) is None


def test_poster_from_infobox_rejects_tiny_icon():
    html = _infobox(f'<img src="{_POSTER}" width="22" height="15">')
    assert poster_from_infobox(html) is None


def test_poster_from_infobox_rejects_offsite_image():
    html = _infobox('<img src="https://example.com/not-wikimedia.jpg" width="250">')
    assert poster_from_infobox(html) is None


def test_poster_from_infobox_none_without_infobox():
    assert poster_from_infobox("<html><body><img src='x.jpg'></body></html>") is None


# ------------------------------------------------------------------------- run


def _rows_responder(rows, update_rows=None):
    def responder(sql, params=None):
        upper = sql.upper()
        if upper.strip().startswith("SELECT"):
            return rows
        if "UPDATE" in upper:
            return update_rows if update_rows is not None else [(1,)]
        return []

    return responder


_ROW_A = (667, "UFC on FOX: Johnson vs. Reis", None, None, date(2017, 4, 15))
_ROW_B = (668, "UFC Fight Night: Lewis vs. Hunt", None, None, date(2017, 6, 11))


def test_run_apply_writes_resolved(fakedb):
    conn = fakedb.Connection(_rows_responder([_ROW_A], update_rows=[(1,)]))
    counts = run(conn, apply=True, resolve=lambda ev: _POSTER)
    assert counts["targets"] == 1
    assert counts["resolved"] == 1
    assert counts["written"] == 1
    assert conn.commits == 1
    muts = fakedb.mutating_statements(conn)
    assert len(muts) == 1
    assert "image_url IS NULL OR image_url = ''" in muts[0]


def test_run_dry_run_writes_nothing(fakedb):
    conn = fakedb.Connection(_rows_responder([_ROW_A]))
    counts = run(conn, apply=False, resolve=lambda ev: _POSTER)
    assert counts["resolved"] == 1
    assert counts["written"] == 0
    assert fakedb.mutating_statements(conn) == []
    assert conn.commits == 0


def test_run_counts_unresolved_gap(fakedb):
    conn = fakedb.Connection(_rows_responder([_ROW_A, _ROW_B]))
    counts = run(conn, apply=True, resolve=lambda ev: None)
    assert counts["no_image"] == 2
    assert counts["resolved"] == 0
    assert counts["written"] == 0


def test_run_resolve_error_is_counted_not_fatal(fakedb):
    conn = fakedb.Connection(_rows_responder([_ROW_A, _ROW_B], update_rows=[(1,)]))
    seen = []

    def resolve(ev):
        seen.append(ev.id)
        if ev.id == 667:
            raise RuntimeError("boom")
        return _POSTER

    counts = run(conn, apply=True, resolve=resolve)
    assert seen == [667, 668]
    assert counts["errors"] == 1
    assert counts["written"] == 1


def test_run_apply_commits_even_on_zero_row_update(fakedb):
    conn = fakedb.Connection(_rows_responder([_ROW_A], update_rows=[]))
    counts = run(conn, apply=True, resolve=lambda ev: _POSTER)
    assert counts["resolved"] == 1
    assert counts["written"] == 0
    assert conn.commits == 1


def test_run_limit_and_record_sink(fakedb):
    rows = [_ROW_A, _ROW_B, (669, "UFC 100", None, None, date(2009, 7, 11))]
    conn = fakedb.Connection(_rows_responder(rows))
    sink: list = []
    seen: list = []
    run(conn, apply=False, resolve=lambda ev: seen.append(ev.id) or _POSTER, limit=2, record_sink=sink)
    assert len(seen) == 2
    assert len(sink) == 2
    assert all(r["resolved"] for r in sink)

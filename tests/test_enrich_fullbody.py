"""Phase 2 (full-body/leg-reach/gym) parsing + backfill tests.

The HTML fixtures mirror a real ufc.com/athlete page: the full-body photo lives
in div.hero-profile__image-wrap > img.hero-profile__image[src] (URL NOT
constructible a priori) and the bio block exposes 'Leg reach' / 'Trains at' in
div.c-bio__field ('Age' comes nested in div.field--name-age and is ignored).
No network, no DB: resolve_athlete gets a fake session and writes go through the
shared fakedb recorder.
"""

from src.scrapers import enrich_fullbody
from src.scrapers.enrich_photos_ufc import (
    AthleteData,
    _extract_bio_fields,
    _extract_full_body,
    _extract_headshot,
    resolve_athlete,
)
from src.scrapers.repositories.fighters import update_fighter_enrichment

# ------------------------------------------------------------------- fixtures

_BIO_BLOCK = """
<div class="c-bio__field">
  <div class="c-bio__label">Height</div>
  <div class="c-bio__text">67.00</div>
</div>
<div class="c-bio__field">
  <div class="c-bio__label">Reach</div>
  <div class="c-bio__text">70.50</div>
</div>
<div class="c-bio__field">
  <div class="c-bio__label">Leg reach</div>
  <div class="c-bio__text">41.50</div>
</div>
<div class="c-bio__field">
  <div class="c-bio__label">Trains at</div>
  <div class="c-bio__text">Millennia MMA, Rancho, CA</div>
</div>
<div class="c-bio__field">
  <div class="c-bio__label">Age</div>
  <div class="c-bio__text"><div class="field field--name-age">32</div></div>
</div>
<div class="c-bio__field">
  <div class="c-bio__label">Place of Birth</div>
  <div class="c-bio__text">Chone, Ecuador</div>
</div>
"""

# Representative athlete page: hero full-body image (relative src, as served) +
# a headshot-style image elsewhere (the current headshot pipeline's pick).
ATHLETE_HTML = f"""
<html><head><title>Marlon Vera | UFC</title></head><body>
<div class="hero-profile">
  <div class="hero-profile__image-wrap">
    <img class="hero-profile__image"
         src="/images/styles/athlete_bio_full_body/s3/2025-04/VERA_MARLON_L_04-12.png?itok=abc123"
         alt="Marlon Vera">
  </div>
  <p class="hero-profile__division-body">23-9-1 (W-L-D)</p>
</div>
<img src="/images/styles/event_results_athlete_headshot/s3/2025-04/VERA_MARLON_L_04-12_HS.png?itok=hs1">
{_BIO_BLOCK}
</body></html>
"""

# Variant: UFC serves a generic silhouette when the fighter has no real photo.
PLACEHOLDER_HTML = f"""
<html><body>
<div class="hero-profile__image-wrap">
  <img class="hero-profile__image"
       src="/images/styles/athlete_bio_full_body/s3/silhouette-full-body.png"
       alt="No photo">
</div>
{_BIO_BLOCK}
</body></html>
"""


class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code
        self.ok = status_code < 400


class _FakeSession:
    def __init__(self, html: str):
        self._html = html
        self.requested: list[str] = []

    def get(self, url, headers=None, timeout=None):
        self.requested.append(url)
        return _FakeResponse(self._html)


# ------------------------------------------------------------ full-body photo


def test_full_body_url_extracted_and_normalized_from_relative_src():
    url = _extract_full_body(ATHLETE_HTML, "Marlon Vera")
    assert url == (
        "https://www.ufc.com/images/styles/athlete_bio_full_body/s3/2025-04/"
        "VERA_MARLON_L_04-12.png?itok=abc123"
    )


def test_full_body_url_normalizes_host_without_www():
    html = ATHLETE_HTML.replace(
        'src="/images/styles/athlete_bio_full_body',
        'src="https://ufc.com/images/styles/athlete_bio_full_body',
    )
    url = _extract_full_body(html, "Marlon Vera")
    assert url is not None
    assert url.startswith("https://www.ufc.com/images/styles/athlete_bio_full_body/")


def test_full_body_url_normalizes_protocol_relative_src():
    html = ATHLETE_HTML.replace(
        'src="/images/styles/athlete_bio_full_body',
        'src="//www.ufc.com/images/styles/athlete_bio_full_body',
    )
    url = _extract_full_body(html, "Marlon Vera")
    assert url is not None
    assert url.startswith("https://www.ufc.com/images/styles/athlete_bio_full_body/")


def test_full_body_placeholder_silhouette_rejected():
    assert _extract_full_body(PLACEHOLDER_HTML, "Marlon Vera") is None


def test_full_body_falls_back_to_named_candidate_when_hero_missing():
    # No hero <img>, but the page still embeds the athlete's own bio_full_body
    # URL elsewhere; an opponent's file must NOT be picked.
    html = """
    <img src="/images/styles/athlete_bio_full_body/s3/2025-01/SMITH_OTHER_L.png">
    <img src="/images/styles/athlete_bio_full_body/s3/2025-01/VERA_MARLON_R.png">
    """
    url = _extract_full_body(html, "Marlon Vera")
    assert url == "https://www.ufc.com/images/styles/athlete_bio_full_body/s3/2025-01/VERA_MARLON_R.png"


# ------------------------------------------------------------------ bio fields


def test_leg_reach_inches_converted_to_cm():
    session = _FakeSession(ATHLETE_HTML)
    data = resolve_athlete(session, "Marlon Vera")
    assert data is not None
    assert data.leg_reach_cm == 105.4  # 41.50 in * 2.54


def test_trains_at_extracted():
    fields = _extract_bio_fields(ATHLETE_HTML)
    assert fields["trains at"] == "Millennia MMA, Rancho, CA"


def test_nested_age_field_does_not_pollute_bio_parsing():
    # 'Age' is nested inside div.field--name-age (no flat text); it must not
    # steal a neighbouring field's value.
    fields = _extract_bio_fields(ATHLETE_HTML)
    assert fields["leg reach"] == "41.50"
    assert fields.get("age", "") == ""


def test_resolve_athlete_fills_new_fields_and_keeps_headshot():
    session = _FakeSession(ATHLETE_HTML)
    data = resolve_athlete(session, "Marlon Vera")
    assert data is not None
    # New Phase-2 fields.
    assert data.full_body_url and "athlete_bio_full_body" in data.full_body_url
    assert data.leg_reach_cm == 105.4
    assert data.trains_at == "Millennia MMA, Rancho, CA"
    # Existing pipeline untouched: headshot + record + measures still resolve.
    assert data.headshot_url == (
        "https://www.ufc.com/images/styles/event_results_athlete_headshot/s3/2025-04/"
        "VERA_MARLON_L_04-12_HS.png?itok=hs1"
    )
    assert (data.wins, data.losses, data.draws) == (23, 9, 1)
    assert data.reach_cm == 179.1  # 70.50 in
    assert data.nationality == "Ecuador"


def test_headshot_extraction_unchanged_by_new_parsing():
    url = _extract_headshot(ATHLETE_HTML, "Marlon Vera")
    assert url is not None
    assert "event_results_athlete_headshot" in url


# ------------------------------------------------- repository: never NULL over data


def test_update_fighter_enrichment_guards_new_columns_with_coalesce(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [])
    update_fighter_enrichment(
        conn, 7, full_body_url="https://www.ufc.com/x.png", leg_reach_cm=105.4, trains_at="Gym"
    )
    sql = " ".join(fakedb.mutating_statements(conn)[0].split())
    # SET only fills empty columns: a NULL argument can never replace a value.
    assert "full_body_url = COALESCE(NULLIF(full_body_url, ''), %s)" in sql
    assert "leg_reach_cm = COALESCE(leg_reach_cm, %s)" in sql
    assert "trains_at = COALESCE(NULLIF(trains_at, ''), %s)" in sql
    # The row only matches when the column is empty AND the new value is NOT NULL.
    assert "(NULLIF(full_body_url, '') IS NULL AND %s IS NOT NULL)" in sql
    assert "(leg_reach_cm IS NULL AND %s IS NOT NULL)" in sql
    assert "(NULLIF(trains_at, '') IS NULL AND %s IS NOT NULL)" in sql


def test_update_with_all_null_new_values_updates_nothing(fakedb):
    # rowcount 0 simulates the WHERE guard matching no row (every "%s IS NOT
    # NULL" arm is false when all new values are NULL) -> nothing overwritten.
    conn = fakedb.Connection(lambda sql, params=None: [])
    assert update_fighter_enrichment(conn, 7) is False
    _, params = conn.cursors[0].executed[0]
    # Everything except the fighter id is NULL -> the WHERE guard can't match.
    assert params.count(None) == len(params) - 1
    assert 7 in params


# ------------------------------------------------------------------- backfill


_TARGET_ROWS = [(1, "Marlon Vera"), (2, "Jon Jones")]


def _responder(update_result):
    def responder(sql, params=None):
        flat = " ".join(sql.split())
        if flat.startswith("SELECT id, name FROM fighters"):
            return _TARGET_ROWS
        if "UPDATE fighters" in flat:
            return update_result  # length simulates rowcount
        return []

    return responder


def _resolved(_session, _name):
    return AthleteData(
        full_body_url="https://www.ufc.com/images/styles/athlete_bio_full_body/s3/x.png",
        leg_reach_cm=105.4,
        trains_at="Millennia MMA, Rancho, CA",
    )


def test_backfill_dry_run_writes_nothing(fakedb):
    conn = fakedb.Connection(_responder(update_result=[(1,)]))
    counts = enrich_fullbody.backfill(
        conn, dry_run=True, resolver=_resolved, sleeper=lambda _s: None
    )
    assert counts["targets"] == 2
    assert counts["resolved"] == 2
    assert counts["with_full_body"] == 2
    assert fakedb.mutating_statements(conn) == []
    assert conn.commits == 0


def test_backfill_writes_and_commits_per_updated_fighter(fakedb):
    conn = fakedb.Connection(_responder(update_result=[(1,)]))
    counts = enrich_fullbody.backfill(
        conn, dry_run=False, resolver=_resolved, sleeper=lambda _s: None
    )
    assert counts["updated"] == 2
    assert conn.commits == 2
    updates = fakedb.mutating_statements(conn)
    assert len(updates) == 2
    assert all("COALESCE" in sql for sql in updates)


def test_backfill_skips_update_when_page_has_no_new_data(fakedb):
    conn = fakedb.Connection(_responder(update_result=[(1,)]))
    counts = enrich_fullbody.backfill(
        conn, dry_run=False, resolver=lambda s, n: AthleteData(), sleeper=lambda _s: None
    )
    assert counts["resolved"] == 2
    assert counts["updated"] == 0
    assert fakedb.mutating_statements(conn) == []


def test_backfill_counts_unresolved_pages(fakedb):
    conn = fakedb.Connection(_responder(update_result=[(1,)]))
    counts = enrich_fullbody.backfill(
        conn, dry_run=False, resolver=lambda s, n: None, sleeper=lambda _s: None
    )
    assert counts["unresolved"] == 2
    assert fakedb.mutating_statements(conn) == []

"""Scraper de gimnasios OSM: filtro (port de gyms.ts), parseo de tags, construcción
de filas y el loop dry-run/apply con poda. Sin red ni BD (fakedb)."""

from datetime import datetime, timezone

from src.scrapers import gyms_osm
from src.scrapers.gyms_osm import (
    build_gym_row,
    build_rows,
    compose_address,
    first_osm_value,
    has_combat_discipline,
    is_excluded_gym,
    normalize_website,
    parse_sport_tokens,
)

RUN_START = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ── filtro ───────────────────────────────────────────────────────────────────
def test_parse_sport_tokens_splits_and_normalizes():
    assert parse_sport_tokens("Boxing; muay_thai ;MMA") == ["boxing", "muay_thai", "mma"]
    assert parse_sport_tokens("fitness boxing") == ["fitness", "boxing"]
    assert parse_sport_tokens(None) == []


def test_has_combat_discipline():
    assert has_combat_discipline("fitness;boxing") is True
    assert has_combat_discipline("jiu-jitsu") is True
    assert has_combat_discipline("fitness;yoga") is False


def test_is_excluded_gym_keeps_combat_even_with_fitness():
    # Fix de sobre-exclusión: 'fitness;boxing' SÍ es gimnasio de lucha.
    assert is_excluded_gym("Club de Boxeo Norte", "fitness;boxing") is False
    assert is_excluded_gym("MMA Team", "artes_marciales_mixtas") is False
    assert is_excluded_gym("Academia", "jiu-jitsu") is False


def test_is_excluded_gym_drops_non_combat_and_chains():
    assert is_excluded_gym("Solo Fitness", "fitness") is True
    assert is_excluded_gym("Estudio Zen", "yoga;pilates") is True
    assert is_excluded_gym("Basic-Fit Centro", "boxing") is True
    assert is_excluded_gym("McFIT", "martial_arts") is True


# ── parseo de tags ───────────────────────────────────────────────────────────
def test_first_osm_value():
    assert first_osm_value("+34 91 555;+34 600 111") == "+34 91 555"
    assert first_osm_value(None) is None
    assert first_osm_value(";") is None


def test_normalize_website():
    assert normalize_website("www.gym.es") == "https://www.gym.es"
    assert normalize_website("http://gym.es") == "http://gym.es"
    assert normalize_website(None) is None


def test_compose_address():
    assert compose_address(
        {"addr:street": "Calle Mayor", "addr:housenumber": "5", "addr:postcode": "28013", "addr:city": "Madrid"}
    ) == "Calle Mayor, 5, 28013 Madrid"
    assert compose_address({"addr:city": "Madrid"}) == "Madrid"
    assert compose_address({"name": "X"}) is None


# ── build_gym_row ────────────────────────────────────────────────────────────
def test_build_gym_row_full():
    row = build_gym_row({
        "type": "node", "id": 123, "lat": 40.42, "lon": -3.70,
        "tags": {
            "name": "Club de Lucha", "sport": "boxing;muay_thai;fitness",
            "addr:street": "Calle Mayor", "addr:housenumber": "5", "addr:city": "Madrid",
            "website": "www.club.es", "phone": "+34 600 000 000", "opening_hours": "Mo-Fr 09:00-22:00",
        },
    })
    assert row["osm_id"] == "node/123"
    assert row["sports"] == ["boxing", "muay_thai"]  # 'fitness' descartado
    assert row["address"] == "Calle Mayor, 5, Madrid"
    assert row["city"] == "Madrid"
    assert row["website"] == "https://www.club.es"
    assert row["phone"] == "+34 600 000 000"
    assert row["opening_hours"] == "Mo-Fr 09:00-22:00"


def test_build_gym_row_way_uses_center():
    row = build_gym_row({
        "type": "way", "id": 9, "center": {"lat": 40.41, "lon": -3.71},
        "tags": {"name": "Dojo", "sport": "judo"},
    })
    assert row["osm_id"] == "way/9"
    assert row["sports"] == ["judo"]
    assert row["lat"] == 40.41


def test_build_gym_row_drops_excluded_and_nameless():
    assert build_gym_row({"type": "node", "id": 1, "lat": 40, "lon": -3, "tags": {"sport": "boxing"}}) is None
    assert build_gym_row({
        "type": "node", "id": 2, "lat": 40, "lon": -3, "tags": {"name": "McFIT", "sport": "boxing"}
    }) is None
    assert build_gym_row({
        "type": "node", "id": 3, "lat": 40, "lon": -3, "tags": {"name": "Yoga", "sport": "yoga"}
    }) is None


def test_build_rows_dedupes_by_osm_id():
    el = {"type": "node", "id": 1, "lat": 40, "lon": -3, "tags": {"name": "A", "sport": "boxing"}}
    assert len(build_rows([el, el])) == 1


# ── run ──────────────────────────────────────────────────────────────────────
def _els(n: int) -> list[dict]:
    return [
        {"type": "node", "id": i, "lat": 40 + i * 0.001, "lon": -3, "tags": {"name": f"Gym {i}", "sport": "boxing"}}
        for i in range(n)
    ]


def test_run_dry_run_writes_nothing(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [])
    counts = gyms_osm.run(conn, apply=False, elements=_els(3), run_start=RUN_START)
    assert counts == {"fetched": 3, "kept": 3, "written": 0, "pruned": 0}
    assert fakedb.mutating_statements(conn) == []


def test_run_apply_upserts_and_prunes_when_healthy(fakedb):
    def responder(sql, params=None):
        return [(1,), (1,)] if "DELETE" in sql.upper() else []

    conn = fakedb.Connection(responder)
    counts = gyms_osm.run(conn, apply=True, elements=_els(120), run_start=RUN_START)
    assert counts["kept"] == 120
    assert counts["written"] == 120
    assert counts["pruned"] == 2  # la poda corrió (>= MIN_HEALTHY_COUNT)
    muts = fakedb.mutating_statements(conn)
    assert any("INSERT INTO gyms" in m for m in muts)
    assert any("ON CONFLICT (osm_id)" in m for m in muts)
    assert any("DELETE FROM gyms" in m for m in muts)


def test_run_apply_skips_prune_when_too_few(fakedb):
    called = {"delete": False}

    def responder(sql, params=None):
        if "DELETE" in sql.upper():
            called["delete"] = True
        return []

    conn = fakedb.Connection(responder)
    counts = gyms_osm.run(conn, apply=True, elements=_els(5), run_start=RUN_START)
    assert counts["written"] == 5
    assert counts["pruned"] == 0
    assert called["delete"] is False  # NO poda con volumen bajo (protege la tabla)

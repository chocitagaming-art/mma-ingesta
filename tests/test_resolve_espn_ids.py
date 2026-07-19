"""Tests de la resolución de fighters.espn_id (S3-G): candidatos de la Search
API, regla de ambigüedad (lección Bruno Silva) y guardas de escritura."""

from __future__ import annotations

from src.scrapers.resolve_espn_ids import (
    AMBIGUITY_MARGIN,
    pick_unambiguous_athlete,
    search_athlete_candidates,
)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload):
        self._payload = payload
        self.requested: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None):
        self.requested.append((url, params or {}))
        return _FakeResponse(self._payload)


def search_payload(*players: tuple[str, str, str]) -> dict:
    """players = (uid, displayName, sport)."""
    return {
        "results": [
            {
                "type": "player",
                "contents": [
                    {"uid": uid, "displayName": name, "sport": sport}
                    for uid, name, sport in players
                ],
            },
            {"type": "article", "contents": [{"uid": "a:999", "displayName": "ruido", "sport": "mma"}]},
        ]
    }


class TestSearchCandidates:
    def test_filters_non_mma_and_parses_uid(self):
        session = _FakeSession(search_payload(
            ("s:3301~a:4275020", "Yaroslav Amosov", "mma"),
            ("s:20~a:111", "Yaroslav Amosov", "soccer"),
        ))
        candidates = search_athlete_candidates(session, "Yaroslav Amosov")
        assert [(espn_id, name) for _, espn_id, name in candidates] == [("4275020", "Yaroslav Amosov")]
        assert candidates[0][0] == 1.0

    def test_dedupes_by_athlete_keeping_best_score_and_sorts_desc(self):
        session = _FakeSession(search_payload(
            ("s:3301~a:1", "Bruno Silva", "mma"),
            ("s:3301~a:1", "Bruno Silva Blindado", "mma"),
            ("s:3301~a:2", "Bruno Henrique Silva", "mma"),
        ))
        candidates = search_athlete_candidates(session, "Bruno Silva")
        assert len(candidates) == 2
        assert candidates[0][1] == "1"
        assert candidates[0][0] >= candidates[1][0]


class TestPickUnambiguous:
    def test_empty_and_weak_candidates_are_rejected(self):
        assert pick_unambiguous_athlete([]) is None
        assert pick_unambiguous_athlete([(0.80, "1", "Otro Nombre")]) is None

    def test_single_strong_candidate_is_picked(self):
        assert pick_unambiguous_athlete([(1.0, "4275020", "Yaroslav Amosov")]) == (
            "4275020", "Yaroslav Amosov",
        )

    def test_two_strong_close_candidates_are_ambiguous(self):
        # Dos "Bruno Silva" reales en ESPN: ambos clavan el umbral -> no adivinar.
        assert pick_unambiguous_athlete([
            (1.0, "1", "Bruno Silva"),
            (1.0, "2", "Bruno Silva"),
        ]) is None

    def test_strong_winner_with_distant_second_is_picked(self):
        picked = pick_unambiguous_athlete([
            (1.0, "1", "Jim Miller"),
            (1.0 - AMBIGUITY_MARGIN - 0.01, "2", "Jim Milley"),
        ])
        assert picked == ("1", "Jim Miller")

    def test_duplicate_entries_of_same_athlete_do_not_trigger_ambiguity(self):
        picked = pick_unambiguous_athlete([
            (1.0, "1", "Jim Miller"),
            (1.0, "1", "Jim Miller"),
            (0.5, "3", "Otro"),
        ])
        assert picked == ("1", "Jim Miller")


class TestTargetFightersScope:
    """El scope por defecto (cron) no debe referenciar columnas inexistentes.

    Regresión real: `fighters.created_at` NO existe -> el cron semanal
    `espn-history` petaba con psycopg2 UndefinedColumn en el paso 1
    (resolve_espn_ids) y el import de historial nunca llegaba a correr.
    """

    def test_default_scope_has_no_created_at_and_scopes_upcoming(self, fakedb):
        from src.scrapers.resolve_espn_ids import _get_target_fighters

        conn = fakedb.Connection(lambda sql, params: [(1, "Foo")])
        _get_target_fighters(conn, all_scope=False)
        (sql, _), = conn.cursors[0].executed
        flat = " ".join(sql.split())
        assert "created_at" not in flat  # columna inexistente en `fighters`
        assert "f.espn_id IS NULL" in flat  # solo los que aún no tienen espn_id
        assert "e.status = 'upcoming'" in flat  # scope real: cartelera próxima

    def test_all_scope_has_no_created_at(self, fakedb):
        from src.scrapers.resolve_espn_ids import _get_target_fighters

        conn = fakedb.Connection(lambda sql, params: [(1, "Foo")])
        _get_target_fighters(conn, all_scope=True)
        (sql, _), = conn.cursors[0].executed
        flat = " ".join(sql.split())
        assert "created_at" not in flat
        assert "f.espn_id IS NULL" in flat


class TestSeedAndWriteGuards:
    def test_seed_from_source_ids_sql_is_guarded(self, fakedb):
        from src.scrapers.resolve_espn_ids import seed_from_source_ids

        conn = fakedb.Connection(lambda sql, params: [(1,)])
        seeded = seed_from_source_ids(conn)
        (sql, _), = conn.cursors[0].executed
        flat = " ".join(sql.split())
        assert "f.source = 'espn'" in flat
        assert "f.espn_id IS NULL" in flat
        assert "~ '^[0-9]+$'" in flat  # solo source_id numéricos (athlete ids)
        assert "NOT EXISTS" in flat  # nunca chocar con un espn_id ya asignado
        assert seeded == 1
        assert conn.commits == 1

    def test_set_fighter_espn_id_is_additive_only(self, fakedb):
        from src.scrapers.repositories.fighters import set_fighter_espn_id

        conn = fakedb.Connection(lambda sql, params: [(1,)])
        assert set_fighter_espn_id(conn, 6191, "4275020") is True
        (sql, params), = conn.cursors[0].executed
        assert "espn_id IS NULL" in sql  # un espn_id poblado nunca se pisa
        assert params == ("4275020", 6191)
        assert set_fighter_espn_id(conn, 6191, "") is False  # vacío: no-op

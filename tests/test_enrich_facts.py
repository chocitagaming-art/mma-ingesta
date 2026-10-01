"""Fighter Facts + Q&A (S2-E) parsing, guard, translation-shape and write tests.

The HTML fixtures mirror the two REAL layouts ufc.com serves (verified against
joel-alvarez and yaroslav-amosov on 2026-07-11): facts as ul>li inside
div.field--name-qna-facts, and Q&A inside div.field--name-qna either as ONE
<p> with <strong> questions separated by <br><br> (Joel) or one <p> per pair
(Amosov). No network, no DB: the resolver/translator are injected and writes
go through the shared fakedb recorder.
"""

import pytest
from bs4 import BeautifulSoup

from src.scrapers import enrich_facts
from src.scrapers.enrich_facts import (
    FactsPage,
    _clean_text,
    _identity_verified,
    parse_fighter_facts,
    parse_fighter_qa,
)
from src.scrapers.enrich_fullbody import _unique_nickname_sql
from src.scrapers.repositories.fighters import update_fighter_facts

# ------------------------------------------------------------------- fixtures

FACTS_BLOCK = """
<div class="field field--name-qna-facts">
  <ul>
    <li>Pro since 2013<br>&nbsp;</li>
    <li>The ﬁrst Spaniard to win via submission</li>
    <li>   </li>
    <li>Trains at El Fortin</li>
  </ul>
</div>
"""

# Joel variant: ONE <p>, questions in <strong>, answers between <br><br>.
QNA_JOEL = """
<div class="field field--name-qna">
  <p><strong>When and why did you start training for fighting?</strong> I have been
  fighting professionally since 2013.<br><br><strong>What titles have you held:</strong>
  I am the AFL lightweight champion.<br><br><strong>Favorite superhero?</strong>
  Deadpool (laughs)</p>
</div>
"""

# Amosov variant: one <p> per Q&A pair.
QNA_AMOSOV = """
<div class="field field--name-qna">
  <p><strong>When did you start?</strong> I lived in a fairly crime-ridden area.</p>
  <p><strong>What titles have you held?</strong> Bellator Champion / Tech-Krep champion.</p>
</div>
"""

PAGE_TMPL = """
<html><body>
<h1 class="hero-profile__name">{name}</h1>
<div class="faq-athlete">{facts}{qna}</div>
</body></html>
"""


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


# --------------------------------------------------------------------- parsing


def test_clean_text_folds_ligature_nbsp_and_whitespace():
    assert _clean_text("The ﬁrst  one\n  here") == "The first one here"


def test_parse_facts_cleans_br_nbsp_and_skips_empties():
    facts = parse_fighter_facts(_soup(FACTS_BLOCK))
    assert facts == [
        "Pro since 2013",
        "The first Spaniard to win via submission",
        "Trains at El Fortin",
    ]


def test_parse_facts_absent_container_returns_empty():
    assert parse_fighter_facts(_soup("<div><ul><li>x</li></ul></div>")) == []


def test_parse_qa_joel_variant_single_p_with_br_br():
    pairs = parse_fighter_qa(_soup(QNA_JOEL))
    assert [p["q"] for p in pairs] == [
        "When and why did you start training for fighting?",
        "What titles have you held:",  # question ending in ':' is valid
        "Favorite superhero?",
    ]
    assert pairs[0]["a"] == "I have been fighting professionally since 2013."
    assert pairs[1]["a"] == "I am the AFL lightweight champion."
    assert pairs[2]["a"] == "Deadpool (laughs)"


def test_parse_qa_amosov_variant_one_p_per_pair():
    pairs = parse_fighter_qa(_soup(QNA_AMOSOV))
    assert len(pairs) == 2
    assert pairs[0] == {
        "q": "When did you start?",
        "a": "I lived in a fairly crime-ridden area.",
    }
    assert pairs[1]["a"] == "Bellator Champion / Tech-Krep champion."


def test_parse_qa_absent_container_returns_empty():
    assert parse_fighter_qa(_soup("<p><strong>Q?</strong> A</p>")) == []


def test_parse_qa_question_without_answer_is_dropped():
    pairs = parse_fighter_qa(
        _soup('<div class="field--name-qna"><p><strong>Q1?</strong></p></div>')
    )
    assert pairs == []


# --- endurecimientos de la revisión adversarial (11-jul) ---


def test_parse_qa_html_comments_never_leak_into_answers():
    pairs = parse_fighter_qa(
        _soup(
            '<div class="field--name-qna"><p><strong>Q1?</strong> Real answer.'
            "<!-- THEME DEBUG --> more text<!--[if !supportLists]--></p></div>"
        )
    )
    assert pairs == [{"q": "Q1?", "a": "Real answer. more text"}]


def test_parse_qa_adjacent_split_strongs_are_one_question():
    pairs = parse_fighter_qa(
        _soup(
            '<div class="field--name-qna"><p><strong>When and why did you </strong>'
            "<strong>start training?</strong> I started in 2013.</p></div>"
        )
    )
    assert pairs == [{"q": "When and why did you start training?", "a": "I started in 2013."}]


def test_parse_qa_bold_emphasis_inside_answer_is_not_a_question():
    pairs = parse_fighter_qa(
        _soup(
            '<div class="field--name-qna"><p><strong>What does this sport mean to you?</strong>'
            " It means <strong>everything</strong> to me and my family.</p></div>"
        )
    )
    assert pairs == [
        {"q": "What does this sport mean to you?", "a": "It means everything to me and my family."}
    ]


def test_parse_qa_bold_question_shape_still_starts_new_pair():
    # Una negrita intermedia que SÍ parece pregunta ('...?' / '...:') corta el par.
    pairs = parse_fighter_qa(
        _soup(
            '<div class="field--name-qna"><p><strong>Q1?</strong> A1.'
            "<br><br><strong>Favorite technique:</strong> Kimura</p></div>"
        )
    )
    assert pairs == [
        {"q": "Q1?", "a": "A1."},
        {"q": "Favorite technique:", "a": "Kimura"},
    ]


def test_parse_qa_nested_strong_keeps_full_question_once():
    pairs = parse_fighter_qa(
        _soup(
            '<div class="field--name-qna"><p><strong>Outer <strong>inner</strong> question?</strong>'
            " The answer.</p></div>"
        )
    )
    assert pairs == [{"q": "Outer inner question?", "a": "The answer."}]


def test_parse_qa_condit_style_label_headers_stay_separate_pairs():
    # Estilo legado real (Carlos Condit): strongs-etiqueta sin '?'/':' como
    # cabecera de cada entrada, separados por <br><br> o por <p> — cada uno
    # abre su propio par porque viene tras frontera de bloque.
    pairs = parse_fighter_qa(
        _soup(
            '<div class="field--name-qna">'
            "<p><strong>Fighter facts</strong> Has won nine of his last 17.</p>"
            "<p><strong>UFC 264</strong> (7/10/21) Condit lost a three round decision to Max Griffin"
            "<br><br><strong>UFC on ABC</strong> (1/16/21) Condit won a three round decision over Matt Brown</p>"
            "</div>"
        )
    )
    assert pairs == [
        {"q": "Fighter facts", "a": "Has won nine of his last 17."},
        {"q": "UFC 264", "a": "(7/10/21) Condit lost a three round decision to Max Griffin"},
        {"q": "UFC on ABC", "a": "(1/16/21) Condit won a three round decision over Matt Brown"},
    ]


# ----------------------------------------------------------------------- guard


def test_identity_guard_matches_and_rejects():
    page = FactsPage(facts=[], qa=[], page_name="Joel Alvarez")
    assert _identity_verified("Joel Álvarez", page, ufc_confirmed=False, nickname=None)
    other = FactsPage(facts=[], qa=[], page_name="Bruno Silva")
    assert not _identity_verified("Joel Álvarez", other, ufc_confirmed=False, nickname=None)
    # No hero name: only trusted when the headshot already proved the page.
    anon = FactsPage(facts=[], qa=[], page_name=None)
    assert _identity_verified("Joel Álvarez", anon, ufc_confirmed=True, nickname=None)
    assert not _identity_verified("Joel Álvarez", anon, ufc_confirmed=False, nickname=None)


def test_identity_guard_accepts_hero_name_equal_to_nickname():
    # Caso real fighters.id=9132: ufc.com pinta 'Tina Black' en la página
    # canónica de Valesca Machado. Apodo exacto -> verificada; nada más laxo.
    page = FactsPage(facts=["x"], qa=[], page_name="Tina Black")
    assert _identity_verified("Valesca Machado", page, ufc_confirmed=False, nickname="Tina Black")
    assert _identity_verified("Valesca Machado", page, ufc_confirmed=False, nickname="Tína BLACK")
    assert not _identity_verified("Joe Smith", page, ufc_confirmed=False, nickname=None)
    assert not _identity_verified("Valesca Machado", page, ufc_confirmed=False, nickname="Black")
    assert not _identity_verified(
        "Valesca Machado", page, ufc_confirmed=False, nickname="Tina Black Jr"
    )


def test_identity_guard_without_page_name_ignores_nickname():
    anon = FactsPage(facts=["x"], qa=[], page_name=None)
    assert _identity_verified("Valesca Machado", anon, ufc_confirmed=True, nickname="Tina Black")
    assert not _identity_verified(
        "Valesca Machado", anon, ufc_confirmed=False, nickname="Tina Black"
    )


# ----------------------------------------------------------- translation shape


def _fake_translator(facts, qa):
    return (
        [f"ES {f}" for f in facts],
        [{"q": f"ES {p['q']}", "a": f"ES {p['a']}"} for p in qa],
    )


_JOEL = (1, "Joel Alvarez", None, False)  # (id, name, nickname, ufc_confirmed)


def _responder(update_result=None, target=_JOEL):
    def responder(sql, params=None):
        upper = sql.upper()
        if upper.strip().startswith("SELECT"):
            return [target]
        if "UPDATE" in upper:
            return update_result or []
        return []

    return responder


def _page_with_content():
    return FactsPage(
        facts=["Pro since 2013"],
        qa=[{"q": "Q?", "a": "A."}],
        page_name="Joel Alvarez",
    )


# ------------------------------------------------------------------- backfill


def test_backfill_writes_translated_content_and_commits(fakedb):
    conn = fakedb.Connection(_responder(update_result=[(1,)]))
    counts = enrich_facts.backfill(
        conn,
        translator=_fake_translator,
        resolver=lambda session, name: _page_with_content(),
        sleeper=lambda seconds: None,
    )
    assert counts["updated"] == 1
    assert counts["nickname_match"] == 0  # accepted by NAME
    updates = fakedb.mutating_statements(conn)
    assert len(updates) == 1
    assert "COALESCE(fighter_facts, %s::jsonb)" in updates[0]
    assert "COALESCE(fighter_qa, %s::jsonb)" in updates[0]
    assert "NULLIF" not in updates[0]  # JSONB additive write, no NULLIF
    assert conn.commits == 1
    # The persisted payload is the TRANSLATED one.
    update_params = [
        params for cur in conn.cursors for sql, params in cur.executed if "UPDATE" in sql.upper()
    ][0]
    assert '"ES Pro since 2013"' in update_params[0]
    assert '"ES Q?"' in update_params[1]


def test_backfill_dry_run_writes_nothing(fakedb):
    conn = fakedb.Connection(_responder(update_result=[(1,)]))
    counts = enrich_facts.backfill(
        conn,
        translator=_fake_translator,
        dry_run=True,
        resolver=lambda session, name: _page_with_content(),
        sleeper=lambda seconds: None,
    )
    assert counts["would_update"] == 1
    assert fakedb.mutating_statements(conn) == []
    assert conn.commits == 0
    assert conn.rollbacks == 1


def test_backfill_skips_no_content_pages_without_translating(fakedb):
    conn = fakedb.Connection(_responder())
    calls = []

    def counting_translator(facts, qa):
        calls.append(1)
        return facts, qa

    counts = enrich_facts.backfill(
        conn,
        translator=counting_translator,
        resolver=lambda session, name: FactsPage(facts=[], qa=[], page_name="Joel Alvarez"),
        sleeper=lambda seconds: None,
    )
    assert counts["no_content"] == 1
    assert calls == []  # no tokens spent on empty pages
    assert fakedb.mutating_statements(conn) == []


def test_backfill_name_mismatch_never_writes(fakedb):
    conn = fakedb.Connection(_responder())
    counts = enrich_facts.backfill(
        conn,
        translator=_fake_translator,
        resolver=lambda session, name: FactsPage(
            facts=["x"], qa=[], page_name="Somebody Else Entirely"
        ),
        sleeper=lambda seconds: None,
    )
    assert counts["name_mismatch"] == 1
    assert fakedb.mutating_statements(conn) == []


def test_backfill_writes_facts_for_fighter_published_under_nickname(fakedb, caplog):
    target = (9132, "Valesca Machado", "Tina Black", False)
    conn = fakedb.Connection(_responder(update_result=[(1,)], target=target))
    with caplog.at_level("INFO", logger="src.scrapers.enrich_facts"):
        counts = enrich_facts.backfill(
            conn,
            translator=_fake_translator,
            resolver=lambda session, name: FactsPage(
                facts=["Pro since 2016"], qa=[], page_name="Tina Black"
            ),
            sleeper=lambda seconds: None,
        )
    assert counts["name_mismatch"] == 0
    assert counts["updated"] == 1
    assert len(fakedb.mutating_statements(conn)) == 1
    # La vía del apodo es más laxa: siempre deja rastro (contador + INFO).
    assert counts["nickname_match"] == 1
    traces = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("Accepted by nickname")
    ]
    assert len(traces) == 1 and "id=9132" in traces[0] and "'Tina Black'" in traces[0]


def test_backfill_translation_failure_skips_row(fakedb):
    conn = fakedb.Connection(_responder(update_result=[(1,)]))

    def broken_translator(facts, qa):
        raise ValueError("translation shape mismatch")

    counts = enrich_facts.backfill(
        conn,
        translator=broken_translator,
        resolver=lambda session, name: _page_with_content(),
        sleeper=lambda seconds: None,
    )
    assert counts["translate_error"] == 1
    assert counts["updated"] == 0
    assert fakedb.mutating_statements(conn) == []


# ------------------------------------------- Neon idle-in-transaction timeout

# (targets, positions whose content is ALREADY stored — the COALESCE guard
# makes their UPDATE hit 0 rows, e.g. facts stored and Q&A still NULL —,
# positions with NEW content). Every other fighter has no faq-athlete block, so
# it runs no SQL at all (most of the --all scope). The fake sleeper advances the
# clock 60 s per fighter: the gaps between statements are minutes.
_NEON_SCENARIOS = {
    # Fighter 1's no-op UPDATE opens a transaction, 2-7 run no SQL, and
    # fighter 8's UPDATE arrives 7 min later: dead unless EVERY iteration
    # closes its transaction, not only the ones that changed a row. This is
    # run 33484806063 (1-sep-2026): three 0-row UPDATEs left uncommitted, 5.2
    # min without SQL, then "SSL connection has been closed unexpectedly".
    "noop-update-then-7-min-gap": (8, {1}, {8}),
    # The first statement after the target SELECT is fighter 6's UPDATE, 6 min
    # later: dead if the read transaction of the SELECT is still open by then.
    "first-sql-6-min-after-select": (12, {6}, {12}),
}


def _neon_targets(total):
    return [(100 + i, f"Fighter Number{i}", None, False) for i in range(1, total + 1)]


def _neon_responder(targets, stored_ids):
    def responder(sql, params=None):
        upper = sql.upper()
        if upper.strip().startswith("SELECT"):
            return targets
        if "UPDATE" in upper:
            return [] if stored_ids.intersection(params) else [(1,)]
        return []

    return responder


@pytest.mark.parametrize("dry_run", [False, True], ids=["write", "dry-run"])
@pytest.mark.parametrize("scenario", sorted(_NEON_SCENARIOS))
def test_backfill_survives_neon_idle_in_transaction_timeout(fakedb, scenario, dry_run):
    total, stored, new = _NEON_SCENARIOS[scenario]
    targets = _neon_targets(total)
    with_content = {targets[i - 1][1] for i in stored | new}
    clock = fakedb.NeonClock()
    conn = fakedb.NeonLikeConnection(
        _neon_responder(targets, {targets[i - 1][0] for i in stored}), clock
    )
    counts = enrich_facts.backfill(
        conn,
        translator=_fake_translator,
        dry_run=dry_run,
        all_scope=True,
        resolver=lambda session, name: FactsPage(
            facts=["Pro since 2013"] if name in with_content else [],
            qa=[],
            page_name=name,
        ),
        sleeper=lambda seconds: clock.advance(60),
    )
    assert counts["with_content"] == 2
    assert clock.now == 60 * total  # every fighter was visited
    assert not conn.in_txn  # nothing left open behind the sweep
    if dry_run:
        assert counts["would_update"] == 2
        assert fakedb.mutating_statements(conn) == []
        assert conn.commits == 0
    else:
        assert counts["updated"] == 1  # the already-stored fighter is a no-op
        assert len(fakedb.mutating_statements(conn)) == 2


@pytest.mark.parametrize("dry_run", [False, True], ids=["write", "dry-run"])
def test_backfill_holds_no_transaction_while_fetching_or_translating(fakedb, dry_run):
    """The invariant behind the scenarios above, independent of timing: when a
    page is fetched or sent to Claude, neither the target SELECT nor any earlier
    UPDATE (0-row ones included) may still hold a transaction. A sweep spends
    ~50 min on ufc.com and Claude, and one slow call can cross Neon's 5 min."""
    targets = _neon_targets(4)
    conn = fakedb.NeonLikeConnection(
        _neon_responder(targets, {targets[0][0]}), fakedb.NeonClock()
    )
    open_during_network = []

    def resolver(session, name):
        if conn.in_txn:
            open_during_network.append(("fetch", name))
        return FactsPage(facts=["Pro since 2013"], qa=[], page_name=name)

    def translator(facts, qa):
        if conn.in_txn:
            open_during_network.append(("translate", facts))
        return _fake_translator(facts, qa)

    counts = enrich_facts.backfill(
        conn, translator=translator, dry_run=dry_run, all_scope=True,
        resolver=resolver, sleeper=lambda seconds: None,
    )
    assert counts["with_content"] == 4
    assert open_during_network == []
    assert not conn.in_txn


def test_target_selection_is_or_and_homonym_safe(fakedb):
    # Revisión adversarial: OR (una mitad publicada más tarde sigue entrando)
    # y exclusión de nombres exactamente duplicados (caso Bruno Silva).
    conn = fakedb.Connection(lambda sql, params=None: [])
    enrich_facts._get_target_fighters(conn, all_scope=True)
    enrich_facts._get_target_fighters(conn, all_scope=False)
    assert len(conn.cursors) == 2
    for cur in conn.cursors:
        sql = " ".join(cur.executed[0][0].split())
        assert "(f.fighter_facts IS NULL OR f.fighter_qa IS NULL)" in sql
        assert "lower(dup.name) = lower(f.name)" in sql


def test_target_selection_brings_the_nickname_in_both_scopes(fakedb):
    # Las filas se desempaquetan por POSICIÓN (id, name, nickname, ufc_confirmed):
    # se fija la lista exacta del SELECT para que una columna cambiada de sitio
    # falle aquí y no en producción.
    nickname = _unique_nickname_sql("f.nickname")
    columns = f"f.id, f.name, {nickname}, (f.headshot_url ILIKE %s) AS ufc_confirmed"
    expected = {
        True: f"SELECT {columns} FROM fighters f WHERE ",
        False: f"SELECT DISTINCT {columns} FROM fighters f JOIN fights fi ",
    }
    rows = [(9132, "Valesca Machado", "Tina Black", False), (1, "Joel Alvarez", "", True)]
    for all_scope in (True, False):
        conn = fakedb.Connection(lambda sql, params=None: rows)
        targets = enrich_facts._get_target_fighters(conn, all_scope=all_scope)
        flat = " ".join(conn.cursors[0].executed[0][0].split())
        assert flat.startswith(expected[all_scope])
        assert targets == [
            (9132, "Valesca Machado", "Tina Black", False),
            (1, "Joel Alvarez", None, True),
        ]


# ------------------------------------------------------------------ repository


def test_update_fighter_facts_sql_is_additive_jsonb(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [])
    update_fighter_facts(conn, 7, facts=["a"], qa=[{"q": "q", "a": "a"}])
    sql = " ".join(fakedb.mutating_statements(conn)[0].split())
    assert "fighter_facts = COALESCE(fighter_facts, %s::jsonb)" in sql
    assert "fighter_qa = COALESCE(fighter_qa, %s::jsonb)" in sql
    assert "(fighter_facts IS NULL AND %s::jsonb IS NOT NULL)" in sql
    assert "(fighter_qa IS NULL AND %s::jsonb IS NOT NULL)" in sql


def test_update_fighter_facts_empty_payload_is_noop(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [])
    assert update_fighter_facts(conn, 7, facts=None, qa=None) is False
    assert update_fighter_facts(conn, 7, facts=[], qa=[]) is False
    assert fakedb.mutating_statements(conn) == []


def test_update_fighter_facts_serializes_unicode_verbatim(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [])
    update_fighter_facts(conn, 7, facts=["Campeón de España"], qa=None)
    params = conn.cursors[0].executed[0][1]
    assert params[0] == '["Campeón de España"]'  # ensure_ascii=False
    assert params[1] is None

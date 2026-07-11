"""Fighter Facts + Q&A (S2-E) parsing, guard, translation-shape and write tests.

The HTML fixtures mirror the two REAL layouts ufc.com serves (verified against
joel-alvarez and yaroslav-amosov on 2026-07-11): facts as ul>li inside
div.field--name-qna-facts, and Q&A inside div.field--name-qna either as ONE
<p> with <strong> questions separated by <br><br> (Joel) or one <p> per pair
(Amosov). No network, no DB: the resolver/translator are injected and writes
go through the shared fakedb recorder.
"""

from bs4 import BeautifulSoup

from src.scrapers import enrich_facts
from src.scrapers.enrich_facts import (
    FactsPage,
    _clean_text,
    _identity_verified,
    parse_fighter_facts,
    parse_fighter_qa,
)
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


# ----------------------------------------------------------------------- guard


def test_identity_guard_matches_and_rejects():
    page = FactsPage(facts=[], qa=[], page_name="Joel Alvarez")
    assert _identity_verified("Joel Álvarez", page, ufc_confirmed=False)
    other = FactsPage(facts=[], qa=[], page_name="Bruno Silva")
    assert not _identity_verified("Joel Álvarez", other, ufc_confirmed=False)
    # No hero name: only trusted when the headshot already proved the page.
    anon = FactsPage(facts=[], qa=[], page_name=None)
    assert _identity_verified("Joel Álvarez", anon, ufc_confirmed=True)
    assert not _identity_verified("Joel Álvarez", anon, ufc_confirmed=False)


# ----------------------------------------------------------- translation shape


def _fake_translator(facts, qa):
    return (
        [f"ES {f}" for f in facts],
        [{"q": f"ES {p['q']}", "a": f"ES {p['a']}"} for p in qa],
    )


def _responder(update_result=None):
    def responder(sql, params=None):
        upper = sql.upper()
        if upper.strip().startswith("SELECT"):
            # One target: (id, name, ufc_confirmed)
            return [(1, "Joel Alvarez", False)]
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

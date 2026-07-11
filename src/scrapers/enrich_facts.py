"""Scrape ufc.com "Fighter Facts" + Q&A and store them translated to Spanish.

ufc.com athlete pages carry, for many fighters, a "Fighter Facts" bullet list
and a Q&A interview block (div.faq-athlete). Both are English-only, so this
pass translates them with Claude (haiku: high volume, simple translation) in
the same sweep and persists the SPANISH content to fighters.fighter_facts /
fighter_qa (JSONB, migration 015).

PARSING — Drupal field classes are the stable selectors (never the positional
#tab-panel-N ids):
  - Facts: div.field--name-qna-facts -> ul > li
  - Q&A:   div.field--name-qna -> every <strong> is a question; the answer is
    the text that follows until the next <strong>. This covers both real-world
    layouts: everything in ONE <p> separated by <br><br> (Joel Alvarez) and one
    <p> per pair (Yaroslav Amosov). Text is NFKC-normalized (the pages carry
    the "fi" ligature) and questions may end in ":" instead of "?".
Fighters without the block simply have no nodes -> skipped as ``no_content``.

ANTI-HOMONYM GUARD — same policy as enrich_fullbody: the hero-profile name the
page renders must match the DB fighter's name; a page without a hero name is
only trusted when the stored headshot already comes from ufc.com.

WRITE POLICY — additive-only via update_fighter_facts (COALESCE on JSONB): a
fighter already populated is NEVER re-translated (no tokens re-spent, no data
overwritten). A translation whose shape does not match the source (different
count of facts/answers) is discarded and counted, never stored.

Default scope = fighters on UPCOMING cards with at least one column NULL
(fast, cheap, mirrors the weekly cron; interviews often land later than facts,
so a half-populated fighter stays selectable and COALESCE protects the stored
half). ``--all`` sweeps the whole table (~2.8k pages). Fighters whose exact
name is shared by another DB row are excluded (homonym safety).

Usage:
    python -m src.scrapers.enrich_facts --probe "Joel Alvarez"   # no DB: fetch+parse+translate, print JSON
    python -m src.scrapers.enrich_facts --dry-run --limit 5      # preview (translates: spends a few tokens)
    python -m src.scrapers.enrich_facts                          # upcoming-card fighters with a gap
    python -m src.scrapers.enrich_facts --all                    # whole-table backfill (30-45 min)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

import requests
from anthropic import Anthropic
from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from .config import get_settings
from .db import connect
from .enrich_fullbody import _names_match
from .enrich_photos_ufc import ATHLETE_URL, REQUEST_DELAY_SECONDS, _HEADERS, _HERO_NAME_RE, slugify
from .logging_config import configure_logging
from .repositories.fighters import update_fighter_facts

LOGGER = logging.getLogger(__name__)

PROGRESS_EVERY = 25
TRANSLATE_MODEL = "claude-haiku-4-5"
TRANSLATE_MAX_TOKENS = 4000


@dataclass(frozen=True)
class FactsPage:
    """Parsed athlete page: English facts/Q&A + the hero name for the guard."""

    facts: list[str]
    qa: list[dict[str, str]]
    page_name: str | None


# resolver(session, name) -> FactsPage | None. Injected in tests.
Resolver = Callable[[requests.Session, str], "FactsPage | None"]
# translator(facts, qa) -> (facts_es, qa_es). Injected in tests (no network).
Translator = Callable[[list[str], list[dict[str, str]]], "tuple[list[str], list[dict[str, str]]]"]

# Target row: (fighter_id, name, ufc_confirmed) — same semantics as enrich_fullbody.
Target = tuple[int, str, bool]


def _clean_text(text: str) -> str:
    """NFKC (folds the 'fi' ligature ufc.com serves), NBSP -> space, collapse
    whitespace."""
    normalized = unicodedata.normalize("NFKC", text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", normalized).strip()


def parse_fighter_facts(soup: BeautifulSoup) -> list[str]:
    container = soup.select_one("div.field--name-qna-facts")
    if container is None:
        return []
    facts = []
    for li in container.select("ul > li"):
        text = _clean_text(li.get_text(" ", strip=True))
        if text:
            facts.append(text)
    return facts


# Tags cuyo comienzo marca frontera de bloque en el Q&A: una negrita que abre
# justo después de una de ellas es una pregunta/etiqueta nueva; una negrita en
# mitad de una frase es énfasis dentro de la respuesta.
_BLOCK_BOUNDARY_TAGS = frozenset({"p", "br", "div", "li", "ul", "ol"})


def parse_fighter_qa(soup: BeautifulSoup) -> list[dict[str, str]]:
    """Every top-level <strong> at a BLOCK BOUNDARY inside the qna field opens
    a question/label; its answer is all the text until the next one (crossing
    <br> and <p> boundaries, which covers both the one-big-<p> and the
    one-<p>-per-pair layouts, and the legacy "label" style — Carlos Condit's
    page uses strongs like 'UFC 264' as headers of each entry).

    CMS artifacts hardened after adversarial review:
      - HTML comments (Comment is a NavigableString subclass) are never answer text.
      - Adjacent <strong>s with no text between them are ONE question split by
        the editor, not two questions.
      - A bold run in the MIDDLE of a sentence ('It means
        <strong>everything</strong> to me') is emphasis: it stays inside the
        answer because no block boundary precedes it. A '?'/':' shape still
        opens a question wherever it appears.
      - Nested <strong>s are read once via the outermost tag's get_text.
    """
    container = soup.select_one("div.field--name-qna")
    if container is None:
        return []
    pairs: list[dict[str, str]] = []
    question: str | None = None
    answer_parts: list[str] = []
    at_block_start = True

    def flush() -> None:
        nonlocal question, answer_parts
        if question is not None:
            q = _clean_text(question)
            a = _clean_text(" ".join(answer_parts))
            if q and a:
                pairs.append({"q": q, "a": a})
        question = None
        answer_parts = []

    for node in container.descendants:
        if isinstance(node, Tag):
            if node.name in _BLOCK_BOUNDARY_TAGS:
                at_block_start = True
            elif node.name == "strong":
                if node.find_parent("strong") is not None:
                    # Nested <strong>: the outer tag's get_text already covered it.
                    continue
                text = _clean_text(node.get_text(" ", strip=True))
                if not text:
                    continue
                is_question_shaped = text.endswith("?") or text.endswith(":")
                if question is not None and not _clean_text(" ".join(answer_parts)):
                    # Adjacent/split <strong>s: same question continued.
                    question = f"{question} {text}".strip()
                elif question is not None and not at_block_start and not is_question_shaped:
                    # Bold emphasis mid-answer, not a new question.
                    answer_parts.append(text)
                else:
                    flush()
                    question = text
                at_block_start = False
        elif isinstance(node, NavigableString):
            if isinstance(node, Comment):
                # Theme/analytics/MSO comments must never leak into answers.
                continue
            # Strings inside the <strong> belong to the question, not the answer.
            if node.find_parent("strong") is not None:
                continue
            answer_parts.append(str(node))
            if str(node).strip():
                at_block_start = False
    flush()
    return pairs


def resolve_facts(session: requests.Session, name: str) -> FactsPage | None:
    """Fetch the athlete page by name slug and parse facts/Q&A + hero name.
    Returns None on network errors and non-200s (404 = fighter without a page,
    expected for historical fighters)."""
    slug = slugify(name)
    if not slug:
        return None
    try:
        response = session.get(ATHLETE_URL.format(slug=slug), headers=_HEADERS, timeout=30)
    except requests.RequestException:
        return None
    if not response.ok:
        return None
    html = response.text
    soup = BeautifulSoup(html, "lxml")
    match = _HERO_NAME_RE.search(html)
    page_name = _clean_text(match.group(1)) if match else None
    return FactsPage(
        facts=parse_fighter_facts(soup),
        qa=parse_fighter_qa(soup),
        page_name=page_name,
    )


def _identity_verified(db_name: str, page: FactsPage, ufc_confirmed: bool) -> bool:
    """Same anti-homonym policy as enrich_fullbody._page_identity_verified."""
    if page.page_name is None:
        return ufc_confirmed
    return _names_match(db_name, page.page_name)


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        # Strip a ```json ... ``` fence if the model added one.
        text = text.strip("`")
        brace = text.find("{")
        if brace != -1:
            text = text[brace:]
    return json.loads(text)


def build_translator(client: Anthropic) -> Translator:
    """One Claude call per fighter with facts+Q&A together (haiku, temp 0)."""

    def translate(
        facts: list[str], qa: list[dict[str, str]]
    ) -> tuple[list[str], list[dict[str, str]]]:
        payload = json.dumps({"facts": facts, "qa": qa}, ensure_ascii=False)
        prompt = (
            "Traduce al español de España estos datos de un luchador de UFC: una lista de "
            "datos breves (facts) y una entrevista de preguntas y respuestas (qa). Conserva "
            "los nombres propios (peleadores, gimnasios, ciudades, promociones como UFC, "
            "Bellator o AFL), los apodos y las cifras. Suena natural y periodístico, no "
            "literal. Devuelve SOLO un objeto JSON con las claves \"facts\" (array de "
            "strings) y \"qa\" (array de objetos con claves \"q\" y \"a\").\n"
            "REGLAS ESTRICTAS DE FORMA (obligatorias): traduce elemento a elemento, 1:1. "
            "\"facts\" debe tener EXACTAMENTE la misma cantidad de elementos y en el mismo "
            "orden que el original, y \"qa\" EXACTAMENTE la misma cantidad de pares {q,a} y "
            "en el mismo orden. NO muevas contenido entre \"facts\" y \"qa\", NO fusiones ni "
            "dividas entradas, NO reclasifiques nada aunque parezca mal categorizado: si "
            "\"facts\" llega vacío, devuélvelo vacío. Cada \"q\" traduce SOLO esa pregunta o "
            "etiqueta y cada \"a\" SOLO esa respuesta.\n\n"
            f"{payload}"
        )
        response = client.messages.create(
            model=TRANSLATE_MODEL,
            max_tokens=TRANSLATE_MAX_TOKENS,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
        data = _extract_json(text)
        facts_es = [str(f).strip() for f in (data.get("facts") or []) if str(f).strip()]
        qa_es = []
        for item in data.get("qa") or []:
            q = str(item.get("q") or "").strip()
            a = str(item.get("a") or "").strip()
            if q and a:
                qa_es.append({"q": q, "a": a})
        if len(facts_es) != len(facts) or len(qa_es) != len(qa):
            # Never store a translation that lost or invented entries.
            raise ValueError(
                f"translation shape mismatch: facts {len(facts)}->{len(facts_es)}, "
                f"qa {len(qa)}->{len(qa_es)}"
            )
        return facts_es, qa_es

    return translate


def _get_target_fighters(
    connection, limit: int | None = None, all_scope: bool = False
) -> list[Target]:
    """Fighters with at least one of the two columns still NULL.

    OR (not AND, adversarial-review fix): ufc.com publishes facts and the Q&A
    interview at different times (interviews land in fight week), so a fighter
    stored with only one half must stay selectable — COALESCE in the writer
    guarantees the stored half is never re-written, so retrying is safe.

    HOMONYM EXCLUSION (adversarial-review fix): two DB fighters with the exact
    same name resolve the SAME ufc.com slug and the hero-name guard validates
    both, which would attribute one person's interview to the other — those
    names are skipped entirely (the real-world case: the two Bruno Silva).

    Default scope: fighters booked on an upcoming event (small, mirrors the
    weekly cron). ``all_scope``: the whole table, ordered by product priority
    (latest-rankings members, then upcoming cards, then anyone with a headshot,
    then the rest) so partial passes cover the most visible fighters first.
    """
    homonym_free = """
              AND NOT EXISTS (
                SELECT 1 FROM fighters dup
                WHERE dup.id <> f.id AND lower(dup.name) = lower(f.name)
              )
    """
    if all_scope:
        sql = f"""
            SELECT f.id, f.name, (f.headshot_url ILIKE %s) AS ufc_confirmed
            FROM fighters f
            WHERE f.name IS NOT NULL AND f.name <> ''
              AND (f.fighter_facts IS NULL OR f.fighter_qa IS NULL)
              {homonym_free}
            ORDER BY
                EXISTS (
                    SELECT 1 FROM rankings r
                    WHERE r.fighter_id = f.id
                      AND r.snapshot_date = (SELECT MAX(snapshot_date) FROM rankings)
                ) DESC,
                EXISTS (
                    SELECT 1 FROM fights fi
                    JOIN events e ON e.id = fi.event_id
                    WHERE e.status = 'upcoming'
                      AND (fi.fighter_red_id = f.id OR fi.fighter_blue_id = f.id)
                ) DESC,
                (NULLIF(f.headshot_url, '') IS NOT NULL) DESC,
                f.name
        """
    else:
        sql = f"""
            SELECT DISTINCT f.id, f.name, (f.headshot_url ILIKE %s) AS ufc_confirmed
            FROM fighters f
            JOIN fights fi ON fi.fighter_red_id = f.id OR fi.fighter_blue_id = f.id
            JOIN events e ON e.id = fi.event_id
            WHERE e.status = 'upcoming'
              AND f.name IS NOT NULL AND f.name <> ''
              AND (f.fighter_facts IS NULL OR f.fighter_qa IS NULL)
              {homonym_free}
            ORDER BY f.name
        """
    params: list = ["%ufc.com%"]
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    with connection.cursor() as cursor:
        cursor.execute(sql, tuple(params))
        return [(int(row[0]), str(row[1]), bool(row[2])) for row in cursor.fetchall()]


def backfill(
    connection,
    *,
    translator: Translator,
    dry_run: bool = False,
    limit: int | None = None,
    all_scope: bool = False,
    resolver: Resolver = resolve_facts,
    sleeper: Callable[[float], None] = time.sleep,
) -> Counter:
    session = requests.Session()
    counts: Counter = Counter()
    targets = _get_target_fighters(connection, limit=limit, all_scope=all_scope)
    total = len(targets)
    counts["targets"] = total
    LOGGER.info(
        "Fighters missing facts/Q&A (scope=%s): %d",
        "all" if all_scope else "upcoming", total,
    )

    for idx, (fighter_id, name, ufc_confirmed) in enumerate(targets, 1):
        page = resolver(session, name)
        sleeper(REQUEST_DELAY_SECONDS)
        if page is None:
            # Includes ufc.com 404s (fighters without a page): expected, never an error.
            counts["unresolved"] += 1
        elif not _identity_verified(name, page, ufc_confirmed):
            counts["name_mismatch"] += 1
            LOGGER.warning(
                "Name mismatch for fighter id=%d %r: page renders %r — skipping",
                fighter_id, name, page.page_name,
            )
        elif not page.facts and not page.qa:
            # Page exists but has no faq-athlete block (most fighters).
            counts["no_content"] += 1
        else:
            counts["with_content"] += 1
            try:
                facts_es, qa_es = translator(page.facts, page.qa)
            except Exception as exc:  # noqa: BLE001 - keep sweeping on a single failure
                counts["translate_error"] += 1
                LOGGER.warning("Translation failed for id=%d %r: %s", fighter_id, name, exc)
                continue
            if dry_run:
                counts["would_update"] += 1
                LOGGER.info(
                    "[dry-run] id=%d %r: %d facts + %d Q&A (es) | primero: %r",
                    fighter_id, name, len(facts_es), len(qa_es),
                    (facts_es[0] if facts_es else (qa_es[0]["q"] if qa_es else "")),
                )
            else:
                updated = update_fighter_facts(
                    connection, fighter_id, facts=facts_es or None, qa=qa_es or None
                )
                if updated:
                    connection.commit()
                    counts["updated"] += 1

        if idx % PROGRESS_EVERY == 0:
            LOGGER.info(
                "Progress %d/%d — with_content=%d updated=%d no_content=%d "
                "unresolved=%d name_mismatch=%d translate_error=%d",
                idx, total, counts["with_content"], counts["updated"],
                counts["no_content"], counts["unresolved"],
                counts["name_mismatch"], counts["translate_error"],
            )
    if dry_run:
        # Release the read-only snapshot; guarantees dry-run never commits.
        connection.rollback()
    return counts


def probe(names: list[str], translator: Translator | None) -> None:
    """No-DB check: fetch + parse (+ translate when a translator is available)
    and print the result as JSON. QA helper for real pages. A translation
    failure reports the error and continues with the next name."""
    session = requests.Session()
    for name in names:
        page = resolve_facts(session, name)
        time.sleep(REQUEST_DELAY_SECONDS)
        if page is None:
            print(json.dumps({"name": name, "resolved": False}, ensure_ascii=False))
            continue
        result: dict = {
            "name": name,
            "resolved": True,
            "page_name": page.page_name,
            "facts_en": page.facts,
            "qa_en_count": len(page.qa),
        }
        if translator and (page.facts or page.qa):
            try:
                facts_es, qa_es = translator(page.facts, page.qa)
            except Exception as exc:  # noqa: BLE001 - keep probing the rest
                result["translate_error"] = str(exc)
            else:
                result["facts_es"] = facts_es
                result["qa_es"] = qa_es
        print(json.dumps(result, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape ufc.com Fighter Facts + Q&A, translate to Spanish and store (JSONB)."
    )
    parser.add_argument("--dry-run", action="store_true", help="Resolve + translate + report, no writes.")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Process at most this many fighters. NOTE: re-runs walk the same "
            "ordered scope from the top — only fighters with stored content drop "
            "out, so no_content/unresolved pages are re-visited (a partial pass "
            "does NOT advance towards the tail)."
        ),
    )
    parser.add_argument("--all", action="store_true", dest="all_scope", help="Whole-table scope (default: upcoming cards only).")
    parser.add_argument("--probe", nargs="+", default=None, help="No DB: fetch/parse/translate these names and print JSON (only needs ANTHROPIC_API_KEY).")
    args = parser.parse_args()
    configure_logging()

    # --probe is documented as "no DB": read the key straight from the env
    # (config.load_dotenv already ran on import) instead of get_settings(),
    # which would demand DATABASE_URL (adversarial-review fix).
    anthropic_api_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip() or None
    if not anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is required to translate facts/Q&A.")
    translator = build_translator(Anthropic(api_key=anthropic_api_key))

    if args.probe:
        probe(args.probe, translator)
        return

    settings = get_settings()
    with connect(settings.database_url) as connection:
        counts = backfill(
            connection,
            translator=translator,
            dry_run=args.dry_run,
            limit=args.limit,
            all_scope=args.all_scope,
        )

    keys = [
        "targets", "with_content", "updated", "would_update", "no_content",
        "unresolved", "name_mismatch", "translate_error",
    ]
    print(json.dumps({key: counts.get(key, 0) for key in keys}, indent=2))
    if args.dry_run:
        print("Dry-run: nothing was written. Re-run without --dry-run to persist.")

    # A dead API key / exhausted credit must NOT leave the cron green: if half
    # or more of the pages WITH content failed to translate, fail the run so
    # GitHub notifies the owner (adversarial-review fix).
    translate_errors = counts.get("translate_error", 0)
    with_content = counts.get("with_content", 0)
    if translate_errors and translate_errors >= max(1, with_content // 2):
        raise SystemExit(
            f"{translate_errors}/{with_content} translations failed — check ANTHROPIC_API_KEY/credit."
        )


if __name__ == "__main__":
    main()

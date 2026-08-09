"""Fill results AND fight_stats onto past ufc.com event bouts from ufcstats.

upcoming_events.py scrapes ufc.com for upcoming cards, storing each bout with NULL
winner/method/end_round/end_time and no fight_stats. Once an event happens it
moves to "Pasados" (by date) but still shows no result and no strike stats,
because ufc.com never publishes them and the ufcstats historical importer keeps
its own event rows (so re-importing would duplicate the event). This closes the
gap onto the EXISTING ufc.com bout rows:

  1. Find past ufc.com events whose bouts still lack a result OR fight_stats
     OR the per-round breakdown (fight_stats_rounds, migration 012 / BE4) OR —
     with the result already filled — the referee (officials, migration 011 /
     BE8: fight_officials only targets source='ufcstats' fights, so ufc.com
     bouts would otherwise never get referee/scorecards).
  2. Match each to the corresponding ufcstats event by date (+ name tie-break).
  3. For each bout, open its ufcstats fight detail page once and fill the result
     (winner/method/round/time), the fight_stats (sig strikes + the #45
     head/body/leg + distance/clinch/ground breakdown) AND one
     fight_stats_rounds row per (fighter, round), matching fighters by name
     (corner-swap tolerant) so no duplicate events/fights are ever created.
  4. From that SAME page (zero extra fetches), fill fights.referee and the
     judges' fight_scorecards with fight_officials' parser/writers. Scorecard
     orientation is resolved source-ids -> fuzzy names; unlike the ufcstats
     path there is NO positional default (ufc.com corner order proves nothing
     about ufcstats page order), so an unresolved bout is skipped and counted.

Idempotent: results only fill where method is still NULL — or still one of the
PROVISIONAL codes the ESPN live updater writes on fight night
(espn_live_results.ESPN_PROVISIONAL_METHODS): ufcstats is the better source,
so its detailed method ('U-DEC', 'KO/TKO - Punches', ...) upgrades the generic
ESPN one, never the other way around. Stats upsert ON CONFLICT. Wired as the
last step of refresh_upcoming.py, so every daily refresh fully ingests whatever
just finished — that's how just-completed events get their strike stats without
the historical re-import.

REQUIRES migration db/migrations/012_fase6.sql applied first (the candidate
query and the stats step touch fight_stats_rounds).

Usage (writes to the DB):
    python -m src.scrapers.backfill_results --dry-run   # find + match + report, no writes
    python -m src.scrapers.backfill_results             # fill results + stats
    python -m src.scrapers.backfill_results --limit 5   # at most N events this run
"""

from __future__ import annotations

import argparse
import json
import logging

import psycopg2
from collections import Counter

from .backfill_event_fights import EVENTS_INDEX_URL, _distinctive_name_overlap
from .config import get_settings
from .db import connect
from .enrich_ranked import _fold
from .espn_live_results import ESPN_PROVISIONAL_METHODS
from .matching import given_name_diminutive_match, token_subset_match
from .fight_officials import (
    TargetFight,
    insert_scorecard,
    parse_fight_officials,
    resolve_first_person_is_red,
    scores_by_person,
    update_fight_referee,
)
from .http import UfcStatsClient
from .logging_config import configure_logging
from .parsers.events import parse_events_index
from .parsers.fights import (
    FightPageRecord,
    build_fight_stats_record,
    build_fight_stats_round_record,
    parse_event_fights,
    parse_fight_stats,
    parse_fight_stats_rounds,
)
from .repositories.fights import upsert_fight_stats, upsert_fight_stats_rounds
from .utils import clean_text

LOGGER = logging.getLogger(__name__)

UFC_SOURCE = "ufc.com"
UFCSTATS_EVENTS_URL = "http://ufcstats.com/statistics/events/completed"

# How far back the daily backfill keeps RE-QUALIFYING a past event for another
# pass. Winner/method/stats land on ufcstats within a day or two, but the
# referee (fights.referee) and the judges' scorecards are often published or
# corrected DAYS-to-WEEKS later; since those officials ride on the same
# fight-details page the result came from, the event must stay eligible long
# enough to catch them. 60 days is the deliberate ceiling: wide enough for
# late officials, bounded so a permanently unmatchable bout (name divergence)
# can't re-qualify forever and grow the cron's work without limit.
BACKFILL_WINDOW_DAYS = 60


def _is_decision(method: str | None) -> bool:
    """Whether a stored/scraped method means the fight went to the judges.

    Matches every spelling in play: 'U-DEC'/'S-DEC'/'M-DEC' (ufcstats event
    page), 'Decision - Unanimous' (detail page) and ESPN's provisional
    'Decision'. Finishes never have scorecards, so anything else is False.
    """
    return method is not None and "dec" in method.lower()


class _Bout:
    __slots__ = (
        "id", "red_id", "blue_id", "red_name", "blue_name", "method",
        "has_stats", "has_round_stats",
        "referee", "has_scorecards", "red_source_id", "blue_source_id",
    )

    def __init__(
        self, id, red_id, blue_id, red_name, blue_name, method=None,
        has_stats=False, has_round_stats=False,
        referee=None, has_scorecards=False, red_source_id=None, blue_source_id=None,
    ):
        self.id = id
        self.red_id = red_id
        self.blue_id = blue_id
        self.red_name = red_name or ""
        self.blue_name = blue_name or ""
        self.method = method
        self.has_stats = has_stats
        self.has_round_stats = has_round_stats
        self.referee = referee
        self.has_scorecards = has_scorecards
        self.red_source_id = red_source_id
        self.blue_source_id = blue_source_id

    def needs_result(self) -> bool:
        """No result yet, or only the PROVISIONAL one the ESPN live updater
        wrote on fight night — ufcstats detail upgrades that."""
        return self.method is None or self.method in ESPN_PROVISIONAL_METHODS

    def needs_officials(self, method: str | None = None) -> bool:
        """Referee still missing, or a decision without its judges' scorecards.

        ``method`` lets _fill_event pass the fresher ufcstats method when the
        stored one is still NULL/provisional (result filled this same pass).
        """
        if self.referee is None:
            return True
        return not self.has_scorecards and _is_decision(method or self.method)

    def key(self) -> frozenset[str]:
        return frozenset({_fold(self.red_name), _fold(self.blue_name)})

    def corner_for(self, name: str) -> str | None:
        """Map a fighter NAME (from ufcstats) onto this bout's corner label.

        Exact folded equality first; then the token-subset fallback for the
        dropped-middle-name divergence (bout 12859: page "Jose Delgado" vs DB
        "Jose Miguel Delgado"), refusing ambiguity — a name contained in BOTH
        corners maps to neither. Works on NAMES, never ids: a corner whose
        fighter went unlinked at import (fighter_red_id NULL, a designed state
        for debutants) still matches, so its bout can consolidate the
        corner-agnostic fields (result, referee).
        """
        folded = _fold(name)
        if folded == _fold(self.red_name):
            return "red"
        if folded == _fold(self.blue_name):
            return "blue"
        red_ok = token_subset_match(name, self.red_name)
        blue_ok = token_subset_match(name, self.blue_name)
        if red_ok != blue_ok:
            return "red" if red_ok else "blue"
        if red_ok and blue_ok:
            return None  # Ambiguo: no bajamos a un nivel más laxo.
        # Tercer nivel: diminutivo del nombre de pila (bout 12850, página
        # "Zach Reese" vs BD "Zachary Reese"). Solo se intenta cuando el
        # subconjunto no ha dicho nada de NINGUNA esquina.
        red_dim = given_name_diminutive_match(name, self.red_name)
        blue_dim = given_name_diminutive_match(name, self.blue_name)
        if red_dim != blue_dim:
            return "red" if red_dim else "blue"
        return None

    def fighter_id_for(self, name: str) -> int | None:
        """:meth:`corner_for` resolved to the corner's fighter id.

        None both for "no corner matched" and "the matched corner's fighter is
        unlinked" — callers that WRITE per-fighter rows (stats) need a real id
        either way; bout-level matching uses :meth:`corner_for` instead.
        """
        corner = self.corner_for(name)
        if corner == "red":
            return self.red_id
        if corner == "blue":
            return self.blue_id
        return None


def events_needing_results_sql(historico: bool = False) -> tuple[str, list]:
    """The 'which cards still need a pass' query, built so it can be tested.

    THREE THINGS MEASURED AGAINST THE REAL DB ON 2026-07-26, none documented:

    * Cancelled bouts kept whole events alive. A cancelled bout has no method
      and no stats BY DEFINITION, so events 1062 (2 cancelled) and 1061 (3)
      re-qualified on every pass for 60 days to do nothing at all.

    * The provisional-method clause caused permanent churn. 'KO/TKO' is in
      ESPN_PROVISIONAL_METHODS because that is what the live loop writes — but
      ufcstats writes the very same string when it doesn't detail the strike,
      so McGregor-Holloway was rewritten 5 times in 11 hours. The clause is
      redundant anyway: a bout whose method still needs upgrading is also
      missing stats or a referee, so it already qualifies through those.

    * `e.source = 'ufc.com'` — not the 60-day window — is what hid the whole
      back catalogue: 761 of the 787 events have source NULL, and all 735
      events with refereeless bouts live in that group.

    ``historico`` drops the source and window filters for a MANUAL batch rescue
    (see the CLI). The daily cron keeps both: the window exists so a bout that
    can never be matched can't re-qualify forever and grow the cron without
    bound, and that reasoning still holds.
    """
    filtros = ["e.event_date IS NOT NULL", "e.event_date < CURRENT_DATE"]
    params: list = []
    if not historico:
        filtros.insert(0, "e.source = %s")
        params.append(UFC_SOURCE)
        filtros.append("e.event_date >= CURRENT_DATE - (%s * INTERVAL '1 day')")
        params.append(BACKFILL_WINDOW_DAYS)

    sql = f"""
        SELECT e.id, e.name, e.event_date
        FROM events e
        WHERE {' AND '.join(filtros)}
          AND EXISTS (
            SELECT 1 FROM fights fi
            WHERE fi.event_id = e.id
              -- A cancelled bout has no method and no stats by definition:
              -- without this it keeps its whole event qualifying forever.
              AND fi.status IS DISTINCT FROM 'cancelled'
              AND (
                fi.method IS NULL
                -- a real bout has 2 fighters -> needs both stat rows
                OR (SELECT COUNT(*) FROM fight_stats fs WHERE fs.fight_id = fi.id) < 2
                -- ... and both fighters' per-round rows (migration 012 / BE4)
                OR (SELECT COUNT(DISTINCT fsr.fighter_id)
                    FROM fight_stats_rounds fsr WHERE fsr.fight_id = fi.id) < 2
                -- result filled but the referee never was (migration 011 /
                -- BE8): officials ride on the same fight page as the result
                OR (fi.referee IS NULL
                    AND (fi.winner_id IS NOT NULL OR fi.method IS NOT NULL))
              )
          )
        ORDER BY e.event_date DESC
    """
    return sql, params


def _get_events_needing_results(
    connection, limit: int | None, historico: bool = False
) -> list[tuple[int, str, object]]:
    sql, params = events_needing_results_sql(historico=historico)
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = [(int(r[0]), str(r[1]), r[2]) for r in cursor.fetchall()]
    return rows[:limit] if limit is not None else rows


def _get_bouts(connection, event_id: int) -> list[_Bout]:
    """All bouts of an event with result/stats state, so we fill whichever is missing."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT fi.id, fi.fighter_red_id, fi.fighter_blue_id,
                   COALESCE(red.name, fi.fighter_red_name) AS red_name,
                   COALESCE(blue.name, fi.fighter_blue_name) AS blue_name,
                   fi.method,
                   (SELECT COUNT(*) FROM fight_stats fs WHERE fs.fight_id = fi.id) >= 2 AS has_stats,
                   (SELECT COUNT(DISTINCT fsr.fighter_id)
                    FROM fight_stats_rounds fsr WHERE fsr.fight_id = fi.id) >= 2 AS has_round_stats,
                   fi.referee,
                   EXISTS (SELECT 1 FROM fight_scorecards sc
                           WHERE sc.fight_id = fi.id) AS has_scorecards,
                   red.source_id, blue.source_id
            FROM fights fi
            LEFT JOIN fighters red ON red.id = fi.fighter_red_id
            LEFT JOIN fighters blue ON blue.id = fi.fighter_blue_id
            WHERE fi.event_id = %s
            """,
            (event_id,),
        )
        return [
            _Bout(
                int(r[0]), r[1], r[2], r[3], r[4], r[5], bool(r[6]), bool(r[7]),
                r[8], bool(r[9]), r[10], r[11],
            )
            for r in cursor.fetchall()
        ]


def _match_event(event_name: str, event_date, index_records) -> str | None:
    """Pick the ufcstats event matching this ufc.com event: same date, best name."""
    same_date = [rec for rec in index_records if rec.event.event_date == event_date]
    if not same_date:
        return None
    if len(same_date) == 1:
        return same_date[0].detail_url
    target = _fold(event_name)
    best = max(same_date, key=lambda rec: _name_overlap(target, _fold(rec.event.name)))
    return best.detail_url


def emparejar_en_indice(event_name: str, event_date, index_records) -> str | None:
    """Same as _match_event but over a PRE-LOADED index, keeping the safety guard.

    The historic rescue can't afford _find_ufcstats_event_url's per-event walk:
    it restarts at page 1 every time, so a 2015 card re-reads ~20 pages and 745
    events spend hours re-reading the same listing. Here the index is loaded
    once and searched in memory.

    What must NOT be lost in the move is the distinctive-token guard: two events
    can share a date, and hanging one's card on the other corrupts BOTH (the
    existing fights get re-pointed at the wrong event_id). When in doubt, don't
    match — an unconsolidated event retries, a corrupted one is fixed by hand.
    """
    same_date = [rec for rec in index_records if rec.event.event_date == event_date]
    if not same_date:
        return None
    best = max(
        same_date,
        key=lambda rec: _distinctive_name_overlap(event_name, rec.event.name),
    )
    if _distinctive_name_overlap(event_name, best.event.name) == 0:
        LOGGER.warning(
            "Rejecting same-date ufcstats event %r for target %r: no distinctive token",
            best.event.name, event_name,
        )
        return None
    return best.detail_url


def rollback_seguro(connection) -> bool:
    """Roll back without letting a dead connection kill the whole run.

    Measured on 2026-07-26: Neon dropped the connection mid-``_fill_event`` and
    the per-event ``except`` — which exists precisely to carry on with the next
    event — blew up on ``connection.rollback()`` with "connection already
    closed". Nothing caught THAT second exception, so the process died and the
    remaining 431 events were lost. The error handling was more fragile than the
    error it handled.
    """
    try:
        connection.rollback()
        return True
    except psycopg2.Error as exc:
        LOGGER.warning("Rollback on a dead connection (%s); will reconnect", exc)
        return False


def reconectar_si_hace_falta(connection, abrir):
    """Return a live connection, opening a new one if the current one is gone.

    A rescue batch is hours of slow scraping against a third party, so losing
    the connection at some point is expected, not exceptional. If the RECONNECT
    also fails we do let it propagate: without a database there is nothing to
    do, and retrying hundreds of events against a downed server only drags it
    out.
    """
    if connection is not None and not connection.closed:
        return connection
    LOGGER.warning("Connection is closed; opening a new one")
    return abrir()


def cargar_indice_completo(client, settings, hasta_fecha, max_paginas: int = 100) -> list:
    """Every completed-events page down to ``hasta_fecha``, read once.

    The listing is newest-first, so the walk stops as soon as a page is already
    older than the oldest event we need. ~25 events per page covers decades well
    before the page cap.
    """
    registros: list = []
    for pagina in range(1, max_paginas + 1):
        page = client.fetch(EVENTS_INDEX_URL.format(page=pagina))
        nuevos = parse_events_index(page.soup, settings)
        if not nuevos:
            break  # ran past the last page
        registros.extend(nuevos)
        fechas = [r.event.event_date for r in nuevos if r.event.event_date]
        if fechas and min(fechas) < hasta_fecha:
            break
    LOGGER.info(
        "ufcstats index loaded once: %d events down to %s", len(registros), hasta_fecha
    )
    return registros


def _name_overlap(a: str, b: str) -> int:
    return len(set(a.split()) & set(b.split()))


def _winner_name_from_soup(soup) -> str | None:
    """Read the winner off a fight's OWN detail-page soup.

    The event page only flags THAT a bout has a winner (a single green "win" flag),
    not which fighter — so we read the per-fighter W/L status. Returns the winning
    fighter's name, or None for a draw / no-contest / unparseable result.
    """
    names = [
        clean_text(a.get_text(" ", strip=True)) or ""
        for a in soup.select(".b-fight-details__person-name a[href*='/fighter-details/']")
    ]
    statuses = [
        s
        for s in (
            (clean_text(n.get_text(" ", strip=True)) or "").lower()
            for n in soup.select(".b-fight-details__person-status")
        )
        if s
    ]
    if len(names) >= 2 and len(statuses) >= 2:
        if statuses[0] == "w":
            return names[0]
        if statuses[1] == "w":
            return names[1]
    return None


def _winner_id_for(bout: _Bout, winner_name: str | None) -> int | None:
    """Map a winning fighter name onto this bout's red/blue id (corner-swap tolerant)."""
    if not winner_name:
        return None
    return bout.fighter_id_for(winner_name)


def _corners_match(bout: _Bout, fight: FightPageRecord) -> bool:
    """Whether this ufcstats fight's two names cover the bout's two corners.

    Corner-swap tolerant (ufcstats lists the winner first, which proves
    nothing about our red/blue), and each page name must claim a DISTINCT
    corner — one DB fighter covering both page names is never a match.
    Compares corner LABELS, not fighter ids: an unlinked corner (id NULL)
    must not veto the bout — the exact-key path never consulted ids either.
    """
    red = bout.corner_for(fight.red_name)
    blue = bout.corner_for(fight.blue_name)
    return red is not None and blue is not None and red != blue


def _match_fight(bout: _Bout, fights: list[FightPageRecord]) -> FightPageRecord | None:
    """Pick the ufcstats fight for a bout: exact folded pair, else unique subset.

    The exact frozenset key is the historical fast path. When it misses
    (ufcstats drops a middle name: bout 12859 "Jose Miguel Delgado" listed as
    "Jose Delgado"), fall back to token-subset corner matching — accepting
    only a UNIQUE candidate. Ambiguity stays unmatched rather than guessing:
    writing another bout's stats would be far worse than writing none.
    """
    by_key: dict[frozenset[str], FightPageRecord] = {
        frozenset({_fold(f.red_name), _fold(f.blue_name)}): f for f in fights
    }
    exact = by_key.get(bout.key())
    if exact is not None:
        return exact
    candidates = [f for f in fights if _corners_match(bout, f)]
    return candidates[0] if len(candidates) == 1 else None


def _fill_event(connection, client, settings, event_id, bouts, detail_url, counts, dry_run):
    page = client.fetch(detail_url)
    fights = parse_event_fights(page.soup, settings)
    for bout in bouts:
        needs_result = bout.needs_result()
        needs_stats = not bout.has_stats or not bout.has_round_stats
        # DB-state check only here; re-checked against the ufcstats method
        # below so a result filled THIS pass pulls its scorecards in too.
        needs_officials = bout.needs_officials()
        if not needs_result and not needs_stats and not needs_officials:
            continue
        fight = _match_fight(bout, fights)
        if fight is None:
            counts["bouts_unmatched"] += 1
            LOGGER.info("  no ufcstats fight for %s vs %s", bout.red_name, bout.blue_name)
            continue
        # One fetch of the fight's own detail page serves both the result and the stats.
        fight_page = client.fetch(fight.detail_url)
        # Only fill a result when ufcstats actually has a method; writing NULL over
        # NULL is a no-op that re-qualifies the bout forever and inflates the count.
        if needs_result and fight.method is not None:
            winner_name = _winner_name_from_soup(fight_page.soup)
            winner_id = _winner_id_for(bout, winner_name)
            LOGGER.info(
                "  %s vs %s -> winner=%r (id=%s) method=%s round=%s time=%s",
                bout.red_name, bout.blue_name,
                winner_name, winner_id, fight.method, fight.end_round, fight.end_time,
            )
            if dry_run:
                counts["bouts_filled"] += 1
            else:
                with connection.cursor() as cursor:
                    # method still NULL or a provisional ESPN live code: the
                    # ufcstats result replaces it (better quality). Real
                    # ufcstats methods are never re-written. COALESCE(%s, col)
                    # keeps the ESPN winner/round/time when this scrape could
                    # not resolve one (never degrade to NULL).
                    cursor.execute(
                        """
                        UPDATE fights
                        SET winner_id = COALESCE(%s, winner_id),
                            method = %s,
                            end_round = COALESCE(%s, end_round),
                            end_time = COALESCE(%s, end_time)
                        WHERE id = %s AND (method IS NULL OR method = ANY(%s))
                        """,
                        (
                            winner_id, fight.method, fight.end_round, fight.end_time,
                            bout.id, list(ESPN_PROVISIONAL_METHODS),
                        ),
                    )
                    if cursor.rowcount:
                        counts["bouts_filled"] += 1
        if needs_stats:
            for fighter_stats in parse_fight_stats(fight_page.soup):
                # A fighter whose stats didn't parse at all (sig strikes None) would
                # otherwise write a hollow row that marks the bout done and hides the
                # failure; skip it so the bout stays "needs stats" and is retried.
                if fighter_stats.stats.get("sig_strikes_attempted") is None:
                    counts["stats_empty"] += 1
                    continue
                fighter_id = bout.fighter_id_for(fighter_stats.fighter_name)
                if fighter_id is None:
                    counts["stats_unmatched"] += 1
                    LOGGER.warning(
                        "  unmatched stats fighter %r on bout %s (%s vs %s)",
                        fighter_stats.fighter_name, bout.id, bout.red_name, bout.blue_name,
                    )
                    continue
                if not dry_run:
                    upsert_fight_stats(connection, build_fight_stats_record(bout.id, fighter_id, fighter_stats))
                counts["stats_rows"] += 1
            _fill_round_stats(connection, bout, fight_page.soup, counts, dry_run)
        # Officials come off the SAME soup already in hand (BE8 parity for
        # ufc.com bouts, zero extra fetches).
        if bout.needs_officials(fight.method):
            _fill_officials(connection, bout, fight_page.soup, counts, dry_run)


def _fill_round_stats(connection, bout, fight_page_soup, counts, dry_run) -> None:
    """Persist the per-round breakdown of a fight-details page (BE4).

    One fight_stats_rounds row per (fighter, round), fighters matched by name
    onto the bout's corners like the totals. Hollow rows (sig strikes didn't
    parse) are skipped so the bout keeps re-qualifying, same policy as totals.
    """
    for round_stats in parse_fight_stats_rounds(fight_page_soup):
        if round_stats.stats.get("sig_strikes_attempted") is None:
            counts["round_stats_empty"] += 1
            continue
        fighter_id = bout.fighter_id_for(round_stats.fighter_name)
        if fighter_id is None:
            counts["round_stats_unmatched"] += 1
            LOGGER.warning(
                "  unmatched round-stats fighter %r on bout %s (%s vs %s)",
                round_stats.fighter_name, bout.id, bout.red_name, bout.blue_name,
            )
            continue
        if not dry_run:
            upsert_fight_stats_rounds(
                connection, build_fight_stats_round_record(bout.id, fighter_id, round_stats)
            )
        counts["round_stats_rows"] += 1


def _fill_officials(connection, bout, fight_page_soup, counts, dry_run) -> None:
    """Persist referee + judges' scorecards off a fight-details page (BE8).

    Reuses fight_officials' parser and writers (referee COALESCE, scorecards
    ON CONFLICT DO NOTHING), so re-runs never stomp or duplicate. The referee
    is corner-agnostic and always safe to fill; scorecards need the page's
    person order mapped onto the bout's corners, and a ufc.com bout gets NO
    positional default — an unresolved orientation skips them (counted) rather
    than risk writing every judge's scores swapped.
    """
    officials = parse_fight_officials(fight_page_soup)
    if officials.referee is None and not officials.scorecards:
        counts["officials_missing"] += 1
        return
    if bout.referee is None and officials.referee is not None:
        counts["referees_filled"] += 1
        if not dry_run:
            update_fight_referee(connection, bout.id, officials.referee)
    if bout.has_scorecards or not officials.scorecards:
        return
    # Orientation target: only the corner fields matter, source_id is unused.
    target = TargetFight(
        fight_id=bout.id,
        source_id="",
        red_source_id=bout.red_source_id,
        blue_source_id=bout.blue_source_id,
        red_name=bout.red_name,
        blue_name=bout.blue_name,
    )
    red_first = resolve_first_person_is_red(officials, target)
    if red_first is None:
        counts["officials_unresolved"] += 1
        LOGGER.warning(
            "  cannot orient scorecards for bout %s (%s vs %s), skipping",
            bout.id, bout.red_name, bout.blue_name,
        )
        return
    # Sin badge W en la página no hay a qué anclar el par de notas: empate,
    # no-contest o marcado ilegible. Se descarta la tarjeta entera antes que
    # inventar la orientación — es el fallo que costó 2412 peleas espejadas.
    # Va DESPUÉS del `red_first is None` porque son dos causas distintas: allí
    # no sabemos qué persona es qué esquina; aquí no sabemos qué nota es de quién.
    if officials.scorecards and officials.winner_index is None:
        counts["officials_unanchored"] += 1
        LOGGER.warning(
            "  sin ganador en la página: no se orientan las tarjetas del bout %s (%s vs %s)",
            bout.id, bout.red_name, bout.blue_name,
        )
        return
    for card in officials.scorecards:
        first, second = scores_by_person(card, officials.winner_index)
        red_score, blue_score = (first, second) if red_first else (second, first)
        if dry_run:
            counts["scorecards_inserted"] += 1
        elif insert_scorecard(connection, bout.id, card.judge_name, red_score, blue_score):
            counts["scorecards_inserted"] += 1


def backfill(
    dry_run: bool = False, limit: int | None = None, historico: bool = False
) -> dict:
    settings = get_settings()
    counts: Counter = Counter()
    with connect(settings.database_url) as connection:
        events = _get_events_needing_results(connection, limit, historico=historico)
        counts["events_candidate"] = len(events)
        LOGGER.info("Past events missing results: %d", len(events))
        if not events:
            return dict(counts)

        client = UfcStatsClient(settings)
        if historico:
            # Page 1 only lists the most recent cards, so the daily path CANNOT
            # find a 2023 event. The back catalogue needs the whole listing —
            # but read ONCE, not re-walked per event: _find_ufcstats_event_url
            # restarts at page 1 every time, so 745 events would re-read the
            # same pages thousands of times (~20 s/event and rising, measured).
            mas_antiguo = min(fecha for _, _, fecha in events)
            index = cargar_indice_completo(client, settings, mas_antiguo)
        else:
            index = parse_events_index(client.fetch(UFCSTATS_EVENTS_URL).soup, settings)
            LOGGER.info("ufcstats completed-events index (page 1): %d events", len(index))

        # Connections opened by the reconnect path. `connect()` is a context
        # manager that closes ITS connection on exit — it can't close one it
        # never opened, so these are tracked and closed here.
        reabiertas: list = []

        def _reabrir():
            nueva = psycopg2.connect(settings.database_url)
            reabiertas.append(nueva)
            return nueva

        try:
            for event_id, name, event_date in events:
                if historico:
                    detail_url = emparejar_en_indice(name, event_date, index)
                else:
                    detail_url = _match_event(name, event_date, index)
                if detail_url is None:
                    counts["events_unmatched"] += 1
                    LOGGER.info("No ufcstats match for %r (%s)", name, event_date)
                    continue
                counts["events_matched"] += 1
                # A rescue batch is hours of slow scraping against a third
                # party, so losing the connection along the way is expected.
                # Every event commits on its own, so reconnecting costs nothing
                # already written.
                connection = reconectar_si_hace_falta(connection, _reabrir)
                bouts = _get_bouts(connection, event_id)
                LOGGER.info("%r (%s) -> %s | %d bouts", name, event_date, detail_url, len(bouts))
                try:
                    _fill_event(connection, client, settings, event_id, bouts, detail_url, counts, dry_run)
                    if not dry_run:
                        connection.commit()
                except Exception as exc:  # noqa: BLE001 - keep going to the next event
                    rollback_seguro(connection)
                    counts["event_errors"] += 1
                    LOGGER.exception("Failed to fill %r: %s", name, exc)
        finally:
            for extra in reabiertas:
                try:
                    extra.close()
                except psycopg2.Error:  # pragma: no cover - best effort
                    pass
    return dict(counts)


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Backfill results onto past ufc.com event bouts from ufcstats.")
    parser.add_argument("--dry-run", action="store_true", help="Find + match + report, no writes.")
    parser.add_argument("--limit", type=int, default=None, help="Process at most this many events.")
    parser.add_argument(
        "--historico",
        action="store_true",
        help=(
            "Rescue the back catalogue: drops the source and 60-day filters and "
            "walks the paginated ufcstats index. MANUAL, IN BATCHES — 735 events "
            "and ~8.2k bouts at ~1.25 s per request. Always pair with --limit."
        ),
    )
    args = parser.parse_args()
    counts = backfill(dry_run=args.dry_run, limit=args.limit, historico=args.historico)
    keys = [
        "events_candidate", "events_matched", "events_unmatched",
        "bouts_filled", "bouts_unmatched", "stats_rows", "stats_unmatched", "stats_empty",
        "round_stats_rows", "round_stats_unmatched", "round_stats_empty",
        "referees_filled", "scorecards_inserted", "officials_unresolved",
        "officials_missing", "event_errors",
    ]
    print(json.dumps({k: counts.get(k, 0) for k in keys}, indent=2))


if __name__ == "__main__":
    main()

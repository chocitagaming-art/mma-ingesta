"""Refresh stored fighter W-L-D from ESPN's overall record (total career).

WHY: the stored fighters.wins/losses/draws is seeded at scrape time and is NEVER
incremented when a fight result is sealed (fill_fight_result only writes the
`fights` row). With no scheduled roster re-scrape, the palmares FREEZES: after a
fighter's latest bout the record still shows the old value. This job re-fetches
the ESPN "overall" record (the total career W-L-D) and writes it back.

SAFETY:
- Fetches BY espn_id first (reliable key: no homonym crossover). Fighters WITHOUT
  an espn_id fall back to a name search, but that ambiguous match is accepted ONLY
  when it is a small, per-component non-decreasing bump over the stored record
  (_name_change_is_safe) -> a real +1/+2 passes, a wrong homonym is rejected.
- Writes through bump_fighter_record, which is STRICTLY MONOTONIC on the bout
  count: it only updates when the incoming total (w+l+d) is greater than the
  stored total. So a transient bad/empty ESPN read can never regress a record,
  and a prospect ESPN doesn't track keeps its stored value untouched.
- Semantics = TOTAL career record (owner decision, 2026-07-17).
- One fetch pass -> write a JSON backup of the OLD values BEFORE applying -> then
  apply. Reversible.
- Manual corrections (record_correcciones.CORRECCIONES): for a fighter whose ESPN
  overall counts a bout it must not (Imanol Rodriguez, TUF 33 exhibition), that
  bout is subtracted while ESPN still lists it. Such a fighter is ALWAYS fetched
  by the corrected espn_id and NEVER by name, the correction is applied BEFORE the
  "unchanged" shortcut, and a corrected record with fewer bouts goes through the
  dedicated set_fighter_record_corrected, which may lower the stored record ONLY by
  the subtracted bouts, component by component (never a real win, loss or draw).
  If a correction is no longer needed the fighter is left untouched and the run
  exits 1 (notify-on-failure opens an Issue).

Usage:
  python -m src.scrapers.refresh_fighter_records --dry-run --days 400   # report only
  python -m src.scrapers.refresh_fighter_records --days 14              # cron: recent fighters
  python -m src.scrapers.refresh_fighter_records --all                  # every fighter w/ espn_id
  python -m src.scrapers.refresh_fighter_records --backup out.json --days 400
  python -m src.scrapers.refresh_fighter_records --probe "Cory Sandhagen"  # no DB, no write
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import sys
import time
from typing import Callable

from .config import get_settings
from .db import connect
from .enrich_ranked import _build_session, _search_espn_athlete
from .enrich_records_espn import _fetch_espn_record, resolve_record
from .espn_fight_history import fetch_career
from .logging_config import configure_logging
from .record_correcciones import (
    CORRECCIONES,
    SIN_VERIFICAR,
    YA_NO_NECESARIA,
    CorreccionRecord,
    correcciones_por_luchador,
    evaluar_correcciones,
)
from .repositories.fighters import bump_fighter_record, set_fighter_record_corrected

LOGGER = logging.getLogger(__name__)

REQUEST_DELAY_SECONDS = 0.35

# When a fighter has NO espn_id we fall back to a name search, whose key is
# ambiguous (homonyms). Such a record is trusted only if it is a small,
# per-component non-decreasing bump over the stored one — at most this many extra
# total bouts. A real "record froze one fight ago" is +1/+2; a wrong homonym has
# an unrelated shape and is rejected.
DEFAULT_MAX_NAME_DELTA = 4

# A record fetcher keyed by espn_id: (espn_id) -> (wins, losses, draws) | None.
RecordFetcher = Callable[[str], "tuple[int, int, int] | None"]
# The athlete's common/v3 payload (with its eventsMap) keyed by espn_id, or None.
# Only called for fighters with a manual correction.
EventsFetcher = Callable[[str], "dict | None"]


def _name_change_is_safe(
    old: "tuple[int, int, int]",
    new: "tuple[int, int, int]",
    max_delta: int,
) -> bool:
    """Whether a name-searched (ambiguous-key) record may be trusted: each of
    wins/losses/draws is non-decreasing (careers only add fights) and the total
    grows by 1..max_delta. Rejects homonyms whose record has an unrelated shape."""
    if not (new[0] >= old[0] and new[1] >= old[1] and new[2] >= old[2]):
        return False
    delta = (new[0] + new[1] + new[2]) - (old[0] + old[1] + old[2])
    return 0 < delta <= max_delta


def _corrected_write_is_safe(
    stored: "tuple[int, int, int]",
    new: "tuple[int, int, int]",
    removed: "tuple[int, int, int]",
) -> bool:
    """Whether the non-monotonic corrected writer may replace `stored` by `new`:
    each of wins/losses/draws may drop only by what the correction removed
    (stored_c <= new_c + removed_c). Same rule as the SQL guard of
    set_fighter_record_corrected, which stays the authoritative gate (it also
    covers a concurrent run that moved the record after this one planned).
    A total-only bound is not enough: stored 9-0-0 with a stale ESPN 8-1-0 gives
    corrected 8-0-0, total 8 <= 8 + 1, and would erase a real win."""
    return all(s <= n + r for s, n, r in zip(stored, new, removed))


def _get_target_fighters(
    connection,
    *,
    days: int,
    all_fighters: bool,
    limit: int | None,
    offset: int = 0,
) -> list[tuple[int, str | None, str | None, int, int, int]]:
    """(id, name, espn_id, wins, losses, draws) for the target fighters.

    Unless all_fighters, restrict to those with a COMPLETED fight in the last
    `days` days (the ones whose palmares most likely moved). Fighters WITHOUT an
    espn_id are INCLUDED: refresh_records refreshes them via the guarded name
    fallback (their espn_id column is NULL, which routes them there).
    """
    with connection.cursor() as cursor:
        if all_fighters:
            cursor.execute(
                """
                SELECT id, name, espn_id, wins, losses, draws
                FROM fighters
                ORDER BY name
                """
            )
        else:
            cursor.execute(
                """
                SELECT DISTINCT f.id, f.name, f.espn_id, f.wins, f.losses, f.draws
                FROM fighters f
                JOIN fights fi ON (fi.fighter_red_id = f.id OR fi.fighter_blue_id = f.id)
                JOIN events e ON e.id = fi.event_id
                WHERE NOT (fi.winner_id IS NULL AND fi.method IS NULL)
                  AND e.event_date >= (CURRENT_DATE - (%s || ' days')::interval)
                ORDER BY f.name
                """,
                (days,),
            )
        rows = [
            # Keep espn_id as-is (str | None); str(None) would become the truthy
            # literal "None" and be mistaken for a real id.
            (int(r[0]), r[1], r[2], int(r[3] or 0), int(r[4] or 0), int(r[5] or 0))
            for r in cursor.fetchall()
        ]
    rows = rows[offset:]
    return rows[:limit] if limit is not None else rows


def _plan_corrected(
    fighter: "tuple[int, str | None, str | None, int, int, int]",
    corrections: list[CorreccionRecord],
    fetch_record: RecordFetcher,
    fetch_events: EventsFetcher | None,
    counts: dict[str, int],
) -> dict | None:
    """Decide the write for a fighter with manual corrections. Returns the planned
    change, or None when nothing must be written.

    Runs INSTEAD of the generic path, so (a) the correction is applied before the
    "rec == stored -> unchanged" shortcut (stored == ESPN == 8-1-0 today) and (b)
    the name fallback is never reached: it would find the same ESPN athlete and
    _name_change_is_safe((8,0,0), (8,1,0)) would let 8-1-0 back in.
    """
    fid, name, espn_id, wins, losses, draws = fighter
    stored = (wins, losses, draws)
    corrected_espn_id = corrections[0].espn_id
    if espn_id and espn_id != corrected_espn_id:
        LOGGER.warning(
            "%s (id=%d): stored espn_id %s differs from the corrected one %s; "
            "using the corrected (hand-verified) id",
            name, fid, espn_id, corrected_espn_id,
        )

    try:
        rec = fetch_record(corrected_espn_id)
    except Exception as exc:  # noqa: BLE001 - one failure must not stop the sweep
        LOGGER.warning(
            "ESPN fetch failed for %s (espn_id=%s): %s", name, corrected_espn_id, exc
        )
        rec = None
    if rec is None:
        counts["unresolved"] += 1
        LOGGER.info(
            "%s (id=%d) has a record correction: no name fallback, skipped", name, fid
        )
        return None
    counts["resolved"] += 1

    payload = None
    if fetch_events is not None:
        try:
            payload = fetch_events(corrected_espn_id)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "ESPN eventsMap fetch failed for %s (espn_id=%s): %s",
                name, corrected_espn_id, exc,
            )
    verdict = evaluar_correcciones(corrections, rec, payload)

    if verdict.estado == SIN_VERIFICAR:
        counts["correcciones_sin_verificar"] += len(verdict.lineas)
        LOGGER.warning(
            "Record correction for %s (id=%d) NOT VERIFIED, nothing written "
            "this run: %s",
            name, fid, verdict.detalle,
        )
        return None
    if verdict.estado == YA_NO_NECESARIA:
        counts["correcciones_ya_no_necesarias"] += len(verdict.lineas)
        LOGGER.warning(
            "Record correction for %s (id=%d) MAY NO LONGER BE NEEDED, nothing "
            "written: %s. "
            "Once confirmed, delete it from record_correcciones.CORRECCIONES.",
            name, fid, verdict.detalle,
        )
        return None

    counts["correcciones_aplicadas"] += len(verdict.lineas)
    new = verdict.record
    LOGGER.info(
        "Record correction applied to %s (id=%d): %s -> %s",
        name, fid, verdict.detalle, "-".join(map(str, new)),
    )
    if new == stored:
        counts["unchanged"] += 1
        return None

    change = {
        "id": fid, "name": name, "old": [wins, losses, draws], "new": list(new),
        "espn_overall": list(rec), "correccion": verdict.detalle,
    }
    if sum(new) > sum(stored):
        return change  # more bouts: the normal monotonic bump writes it
    removed = tuple(r - n for r, n in zip(rec, new))
    if not _corrected_write_is_safe(stored, new, removed):
        # Same or fewer bouts than stored, and NOT only because of the subtracted
        # ones: a stale or regressing ESPN read (e.g. stored 9-0-0 after a real
        # win, ESPN still 8-1-0). The corrected writer is not monotonic; never let
        # it erase a real bout. Every other fighter refuses the same read too.
        counts["not_greater_skipped"] += 1
        LOGGER.info(
            "skip (would drop a real bout) %s: stored %d-%d-%d vs ESPN %d-%d-%d "
            "corrected %s",
            name, wins, losses, draws, rec[0], rec[1], rec[2], "-".join(map(str, new)),
        )
        return None
    # Same or fewer bouts than stored, and only because of the subtracted ones.
    change["writer"] = "corrected"
    change["removed"] = list(removed)
    return change


def refresh_records(
    *,
    dry_run: bool = False,
    days: int = 30,
    all_fighters: bool = False,
    limit: int | None = None,
    offset: int = 0,
    backup_path: str | None = None,
    connection=None,
    fetch_record: RecordFetcher | None = None,
    fetch_by_name: "Callable[[str], tuple[int, int, int] | None] | None" = None,
    fetch_events: EventsFetcher | None = None,
    correcciones: list[CorreccionRecord] | None = None,
    max_name_delta: int = DEFAULT_MAX_NAME_DELTA,
    delay: float = REQUEST_DELAY_SECONDS,
) -> dict[str, int]:
    """Refresh records for the target set. Returns counts.

    `connection`, `fetch_record`, `fetch_by_name`, `fetch_events` and
    `correcciones` are injectable for tests (no socket, no network). In
    production a live connection is opened, the fetchers share one HTTP session
    and the corrections are record_correcciones.CORRECCIONES. With an injected
    fetch_record and no fetch_events, a corrected fighter cannot be verified and
    is left untouched (no network).
    """
    counts = {
        "targets": 0,
        "resolved": 0,
        "resolved_by_name": 0,
        "updated": 0,
        "unchanged": 0,
        "unresolved": 0,
        "not_greater_skipped": 0,
        "name_rejected": 0,
        "correcciones_aplicadas": 0,
        "correcciones_ya_no_necesarias": 0,
        "correcciones_sin_verificar": 0,
    }
    corrections_by_fighter = correcciones_por_luchador(
        CORRECCIONES if correcciones is None else correcciones
    )

    if fetch_record is None:
        session = _build_session(get_settings())

        def fetch_record(espn_id: str):  # noqa: ANN001
            return _fetch_espn_record(session, espn_id)

        if fetch_by_name is None:
            # Fallback for fighters without an espn_id: search ESPN by name and
            # take the top athlete's overall record (guarded by _name_change_is_safe).
            def fetch_by_name(name: str):  # noqa: ANN001
                found = _search_espn_athlete(session, name)
                return _fetch_espn_record(session, found[0]) if found else None

        if fetch_events is None:
            # The athlete career payload espn_fight_history already reads; one extra
            # request, and only for fighters with a correction.
            def fetch_events(espn_id: str):  # noqa: ANN001
                return fetch_career(session, espn_id)

    @contextlib.contextmanager
    def _conn():
        # An injected connection (tests) is reused as-is; otherwise open a fresh
        # short-lived one. IMPORTANT: the DB connection is only held during the
        # fast read (targets) and fast write phases, NEVER during the slow ESPN
        # fetch loop — Neon drops a connection left idle for minutes.
        if connection is not None:
            yield connection
        else:
            with connect(get_settings().database_url) as c:
                yield c

    # Phase A: read the target set (short-lived connection).
    with _conn() as conn:
        targets = _get_target_fighters(
            conn, days=days, all_fighters=all_fighters, limit=limit, offset=offset
        )
    counts["targets"] = len(targets)
    scope = "ALL fighters with espn_id" if all_fighters else f"fought in last {days} days"
    LOGGER.info("Targets (%s): %d", scope, len(targets))

    # Phase B: fetch every target's ESPN overall and decide who moves — with NO
    # DB connection held. Collect the planned changes so we can back up BEFORE
    # writing anything.
    planned: list[dict] = []
    for idx, (fid, name, espn_id, w, l, d) in enumerate(targets, 1):
        corrections = corrections_by_fighter.get(fid)
        if corrections:
            change = _plan_corrected(
                (fid, name, espn_id, w, l, d), corrections,
                fetch_record, fetch_events, counts,
            )
            if change is not None:
                planned.append(change)
            if delay:
                time.sleep(delay)
            continue

        rec = None
        via = None
        if espn_id:
            try:
                rec = fetch_record(espn_id)
            except Exception as exc:  # noqa: BLE001 - one failure must not stop the sweep
                LOGGER.warning("ESPN fetch failed for %s (espn_id=%s): %s", name, espn_id, exc)
                rec = None
            if rec is not None:
                via = "id"
        if rec is None and fetch_by_name is not None:
            try:
                rec = fetch_by_name(name)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("ESPN name search failed for %s: %s", name, exc)
                rec = None
            if rec is not None:
                via = "name"
        if delay:
            time.sleep(delay)

        stored = (w, l, d)
        if rec is None:
            counts["unresolved"] += 1
            continue
        counts["resolved"] += 1
        if rec == stored:
            counts["unchanged"] += 1
            continue

        if via == "name":
            # Ambiguous key: accept only a small, per-component non-decreasing bump.
            counts["resolved_by_name"] += 1
            if _name_change_is_safe(stored, rec, max_name_delta):
                planned.append({"id": fid, "name": name, "old": [w, l, d], "new": list(rec)})
            else:
                counts["name_rejected"] += 1
                LOGGER.info(
                    "reject name-match %s: stored %d-%d-%d vs ESPN %d-%d-%d (unsafe shape)",
                    name, w, l, d, rec[0], rec[1], rec[2],
                )
            continue

        # via == "id": reliable key, monotonic total guard only.
        if (rec[0] + rec[1] + rec[2]) > (w + l + d):
            planned.append({"id": fid, "name": name, "old": [w, l, d], "new": list(rec)})
        else:
            # Fewer total bouts but different split (overturn or bad read). The SQL
            # guard would reject it too -> report, never write.
            counts["not_greater_skipped"] += 1
            LOGGER.info(
                "skip (not greater) %s: stored %d-%d-%d vs ESPN %d-%d-%d",
                name, w, l, d, rec[0], rec[1], rec[2],
            )

        if idx % 50 == 0:
            LOGGER.info("Fetched %d/%d — planned changes so far=%d", idx, len(targets), len(planned))

    # Backup the OLD values of everything we intend to touch (always, even in
    # dry-run, so there is a record of what a real run would do).
    if backup_path and planned:
        with open(backup_path, "w", encoding="utf-8") as fh:
            json.dump(planned, fh, ensure_ascii=False, indent=2)
        LOGGER.info("Backup of %d planned changes written to %s", len(planned), backup_path)

    if dry_run:
        for change in planned:
            counts["updated"] += 1
            suffix = ""
            if "correccion" in change:
                suffix = " (correction: %s; writer: %s)" % (
                    change["correccion"],
                    "corrected" if change.get("writer") == "corrected" else "monotonic bump",
                )
            LOGGER.info(
                "[dry-run] %s: %s -> %s%s",
                change["name"], "-".join(map(str, change["old"])), "-".join(map(str, change["new"])),
                suffix,
            )
        return counts

    # Phase C: apply (short-lived connection; the SQL guards are the authoritative
    # gate: monotonic for everyone, "only the subtracted bouts, per component" for
    # a corrected fighter -- so a record another run advanced between Phase B and
    # here is never lowered). Fast, so the connection never goes idle long enough
    # to be dropped.
    if planned:
        with _conn() as conn:
            for change in planned:
                wins, losses, draws = change["new"]
                if change.get("writer") == "corrected":
                    written = set_fighter_record_corrected(
                        conn, change["id"], wins=wins, losses=losses, draws=draws,
                        removed=tuple(change["removed"]),
                    )
                else:
                    written = bump_fighter_record(
                        conn, change["id"], wins=wins, losses=losses, draws=draws
                    )
                if written:
                    conn.commit()
                    counts["updated"] += 1
                else:
                    # Guard rejected it (race: record already advanced). Not an error.
                    counts["not_greater_skipped"] += 1

    return counts


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(
        description="Refresh stored fighter W-L-D from ESPN overall record (monotonic)."
    )
    parser.add_argument("--dry-run", action="store_true", help="Report only; do not write.")
    parser.add_argument("--days", type=int, default=30, help="Refresh fighters with a completed fight in the last N days (default 30).")
    parser.add_argument("--all", action="store_true", dest="all_fighters", help="Refresh EVERY fighter with an espn_id (ignores --days).")
    parser.add_argument("--limit", type=int, default=None, help="Process at most this many fighters.")
    parser.add_argument("--offset", type=int, default=0, help="Skip the first N target fighters (for chunking a big --all run).")
    parser.add_argument("--backup", metavar="PATH", default=None, help="Write a JSON backup of the OLD values of changed fighters.")
    parser.add_argument("--delay", type=float, default=REQUEST_DELAY_SECONDS, help=f"Seconds between ESPN requests (default {REQUEST_DELAY_SECONDS}).")
    parser.add_argument("--probe", nargs="+", metavar="NAME", help="Resolve ESPN overall record by name (no DB, no write).")
    args = parser.parse_args()

    if args.probe:
        session = _build_session(get_settings())
        for name in args.probe:
            print(json.dumps({"name": name, "record": resolve_record(session, name)}, ensure_ascii=False))
        return

    counts = refresh_records(
        dry_run=args.dry_run,
        days=args.days,
        all_fighters=args.all_fighters,
        limit=args.limit,
        offset=args.offset,
        backup_path=args.backup,
        delay=args.delay,
    )
    print(json.dumps(counts, indent=2))
    if counts.get("correcciones_ya_no_necesarias"):
        # Red on purpose: notify-on-failure watches "Refresh fighter records" and
        # opens an Issue. The rest of the run was already applied; only the
        # fighter whose correction went stale was left untouched. In the live
        # loop the step is continue-on-error, so the night is not affected.
        LOGGER.error(
            "%d record correction(s) are no longer needed (ESPN changed): delete them "
            "from src/scrapers/record_correcciones.py. Those fighters were not written.",
            counts["correcciones_ya_no_necesarias"],
        )
        sys.exit(1)


if __name__ == "__main__":
    main()

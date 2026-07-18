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
import time
from typing import Callable

from .config import get_settings
from .db import connect
from .enrich_ranked import _build_session, _search_espn_athlete
from .enrich_records_espn import _fetch_espn_record, resolve_record
from .logging_config import configure_logging
from .repositories.fighters import bump_fighter_record

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
    max_name_delta: int = DEFAULT_MAX_NAME_DELTA,
    delay: float = REQUEST_DELAY_SECONDS,
) -> dict[str, int]:
    """Refresh records for the target set. Returns counts.

    `connection` and `fetch_record` are injectable for tests (no socket, no
    network). In production a live connection is opened and fetch_record wraps
    _fetch_espn_record over a shared HTTP session.
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
    }

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
            LOGGER.info(
                "[dry-run] %s: %s -> %s",
                change["name"], "-".join(map(str, change["old"])), "-".join(map(str, change["new"])),
            )
        return counts

    # Phase C: apply (short-lived connection; monotonic guard is the authoritative
    # gate at SQL level). Fast, so the connection never goes idle long enough to
    # be dropped.
    if planned:
        with _conn() as conn:
            for change in planned:
                wins, losses, draws = change["new"]
                if bump_fighter_record(conn, change["id"], wins=wins, losses=losses, draws=draws):
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


if __name__ == "__main__":
    main()

"""Repair bouts whose per-round stats never covered the whole fight.

A bout consolidated WHILE ufcstats was still filling the card in keeps that
partial page forever. Fight 14895 (UFC 330, Njokuani vs Alvarez) was read when
only round 1 was published, so fight_stats_rounds got 2 rows (round 1) and —
because parse_fight_stats SUMS the per-round rows it can see — fight_stats got
round 1's numbers stored as the fight TOTALS: 46/69 and 22/50 where ufcstats now
says 155/229 and 74/164. That is not missing data, it is WRONG data on the
published fight card, and nothing re-qualifies it: backfill_results asks for
">= 2 fight_stats rows" and ">= 2 distinct per-round fighters", and the bout has
both.

The detector counts the rounds that carry BOTH corners' rows and compares them
against fights.end_round — the same yardstick backfill_results uses, so the two
never disagree about which bouts are damaged. Contrasting the totals against the
sum of the rounds does NOT see this case — the bad totals ARE exactly that sum.

Safety:
  - re-reads the fight off ufcstats and VALIDATES before touching anything:
    rounds 1..end_round for BOTH fighters, no unparsed metric, and the rounds
    must add up to the page totals; anything short leaves the database exactly
    as it was and exits 1 — ufcstats serving a still-incomplete page is the very
    bug this repairs, so an empty/partial parse is never "nothing to do";
  - fighters are resolved through the bout's own corners (corner-swap tolerant
    name matching, _match_fight / fighter_id_for) with the ufcstats fighter
    source_id as fallback, never positionally: event 1064 has Njokuani in two
    bouts and a hand-rolled match would write the stats onto the wrong one;
  - deleting the stale per-round rows and rewriting rounds + totals happen in
    ONE transaction, so the fight card is never left without any stats;
  - DRY-RUN by default — the SESSION itself is opened read-only, so Postgres
    rejects any write; it prints the before/after row by row and touches nothing;
  - --apply commits with a hard guard on the deleted ids and on the resulting
    row counts, rolling back on any surprise.

Sweeping (no --fight-id) is the default because the detector is cheap and finds
the damaged bouts itself; measured against the real DB on 2026-08-16 it returns
exactly one row (14895), so nothing is hardcoded and a future repeat is caught.

    python -m scripts.repair_incomplete_round_stats                    # dry-run, sweep
    python -m scripts.repair_incomplete_round_stats --fight-id 14895   # dry-run, one bout
    python -m scripts.repair_incomplete_round_stats --fight-id 14895 --apply
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

import psycopg2

from src.scrapers.backfill_results import (
    UFCSTATS_EVENTS_URL,
    _get_bouts,
    _match_event,
    _match_fight,
)
from src.scrapers.config import get_settings
from src.scrapers.http import UfcStatsClient
from src.scrapers.models import FightStatsRecord, FightStatsRoundRecord
from src.scrapers.parsers.events import parse_events_index
from src.scrapers.parsers.fights import (
    build_fight_stats_record,
    build_fight_stats_round_record,
    parse_event_fights,
    parse_fight_stats,
    parse_fight_stats_rounds,
)
from src.scrapers.repositories.fighters import get_fighter_id_by_source
from src.scrapers.repositories.fights import upsert_fight_stats, upsert_fight_stats_rounds

if hasattr(sys.stdout, "reconfigure"):  # fighter names carry accents on Windows
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

SEPARATOR = "=" * 74

# Columns of each table, in the order the SELECTs below read them AND with the
# exact names of the record dataclass fields, so the stored row and the row we
# would write can be diffed field by field with getattr().
TOTAL_COLUMNS = (
    "sig_strikes_landed", "sig_strikes_attempted",
    "takedowns_landed", "takedowns_attempted",
    "submission_attempts", "control_time_seconds", "knockdowns",
    "sig_str_head_landed", "sig_str_head_attempted",
    "sig_str_body_landed", "sig_str_body_attempted",
    "sig_str_leg_landed", "sig_str_leg_attempted",
    "sig_str_distance_landed", "sig_str_distance_attempted",
    "sig_str_clinch_landed", "sig_str_clinch_attempted",
    "sig_str_ground_landed", "sig_str_ground_attempted",
)
ROUND_COLUMNS = (
    "sig_strikes_landed", "sig_strikes_attempted",
    "takedowns_landed", "takedowns_attempted",
    "submission_attempts", "control_time_seconds", "knockdowns",
)

# How each metric is printed: a landed/attempted pair, or a single number.
PAIR_LABELS = (
    ("sig strikes", "sig_strikes_landed", "sig_strikes_attempted"),
    ("takedowns", "takedowns_landed", "takedowns_attempted"),
    ("head", "sig_str_head_landed", "sig_str_head_attempted"),
    ("body", "sig_str_body_landed", "sig_str_body_attempted"),
    ("leg", "sig_str_leg_landed", "sig_str_leg_attempted"),
    ("distance", "sig_str_distance_landed", "sig_str_distance_attempted"),
    ("clinch", "sig_str_clinch_landed", "sig_str_clinch_attempted"),
    ("ground", "sig_str_ground_landed", "sig_str_ground_attempted"),
)
SINGLE_LABELS = (
    ("sub attempts", "submission_attempts"),
    ("control time (s)", "control_time_seconds"),
    ("knockdowns", "knockdowns"),
)

# THE detector. A bout that ended in round N must carry a COMPLETE per-round row
# pair for each of rounds 1..N; anything fewer means the page was read before
# ufcstats had finished publishing it. "Complete" (both corners) and not merely
# "distinct rounds" ON PURPOSE: it is the same yardstick backfill_results'
# events_needing_results_sql now uses, so this script never denies a symptom the
# daily cron is re-queueing — the half-round case (one corner's row lost to
# round_stats_unmatched) is precisely the one the cron cannot fix on its own,
# because its upsert never deletes and the name will fail to match again.
# Cancelled bouts are excluded (they legitimately have no stats at all) and
# end_round IS NULL bouts too (nothing to compare against). The GROUP BY carries
# the display fields so one query gives the whole "before" headline without a
# second round trip.
CANDIDATES = """
    SELECT f.id, f.event_id, e.name, e.event_date, f.method, f.end_round,
           f.fighter_red_id, f.fighter_blue_id,
           COALESCE(red.name, f.fighter_red_name) AS red_name,
           COALESCE(blue.name, f.fighter_blue_name) AS blue_name,
           COUNT(*) AS round_rows,
           ARRAY_AGG(DISTINCT fsr.round ORDER BY fsr.round) AS rounds_present
    FROM fights f
    JOIN events e ON e.id = f.event_id
    JOIN fight_stats_rounds fsr ON fsr.fight_id = f.id
    LEFT JOIN fighters red ON red.id = f.fighter_red_id
    LEFT JOIN fighters blue ON blue.id = f.fighter_blue_id
    WHERE f.end_round IS NOT NULL
      AND f.status IS DISTINCT FROM 'cancelled'
      {extra}
    GROUP BY f.id, f.event_id, e.name, e.event_date, f.method, f.end_round,
             red.name, blue.name
    HAVING (SELECT COUNT(*) FROM (
              SELECT 1 FROM fight_stats_rounds cr
              WHERE cr.fight_id = f.id
              GROUP BY cr.round
              HAVING COUNT(DISTINCT cr.fighter_id) >= 2
            ) complete_rounds) < f.end_round
    ORDER BY e.event_date DESC, f.id
"""

CURRENT_TOTALS = f"""
    SELECT fs.fighter_id, {', '.join('fs.' + column for column in TOTAL_COLUMNS)}
    FROM fight_stats fs
    WHERE fs.fight_id = %s
    ORDER BY fs.fighter_id
"""

CURRENT_ROUNDS = f"""
    SELECT fsr.id, fsr.round, fsr.fighter_id,
           {', '.join('fsr.' + column for column in ROUND_COLUMNS)}
    FROM fight_stats_rounds fsr
    WHERE fsr.fight_id = %s
    ORDER BY fsr.round, fsr.fighter_id
"""


@dataclass(frozen=True)
class Candidate:
    """One damaged bout as the detector sees it, plus its stored rows."""

    fight_id: int
    event_id: int
    event_name: str
    event_date: object
    method: str | None
    end_round: int
    red_id: int | None
    blue_id: int | None
    red_name: str
    blue_name: str
    round_rows: int
    rounds_present: list[int]
    stored_totals: dict[int, dict]  # fighter_id -> column -> value
    stored_rounds: dict[tuple[int, int], dict]  # (round, fighter_id) -> row


@dataclass(frozen=True)
class Plan:
    """What we would write for a candidate, already validated against ufcstats."""

    candidate: Candidate
    detail_url: str
    stale_round_ids: list[int]
    round_records: list[FightStatsRoundRecord]
    total_records: list[FightStatsRecord]
    names: dict[int, str]


def _open_connection(dsn: str, readonly: bool):
    """Open the connection with the SESSION read-only unless we are applying.

    In dry-run it is Postgres that refuses every write, not the discipline of
    the code — the safety pattern of scripts/clean_duplicate_fights.py.
    """
    connection = psycopg2.connect(dsn)
    connection.set_session(readonly=readonly)
    return connection


def _num(value) -> str:
    return "-" if value is None else str(value)


def _pair(row, landed_column: str, attempted_column: str) -> str:
    return f"{_num(row.get(landed_column))}/{_num(row.get(attempted_column))}"


def _round_line(row: dict) -> str:
    return (
        f"sig {_pair(row, 'sig_strikes_landed', 'sig_strikes_attempted')}"
        f"  td {_pair(row, 'takedowns_landed', 'takedowns_attempted')}"
        f"  sub {_num(row.get('submission_attempts'))}"
        f"  ctrl {_num(row.get('control_time_seconds'))}s"
        f"  kd {_num(row.get('knockdowns'))}"
    )


def _as_row(record, columns) -> dict:
    return {column: getattr(record, column) for column in columns}


def find_candidates(connection, fight_id: int | None) -> list[Candidate]:
    """Bouts whose per-round rows do not cover their end_round, newest first."""
    extra, params = ("AND f.id = %s", [fight_id]) if fight_id else ("", [])
    candidates: list[Candidate] = []
    with connection.cursor() as cursor:
        cursor.execute(CANDIDATES.format(extra=extra), params)
        rows = cursor.fetchall()
        for row in rows:
            fight = int(row[0])
            cursor.execute(CURRENT_TOTALS, (fight,))
            totals = {
                int(total[0]): dict(zip(TOTAL_COLUMNS, total[1:]))
                for total in cursor.fetchall()
            }
            cursor.execute(CURRENT_ROUNDS, (fight,))
            rounds = {}
            for stored in cursor.fetchall():
                key = (int(stored[1]), int(stored[2]))
                rounds[key] = dict(
                    zip(("id", "round", "fighter_id") + ROUND_COLUMNS, stored)
                )
            candidates.append(
                Candidate(
                    fight_id=fight,
                    event_id=int(row[1]),
                    event_name=row[2],
                    event_date=row[3],
                    method=row[4],
                    end_round=int(row[5]),
                    red_id=row[6],
                    blue_id=row[7],
                    red_name=row[8] or "",
                    blue_name=row[9] or "",
                    round_rows=int(row[10]),
                    rounds_present=[int(value) for value in (row[11] or [])],
                    stored_totals=totals,
                    stored_rounds=rounds,
                )
            )
    return candidates


def _resolve_fighter_id(connection, bout, parsed) -> int | None:
    """The bout corner this parsed fighter belongs to, name first, id second.

    fighter_id_for is the same corner-swap tolerant matcher backfill_results
    uses; when the name diverges too much we fall back to the ufcstats
    source_id, which does not depend on spelling (fighter_blue_name for 14895
    is stored mojibaked: 'Joel ?lvarez').

    The fallback is CONFINED to this bout's own corners. get_fighter_id_by_source
    searches the whole fighters table, so a duplicate fighter row (there is one
    such name pair in the DB today) would hand back somebody who never fought
    this bout — and this script deletes the stored rows before rewriting, so
    that id would end up ON the fight card. Anything outside the two corners is
    treated as "unresolved": the caller then refuses the whole bout and nothing
    is deleted.
    """
    fighter_id = bout.fighter_id_for(parsed.fighter_name)
    if fighter_id is not None:
        return fighter_id
    if parsed.fighter_source_id:
        by_source = get_fighter_id_by_source(connection, "ufcstats", parsed.fighter_source_id)
        if by_source is not None and by_source in (bout.red_id, bout.blue_id):
            return by_source
    return None


def plan_repair(connection, client, settings, candidate: Candidate, index) -> tuple[Plan | None, str | None]:
    """Re-read the bout off ufcstats and validate it; (plan, None) or (None, reason).

    Everything is checked BEFORE anything is deleted: a page that is still
    incomplete (or an anti-bot response with no tables at all) must leave the
    database untouched, otherwise this script reproduces the very bug it fixes.
    """
    detail_url = _match_event(candidate.event_name, candidate.event_date, index)
    if detail_url is None:
        return None, f"no ufcstats event matches {candidate.event_name!r} ({candidate.event_date})"
    bouts = [bout for bout in _get_bouts(connection, candidate.event_id) if bout.id == candidate.fight_id]
    if not bouts:
        return None, "the bout is no longer in the event"
    bout = bouts[0]
    event_page = client.fetch(detail_url)
    fight = _match_fight(bout, parse_event_fights(event_page.soup, settings))
    if fight is None:
        return None, f"no ufcstats fight matches {bout.red_name} vs {bout.blue_name}"
    fight_page = client.fetch(fight.detail_url)
    parsed_rounds = parse_fight_stats_rounds(fight_page.soup)
    parsed_totals = parse_fight_stats(fight_page.soup)
    if not parsed_rounds:
        return None, f"ufcstats serves no per-round table at {fight.detail_url}"

    names: dict[int, str] = {}
    round_records: list[FightStatsRoundRecord] = []
    for parsed in parsed_rounds:
        if parsed.stats.get("sig_strikes_attempted") is None:
            return None, f"round {parsed.round} of {parsed.fighter_name!r} did not parse (sig strikes empty)"
        fighter_id = _resolve_fighter_id(connection, bout, parsed)
        if fighter_id is None:
            return None, f"cannot map round-stats fighter {parsed.fighter_name!r} onto a corner"
        names[fighter_id] = parsed.fighter_name
        round_records.append(build_fight_stats_round_record(candidate.fight_id, fighter_id, parsed))

    # The page must cover rounds 1..end_round for BOTH fighters: that is exactly
    # what was missing when the bad rows were written.
    expected = list(range(1, candidate.end_round + 1))
    by_fighter: dict[int, list[int]] = {}
    for record in round_records:
        by_fighter.setdefault(record.fighter_id, []).append(record.round)
    if len(by_fighter) != 2:
        return None, f"ufcstats gives per-round rows for {len(by_fighter)} fighter(s), expected 2"
    for fighter_id, rounds in by_fighter.items():
        if sorted(rounds) != expected:
            return None, (
                f"ufcstats still publishes rounds {sorted(rounds)} for "
                f"{names[fighter_id]}, expected {expected}"
            )

    total_records: list[FightStatsRecord] = []
    for parsed in parsed_totals:
        if parsed.stats.get("sig_strikes_attempted") is None:
            return None, f"totals of {parsed.fighter_name!r} did not parse (sig strikes empty)"
        fighter_id = _resolve_fighter_id(connection, bout, parsed)
        if fighter_id is None:
            return None, f"cannot map totals fighter {parsed.fighter_name!r} onto a corner"
        names.setdefault(fighter_id, parsed.fighter_name)
        total_records.append(build_fight_stats_record(candidate.fight_id, fighter_id, parsed))
    if {record.fighter_id for record in total_records} != set(by_fighter):
        return None, "the page totals and the per-round rows are not about the same two fighters"

    # upsert_fight_stats has NO COALESCE (that is what lets it correct the fake
    # totals), so a metric this scrape failed to parse would be written as NULL
    # over a stored number. Refuse instead of degrading a column.
    for record in total_records:
        stored = candidate.stored_totals.get(record.fighter_id, {})
        losses = [
            column for column in TOTAL_COLUMNS
            if stored.get(column) is not None and getattr(record, column) is None
        ]
        if losses:
            return None, (
                f"the page would write NULL over stored values of "
                f"{names[record.fighter_id]}: {losses}"
            )

    # Coherence: the rounds we are about to write must add up to the totals we
    # are about to write. If they do not, the page is being served mid-update
    # and neither number can be trusted.
    for record in total_records:
        rounds = [row for row in round_records if row.fighter_id == record.fighter_id]
        landed = sum(row.sig_strikes_landed or 0 for row in rounds)
        attempted = sum(row.sig_strikes_attempted or 0 for row in rounds)
        if (landed, attempted) != (record.sig_strikes_landed, record.sig_strikes_attempted):
            return None, (
                f"rounds add up to {landed}/{attempted} but the page totals say "
                f"{record.sig_strikes_landed}/{record.sig_strikes_attempted} for "
                f"{names[record.fighter_id]}"
            )

    stale = sorted(int(row["id"]) for row in candidate.stored_rounds.values())
    return Plan(
        candidate=candidate,
        detail_url=fight.detail_url,
        stale_round_ids=stale,
        round_records=sorted(round_records, key=lambda r: (r.round, r.fighter_id)),
        total_records=sorted(total_records, key=lambda r: r.fighter_id),
        names=names,
    ), None


def print_before(candidate: Candidate) -> None:
    names = {candidate.red_id: candidate.red_name, candidate.blue_id: candidate.blue_name}
    expected_rows = candidate.end_round * 2
    print(f"  event {candidate.event_id} {candidate.event_name!r} ({candidate.event_date})")
    print(f"  {candidate.red_name} (id {candidate.red_id}) vs {candidate.blue_name} (id {candidate.blue_id})")
    print(f"  method={candidate.method} end_round={candidate.end_round}")
    print(
        f"  per-round rows stored: {candidate.round_rows} "
        f"(rounds {candidate.rounds_present}) — should be {expected_rows} "
        f"(rounds {list(range(1, candidate.end_round + 1))})"
    )
    print("\n  BEFORE — fight_stats (the totals published on the fight card today)")
    for fighter_id, row in sorted(candidate.stored_totals.items()):
        print(f"    {fighter_id} {names.get(fighter_id, '?')}: {_round_line(row)}")
    if not candidate.stored_totals:
        print("    (no rows)")
    print("  BEFORE — fight_stats_rounds")
    for (round_number, fighter_id), row in sorted(candidate.stored_rounds.items()):
        print(
            f"    row {row['id']}  R{round_number} {fighter_id} "
            f"{names.get(fighter_id, '?')}: {_round_line(row)}"
        )
    if not candidate.stored_rounds:
        print("    (no rows)")


def print_after(plan: Plan) -> None:
    candidate = plan.candidate
    print(f"\n  ufcstats -> {plan.detail_url}")
    print("  AFTER — fight_stats (totals to write, old -> new)")
    for record in plan.total_records:
        stored = candidate.stored_totals.get(record.fighter_id, {})
        new = _as_row(record, TOTAL_COLUMNS)
        print(f"    {record.fighter_id} {plan.names.get(record.fighter_id, '?')}")
        for label, landed, attempted in PAIR_LABELS:
            before = _pair(stored, landed, attempted)
            after = _pair(new, landed, attempted)
            flag = "" if before == after else "   <-- changes"
            print(f"        {label:<16} {before:>9}  ->  {after:>9}{flag}")
        for label, column in SINGLE_LABELS:
            before = _num(stored.get(column))
            after = _num(new.get(column))
            flag = "" if before == after else "   <-- changes"
            print(f"        {label:<16} {before:>9}  ->  {after:>9}{flag}")
    print("  AFTER — fight_stats_rounds (rows to write)")
    for record in plan.round_records:
        key = (record.round, record.fighter_id)
        stored = candidate.stored_rounds.get(key)
        new = _as_row(record, ROUND_COLUMNS)
        if stored is None:
            tag = "NEW"
        elif all(stored.get(column) == new.get(column) for column in ROUND_COLUMNS):
            tag = "unchanged"
        else:
            tag = f"REWRITTEN (was {_round_line(stored)})"
        print(
            f"    R{record.round} {record.fighter_id} "
            f"{plan.names.get(record.fighter_id, '?')}: {_round_line(new)}   [{tag}]"
        )
    written = {(record.round, record.fighter_id) for record in plan.round_records}
    dropped = [row for key, row in sorted(candidate.stored_rounds.items()) if key not in written]
    for row in dropped:
        print(f"    row {row['id']} R{row['round']} {row['fighter_id']}: DROPPED (not on ufcstats)")
    print(
        f"\n  PLAN: delete {len(plan.stale_round_ids)} per-round row(s) "
        f"{plan.stale_round_ids}, write {len(plan.round_records)} per-round row(s) "
        f"and {len(plan.total_records)} totals row(s), in one transaction."
    )


def apply_plan(connection, plan: Plan) -> None:
    """Delete the stale per-round rows and rewrite rounds + totals, atomically.

    The delete RETURNS its ids so the guard compares what actually went against
    what the dry-run announced; the final count check re-reads the table inside
    the same transaction. Any mismatch rolls back the WHOLE fight — a half
    repaired bout would be worse than the damage we came to fix.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM fight_stats_rounds WHERE fight_id = %s RETURNING id",
            (plan.candidate.fight_id,),
        )
        deleted = sorted(int(row[0]) for row in cursor.fetchall())
    if deleted != plan.stale_round_ids:
        connection.rollback()
        raise SystemExit(
            f"GUARD TRIPPED: expected to delete {plan.stale_round_ids} but "
            f"deleted {deleted}; rolled back, nothing changed."
        )
    for record in plan.round_records:
        upsert_fight_stats_rounds(connection, record)
    for record in plan.total_records:
        upsert_fight_stats(connection, record)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*), COUNT(DISTINCT round) FROM fight_stats_rounds WHERE fight_id = %s",
            (plan.candidate.fight_id,),
        )
        rows, distinct_rounds = cursor.fetchone()
    if int(rows) != len(plan.round_records) or int(distinct_rounds) != plan.candidate.end_round:
        connection.rollback()
        raise SystemExit(
            f"GUARD TRIPPED: fight {plan.candidate.fight_id} ended with {rows} row(s) "
            f"over {distinct_rounds} round(s), expected {len(plan.round_records)} over "
            f"{plan.candidate.end_round}; rolled back, nothing changed."
        )
    connection.commit()
    print(
        f"  APPLIED: fight {plan.candidate.fight_id} now has {rows} per-round rows "
        f"over {distinct_rounds} rounds, and {len(plan.total_records)} totals rows. Committed."
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Repair bouts whose fight_stats_rounds do not cover fights.end_round."
    )
    parser.add_argument(
        "--fight-id", type=int, default=None,
        help="only this bout (default: sweep every bout with the symptom)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="execute the repair (default: dry-run)",
    )
    args = parser.parse_args()

    settings = get_settings()
    connection = _open_connection(settings.database_url, readonly=not args.apply)
    try:
        candidates = find_candidates(connection, args.fight_id)
        scope = f"fight {args.fight_id}" if args.fight_id else "every bout in the database"
        print(SEPARATOR)
        print("INCOMPLETE PER-ROUND STATS — scope:", scope)
        print(f"mode: {'APPLY (writes)' if args.apply else 'DRY-RUN (session is read-only)'}")
        print(SEPARATOR)
        print(f"Bouts whose per-round rows do not cover end_round: {len(candidates)}")
        if not candidates:
            print("\nNothing to repair.")
            if args.fight_id:
                # Be explicit: "0 candidates" for a named bout means it does not
                # have THIS symptom, which is not the same as "it is healthy".
                print(
                    f"  fight {args.fight_id} either already covers its end_round, "
                    "has no per-round rows at all (that is backfill_results' job), "
                    "is cancelled, or has no end_round."
                )
            return

        client = UfcStatsClient(settings)
        index = parse_events_index(client.fetch(UFCSTATS_EVENTS_URL).soup, settings)
        print(f"ufcstats completed-events index (page 1): {len(index)} events")

        plans: list[Plan] = []
        blocked: list[tuple[Candidate, str]] = []
        for position, candidate in enumerate(candidates, start=1):
            print(f"\n{SEPARATOR}")
            print(f"Candidate {position}/{len(candidates)} — fight {candidate.fight_id}")
            print(SEPARATOR)
            print_before(candidate)
            plan, reason = plan_repair(connection, client, settings, candidate, index)
            if plan is None:
                blocked.append((candidate, reason or "unknown"))
                print(f"\n  SKIP — {reason}")
                print("  Nothing is deleted or written for this bout.")
                continue
            print_after(plan)
            plans.append(plan)

        print(f"\n{SEPARATOR}")
        print(f"Would repair {len(plans)} fight(s): {[plan.candidate.fight_id for plan in plans]}")
        print(f"Cannot repair {len(blocked)} fight(s): {[c.fight_id for c, _ in blocked]}")
        for candidate, reason in blocked:
            print(f"  fight {candidate.fight_id}: {reason}")

        if not args.apply:
            print("\nDRY-RUN — nothing written. Re-run with --apply to execute.")
            if blocked:
                raise SystemExit(1)
            return

        for plan in plans:
            apply_plan(connection, plan)
        if blocked:
            raise SystemExit(1)
    finally:
        connection.close()


if __name__ == "__main__":
    main()

"""Recover fight winners from UFCStats event pages (fast path).

Background
----------
The historical re-scrape stored ``fighter_red_id`` as the *first-listed*
fighter on each UFCStats event page. UFCStats lists the winner first, so the
stored red corner ended up equal to the winner for every decided fight, while
``winner_id`` was left NULL (the event-page winner parser never fired).

This script:
  1. Walks every completed UFCStats event page (one request per event).
  2. For each fight row reads the green "win" flag -> winner = first fighter.
     No "win" flag (draw / NC) -> no winner.
  3. Maps the winner's UFCStats id to our DB fighter id.
  4. Normalizes corners with a winner-independent rule (red = fighter whose
     UFCStats source_id sorts first) so the dataset is not degenerate.
  5. Sets ``winner_id`` (NULL for draws / NC).

The scraped winner map is cached to ``winners_scraped.json`` so the DB update
phase can be retried without re-fetching.

Safety
------
Writes are OFF by default (dry-run): the script prints what it would change and
exits. Pass ``--apply`` to mutate the DB. It only touches fights that were
actually present in the scrape, and it never clears an already-stored
``winner_id`` just because a fight was missing from (or unresolved by) the
scrape.

Usage::

    python -m src.scrapers.recover_winners_eventpage             # dry-run (no writes)
    python -m src.scrapers.recover_winners_eventpage --apply     # write to DB
    python -m src.scrapers.recover_winners_eventpage --use-cache # skip scraping
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parents[2] / ".env")

from .config import get_settings
from .db import connect
from .http import UfcStatsClient
from .parsers.events import parse_events_index
from .parsers.fights import _extract_column_values
from .utils import source_id_from_url

EVENTS_URL = "http://ufcstats.com/statistics/events/completed"
CACHE_PATH = Path("winners_scraped.json")


def _extract_row_result(row) -> tuple[str | None, str | None, str]:
    """Return (fight_source_id, winner_source_id|None, status) for an event row."""
    detail_url = row.get("data-link")
    fight_src = source_id_from_url(detail_url) if detail_url else None
    fighter_links = row.select("a[href*='/fighter-details/']")
    if not fight_src or len(fighter_links) < 2:
        return fight_src, None, "unparsed"
    fighter_srcs = [source_id_from_url(a.get("href")) for a in fighter_links[:2]]
    first_cell = row.select_one("td")
    result_vals = [v.lower() for v in _extract_column_values(first_cell)] if first_cell else []
    if "win" in result_vals:
        idx = result_vals.index("win")
        idx = idx if idx < 2 else 0
        return fight_src, fighter_srcs[idx], "decided"
    if any(v in ("draw", "nc") for v in result_vals):
        return fight_src, None, "draw_nc"
    return fight_src, None, "undetermined"


def scrape_winner_map(client: UfcStatsClient, settings) -> dict[str, dict]:
    """fight_source_id -> {winner_src, status} for every event row."""
    print(f"Fetching events index from {EVENTS_URL} ...", flush=True)
    index_pages = client.fetch_all_pages(EVENTS_URL)
    event_records = []
    seen = set()
    for page in index_pages:
        for rec in parse_events_index(page.soup, settings):
            if rec.detail_url not in seen:
                seen.add(rec.detail_url)
                event_records.append(rec)
    print(f"  -> {len(event_records)} eventos únicos", flush=True)

    winner_map: dict[str, dict] = {}
    status_counts: Counter = Counter()
    for i, rec in enumerate(event_records, 1):
        try:
            page = client.fetch(rec.detail_url)
        except Exception as exc:  # noqa: BLE001
            status_counts["event_fetch_error"] += 1
            print(f"  [{i}/{len(event_records)}] ERROR {rec.detail_url}: {exc}", flush=True)
            continue
        rows = page.soup.select("tr[data-link]")
        for row in rows:
            fight_src, winner_src, status = _extract_row_result(row)
            if not fight_src:
                continue
            winner_map[fight_src] = {"winner_src": winner_src, "status": status}
            status_counts[status] += 1
        if i % 25 == 0 or i == len(event_records):
            print(f"  [{i}/{len(event_records)}] {rec.event.name} | filas acumuladas={len(winner_map)}", flush=True)
    print("Status de filas:", dict(status_counts), flush=True)
    return winner_map


def compute_updates(
    winner_map: dict[str, dict],
    fighter_by_src: dict[str, int],
    fights: list[dict],
) -> tuple[list[tuple[int, int, int | None, int]], Counter]:
    """Pure planner: decide the (red, blue, winner, fight_id) update per fight.

    Two safety invariants enforced here (see the module docstring):

    * A fight that was NOT present in the scrape (``winner_map`` has no row for
      its source_id) is skipped entirely -- never updated, so a missing event
      page can never wipe corners or a stored winner.
    * An existing ``winner_id`` is never cleared to NULL "by absence": only a
      scraped, *confirmed* draw/NC sets the winner to NULL. An unresolved or
      undetermined scrape preserves whatever winner the DB already holds.
    """
    counts: Counter = Counter()
    updates: list[tuple[int, int, int | None, int]] = []  # red_new, blue_new, winner_id, fight_id
    for fr in fights:
        info = winner_map.get(fr["source_id"])
        if info is None:
            counts["skipped_not_scraped"] += 1
            continue

        current_winner = fr.get("winner_id")
        # winner-independent corner normalization (sort by fighter source_id)
        pair = sorted([(fr["red_src"], fr["fighter_red_id"]), (fr["blue_src"], fr["fighter_blue_id"])])
        red_new, blue_new = pair[0][1], pair[1][1]
        fight_ids = {fr["fighter_red_id"], fr["fighter_blue_id"]}

        status = info["status"]
        new_winner: int | None = None
        if status == "decided":
            wid = fighter_by_src.get(info["winner_src"])
            if wid is None:
                counts["winner_fighter_not_in_db"] += 1
            elif wid not in fight_ids:
                counts["winner_not_in_fight"] += 1
            else:
                new_winner = wid
                counts["decided"] += 1
        elif status == "draw_nc":
            counts["draw_nc"] += 1  # confirmed no winner -> leave new_winner None
        else:
            counts["undetermined"] += 1

        # Never turn a stored winner into NULL unless the scrape confirmed a
        # draw/NC. Covers decided-but-unresolved and undetermined statuses.
        if new_winner is None and current_winner is not None and status != "draw_nc":
            new_winner = current_winner
            counts["winner_preserved"] += 1

        if new_winner == red_new:
            counts["winner_is_red_after_norm"] += 1
        elif new_winner == blue_new:
            counts["winner_is_blue_after_norm"] += 1
        updates.append((red_new, blue_new, new_winner, fr["id"]))
    return updates, counts


def apply_updates(winner_map: dict[str, dict], apply: bool, settings) -> None:
    import psycopg2.extras

    with connect(settings.database_url) as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # fighter source_id -> id
        cur.execute("SELECT id, source_id FROM fighters WHERE source = 'ufcstats' AND source_id IS NOT NULL")
        fighter_by_src = {r["source_id"]: r["id"] for r in cur.fetchall()}
        # all ufcstats fights with both fighters' source_ids + the stored winner
        cur.execute(
            """
            SELECT f.id, f.source_id, f.fighter_red_id, f.fighter_blue_id, f.winner_id,
                   r.source_id AS red_src, b.source_id AS blue_src
            FROM fights f
            JOIN fighters r ON r.id = f.fighter_red_id
            JOIN fighters b ON b.id = f.fighter_blue_id
            WHERE f.source = 'ufcstats' AND f.source_id IS NOT NULL
            """
        )
        fights = cur.fetchall()

    updates, counts = compute_updates(winner_map, fighter_by_src, fights)

    print("\n=== Resumen de cómputo ===", flush=True)
    for k, v in sorted(counts.items()):
        print(f"  {k}: {v}", flush=True)
    decided = counts["decided"]
    if decided:
        bal = counts["winner_is_red_after_norm"] / decided
        print(f"  balance rojo/azul tras normalizar: red={bal:.3f} blue={1 - bal:.3f}", flush=True)

    if not apply:
        print(
            f"\n[DRY-RUN] No se escribió nada en la BD. "
            f"{len(updates)} fights se actualizarían. Usa --apply para escribir.",
            flush=True,
        )
        return

    print(f"\nAplicando {len(updates)} updates...", flush=True)
    with connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                UPDATE fights
                SET fighter_red_id = %s,
                    fighter_blue_id = %s,
                    winner_id = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                updates,
            )
        conn.commit()
    print("Commit OK.", flush=True)

    with connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM fights WHERE winner_id IS NULL")
            null_now = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM fights")
            total = cur.fetchone()[0]
    print(f"Estado final: winner_id NULL = {null_now} / {total}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Escribe en la BD (por defecto: dry-run)")
    parser.add_argument("--use-cache", action="store_true", help="Usa winners_scraped.json en vez de scrapear")
    args = parser.parse_args()

    settings = get_settings()

    if args.use_cache and CACHE_PATH.exists():
        print(f"Usando caché {CACHE_PATH}", flush=True)
        winner_map = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    else:
        client = UfcStatsClient(settings)
        winner_map = scrape_winner_map(client, settings)
        CACHE_PATH.write_text(json.dumps(winner_map), encoding="utf-8")
        print(f"Caché guardada en {CACHE_PATH} ({len(winner_map)} filas)", flush=True)

    apply_updates(winner_map, apply=args.apply, settings=settings)


if __name__ == "__main__":
    main()

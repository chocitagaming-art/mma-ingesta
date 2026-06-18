"""Independent verification of recovered fight winners.

Samples decided fights from the DB and cross-checks ``winner_id`` against the
UFCStats *fight detail page* W/L badge -- a source that was NOT used during the
event-page recovery, so agreement is real evidence the recovery is correct.

Usage::

    python -m src.scrapers.verify_winners --sample 80
"""
from __future__ import annotations

import argparse
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parents[2] / ".env")

import psycopg2.extras

from .config import get_settings
from .db import connect
from .http import UfcStatsClient
from .utils import clean_text, source_id_from_url


def detail_winner_source_id(client: UfcStatsClient, fight_source_id: str) -> tuple[str | None, str]:
    url = (
        f"http://ufcstats.com{fight_source_id}"
        if fight_source_id.startswith("/")
        else f"http://ufcstats.com/fight-details/{fight_source_id}"
    )
    page = client.fetch(url)
    statuses = [
        clean_text(n.get_text(" ", strip=True))
        for n in page.soup.select(".b-fight-details__person-status")
    ]
    hrefs = [source_id_from_url(n.get("href")) for n in page.soup.select(".b-fight-details__person-name a")]
    statuses = [s.upper() if s else s for s in statuses]
    if "W" in statuses:
        return hrefs[statuses.index("W")], "decided"
    if statuses and all(s == "D" for s in statuses):
        return None, "draw"
    return None, "nc_or_other:" + ",".join(str(s) for s in statuses)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=80)
    args = parser.parse_args()

    settings = get_settings()
    client = UfcStatsClient(settings)

    with connect(settings.database_url) as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT f.id, f.source_id, w.source_id AS db_winner_src, w.name AS db_winner_name
            FROM fights f
            JOIN fighters w ON w.id = f.winner_id
            WHERE f.source = 'ufcstats' AND f.winner_id IS NOT NULL
            ORDER BY RANDOM()
            LIMIT %s
            """,
            (args.sample,),
        )
        sample = cur.fetchall()

    match = mismatch = errors = 0
    for fr in sample:
        try:
            detail_src, status = detail_winner_source_id(client, fr["source_id"])
        except Exception as exc:  # noqa: BLE001
            errors += 1
            print(f"  ERROR fight {fr['id']}: {exc}")
            continue
        if status != "decided":
            print(f"  ? fight {fr['id']} detalle status={status} (DB winner={fr['db_winner_name']})")
            continue
        if detail_src == fr["db_winner_src"]:
            match += 1
        else:
            mismatch += 1
            print(f"  MISMATCH fight {fr['id']}: DB={fr['db_winner_name']} detail_src={detail_src}")

    total = match + mismatch
    rate = (match / total * 100) if total else 0.0
    print("\n=== Verificación ===")
    print(f"  coincidencias: {match}/{total}  ({rate:.1f}%)")
    print(f"  mismatches:    {mismatch}")
    print(f"  errores fetch: {errors}")
    if mismatch == 0 and total > 0:
        print("  RESULTADO: OK — la recuperación es fiable.")
    else:
        print("  RESULTADO: revisar — hay discrepancias.")


if __name__ == "__main__":
    main()

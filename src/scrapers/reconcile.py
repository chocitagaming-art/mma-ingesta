"""Reconciliation job: re-link NULL ``fighter_id`` rankings + dedupe fighters by folded name.

This module is the *safe* counterpart to ``merge_duplicate_fighters.py``. Two
differences justify its existence:

1. **Folded grouping (accent-insensitive).** ``merge_duplicate_fighters`` groups by
   :func:`matching.casefold_name`, which is accent-*sensitive*: it keeps
   "Jose Aldo" and "José Aldo" apart. This job groups by :func:`matching.fold`
   (NFKD strip-accents), so the two collapse into one group and the accent-only
   duplicates -- the bulk of the cross-source (ESPN vs ufcstats) noise -- are
   finally caught.

2. **Safety first.** Because folding is more aggressive it can also collide *genuine
   homonyms* (e.g. two different real fighters both named "Bruno Silva"). So the
   default mode is ``--dry-run``: it ONLY REPORTS (how many relinks, which duplicate
   pairs, which record it would keep) and issues **zero** INSERT/UPDATE/DELETE. Each
   group is classified and risky homonyms are flagged ``needs_review`` and excluded
   from the (separately gated) ``--apply`` path.

Re-linking reuses the single-source-of-truth matcher in :mod:`matching` (#18):
folded-key lookup with a guarded fuzzy fallback at :data:`matching.IDENTITY_THRESHOLD`
(the stricter cutoff, because attaching a wrong ``fighter_id`` corrupts data).

Keeper policy
-------------
The canonical record in a duplicate group is the one with **the most fights / data**:
ordered by (fights, fight_stats, wins+losses+draws, has_headshot, is-espn, lowest id).

Classification
--------------
* ``accent_variant``          -- names differ only by accents/punctuation. Same person.
* ``cross_source_duplicate``  -- identical name, different ``source``. Same person.
* ``possible_homonym``        -- identical name, same source, and BOTH have fights.
                                 Likely two *different* people -> ``needs_review``,
                                 NOT auto-merged.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import get_close_matches
from typing import Any

from .config import get_settings
from .db import connect
from .matching import (
    IDENTITY_THRESHOLD,
    casefold_name,
    fold,
    ratio,
)

# Guarantee ASCII-safe stdout: the Windows console is cp1252 and chokes on accented
# fighter names ("Procházka"). All report payloads are emitted with ensure_ascii=True,
# and we additionally harden stdout so any stray accented char cannot crash the job.
try:  # pragma: no cover - depends on platform stream type
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass


# --------------------------------------------------------------------------- models


@dataclass(frozen=True)
class FighterAgg:
    """A fighter plus the activity counts used to score who is the canonical keeper."""

    id: int
    name: str
    source: str | None
    has_headshot: bool
    record_total: int  # wins + losses + draws
    fights: int
    fight_stats: int
    rankings: int
    news: int

    def score(self) -> tuple[int, int, int, int, int, int]:
        """Higher is more canonical. 'More fights/data' dominates; id breaks ties low."""
        return (
            self.fights,
            self.fight_stats,
            self.record_total,
            1 if self.has_headshot else 0,
            1 if self.source == "espn" else 0,
            -self.id,
        )


# --------------------------------------------------------------------------- loading


def _count_map(cursor, query: str) -> dict[int, int]:
    cursor.execute(query)
    return {int(row[0]): int(row[1]) for row in cursor.fetchall() if row[0] is not None}


def _load_fighters(connection) -> list[FighterAgg]:
    with connection.cursor() as cursor:
        fight_counts = _count_map(
            cursor,
            """
            SELECT fighter_id, COUNT(*) FROM (
                SELECT fighter_red_id  AS fighter_id FROM fights WHERE fighter_red_id  IS NOT NULL
                UNION ALL
                SELECT fighter_blue_id AS fighter_id FROM fights WHERE fighter_blue_id IS NOT NULL
            ) t
            GROUP BY fighter_id
            """,
        )
        stat_counts = _count_map(
            cursor, "SELECT fighter_id, COUNT(*) FROM fight_stats GROUP BY fighter_id"
        )
        ranking_counts = _count_map(
            cursor,
            "SELECT fighter_id, COUNT(*) FROM rankings WHERE fighter_id IS NOT NULL GROUP BY fighter_id",
        )
        news_counts = _count_map(
            cursor,
            "SELECT fighter_id, COUNT(*) FROM news WHERE fighter_id IS NOT NULL GROUP BY fighter_id",
        )

        cursor.execute(
            "SELECT id, name, source, headshot_url, wins, losses, draws FROM fighters"
        )
        fighters: list[FighterAgg] = []
        for fid, name, source, headshot, wins, losses, draws in cursor.fetchall():
            fid = int(fid)
            fighters.append(
                FighterAgg(
                    id=fid,
                    name=name,
                    source=source,
                    has_headshot=bool(headshot),
                    record_total=int(wins or 0) + int(losses or 0) + int(draws or 0),
                    fights=fight_counts.get(fid, 0),
                    fight_stats=stat_counts.get(fid, 0),
                    rankings=ranking_counts.get(fid, 0),
                    news=news_counts.get(fid, 0),
                )
            )
    return fighters


# --------------------------------------------------------------------------- relink


def _build_folded_index(fighters: list[FighterAgg]) -> dict[str, FighterAgg]:
    """Map fold(name) -> fighter, dropping keys that map to >1 distinct id as ambiguous.

    Mirrors ``rankings._build_folded_index``: an ambiguous folded key must NOT steer a
    name to an arbitrary fighter, so it is removed and the name simply stays unlinked.
    """
    index: dict[str, FighterAgg] = {}
    ambiguous: set[str] = set()
    for fighter in fighters:
        if not fighter.name:
            continue
        key = fold(fighter.name)
        existing = index.get(key)
        if existing is not None and existing.id != fighter.id:
            ambiguous.add(key)
        else:
            index[key] = fighter
    for key in ambiguous:
        index.pop(key, None)
    return index


def _match_folded(name: str, folded_index: dict[str, FighterAgg]) -> FighterAgg | None:
    """Exact folded-key hit, else a guarded fuzzy fallback at IDENTITY_THRESHOLD.

    The fuzzy guard (same token count + same first token) is the same defensive check
    rankings.py uses, so a genuinely-absent fighter is not welded onto a near neighbour.
    """
    key = fold(name)
    if not key:
        return None
    direct = folded_index.get(key)
    if direct is not None:
        return direct
    candidates = get_close_matches(key, list(folded_index.keys()), n=1, cutoff=IDENTITY_THRESHOLD)
    if not candidates:
        return None
    candidate = candidates[0]
    if ratio(key, candidate) < IDENTITY_THRESHOLD:
        return None
    key_tokens = key.split()
    candidate_tokens = candidate.split()
    if len(key_tokens) != len(candidate_tokens) or key_tokens[0] != candidate_tokens[0]:
        return None
    return folded_index[candidate]


def detect_relinks(connection, fighters: list[FighterAgg]) -> dict[str, Any]:
    """Find rankings rows with NULL fighter_id and propose a re-link by folded name."""
    folded_index = _build_folded_index(fighters)
    with connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM rankings")
        total = int(cursor.fetchone()[0])
        cursor.execute(
            """
            SELECT id, fighter_name, division, snapshot_date
            FROM rankings
            WHERE fighter_id IS NULL
            ORDER BY snapshot_date DESC, division, rank_position
            """
        )
        null_rows = cursor.fetchall()

    proposals: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for ranking_id, fighter_name, division, snapshot_date in null_rows:
        match = _match_folded(fighter_name or "", folded_index)
        entry = {
            "ranking_id": int(ranking_id),
            "fighter_name": fighter_name,
            "division": division,
            "snapshot_date": snapshot_date.isoformat() if snapshot_date else None,
        }
        if match is None:
            unresolved.append(entry)
        else:
            proposals.append({**entry, "proposed_fighter_id": match.id, "matched_name": match.name})

    return {
        "rankings_total": total,
        "null_fighter_id": len(null_rows),
        "relinks_proposed": len(proposals),
        "unresolved": len(unresolved),
        "proposals": proposals,
        "unresolved_rows": unresolved,
    }


# --------------------------------------------------------------------------- dedupe


def _classify(group: list[FighterAgg]) -> str:
    casefolds = {casefold_name(f.name) for f in group}
    if len(casefolds) > 1:
        # Raw spellings differ only after accent/punctuation stripping -> same person.
        return "accent_variant"
    # Identical raw name. Distinguish a cross-source duplicate from a real homonym.
    sources = {f.source for f in group}
    with_fights = [f for f in group if f.fights > 0]
    if len(sources) > 1:
        return "cross_source_duplicate"
    if len(with_fights) > 1:
        # Same name, same source, multiple records that each have real fight history:
        # almost certainly different people. Do not auto-merge.
        return "possible_homonym"
    return "same_name_duplicate"


def detect_duplicates(fighters: list[FighterAgg]) -> dict[str, Any]:
    grouped: dict[str, list[FighterAgg]] = defaultdict(list)
    for fighter in fighters:
        if not fighter.name:
            continue
        grouped[fold(fighter.name)].append(fighter)

    groups: list[dict[str, Any]] = []
    auto_eligible = 0
    needs_review = 0
    for fold_key, members in sorted(grouped.items()):
        distinct_ids = {f.id for f in members}
        if len(distinct_ids) < 2:
            continue
        classification = _classify(members)
        is_review = classification == "possible_homonym"
        keeper = max(members, key=lambda f: f.score())
        duplicates = [f for f in members if f.id != keeper.id]

        fk_moves = {
            "fights": sum(f.fights for f in duplicates),
            "fight_stats": sum(f.fight_stats for f in duplicates),
            "rankings": sum(f.rankings for f in duplicates),
            "news": sum(f.news for f in duplicates),
        }

        if is_review:
            needs_review += 1
        else:
            auto_eligible += 1

        groups.append(
            {
                "fold_key": fold_key,
                "classification": classification,
                "needs_review": is_review,
                "auto_merge_eligible": not is_review,
                "keeper": _fighter_brief(keeper),
                "duplicates": [_fighter_brief(f) for f in duplicates],
                "fk_rows_to_move": fk_moves,
            }
        )

    return {
        "folded_groups": len(groups),
        "auto_merge_eligible": auto_eligible,
        "needs_review": needs_review,
        "groups": groups,
    }


def _fighter_brief(fighter: FighterAgg) -> dict[str, Any]:
    return {
        "id": fighter.id,
        "name": fighter.name,
        "source": fighter.source,
        "fights": fighter.fights,
        "fight_stats": fighter.fight_stats,
        "rankings": fighter.rankings,
        "news": fighter.news,
        "record_total": fighter.record_total,
        "has_headshot": fighter.has_headshot,
    }


# --------------------------------------------------------------------------- apply (gated)


def _apply_merges(
    connection,
    relinks: dict[str, Any],
    duplicates: dict[str, Any],
) -> dict[str, Any]:
    """Execute the reconciliation in a SINGLE transaction. NOT reached in --dry-run.

    For each auto-eligible duplicate group it reassigns every FK that points at a
    duplicate (fights.fighter_red_id / fighter_blue_id / winner_id, fight_stats.fighter_id,
    rankings.fighter_id, news.fighter_id) to the keeper, deletes rows that would violate
    the (fight_id, fighter_id) / (fighter_id, promotion_id, division, snapshot_date)
    unique constraints first, then deletes the duplicate fighter. Groups flagged
    ``needs_review`` (possible homonyms) are skipped. NULL rankings are re-linked too.
    The whole thing commits once, or rolls back on any error.
    """
    applied = {"relinked": 0, "groups_merged": 0, "fighters_deleted": 0, "skipped_review": 0}
    try:
        with connection.cursor() as cursor:
            for proposal in relinks["proposals"]:
                cursor.execute(
                    "UPDATE rankings SET fighter_id = %s WHERE id = %s AND fighter_id IS NULL",
                    (proposal["proposed_fighter_id"], proposal["ranking_id"]),
                )
                applied["relinked"] += cursor.rowcount

            for group in duplicates["groups"]:
                if group["needs_review"]:
                    applied["skipped_review"] += 1
                    continue
                keeper_id = group["keeper"]["id"]
                for dup in group["duplicates"]:
                    dup_id = dup["id"]
                    cursor.execute(
                        "UPDATE fights SET fighter_red_id = %s WHERE fighter_red_id = %s",
                        (keeper_id, dup_id),
                    )
                    cursor.execute(
                        "UPDATE fights SET fighter_blue_id = %s WHERE fighter_blue_id = %s",
                        (keeper_id, dup_id),
                    )
                    cursor.execute(
                        "UPDATE fights SET winner_id = %s WHERE winner_id = %s",
                        (keeper_id, dup_id),
                    )
                    cursor.execute(
                        """
                        DELETE FROM fight_stats
                        WHERE fighter_id = %s
                          AND EXISTS (
                            SELECT 1 FROM fight_stats existing
                            WHERE existing.fight_id = fight_stats.fight_id
                              AND existing.fighter_id = %s
                          )
                        """,
                        (dup_id, keeper_id),
                    )
                    cursor.execute(
                        "UPDATE fight_stats SET fighter_id = %s WHERE fighter_id = %s",
                        (keeper_id, dup_id),
                    )
                    cursor.execute(
                        """
                        DELETE FROM rankings
                        WHERE fighter_id = %s
                          AND EXISTS (
                            SELECT 1 FROM rankings existing
                            WHERE existing.promotion_id = rankings.promotion_id
                              AND existing.division = rankings.division
                              AND existing.snapshot_date = rankings.snapshot_date
                              AND existing.fighter_id = %s
                          )
                        """,
                        (dup_id, keeper_id),
                    )
                    cursor.execute(
                        "UPDATE rankings SET fighter_id = %s WHERE fighter_id = %s",
                        (keeper_id, dup_id),
                    )
                    cursor.execute(
                        "UPDATE news SET fighter_id = %s WHERE fighter_id = %s",
                        (keeper_id, dup_id),
                    )
                    cursor.execute("DELETE FROM fighters WHERE id = %s", (dup_id,))
                    applied["fighters_deleted"] += 1
                applied["groups_merged"] += 1
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return applied


# --------------------------------------------------------------------------- orchestration


def reconcile(dry_run: bool = True) -> dict[str, Any]:
    settings = get_settings()
    with connect(settings.database_url) as connection:
        fighters = _load_fighters(connection)
        relinks = detect_relinks(connection, fighters)
        duplicates = detect_duplicates(fighters)

        report: dict[str, Any] = {
            "mode": "dry-run" if dry_run else "apply",
            "applied": False,
            "fighters_total": len(fighters),
            "rankings": relinks,
            "duplicates": duplicates,
        }

        if dry_run:
            # Explicitly perform no writes. Defensive: nothing above issued a write,
            # but rolling back makes the read-only contract unambiguous.
            connection.rollback()
        else:
            report["apply_result"] = _apply_merges(connection, relinks, duplicates)
            report["applied"] = True

    report["summary"] = _summarize(report)
    return report


def _summarize(report: dict[str, Any]) -> str:
    relinks = report["rankings"]
    dups = report["duplicates"]
    return (
        f"mode={report['mode']} | "
        f"rankings: {relinks['null_fighter_id']} NULL, "
        f"{relinks['relinks_proposed']} relink(s) proposed, {relinks['unresolved']} unresolved | "
        f"duplicates: {dups['folded_groups']} folded group(s), "
        f"{dups['auto_merge_eligible']} auto-merge-eligible, {dups['needs_review']} need review"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile rankings/fighters: re-link NULL fighter_id rankings by folded "
            "name and dedupe fighters whose names collide after accent folding. "
            "Defaults to a read-only --dry-run report."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report only; issue ZERO writes (default behaviour).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute the relinks/merges in one transaction. Ignored if --dry-run is set.",
    )
    args = parser.parse_args()

    # Safety: dry-run is the default and always wins over a stray --apply. A real apply
    # requires --apply AND the explicit absence of --dry-run.
    dry_run = args.dry_run or not args.apply

    report = reconcile(dry_run=dry_run)
    print(json.dumps(report, ensure_ascii=True, indent=2, default=str))


if __name__ == "__main__":
    main()

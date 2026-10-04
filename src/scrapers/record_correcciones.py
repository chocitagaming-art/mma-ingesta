"""Manual corrections to the ESPN "overall" record, for bouts ESPN counts wrongly.

WHY THIS EXISTS. refresh_fighter_records copies ESPN's overall W-L-D into
fighters.wins/losses/draws every day (cron --days 14 and the --days 3 step of the
live loop). That is right 99 % of the time. But ESPN counts some bouts that are not
part of the professional record: Imanol Rodriguez (fighters.id 7150) is 8-0-0 on
ufc.com and UFCStats, and ESPN says 8-1-0 because it counts the TUF 33 semifinal
(2-ago-2025, split decision loss to Joseph Morales), an exhibition. Fixing the row
by hand did nothing: the next run wrote 8-1-0 back.

HOW A CORRECTION WORKS. It is ANCHORED to one ESPN competition, not to a frozen
record. While the fighter's ESPN eventsMap (common/v3, the same payload
espn_fight_history reads) still lists that competition with that result, AND the
overall still counts it (overall == W/L/D tally of the eventsMap), that bout is
subtracted from the overall. So it keeps working after the fighter's next fight
(ESPN 9-1-0 -> 9-0-0) instead of freezing him at 8-0-0.

THE RULES THAT KEEP IT FROM ROTTING. A manual correction is debt:

1. **It retires itself, loudly.** If ESPN drops the competition, changes its
   result, or stops counting it in the overall, the correction is NOT applied and
   NOTHING is written for that fighter. The run counts it as
   `correcciones_ya_no_necesarias` and exits non-zero, so notify-on-failure
   ("Refresh fighter records") opens an Issue: delete the line.
2. **When in doubt, nothing is written.** If the eventsMap cannot be read, or the
   overall and the eventsMap disagree (one of them lagging), the fighter is
   skipped that run (`correcciones_sin_verificar`). A skipped day is harmless;
   writing a record we could not verify is not.
3. **No expiry date, on purpose.** The ranking corrections carry `caduca` because
   ufc.com WILL eventually crown someone and the patch must not outlive that. Here
   the fact does not age (an exhibition stays an exhibition), and an expiry would
   do harm: after it, the monotonic refresh would silently write 8-1-0 back. Staleness
   is detected by the anchor itself (rule 1), not by the calendar, and a
   date-based zombie test would turn CI red on unrelated PRs for a line that is
   still true. `desde` is kept to date the decision.

WHAT THIS IS NOT. It is not a general "drop every TUF bout" rule: Joseph Morales
won that same semifinal and is 15-3 on ufc.com, UFCStats AND ESPN; such a rule
would wrongly make him 14-3. Each line names one fighter and one competition,
checked by hand against ufc.com and UFCStats.

HOW TO ADD ONE. Write it here with its reason and a citable URL. It lives in the
repo and not in the database on purpose: it travels with its why, is reviewed as
code and stays in git.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from .espn_fight_history import _UID_RE

LOGGER = logging.getLogger(__name__)

# Index of each result in a (wins, losses, draws) tuple.
_COMPONENT = {"W": 0, "L": 1, "D": 2}

# Verdicts of evaluar_correcciones.
APLICAR = "aplicar"
YA_NO_NECESARIA = "ya_no_necesaria"
SIN_VERIFICAR = "sin_verificar"


@dataclass(frozen=True)
class CorreccionRecord:
    """One ESPN bout that must not count in a fighter's stored record.

    fighter_id        fighters.id the correction applies to.
    espn_id           ESPN athlete id verified by hand for that fighter. The
                      fighter is ALWAYS fetched by this id, never by name.
    competicion_espn  ESPN competition id (the `c:` part of the eventsMap uid).
    resultado         the result ESPN gives it for this fighter: 'W', 'L' or 'D'.
    motivo / fuente   why it exists and where it is checked. Mandatory.
    desde             when the decision was taken.
    """

    fighter_id: int
    espn_id: str
    competicion_espn: str
    resultado: str
    motivo: str
    fuente: str
    desde: date


# --------------------------------------------------------------------------- live

CORRECCIONES: list[CorreccionRecord] = [
    CorreccionRecord(
        fighter_id=7150,
        espn_id="5289578",
        # The Ultimate Fighter 33 Semifinal: Cormier vs. Sonnen, 2-ago-2025,
        # split decision loss to Joseph Morales (ESPN 4238229). Event 600055505,
        # league 3359, uid s:3301~l:3359~e:600055505~c:401811187.
        competicion_espn="401811187",
        resultado="L",
        motivo=(
            "TUF 33 semifinal is an exhibition: ufc.com and UFCStats give Imanol "
            "Rodriguez 8-0-0, ESPN counts it in overall (8-1-0)."
        ),
        fuente="https://www.ufc.com/athlete/imanol-rodriguez",
        desde=date(2026, 10, 4),
    ),
]


# --------------------------------------------------------------------------- logic


@dataclass(frozen=True)
class Veredicto:
    """Outcome of checking a fighter's corrections against ESPN.

    estado   APLICAR, YA_NO_NECESARIA or SIN_VERIFICAR.
    record   the corrected (wins, losses, draws); only with APLICAR.
    lineas   the correction lines the verdict is about (the stale ones for
             YA_NO_NECESARIA), so the counters say how many lines to act on.
    detalle  human-readable reason, for the logs.
    """

    estado: str
    record: tuple[int, int, int] | None
    lineas: tuple[CorreccionRecord, ...]
    detalle: str


def correcciones_por_luchador(
    correcciones: list[CorreccionRecord],
) -> dict[int, list[CorreccionRecord]]:
    grouped: dict[int, list[CorreccionRecord]] = {}
    for c in correcciones:
        grouped.setdefault(c.fighter_id, []).append(c)
    return grouped


def _entry_result(entry: dict) -> str | None:
    """'W' / 'L' / 'D' / 'NC' for an eventsMap entry, None if it has no result
    (scheduled). Same precedence as espn_fight_history.parse_career: a no-contest
    token wins over gameResult."""
    status = entry.get("status") or {}
    token = str((status.get("result") or {}).get("name") or "").strip().lower()
    if token == "no-contest":
        return "NC"
    game_result = str(entry.get("gameResult") or "").strip().upper()
    return game_result if game_result in _COMPONENT else None


def _fmt(record: tuple[int, int, int] | list[int]) -> str:
    return "-".join(str(n) for n in record)


def evaluar_correcciones(
    correcciones: list[CorreccionRecord],
    overall: tuple[int, int, int],
    payload: dict | None,
) -> Veredicto:
    """Decide whether a fighter's corrections still apply to ESPN's `overall`.

    `payload` is the common/v3 athlete payload (with its eventsMap), or None when
    it could not be fetched. Pure: no network, no DB.
    """
    todas = tuple(correcciones)
    events_map = payload.get("eventsMap") if isinstance(payload, dict) else None
    if not isinstance(events_map, dict) or not events_map:
        return Veredicto(SIN_VERIFICAR, None, todas, "ESPN eventsMap unavailable")

    by_competition: dict[str, str | None] = {}
    tally = [0, 0, 0]
    for uid, entry in events_map.items():
        if not isinstance(entry, dict):
            continue
        result = _entry_result(entry)
        if result in _COMPONENT:
            tally[_COMPONENT[result]] += 1
        match = _UID_RE.search(str(uid))
        if match:
            by_competition[match.group(3)] = result

    stale = []
    reasons = []
    for c in correcciones:
        if c.competicion_espn not in by_competition:
            stale.append(c)
            reasons.append(
                f"competition {c.competicion_espn} is no longer in the ESPN eventsMap"
            )
        elif by_competition[c.competicion_espn] != c.resultado:
            stale.append(c)
            reasons.append(
                f"ESPN now gives competition {c.competicion_espn} as "
                f"{by_competition[c.competicion_espn]!r}, not {c.resultado!r}"
            )
    if stale:
        return Veredicto(YA_NO_NECESARIA, None, tuple(stale), "; ".join(reasons))

    removed = [0, 0, 0]
    for c in correcciones:
        removed[_COMPONENT[c.resultado]] += 1
    corrected = tuple(o - r for o, r in zip(overall, removed))
    competitions = ", ".join(
        f"{c.competicion_espn} {c.resultado}" for c in correcciones
    )

    if tuple(tally) == tuple(overall):
        # The overall counts exactly what the eventsMap lists, the corrected bouts
        # included: subtract them.
        return Veredicto(
            APLICAR, corrected, todas,
            f"ESPN overall {_fmt(overall)} minus competition {competitions}",
        )
    if tuple(t - r for t, r in zip(tally, removed)) == tuple(overall):
        # ESPN fixed the overall but kept the bout in the eventsMap: subtracting it
        # again would erase a bout that is no longer counted.
        return Veredicto(
            YA_NO_NECESARIA, None, todas,
            f"ESPN overall {_fmt(overall)} already excludes competition {competitions}",
        )
    return Veredicto(
        SIN_VERIFICAR, None, todas,
        f"ESPN overall {_fmt(overall)} does not match its eventsMap tally "
        f"{_fmt(tally)}",
    )

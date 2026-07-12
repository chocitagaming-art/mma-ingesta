"""Report fighters that still have no headshot — and no usable body photo — so
the gaps can be filled by hand or by the enrichment crons.

Run this AFTER the automated photo enrichment (UFC/ESPN). Whatever remains are
fighters those sources don't cover — regional debutants — which need a manual
Tapology photo via add_manual_fighter. The report focuses on UPCOMING-event
fighters (the ones users actually see on cards) and cross-references the
frontend's local-headshots.ts / local-bodies.ts, because a manually-curated
fighter has headshot_url (or the body columns) NULL in the DB yet still renders a
photo on the site via those maps — so it must NOT be re-listed as missing.

Beyond headshots (F0) this also reports the F1 "espejo definitivo" body columns:
per-column coverage (standing_body_url_l / _r / full_body_url) and the DISCORDANT
PAIRS — face-off bouts where a corner lacks its exact directional photo, so the
frontend has to mirror it (logo inverted) or fall back to the headshot. The
forward auto-fill chain (enrich_fullbody + backfill_standing_photos writing L/R)
already exists; this is the read-only reporting side of it.

Usage (read-only). On Windows force UTF-8 so accented names don't crash the console:
    PYTHONUTF8=1 python -m src.scrapers.photo_coverage           # markdown report
    PYTHONUTF8=1 python -m src.scrapers.photo_coverage --json    # machine-readable
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .config import get_settings
from .db import connect

# Frontend fallback map (sibling repo): names already mapped to a /public photo.
_LOCAL_HEADSHOTS_TS = (
    Path(__file__).resolve().parents[2].parent / "mma-app" / "src" / "lib" / "local-headshots.ts"
)

# Frontend curated body map (sibling repo): names shown with a hand-picked body
# photo (localBody, PRIORITY over the DB) — never count these as a body gap.
_LOCAL_BODIES_TS = (
    Path(__file__).resolve().parents[2].parent / "mma-app" / "src" / "lib" / "local-bodies.ts"
)

# Token _L_/_R_ in a ufc.com standing URL → the side the photo faces. The red
# corner needs the _L_ variant, the blue the _R_ (migration 019 / fighter-photo.ts).
_STANDING_TOKEN_RE = re.compile(r"_([LR])_\d")


# --------------------------------------------------------------- pure helpers


def _nonempty(value: object) -> bool:
    return value is not None and str(value).strip() != ""


def _standing_token(url: str | None) -> str | None:
    """Direction a standing URL faces, from its _L_/_R_ token (or None)."""
    if not url:
        return None
    match = _STANDING_TOKEN_RE.search(url)
    return match.group(1) if match else None


def _corner_photo(
    want: str,
    standing_l: str | None,
    standing_r: str | None,
    standing: str | None,
    full: str | None,
) -> tuple[str | None, bool]:
    """Python port of fighter-photo.pickCornerBodyPhoto → (source, mirror).

    ``want`` is the direction this corner needs ("L" for red, "R" for blue).
    Returns the chosen source tag ("exact"/"opposite"/"legacy"/"full" or None
    when there is no body photo) and whether the frontend would mirror it
    (scaleX(-1), i.e. facing right but with an inverted logo).
    """
    left = standing_l if _nonempty(standing_l) else None
    right = standing_r if _nonempty(standing_r) else None
    exact = left if want == "L" else right
    if exact:
        return ("exact", False)
    opposite = right if want == "L" else left
    if opposite:
        return ("opposite", True)
    if _nonempty(standing):
        token = _standing_token(standing)
        return ("legacy", token is not None and token != want)
    if _nonempty(full):
        token = _standing_token(full)
        return ("full", token is not None and token != want)
    return (None, False)


def _corner_status(
    want: str,
    standing_l: str | None,
    standing_r: str | None,
    standing: str | None,
    full: str | None,
    is_local: bool,
) -> str:
    """Face-off corner body status: 'local' | 'ok' | 'mirror' | 'none'.

    'local'  = curated in local-bodies (frontend renders it with priority).
    'ok'     = has a body photo facing the right way (no mirror).
    'mirror' = has a photo but the wrong direction → frontend mirrors it.
    'none'   = no usable body photo → falls back to the headshot.
    """
    if is_local:
        return "local"
    source, mirror = _corner_photo(want, standing_l, standing_r, standing, full)
    if source is None:
        return "none"
    return "mirror" if mirror else "ok"


# ------------------------------------------------------------- database reads


def _upcoming_without_photo(connection) -> list[tuple[int, str, str, str]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT f.id, f.name, e.name AS event_name, e.event_date::text
            FROM events e
            JOIN fights fi ON fi.event_id = e.id
            JOIN fighters f ON (f.id = fi.fighter_red_id OR f.id = fi.fighter_blue_id)
            WHERE e.status = 'upcoming'
              AND (f.headshot_url IS NULL OR f.headshot_url = '')
            ORDER BY e.event_date::text, f.name
            """
        )
        return [(int(r[0]), str(r[1]), str(r[2]), str(r[3])) for r in cursor.fetchall()]


def _upcoming_zero_record(connection) -> list[tuple[int, str, str, str]]:
    """Upcoming-event fighters whose record is 0-0-0 (wins=losses=draws=0)."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT f.id, f.name, e.name AS event_name, e.event_date::text
            FROM events e
            JOIN fights fi ON fi.event_id = e.id
            JOIN fighters f ON (f.id = fi.fighter_red_id OR f.id = fi.fighter_blue_id)
            WHERE e.status = 'upcoming'
              AND COALESCE(f.wins, 0) = 0
              AND COALESCE(f.losses, 0) = 0
              AND COALESCE(f.draws, 0) = 0
            ORDER BY e.event_date::text, f.name
            """
        )
        return [(int(r[0]), str(r[1]), str(r[2]), str(r[3])) for r in cursor.fetchall()]


def _upcoming_missing_nationality(connection) -> list[tuple[int, str, str, str]]:
    """Upcoming-event fighters with nationality NULL or empty string."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT f.id, f.name, e.name AS event_name, e.event_date::text
            FROM events e
            JOIN fights fi ON fi.event_id = e.id
            JOIN fighters f ON (f.id = fi.fighter_red_id OR f.id = fi.fighter_blue_id)
            WHERE e.status = 'upcoming'
              AND (f.nationality IS NULL OR f.nationality = '')
            ORDER BY e.event_date::text, f.name
            """
        )
        return [(int(r[0]), str(r[1]), str(r[2]), str(r[3])) for r in cursor.fetchall()]


def _upcoming_body_coverage(connection) -> list[tuple]:
    """Body-photo columns for every distinct fighter on an upcoming card."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT f.id, f.name, f.standing_body_url,
                   f.standing_body_url_l, f.standing_body_url_r, f.full_body_url
            FROM events e
            JOIN fights fi ON fi.event_id = e.id
            JOIN fighters f ON (f.id = fi.fighter_red_id OR f.id = fi.fighter_blue_id)
            WHERE e.status = 'upcoming'
            ORDER BY f.name
            """
        )
        return [
            (int(r[0]), str(r[1]), r[2], r[3], r[4], r[5]) for r in cursor.fetchall()
        ]


def _upcoming_pairs(connection) -> list[tuple]:
    """Both corners (with their body columns) for every resolved upcoming bout."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT e.name, e.event_date::text,
                   r.id, r.name, r.standing_body_url_l, r.standing_body_url_r,
                   r.standing_body_url, r.full_body_url,
                   b.id, b.name, b.standing_body_url_l, b.standing_body_url_r,
                   b.standing_body_url, b.full_body_url
            FROM events e
            JOIN fights fi ON fi.event_id = e.id
            JOIN fighters r ON r.id = fi.fighter_red_id
            JOIN fighters b ON b.id = fi.fighter_blue_id
            WHERE e.status = 'upcoming'
            ORDER BY e.event_date::text, r.name
            """
        )
        return [tuple(row) for row in cursor.fetchall()]


def _total_without_photo(connection) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM fighters WHERE headshot_url IS NULL OR headshot_url = ''"
        )
        return int(cursor.fetchone()[0])


# --------------------------------------------------- frontend curated-name maps


def _local_headshot_names() -> set[str]:
    """Lowercase names already mapped to a manual photo in local-headshots.ts."""
    try:
        text = _LOCAL_HEADSHOTS_TS.read_text(encoding="utf-8")
    except OSError:
        return set()
    return {name.lower() for name in re.findall(r'"([^"]+)":\s*"/fighters/', text)}


def _local_body_names() -> set[str]:
    """Lowercase names curated with a manual body photo in local-bodies.ts.

    Entries look like ``"forrest griffin": { src: "...", fit: "..." }`` — a
    quoted key followed by an object literal (distinct from local-headshots,
    whose values are string paths). Comment lines never match ``": {``.
    """
    try:
        text = _LOCAL_BODIES_TS.read_text(encoding="utf-8")
    except OSError:
        return set()
    return {name.lower() for name in re.findall(r'"([^"]+)"\s*:\s*\{', text)}


# ------------------------------------------------------------- aggregation


def _summarize_body_coverage(body_rows: list[tuple], local_body_names: set[str]) -> dict:
    with_any = with_l = with_r = with_full = 0
    missing: list[dict] = []
    covered_local: list[dict] = []
    for fid, name, standing, standing_l, standing_r, full in body_rows:
        has_l = _nonempty(standing_l)
        has_r = _nonempty(standing_r)
        has_full = _nonempty(full)
        has_any = has_l or has_r or has_full or _nonempty(standing)
        with_l += int(has_l)
        with_r += int(has_r)
        with_full += int(has_full)
        with_any += int(has_any)
        if not has_any:
            entry = {"id": int(fid), "name": str(name)}
            if str(name).lower() in local_body_names:
                covered_local.append(entry)
            else:
                missing.append(entry)
    return {
        "total_upcoming_fighters": len(body_rows),
        "with_any_body": with_any,
        "with_standing_l": with_l,
        "with_standing_r": with_r,
        "with_full_body": with_full,
        "missing_body": missing,
        "covered_local": covered_local,
    }


def _summarize_discordant_pairs(pair_rows: list[tuple], local_body_names: set[str]) -> list[dict]:
    """Upcoming bouts where the face-off is imperfect (a corner mirrors/falls back)."""
    out: list[dict] = []
    for row in pair_rows:
        (
            event,
            date,
            r_id,
            r_name,
            r_l,
            r_r,
            r_standing,
            r_full,
            b_id,
            b_name,
            b_l,
            b_r,
            b_standing,
            b_full,
        ) = row
        red_status = _corner_status(
            "L", r_l, r_r, r_standing, r_full, str(r_name).lower() in local_body_names
        )
        blue_status = _corner_status(
            "R", b_l, b_r, b_standing, b_full, str(b_name).lower() in local_body_names
        )
        if red_status in ("mirror", "none") or blue_status in ("mirror", "none"):
            out.append(
                {
                    "event": event,
                    "date": date,
                    "red": {"id": int(r_id), "name": str(r_name), "status": red_status},
                    "blue": {"id": int(b_id), "name": str(b_name), "status": blue_status},
                }
            )
    return out


# ----------------------------------------------------------------- collect


def _collect_raw(connection) -> dict:
    return {
        "upcoming": _upcoming_without_photo(connection),
        "total": _total_without_photo(connection),
        "zero_record": _upcoming_zero_record(connection),
        "missing_nationality": _upcoming_missing_nationality(connection),
        "body_rows": _upcoming_body_coverage(connection),
        "pair_rows": _upcoming_pairs(connection),
    }


def collect(connection=None) -> dict:
    """Gather the report. Pass a ``connection`` to inject a fake DB in tests;
    otherwise one is opened (read-only) from the configured DATABASE_URL."""
    if connection is None:
        settings = get_settings()
        with connect(settings.database_url) as conn:
            raw = _collect_raw(conn)
    else:
        raw = _collect_raw(connection)

    local_head = _local_headshot_names()
    local_body = _local_body_names()
    marked = [
        {"id": fid, "name": name, "event": event, "date": date,
         "has_local": name.lower() in local_head}
        for fid, name, event, date in raw["upcoming"]
    ]
    zero_record_list = [
        {"id": fid, "name": name, "event": event, "date": date}
        for fid, name, event, date in raw["zero_record"]
    ]
    missing_nationality_list = [
        {"id": fid, "name": name, "event": event, "date": date}
        for fid, name, event, date in raw["missing_nationality"]
    ]
    return {
        "total_without_photo": raw["total"],
        "upcoming_without_photo": marked,
        "upcoming_zero_record": zero_record_list,
        "upcoming_missing_nationality": missing_nationality_list,
        "body_coverage": _summarize_body_coverage(raw["body_rows"], local_body),
        "discordant_pairs": _summarize_discordant_pairs(raw["pair_rows"], local_body),
    }


# ------------------------------------------------------------------ render


def _render_markdown(data: dict) -> str:
    upcoming = data["upcoming_without_photo"]
    missing = [u for u in upcoming if not u["has_local"]]
    has_local = [u for u in upcoming if u["has_local"]]
    lines = [
        "# Luchadores SIN foto",
        "",
        "> Generado por `python -m src.scrapers.photo_coverage`.",
        "> **FALTAN** = sin foto en ningún sitio → foto manual de Tapology con `add_manual_fighter --photo`.",
        "> **Ya con foto manual** = `headshot_url` NULL en la BD pero YA se ven en la web (local-headshots.ts).",
        "",
        f"**Total en la BD sin `headshot_url`:** {data['total_without_photo']} "
        "(la mayoría son históricos oscuros, no accionables).",
        "",
        f"## ⚠️ FALTAN de verdad — carteleras próximas ({len(missing)})",
        "",
    ]
    if not missing:
        lines.append("_Ninguno: toda cartelera próxima tiene foto (oficial o manual)._")
    else:
        for u in missing:
            lines.append(f"- **{u['name']}** (id={u['id']}) — {u['event']} [{u['date']}]")
    lines += ["", f"## Ya con foto manual local ({len(has_local)}) — no tocar", ""]
    for u in has_local:
        lines.append(f"- {u['name']} (id={u['id']})")

    zero_record = data["upcoming_zero_record"]
    lines += [
        "",
        f"## 🥋 Record 0-0-0 — carteleras próximas ({len(zero_record)})",
        "",
        "> Record vacío (`wins=0 AND losses=0 AND draws=0`) → completar con `add_manual_fighter` / scrape.",
        "",
    ]
    if not zero_record:
        lines.append("_Ninguno: toda cartelera próxima tiene record._")
    else:
        for u in zero_record:
            lines.append(f"- **{u['name']}** (id={u['id']}) — {u['event']} [{u['date']}]")

    missing_nationality = data["upcoming_missing_nationality"]
    lines += [
        "",
        f"## 🌍 Sin nacionalidad — carteleras próximas ({len(missing_nationality)})",
        "",
        "> `nationality` NULL o vacía → completar con `add_manual_fighter` / scrape.",
        "",
    ]
    if not missing_nationality:
        lines.append("_Ninguno: toda cartelera próxima tiene nacionalidad._")
    else:
        for u in missing_nationality:
            lines.append(f"- **{u['name']}** (id={u['id']}) — {u['event']} [{u['date']}]")

    body = data["body_coverage"]
    missing_body = body["missing_body"]
    lines += [
        "",
        "## 📸 Cobertura de foto de cuerpo — carteleras próximas",
        "",
        "> Columnas de la F1 (espejo definitivo). El rojo usa `standing_body_url_l`,",
        "> el azul `standing_body_url_r`; sin la suya, el frontend espeja (logo invertido).",
        "",
        f"- Luchadores próximos: **{body['total_upcoming_fighters']}**",
        f"- Con alguna foto de cuerpo: **{body['with_any_body']}**",
        f"- Con variante izquierda (`_l`, esquina roja): **{body['with_standing_l']}**",
        f"- Con variante derecha (`_r`, esquina azul): **{body['with_standing_r']}**",
        f"- Con full body: **{body['with_full_body']}**",
        "",
        f"### 🚫 Sin ninguna foto de cuerpo ({len(missing_body)})",
        "",
    ]
    if not missing_body:
        lines.append("_Ninguno: toda cartelera próxima tiene al menos una foto de cuerpo._")
    else:
        for u in missing_body:
            lines.append(f"- **{u['name']}** (id={u['id']})")
    if body["covered_local"]:
        lines += [
            "",
            "_Cubiertos por local-bodies (no tocar): "
            + ", ".join(u["name"] for u in body["covered_local"])
            + "._",
        ]

    pairs = data["discordant_pairs"]
    lines += [
        "",
        f"## ↔️ Parejas discordantes — el cara a cara no es perfecto ({len(pairs)})",
        "",
        "> Al menos una esquina no tiene su foto en la dirección exacta: el frontend",
        "> la **espeja** (facing correcto, logo invertido) o cae al **headshot**.",
        "> Se resuelven solas cuando la cron rellena ambas direcciones.",
        "",
    ]
    if not pairs:
        lines.append("_Ninguna: todas las esquinas próximas miran en su dirección correcta._")
    else:
        for p in pairs:
            red, blue = p["red"], p["blue"]
            lines.append(
                f"- **{p['event']}** [{p['date']}] — "
                f"{red['name']} ({red['status']}) vs {blue['name']} ({blue['status']})"
            )

    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Report fighters without a headshot / body photo (read-only)."
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of Markdown.")
    args = parser.parse_args()
    data = collect()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(_render_markdown(data), end="")


if __name__ == "__main__":
    main()

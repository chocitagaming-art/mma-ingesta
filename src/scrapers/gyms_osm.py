"""Populate the `gyms` table (migration 023) from OpenStreetMap / Overpass.

SOLO España, disciplinas de LUCHA. Una sola consulta Overpass (área ES, ~255
resultados en ~30s; probado) filtra por el tag `sport` con los tokens marciales;
el post-filtro descarta cadenas de fitness y sitios sin ninguna disciplina de
lucha. UPSERT por osm_id; al final poda los gimnasios que ya no están en OSM
(solo si el scrapeo trajo un volumen sano, para no vaciar la tabla ante un fallo).

El filtro (tokens marciales + exclusión) es un PORT de src/lib/gyms.ts de mma-app,
que es la fuente de verdad de presentación (etiquetas ES + descripción por plantilla
se derivan allí en /api/gyms). Aquí solo se guardan los tokens OSM crudos marciales.

Uso:
    python -m src.scrapers.gyms_osm --dump     # imprime el conteo, no escribe
    python -m src.scrapers.gyms_osm            # dry-run: no escribe (reporta)
    python -m src.scrapers.gyms_osm --apply    # UPSERT + poda en Neon
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from datetime import datetime, timezone

import requests

from .config import get_settings
from .db import connect
from .logging_config import configure_logging

LOGGER = logging.getLogger(__name__)

# Tokens de disciplinas de lucha (sinónimos + español + variantes de BJJ). DEBE
# coincidir con MARTIAL_SPORT_TOKENS de mma-app/src/lib/gyms.ts.
MARTIAL_TOKENS = {
    "martial_arts", "boxing", "boxeo", "mixed_martial_arts", "mma",
    "artes_marciales_mixtas", "muay_thai", "thai_boxing", "brazilian_jiu_jitsu",
    "jiu-jitsu", "jiu_jitsu", "bjj", "kickboxing", "kick_boxing", "k1", "judo",
    "karate", "taekwondo", "wrestling", "sambo", "grappling", "kung_fu", "aikido",
    "krav_maga", "defensa_personal",
}
# Regex Overpass anclada por token (misma que OVERPASS_SPORT_REGEX en gyms.ts).
OVERPASS_SPORT_REGEX = "(^|;| )(" + "|".join(sorted(MARTIAL_TOKENS)) + ")( |;|$)"

# Cadenas de fitness que etiquetan boxing/martial_arts pero no son de lucha.
FITNESS_CHAIN_RE = re.compile(
    r"basic.?fit|mcfit|anytime|smart.?fit|vivagym|synergym|altafit|holmes|"
    r"metropolitan\b|fitboxing",
    re.IGNORECASE,
)

UA = "MMA-STATUS/1.0 (gym directory; +https://mma-status.app; gpicomanville@gmail.com)"
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
# Volumen mínimo esperado para atreverse a podar (España tiene ~255).
MIN_HEALTHY_COUNT = 100

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS gyms (
  osm_id        TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  lat           DOUBLE PRECISION NOT NULL,
  lon           DOUBLE PRECISION NOT NULL,
  sports        TEXT[] NOT NULL DEFAULT '{}',
  address       TEXT,
  city          TEXT,
  province      TEXT,
  website       TEXT,
  phone         TEXT,
  opening_hours TEXT,
  source        TEXT NOT NULL DEFAULT 'osm',
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_gyms_lat_lon ON gyms (lat, lon);
"""


# ── Filtro / parseo (puro, testeable) ────────────────────────────────────────
def parse_sport_tokens(sport: str | None) -> list[str]:
    if not sport:
        return []
    return [t for t in (tok.strip() for tok in re.split(r"[;,\s]+", sport.lower())) if t]


def has_combat_discipline(sport: str | None) -> bool:
    return any(token in MARTIAL_TOKENS for token in parse_sport_tokens(sport))


def is_excluded_gym(name: str | None, sport: str | None) -> bool:
    if FITNESS_CHAIN_RE.search(name or ""):
        return True
    return not has_combat_discipline(sport)


def first_osm_value(raw: str | None) -> str | None:
    if not raw:
        return None
    value = raw.split(";")[0].strip()
    return value or None


def normalize_website(raw: str | None) -> str | None:
    value = first_osm_value(raw)
    if not value:
        return None
    return value if re.match(r"^https?://", value, re.IGNORECASE) else f"https://{value}"


def compose_address(tags: dict) -> str | None:
    street = tags.get("addr:street")
    number = tags.get("addr:housenumber")
    postcode = tags.get("addr:postcode")
    city = tags.get("addr:city")
    street_line = (f"{street}, {number}" if number else street) if street else None
    city_line = " ".join(x for x in [postcode, city] if x) or None
    parts = [p for p in [street_line, city_line] if p]
    return ", ".join(parts) if parts else None


def build_gym_row(element: dict) -> dict | None:
    """Un elemento Overpass → fila de `gyms`, o None si se descarta."""
    tags = element.get("tags") or {}
    name = tags.get("name")
    lat = element.get("lat")
    lon = element.get("lon")
    if lat is None or lon is None:
        center = element.get("center") or {}
        lat, lon = center.get("lat"), center.get("lon")
    if lat is None or lon is None or not name:
        return None
    if is_excluded_gym(name, tags.get("sport")):
        return None
    # Guarda solo los tokens marciales reconocidos (descarta 'fitness', 'yoga'…).
    martial = [t for t in parse_sport_tokens(tags.get("sport")) if t in MARTIAL_TOKENS]
    return {
        "osm_id": f"{element['type']}/{element['id']}",
        "name": name,
        "lat": float(lat),
        "lon": float(lon),
        "sports": martial,
        "address": compose_address(tags),
        "city": tags.get("addr:city"),
        "province": tags.get("addr:province"),
        "website": normalize_website(tags.get("website") or tags.get("contact:website")),
        "phone": first_osm_value(tags.get("phone")) or first_osm_value(tags.get("contact:phone")),
        "opening_hours": tags.get("opening_hours"),
    }


def build_rows(elements: list[dict]) -> list[dict]:
    """Filas válidas, deduplicadas por osm_id (Overpass no repite, pero por si acaso)."""
    seen: set[str] = set()
    rows: list[dict] = []
    for element in elements:
        row = build_gym_row(element)
        if row and row["osm_id"] not in seen:
            seen.add(row["osm_id"])
            rows.append(row)
    return rows


# ── Fetch Overpass ───────────────────────────────────────────────────────────
def _overpass_query() -> str:
    return (
        f'[out:json][timeout:180];'
        f'area["ISO3166-1"="ES"][admin_level=2]->.es;'
        f'nwr[sport~"{OVERPASS_SPORT_REGEX}"](area.es);'
        f'out center tags;'
    )


def fetch_spain_gyms(session: requests.Session) -> list[dict]:
    """Elementos Overpass de gimnasios de lucha de España. Reintenta y prueba un
    endpoint de respaldo antes de rendirse."""
    query = _overpass_query()
    last_error: Exception | None = None
    for attempt, endpoint in enumerate(OVERPASS_ENDPOINTS):
        try:
            response = session.post(
                endpoint,
                data={"data": query},
                headers={"User-Agent": UA},
                timeout=200,
            )
            if response.status_code in (429, 504):
                LOGGER.warning("Overpass %s busy (HTTP %s), backoff", endpoint, response.status_code)
                time.sleep(5 * (attempt + 1))
                continue
            response.raise_for_status()
            return response.json().get("elements", [])
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            LOGGER.warning("Overpass %s failed: %s", endpoint, exc)
            time.sleep(3)
    if last_error:
        raise last_error
    return []


# ── Escritura ────────────────────────────────────────────────────────────────
def ensure_table(connection) -> None:
    """CREATE TABLE IF NOT EXISTS en su propia transacción (libera el lock DDL
    antes del trabajo largo). Idempotente; espeja migración 023."""
    with connection.cursor() as cursor:
        cursor.execute(CREATE_TABLE_SQL)
    connection.commit()


_UPSERT_SQL = """
INSERT INTO gyms (
  osm_id, name, lat, lon, sports, address, city, province,
  website, phone, opening_hours, source, updated_at
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'osm', now())
ON CONFLICT (osm_id) DO UPDATE SET
  name = EXCLUDED.name, lat = EXCLUDED.lat, lon = EXCLUDED.lon,
  sports = EXCLUDED.sports, address = EXCLUDED.address, city = EXCLUDED.city,
  province = EXCLUDED.province, website = EXCLUDED.website, phone = EXCLUDED.phone,
  opening_hours = EXCLUDED.opening_hours, updated_at = now()
"""


def upsert_gym(connection, row: dict) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            _UPSERT_SQL,
            (
                row["osm_id"], row["name"], row["lat"], row["lon"], row["sports"],
                row["address"], row["city"], row["province"], row["website"],
                row["phone"], row["opening_hours"],
            ),
        )


def prune_stale(connection, run_start: datetime) -> int:
    """Borra gimnasios OSM no tocados en este run (desaparecidos de OSM)."""
    with connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM gyms WHERE source = 'osm' AND updated_at < %s",
            (run_start,),
        )
        return cursor.rowcount


def run(connection, *, apply: bool, elements: list[dict], run_start: datetime) -> dict:
    rows = build_rows(elements)
    counts = {"fetched": len(elements), "kept": len(rows), "written": 0, "pruned": 0}
    if not apply:
        return counts
    ensure_table(connection)
    for row in rows:
        upsert_gym(connection, row)
    connection.commit()
    counts["written"] = len(rows)
    # Poda solo si el volumen es sano (no vaciar la tabla ante un scrapeo parcial).
    if len(rows) >= MIN_HEALTHY_COUNT:
        counts["pruned"] = prune_stale(connection, run_start)
        connection.commit()
    else:
        LOGGER.warning("Solo %d gimnasios (< %d): se omite la poda", len(rows), MIN_HEALTHY_COUNT)
    return counts


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Poblar `gyms` desde OSM/Overpass (España).")
    parser.add_argument("--apply", action="store_true", help="UPSERT + poda en Neon (default: dry-run).")
    parser.add_argument("--dump", action="store_true", help="Solo cuenta (sin BD).")
    args = parser.parse_args()

    run_start = datetime.now(timezone.utc)
    session = requests.Session()
    elements = fetch_spain_gyms(session)

    if args.dump:
        rows = build_rows(elements)
        print(json.dumps({"fetched": len(elements), "kept": len(rows)}, indent=2))
        for row in rows[:10]:
            print(f"  {row['osm_id']}  {row['name']!r}  {row['sports']}  city={row['city']!r}")
        return

    if not elements:
        raise SystemExit("Overpass no devolvió elementos — se aborta (sin escrituras).")

    settings = get_settings()
    with connect(settings.database_url) as connection:
        counts = run(connection, apply=args.apply, elements=elements, run_start=run_start)

    print(json.dumps(counts, indent=2))
    if not args.apply:
        print("Dry-run: nada escrito. Re-ejecuta con --apply para persistir.")


if __name__ == "__main__":
    main()

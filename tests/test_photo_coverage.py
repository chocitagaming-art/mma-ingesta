"""Tests for the read-only photo-coverage report (F2 Tanda 4).

Covers the new body-photo reporting: the local-bodies.ts name parser, the
face-off corner status logic (a Python port of the frontend's pickCornerBodyPhoto),
the aggregation of per-column coverage / discordant pairs, and that collect()
never issues a write (it is a read-only report). No network, no DB — the fakedb
recorder answers every query and captures what SQL would run.
"""

from src.scrapers import photo_coverage
from src.scrapers.photo_coverage import (
    _corner_photo,
    _corner_status,
    _local_body_names,
    _summarize_body_coverage,
    _summarize_discordant_pairs,
)

# A directional standing URL faces right (_L_) or left (_R_).
L_URL = "https://www.ufc.com/images/styles/x/s3/2026-05/DOE_JOHN_L_05-09.png?itok=a"
R_URL = "https://www.ufc.com/images/styles/x/s3/2026-05/DOE_JOHN_R_05-09.png?itok=b"
FULL_URL = "https://www.ufc.com/images/styles/athlete_bio_full_body/s3/x.png"


# ----------------------------------------------------- local-bodies.ts parser


def test_local_body_names_parses_object_entries(tmp_path, monkeypatch):
    ts = tmp_path / "local-bodies.ts"
    ts.write_text(
        # Real shape: a comment with quotes+colon (must NOT match) + a real entry.
        '// - "cover-top": recorte cabeza-muslo (no es una entrada)\n'
        'export type BodyFit = "cover-top" | "contain-bottom";\n'
        "const LOCAL_BODIES: Record<string, LocalBody> = {\n"
        '  "Forrest Griffin": { src: "/fighters/forrest-griffin-body.webp", fit: "contain-bottom" },\n'
        '  "chael sonnen": { src: "/fighters/chael.webp", fit: "cover-top" },\n'
        "};\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(photo_coverage, "_LOCAL_BODIES_TS", ts)
    names = _local_body_names()
    assert names == {"forrest griffin", "chael sonnen"}
    # The comment line "cover-top": recorte ... has no `{` after the colon.
    assert "cover-top" not in names


def test_local_body_names_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(photo_coverage, "_LOCAL_BODIES_TS", tmp_path / "nope.ts")
    assert _local_body_names() == set()


# ------------------------------------------------ corner photo / status logic


def test_corner_photo_exact_direction_no_mirror():
    # Red wants "L": with the L variant it faces right, logo correct, no mirror.
    assert _corner_photo("L", L_URL, None, None, None) == ("exact", False)
    # Blue wants "R": with the R variant, no mirror.
    assert _corner_photo("R", None, R_URL, None, None) == ("exact", False)


def test_corner_photo_opposite_direction_mirrors():
    # Red wants "L" but only has "R" → frontend mirrors it (inverted logo).
    assert _corner_photo("L", None, R_URL, None, None) == ("opposite", True)


def test_corner_photo_legacy_and_full_mirror_by_token():
    # Legacy single column whose token (_R_) disagrees with want (L) → mirror.
    assert _corner_photo("L", None, None, R_URL, None) == ("legacy", True)
    # Full-body fallback with a matching token → no mirror.
    assert _corner_photo("L", None, None, None, L_URL) == ("full", False)


def test_corner_photo_none_when_no_body():
    assert _corner_photo("L", None, None, None, None) == (None, False)
    # Empty strings are treated as absent, never as a URL.
    assert _corner_photo("L", "", "", "", "") == (None, False)


def test_corner_status_maps_sources():
    assert _corner_status("L", L_URL, None, None, None, False) == "ok"
    assert _corner_status("L", None, R_URL, None, None, False) == "mirror"
    assert _corner_status("L", None, None, None, None, False) == "none"
    # local-bodies wins regardless of DB columns (frontend renders it).
    assert _corner_status("L", None, None, None, None, True) == "local"


# ----------------------------------------------------- coverage aggregation


def test_summarize_body_coverage_counts_and_splits_gaps():
    rows = [
        # (id, name, standing, standing_l, standing_r, full_body)
        (1, "Has Both", None, L_URL, R_URL, None),
        (2, "Only Left", None, L_URL, None, None),
        (3, "Full Only", None, None, None, FULL_URL),
        (4, "No Body", None, None, None, None),
        (5, "Forrest Griffin", None, None, None, None),  # covered by local-bodies
        (6, "Empty Strings", "", "", "", ""),
    ]
    summary = _summarize_body_coverage(rows, {"forrest griffin"})
    assert summary["total_upcoming_fighters"] == 6
    assert summary["with_standing_l"] == 2  # rows 1, 2
    assert summary["with_standing_r"] == 1  # row 1
    assert summary["with_full_body"] == 1  # row 3
    assert summary["with_any_body"] == 3  # rows 1, 2, 3
    # Real gaps exclude the local-bodies curated fighter.
    assert [u["name"] for u in summary["missing_body"]] == ["No Body", "Empty Strings"]
    assert [u["name"] for u in summary["covered_local"]] == ["Forrest Griffin"]


# ------------------------------------------------------ discordant pairs


def _pair(event, red_cols, blue_cols):
    """Build a pair row: red_cols/blue_cols = (id, name, l, r, standing, full)."""
    return (event, "2026-08-01", *red_cols, *blue_cols)


def test_summarize_discordant_pairs_flags_only_imperfect_bouts():
    rows = [
        # Concordant: red has L, blue has R → both face correctly.
        _pair("Good", (1, "Red A", L_URL, None, None, None), (2, "Blue A", None, R_URL, None, None)),
        # Discordant: red missing L but has R (mirror); blue fine.
        _pair("Mirror", (3, "Red B", None, R_URL, None, None), (4, "Blue B", None, R_URL, None, None)),
        # Discordant: blue has no body at all (falls back to headshot).
        _pair("None", (5, "Red C", L_URL, None, None, None), (6, "Blue C", None, None, None, None)),
        # Concordant via local-bodies on the red corner.
        _pair("Local", (7, "Forrest Griffin", None, None, None, None), (8, "Blue D", None, R_URL, None, None)),
    ]
    pairs = _summarize_discordant_pairs(rows, {"forrest griffin"})
    events = [p["event"] for p in pairs]
    assert events == ["Mirror", "None"]
    mirror = next(p for p in pairs if p["event"] == "Mirror")
    assert mirror["red"]["status"] == "mirror"
    assert mirror["blue"]["status"] == "ok"
    none = next(p for p in pairs if p["event"] == "None")
    assert none["blue"]["status"] == "none"


# ------------------------------------------------- collect() is read-only


def _responder(body_rows, pair_rows):
    def responder(sql, params=None):
        flat = " ".join(sql.split())
        if "count(*)" in flat:
            return [(42,)]
        if "f.standing_body_url_l, f.standing_body_url_r, f.full_body_url" in flat:
            return body_rows
        if flat.startswith("SELECT e.name, e.event_date::text, r.id"):
            return pair_rows
        # headshot / zero-record / nationality upcoming lists → empty here
        return []

    return responder


def test_collect_with_injected_connection_is_read_only(fakedb, monkeypatch):
    monkeypatch.setattr(photo_coverage, "_local_headshot_names", set)
    monkeypatch.setattr(photo_coverage, "_local_body_names", lambda: {"forrest griffin"})
    body_rows = [
        (1, "Has Both", None, L_URL, R_URL, None),
        (2, "No Body", None, None, None, None),
    ]
    pair_rows = [
        (
            "Card", "2026-08-01",
            1, "Has Both", L_URL, None, None, None,
            2, "No Body", None, None, None, None,
        )
    ]
    conn = fakedb.Connection(_responder(body_rows, pair_rows))
    data = photo_coverage.collect(connection=conn)

    # A read-only report must never write.
    assert fakedb.mutating_statements(conn) == []
    assert conn.commits == 0
    # New sections are present and computed from the injected rows.
    assert data["body_coverage"]["with_any_body"] == 1
    assert [u["name"] for u in data["body_coverage"]["missing_body"]] == ["No Body"]
    assert len(data["discordant_pairs"]) == 1
    assert data["discordant_pairs"][0]["blue"]["status"] == "none"


def test_render_markdown_includes_new_sections():
    data = {
        "total_without_photo": 0,
        "upcoming_without_photo": [],
        "upcoming_zero_record": [],
        "upcoming_missing_nationality": [],
        "body_coverage": {
            "total_upcoming_fighters": 3,
            "with_any_body": 2,
            "with_standing_l": 1,
            "with_standing_r": 1,
            "with_full_body": 0,
            "missing_body": [{"id": 9, "name": "Gap Guy"}],
            "covered_local": [{"id": 5, "name": "Forrest Griffin"}],
        },
        "discordant_pairs": [
            {
                "event": "UFC X",
                "date": "2026-08-01",
                "red": {"id": 1, "name": "Red", "status": "mirror"},
                "blue": {"id": 2, "name": "Blue", "status": "ok"},
            }
        ],
    }
    md = photo_coverage._render_markdown(data)
    assert "Cobertura de foto de cuerpo" in md
    assert "Parejas discordantes" in md
    assert "Gap Guy" in md
    assert "Forrest Griffin" in md
    assert "Red (mirror) vs Blue (ok)" in md

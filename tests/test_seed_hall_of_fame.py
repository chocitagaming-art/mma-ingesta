"""Tests for the Hall of Fame seed: curated photos, fight videos and the UPSERT.

No database: build_rows() is pure (the fighter index is a dict) and seed() runs
against the shared fakedb recorder. The values checked here are the owner's
decisions of 18-oct-2026 (photos linked from ufc.com, the 11 Fight Wing videos,
"if UFC Español has the full fight, it goes").
"""

import re
from pathlib import Path

import pytest
import yaml

from src.scrapers import seed_hall_of_fame as s
from src.scrapers.matching import fold

YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
# Same rule as the CHECK of migration 031.
FOCUS = re.compile(r"^(100|[1-9]?[0-9])% (100|[1-9]?[0-9])%$")


def _full_index() -> dict[str, int]:
    """Every name the seed resolves, linked to a fake id (exact folded match)."""
    names = [n for _, n in s.PIONEER] + [n for _, n in s.MODERN]
    for fight in s.FIGHT:
        names += [fight[2], fight[3]]
    return {fold(n): 1000 + i for i, n in enumerate(dict.fromkeys(names))}


@pytest.fixture(scope="module")
def rows():
    built, _counts, _unmatched = s.build_rows(_full_index())
    return {(r["wing"], r["display_name"]): r for r in built}


# ---------------------------------------------------------------- shape


def test_61_rows_in_four_wings(rows):
    wings = [w for w, _ in rows]
    assert len(rows) == 61
    assert {w: wings.count(w) for w in set(wings)} == {"pioneer": 22, "modern": 19, "contributor": 9, "fight": 11}


def test_every_row_has_every_column(rows):
    for row in rows.values():
        assert tuple(row) == s.COLUMNS


def test_upsert_writes_and_updates_every_column():
    sql = " ".join(s.UPSERT.split())
    insert_cols = re.search(r"INSERT INTO hall_of_fame \((.*?)\) VALUES", sql).group(1)
    assert tuple(c.strip() for c in insert_cols.split(",")) == s.COLUMNS
    assert sql.count("%s") == len(s.COLUMNS)
    for column in s.COLUMNS:
        if column not in ("wing", "display_name"):
            assert f"{column} = EXCLUDED.{column}" in sql


# ---------------------------------------------------------------- videos


def test_all_11_fights_have_a_video(rows):
    fights = [name for wing, name in rows if wing == "fight"]
    assert sorted(fights) == sorted(s.FIGHT_VIDEO)
    for name in fights:
        assert rows[("fight", name)]["video_id"] is not None


def test_video_ids_and_segments_are_valid():
    for name, (video_id, start, end) in s.FIGHT_VIDEO.items():
        assert YOUTUBE_ID.match(video_id), name
        assert start is None or (isinstance(start, int) and start >= 0), name
        assert end is None or (isinstance(end, int) and end > (start or 0)), name


def test_the_four_marathon_fights_start_at_their_chapter():
    assert s.FIGHT_VIDEO["Forrest Griffin vs. Stephan Bonnar I"] == ("R24nT158alA", 6282, None)
    assert s.FIGHT_VIDEO["Matt Hughes vs. Frank Trigg II"] == ("E2fUq7gT8z4", 6646, None)
    assert s.FIGHT_VIDEO["Mark Coleman vs. Pete Williams"] == ("E2fUq7gT8z4", 3016, 3909)
    assert s.FIGHT_VIDEO["Diego Sanchez vs. Clay Guida"] == ("x0YxW2uF3Bo", 1652, 2617)


def test_ufc_espanol_full_fights_are_used():
    # Owner's rule: if UFC Español has the FULL fight, it is the one we show.
    assert s.FIGHT_VIDEO["Jon Jones vs. Alexander Gustafsson I"][0] == "vdY36eZZ6IY"
    assert s.FIGHT_VIDEO["Israel Adesanya vs. Kelvin Gastelum"][0] == "CUrZ9OSC-yI"
    assert s.FIGHT_VIDEO["Zhang Weili vs. Joanna Jędrzejczyk I"][0] == "vSMQ-_Nnk-A"


def test_only_fights_carry_a_video(rows):
    for (wing, name), row in rows.items():
        if wing != "fight":
            assert (row["video_id"], row["video_start"], row["video_end"]) == (None, None, None), name


def test_fight_without_video_is_seeded_and_logged(monkeypatch, caplog):
    extra = (2027, "Fighter One vs. Fighter Two", "Fighter One", "Fighter Two", "UFC 999", "Bio.")
    monkeypatch.setattr(s, "FIGHT", [*s.FIGHT, extra])
    built, _, _ = s.build_rows(_full_index())
    row = next(r for r in built if r["display_name"] == extra[1])
    assert (row["video_id"], row["video_start"], row["video_end"]) == (None, None, None)
    assert "SIN VÍDEO" in caplog.text and extra[1] in caplog.text


# ---------------------------------------------------------------- photos


def test_photo_keys_exist_in_the_seed(rows):
    for key in s.PHOTO:
        assert key in rows, key


def test_photo_focus_matches_the_db_check():
    for key, (_url, focus) in s.PHOTO.items():
        assert focus is None or FOCUS.match(focus), key


def test_all_nine_contributors_use_the_ufc_hall_of_fame_portrait(rows):
    contributors = [r for (wing, _), r in rows.items() if wing == "contributor"]
    assert len(contributors) == 9
    for row in contributors:
        assert row["photo_url"].startswith("https://www.ufc.com/images/styles/382x217_2/s3/"), row["display_name"]
        assert row["photo_focus"] is not None, row["display_name"]
    # Mask and Davie no longer point at the Wikimedia copies hosted in public/hof.
    assert not any(str(r["photo_url"]).startswith("/hof/") for r in rows.values())


@pytest.mark.parametrize("name", ["Don Frye", "Maurice Smith", "Dan Severn"])
def test_pioneers_without_a_photo_get_the_ufc_portrait(rows, name):
    row = rows[("pioneer", name)]
    assert row["photo_url"].startswith("https://www.ufc.com/images/styles/382x217_2/s3/2023-11/HOF_")
    assert FOCUS.match(row["photo_focus"])


def test_forrest_griffin_photo_is_his_local_cutout(rows):
    assert rows[("modern", "Forrest Griffin")]["photo_url"] == "/fighters/forrest-griffin.webp"


def test_untouched_people_keep_photo_null(rows):
    # The 6 Wikimedia pioneers keep their local fallback (web side); Nogueira is
    # fixed on his fighters row, not here. None of them may get a photo_url.
    for name in ["Pat Miletich", "Bas Rutten", "Kazushi Sakuraba", "Kevin Randleman", "Jens Pulver",
                 "Mark Kerr", "Antônio Rodrigo Nogueira", "Royce Gracie"]:
        assert rows[("pioneer", name)]["photo_url"] is None, name
        assert rows[("pioneer", name)]["photo_focus"] is None, name


def test_fight_rows_have_no_own_photo(rows):
    for (wing, name), row in rows.items():
        if wing == "fight":
            assert row["photo_url"] is None and row["photo_focus"] is None, name


def test_photo_urls_have_no_html_entities():
    # Our own URLs must be clean: '&amp;' would be stored and requested literally.
    for key, (url, _focus) in s.PHOTO.items():
        assert "&amp;" not in url, key


# ---------------------------------------------------------------- links + seed()


def test_corners_and_people_are_linked(rows):
    index = _full_index()
    assert rows[("pioneer", "Royce Gracie")]["fighter_id"] == index[fold("Royce Gracie")]
    fight = rows[("fight", "Matt Hughes vs. Frank Trigg II")]
    assert fight["fighter_a_id"] == index[fold("Matt Hughes")]
    assert fight["fighter_b_id"] == index[fold("Frank Trigg")]
    assert rows[("contributor", "Joe Silva")]["fighter_id"] is None


def test_seed_executes_one_upsert_per_row_in_column_order(fakedb):
    index = _full_index()

    def respond(sql, params):
        if "FROM fighters" in sql:
            # _build_folded_index reads (id, name); the folded name maps back.
            return [(fid, key) for key, fid in index.items()]
        return []

    conn = fakedb.Connection(respond)
    counts, unmatched = s.seed(conn)
    upserts = [p for cur in conn.cursors for sql, p in cur.executed if "INSERT INTO hall_of_fame" in sql]
    assert len(upserts) == 61 and counts["upserted"] == 61
    assert unmatched == [] and counts["fighters_unlinked"] == 0
    assert all(len(p) == len(s.COLUMNS) for p in upserts)
    coleman = next(p for p in upserts if p[1] == "Mark Coleman vs. Pete Williams")
    as_dict = dict(zip(s.COLUMNS, coleman))
    assert (as_dict["video_id"], as_dict["video_start"], as_dict["video_end"]) == ("E2fUq7gT8z4", 3016, 3909)
    assert conn.commits == 1


# ---------------------------------------------------------------- workflow + 031

ROOT = Path(__file__).resolve().parent.parent
MIGRATION_031 = ROOT / "db" / "migrations" / "031_salon_video_recortes.sql"


def _seed_workflow_steps() -> list[dict]:
    data = yaml.safe_load((ROOT / ".github" / "workflows" / "seed-hall-of-fame.yml").read_text(encoding="utf-8"))
    return data["jobs"]["seed"]["steps"]


def test_workflow_applies_031_before_the_seed_and_measures_after():
    runs = [step.get("run", "") for step in _seed_workflow_steps()]
    applies_031 = next(i for i, r in enumerate(runs) if "031_salon_video_recortes.sql" in r)
    seeds = next(i for i, r in enumerate(runs) if "src.scrapers.seed_hall_of_fame" in r)
    measures = next(i for i, r in enumerate(runs) if "src.scrapers.measure_photo_cutout" in r)
    assert applies_031 < seeds < measures
    assert "-v ON_ERROR_STOP=1" in runs[applies_031]


def test_workflow_installs_what_the_measure_imports():
    install = next(step["run"] for step in _seed_workflow_steps() if "pip install" in step.get("run", ""))
    assert "pillow" in install.lower() and "requests" in install


def test_pillow_is_in_the_requirements_the_ci_installs():
    lines = (ROOT / "requirements-scrapers.txt").read_text(encoding="utf-8").splitlines()
    assert any(line.strip().lower().startswith("pillow") for line in lines)


def test_migration_031_is_additive_and_grants_the_web_role():
    sql = MIGRATION_031.read_text(encoding="utf-8")
    code = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    for column in ("video_id TEXT", "video_start INTEGER", "video_end INTEGER", "photo_focus TEXT"):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in code
    assert "CREATE TABLE IF NOT EXISTS photo_cutout" in code
    assert "GRANT SELECT ON TABLE photo_cutout TO mma_app_readonly" in code
    # Every constraint is re-creatable: DROP ... IF EXISTS right before its ADD.
    added = re.findall(r"ADD CONSTRAINT (\w+)", code)
    assert added and all(f"DROP CONSTRAINT IF EXISTS {name}" in code for name in added)
    assert not re.search(r"\b(DROP TABLE|DROP COLUMN|TRUNCATE|DELETE FROM|UPDATE)\b", code)

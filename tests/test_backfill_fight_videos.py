"""backfill_fight_videos is dry-run by default (no UPDATE, no commit), only writes
trusted matches under --apply, rejects non-official channels / titles missing a
surname, is idempotent (WHERE video_url IS NULL), and never touches the DB without
a YOUTUBE_API_KEY."""

import sys

import pytest

from src.scrapers import backfill_fight_videos as bfv
from src.scrapers.backfill_fight_videos import backfill, is_trusted_match
from src.scrapers.youtube_search import UFC_CHANNEL_ID, YouTubeVideo

_FIGHT_ROW = (101, 1, "Israel Adesanya", "Alex Pereira", "UFC 287", None)


def _trusted_video() -> YouTubeVideo:
    return YouTubeVideo(
        video_id="abc123",
        title="UFC 287: Adesanya vs Pereira | Full Fight Highlights",
        channel_id=UFC_CHANNEL_ID,
    )


def _responder(fight_rows, update_result):
    def responder(sql, params=None):
        flat = " ".join(sql.split())
        if flat.startswith("SELECT") and "FROM fights f" in flat:
            return fight_rows
        if "UPDATE fights" in flat:
            return update_result  # length simulates rowcount
        return []

    return responder


def _update_statements(conn):
    return [
        " ".join(sql.split())
        for cur in conn.cursors
        for sql, _ in cur.executed
        if "UPDATE" in sql.upper()
    ]


# --------------------------------------------------------------- confidence guard


def test_guard_accepts_official_channel_with_both_surnames():
    assert is_trusted_match(_trusted_video(), "Israel Adesanya", "Alex Pereira")


def test_guard_rejects_non_official_channel():
    video = YouTubeVideo(
        video_id="x",
        title="Adesanya vs Pereira Full Fight",
        channel_id="UCsomeRandomChannel",
    )
    assert not is_trusted_match(video, "Israel Adesanya", "Alex Pereira")


def test_guard_rejects_title_missing_a_surname():
    video = YouTubeVideo(
        video_id="x",
        title="Israel Adesanya Free Fight",  # no "Pereira"
        channel_id=UFC_CHANNEL_ID,
    )
    assert not is_trusted_match(video, "Israel Adesanya", "Alex Pereira")


def test_guard_rejects_non_fight_content():
    # Official channel, both surnames, but it's a press conference -> reject.
    video = YouTubeVideo(
        video_id="x",
        title="UFC 287: Adesanya vs Pereira | Press Conference",
        channel_id=UFC_CHANNEL_ID,
    )
    assert not is_trusted_match(video, "Israel Adesanya", "Alex Pereira")


def test_guard_requires_a_fight_keyword():
    # Both surnames + official channel but no "free fight"/"highlights" marker.
    video = YouTubeVideo(
        video_id="x",
        title="Adesanya vs Pereira faceoff",
        channel_id=UFC_CHANNEL_ID,
    )
    assert not is_trusted_match(video, "Israel Adesanya", "Alex Pereira")


def test_guard_matches_accented_surnames():
    # DB name carries accents; the YouTube title is plain ASCII.
    video = YouTubeVideo(
        video_id="x",
        title="Free Fight: Figueiredo vs Moreno",
        channel_id=UFC_CHANNEL_ID,
    )
    assert is_trusted_match(video, "Deiveson Figueiredó", "Brandon Moreno")


def test_guard_surname_word_boundary_no_substring_hit():
    # "Lee" must not match inside "asleep"; whole-word match only.
    video = YouTubeVideo(
        video_id="x",
        title="Free Fight: put him asleep, Smith finishes it",
        channel_id=UFC_CHANNEL_ID,
    )
    assert not is_trusted_match(video, "Alan Lee", "Bob Smith")


# --------------------------------------------------------------------- dry-run


def test_dry_run_writes_nothing(fakedb):
    conn = fakedb.Connection(_responder([_FIGHT_ROW], update_result=[]))
    search_client = lambda q, k: [_trusted_video()]

    proposals, counts = backfill(
        conn, api_key="k", max_bout_order=1, apply=False, search_client=search_client
    )

    assert len(proposals) == 1
    assert counts["proposed"] == 1
    assert fakedb.mutating_statements(conn) == []
    assert conn.commits == 0


# ----------------------------------------------------------------------- apply


def test_apply_writes_only_trusted_matches(fakedb):
    rows = [
        _FIGHT_ROW,
        (102, 1, "Conor McGregor", "Dustin Poirier", "UFC 264", None),
    ]
    conn = fakedb.Connection(_responder(rows, update_result=[(1,)]))

    def search_client(query, key):
        if "Adesanya" in query:
            return [_trusted_video()]
        # impostor channel for the second bout -> guard rejects it
        return [YouTubeVideo("y", "McGregor vs Poirier", channel_id="UCimpostor")]

    proposals, counts = backfill(
        conn, api_key="k", max_bout_order=1, apply=True, search_client=search_client
    )

    assert counts["proposed"] == 1
    assert counts["written"] == 1
    assert counts["no_candidate"] == 1
    assert conn.commits == 1
    updates = _update_statements(conn)
    assert len(updates) == 1
    assert "video_url IS NULL" in updates[0]


# ------------------------------------------------------------------ idempotency


def test_apply_is_idempotent_when_row_already_set(fakedb):
    # rowcount 0 => the WHERE video_url IS NULL guard matched nothing.
    conn = fakedb.Connection(_responder([_FIGHT_ROW], update_result=[]))
    search_client = lambda q, k: [_trusted_video()]

    proposals, counts = backfill(
        conn, api_key="k", max_bout_order=1, apply=True, search_client=search_client
    )

    assert counts["proposed"] == 1
    assert counts["written"] == 0
    assert counts["already_set"] == 1
    updates = _update_statements(conn)
    assert "video_url IS NULL" in updates[0]


# --------------------------------------------------------------------- no key


def test_main_without_key_does_not_touch_db(monkeypatch, capsys):
    monkeypatch.setattr(bfv, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)

    def boom(*a, **k):
        raise AssertionError("connect must not be called without a YOUTUBE_API_KEY")

    monkeypatch.setattr(bfv, "connect", boom)
    monkeypatch.setattr(sys, "argv", ["prog"])

    with pytest.raises(SystemExit) as exc:
        bfv.main()

    assert exc.value.code == 1
    assert "YOUTUBE_API_KEY" in capsys.readouterr().out

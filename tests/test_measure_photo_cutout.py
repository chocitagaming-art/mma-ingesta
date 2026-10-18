"""Tests for measure_photo_cutout: the cutout rule and the measuring pass.

Synthetic images only (built with Pillow in memory) and the shared fakedb
recorder: no network, no database. The images reproduce the real cases measured
on 18-oct-2026: a ufc.com/ESPN headshot cutout (transparent all around, shoulders
on the bottom edge), an opaque JPG, Dan Severn's gallery PNG (RGBA but with a
background: 27 % of the ring transparent) and a cutout touching one side.
"""

import io

from PIL import Image, ImageDraw

from src.scrapers import measure_photo_cutout as m


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _jpg(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG")
    return buffer.getvalue()


def headshot_cutout(width: int = 200, height: int = 150) -> Image.Image:
    """Transparent background, opaque head + shoulders resting on the bottom edge."""
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((70, 20, 130, 90), fill=(200, 150, 120, 255))       # head
    draw.rectangle((30, 100, width - 31, height - 1), fill=(90, 60, 40, 255))  # shoulders
    return image


def opaque_photo(width: int = 200, height: int = 150) -> Image.Image:
    return Image.new("RGB", (width, height), (120, 120, 120))


def severn_like(width: int = 200, height: int = 270) -> Image.Image:
    """RGBA, transparent top corners, but a background that fills most of the ring
    (the real one: corners [0, 0, 255, 255], ring 0.269)."""
    image = Image.new("RGBA", (width, height), (60, 60, 60, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width - 1, 40), fill=(0, 0, 0, 0))  # only a strip at the top
    return image


# ------------------------------------------------------------- is_cutout rule


def test_headshot_cutout_is_cutout():
    assert m.is_cutout(headshot_cutout()) is True


def test_opaque_rgb_photo_is_not_cutout():
    assert m.is_cutout(opaque_photo()) is False


def test_rgba_without_transparency_is_not_cutout():
    assert m.is_cutout(opaque_photo().convert("RGBA")) is False


def test_severn_like_background_is_not_cutout():
    # Top corners ARE transparent, so only the 90 % ring rule catches it.
    image = severn_like()
    alpha = image.getchannel("A")
    assert alpha.getpixel((0, 0)) == 0 and alpha.getpixel((image.width - 1, 0)) == 0
    assert m.is_cutout(image) is False


def test_bottom_edge_does_not_count():
    # Shoulders cover the WHOLE bottom row: still a cutout.
    image = headshot_cutout()
    ImageDraw.Draw(image).rectangle((0, image.height - 3, image.width - 1, image.height - 1),
                                    fill=(90, 60, 40, 255))
    assert m.is_cutout(image) is True


def test_one_opaque_top_corner_is_not_cutout():
    image = headshot_cutout()
    image.putpixel((image.width - 1, 0), (10, 10, 10, 255))
    assert m.is_cutout(image) is False


def test_ring_threshold_is_ninety_percent():
    # 200x150: ring = 200 (top) + 150 (left) + 150 (right) = 500 px.
    def with_opaque_side_pixels(count: int) -> Image.Image:
        image = Image.new("RGBA", (200, 150), (0, 0, 0, 0))
        for y in range(1, count + 1):  # left column, below the corner
            image.putpixel((0, y), (1, 1, 1, 255))
        return image

    assert m.is_cutout(with_opaque_side_pixels(50)) is True    # 450/500 = 0.90
    assert m.is_cutout(with_opaque_side_pixels(51)) is False   # 449/500 < 0.90


def test_semi_transparent_ring_is_not_fully_transparent():
    # A soft 50 % alpha halo around the edge is NOT a transparent background.
    image = Image.new("RGBA", (100, 100), (0, 0, 0, 128))
    image.putpixel((0, 0), (0, 0, 0, 0))
    image.putpixel((99, 0), (0, 0, 0, 0))
    assert m.is_cutout(image) is False


def test_palette_png_with_transparency_is_measured():
    cut = headshot_cutout().convert("P")  # Pillow keeps a transparent index
    reopened = Image.open(io.BytesIO(_png(cut)))
    assert reopened.mode == "P" and "transparency" in reopened.info
    assert m.is_cutout(reopened) is True


def test_tiny_image_is_not_cutout():
    assert m.is_cutout(Image.new("RGBA", (1, 1), (0, 0, 0, 0))) is False


# ------------------------------------------------------------- measure_bytes


def test_measure_bytes_cutout_png():
    result = m.measure_bytes("u", 200, _png(headshot_cutout(220, 160)))
    assert result == m.Measurement("u", True, 220, 160, 200)


def test_measure_bytes_jpg_is_framed_with_size():
    result = m.measure_bytes("u", 200, _jpg(opaque_photo(764, 434)))
    assert result == m.Measurement("u", False, 764, 434, 200)


def test_measure_bytes_http_error_is_framed_and_keeps_status():
    assert m.measure_bytes("u", 404, None) == m.Measurement("u", False, None, None, 404)


def test_measure_bytes_network_error_has_no_status():
    assert m.measure_bytes("u", None, None) == m.Measurement("u", False, None, None, None)


def test_measure_bytes_html_instead_of_image_is_framed():
    result = m.measure_bytes("u", 200, b"<!doctype html><title>Not found</title>")
    assert result == m.Measurement("u", False, None, None, 200)


def test_measure_bytes_truncated_image_is_framed():
    data = _png(headshot_cutout())
    result = m.measure_bytes("u", 200, data[: len(data) // 2])
    assert result.is_cutout is False and result.http_status == 200


# ------------------------------------------------------------- fetch_url_for


def test_local_paths_are_fetched_from_the_site():
    assert m.fetch_url_for("/fighters/forrest-griffin.webp") == "https://mmastatus.app/fighters/forrest-griffin.webp"


def test_remote_url_is_requested_verbatim_with_literal_amp():
    url = "https://www.ufc.com/images/x.png?VersionId=abc&amp;itok=9THc76zi"
    assert m.fetch_url_for(url) == url


# ------------------------------------------------------------- the pass


CUT_URL = "https://a.espncdn.com/i/headshots/mma/players/full/1.png"
AMP_URL = "https://www.ufc.com/images/styles/x/Griffin.png?VersionId=v&amp;itok=t"
LOCAL_URL = "/fighters/forrest-griffin.webp"
DEAD_URL = "https://www.ufc.com/images/gone.jpg"


def _responder(salon, stale):
    def respond(sql, params):
        if "FROM hall_of_fame" in sql:
            return [(u,) for u in salon]
        if "unnest" in sql:
            assert params[0] == salon and params[1] == m.RECHECK_AFTER_DAYS
            return [(u,) for u in stale]
        return []
    return respond


def _fetcher(calls):
    payloads = {
        CUT_URL: (200, _png(headshot_cutout())),
        AMP_URL: (200, _jpg(opaque_photo())),
        LOCAL_URL: (200, _png(headshot_cutout())),
        DEAD_URL: (404, None),
    }

    def fetch(url):
        calls.append(url)
        return payloads[url]
    return fetch


def test_run_measures_only_stale_urls_and_upserts_exact_keys(fakedb):
    salon = [AMP_URL, CUT_URL, DEAD_URL, LOCAL_URL]
    stale = [AMP_URL, DEAD_URL, LOCAL_URL]  # CUT_URL measured recently: skipped
    conn = fakedb.Connection(_responder(salon, stale))
    calls: list[str] = []
    summary = m.run(conn, _fetcher(calls), sleep=lambda _s: None)

    assert calls == stale
    assert summary == {
        "salon_urls": 4, "measured": 3, "cutouts": 1, "framed": 2,
        "fetch_errors": [DEAD_URL], "written": 3, "dry_run": False,
    }
    upserts = [p for cur in conn.cursors for sql, p in cur.executed if "INSERT INTO photo_cutout" in sql]
    assert upserts == [
        (AMP_URL, False, 200, 150, 200),   # key keeps the literal '&amp;'
        (DEAD_URL, False, None, None, 404),
        (LOCAL_URL, True, 200, 150, 200),  # key is the local path, not the site URL
    ]
    assert conn.commits == 1
    # The read transaction is closed before the downloads (Neon idle timeout).
    assert conn.rollbacks == 1


def test_run_dry_run_writes_nothing(fakedb):
    conn = fakedb.Connection(_responder([CUT_URL], [CUT_URL]))
    summary = m.run(conn, _fetcher([]), dry_run=True, sleep=lambda _s: None)
    assert summary["measured"] == 1 and summary["cutouts"] == 1 and summary["written"] == 0
    assert fakedb.mutating_statements(conn) == []
    assert conn.commits == 0


def test_run_with_nothing_stale_does_not_fetch(fakedb):
    conn = fakedb.Connection(_responder([CUT_URL], []))
    calls: list[str] = []
    summary = m.run(conn, _fetcher(calls), sleep=lambda _s: None)
    assert calls == [] and summary["measured"] == 0 and summary["written"] == 0


def test_measure_all_sleeps_between_requests_only():
    sleeps: list[float] = []
    m.measure_all([CUT_URL, LOCAL_URL, DEAD_URL], _fetcher([]), sleep=sleeps.append)
    assert sleeps == [m.REQUEST_DELAY_SECONDS] * 2


def test_salon_query_covers_person_photo_and_both_corners():
    sql = " ".join(m.SALON_URLS_SQL.split())
    assert "COALESCE(h.photo_url, f.headshot_url)" in sql
    assert "fa.id = h.fighter_a_id" in sql and "fb.id = h.fighter_b_id" in sql


def test_stale_query_rechecks_old_and_failed_rows():
    sql = " ".join(m.STALE_SQL.split())
    assert "pc.url IS NULL" in sql
    assert "make_interval(days => %s)" in sql
    assert "pc.http_status IS DISTINCT FROM 200" in sql


def test_does_not_ask_for_avif_or_webp():
    # ufc.com answers 'image/avif' with a lossy re-encode whose alpha is noisy:
    # Rousey's cutout measured as framed (ring 0.896 vs 1.0 on the PNG).
    accept = m._HEADERS.get("Accept", "*/*").lower()
    assert "avif" not in accept and "webp" not in accept
    assert m.build_session().headers["Accept"] == accept

"""RSS fetching for the news scraper (feeds blocked in GitHub Actions).

Since 2026-06-30 the ESPN Deportes feed answers non-browser TLS fingerprints
with an empty/block body that feedparser parses to bozo=False + 0 entries, so
the run stayed GREEN with "fetched": 0. ufcespanol.com's WAF 403s plain
requests the same way.

The fetch now goes through http_browser.fetch_url (curl_cffi impersonating
Chrome when installed, plain requests with browser headers otherwise), every
run reports a per-source breakdown with a ::warning:: annotation for any feed
at 0 articles, and a run where EVERY feed yields zero articles must still fail
loudly instead of silently succeeding. Everything is mocked: no network. The
tests force the requests fallback (or fake the curl path) so results do not
depend on whether curl_cffi is installed in the environment running the suite.
"""

from datetime import datetime, timezone
from collections import Counter
import json

import pytest
import requests

from src.scrapers import http_browser, news


VALID_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>ESPN Deportes - MMA</title>
    <item>
      <title>Islam Makhachev defiende el titulo</title>
      <link>https://espndeportes.espn.com/mma/nota/_/id/1</link>
      <description>Resumen uno</description>
      <pubDate>Wed, 01 Jul 2026 12:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Alex Pereira anuncia su regreso</title>
      <link>https://espndeportes.espn.com/mma/nota/_/id/2</link>
      <description>Resumen dos</description>
      <pubDate>Tue, 30 Jun 2026 12:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

# UFC Español-style feed (Drupal): Spanish day/month abbreviations in pubDate,
# single-digit hour, and NO image elements anywhere (og:image covers that in
# production). The third item carries an unparseable date on purpose: the
# article must survive with published_at=None.
UFC_ESPANOL_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>UFC Espanol</title>
    <item>
      <title>Topuria defiende el cinturon en Madrid</title>
      <link>https://www.ufcespanol.com/news/topuria-madrid</link>
      <description>Resumen con acento: pelea confirmada</description>
      <pubDate>Dom, 19 Jul 2026 5:44:56 GMT</pubDate>
    </item>
    <item>
      <title>Brandon Moreno busca el titulo mosca</title>
      <link>https://www.ufcespanol.com/news/moreno-titulo</link>
      <description>Resumen dos</description>
      <pubDate>Sáb, 18 Ene 2026 10:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Noticia con fecha rota</title>
      <link>https://www.ufcespanol.com/news/fecha-rota</link>
      <description>Resumen tres</description>
      <pubDate>esto no es una fecha</pubDate>
    </item>
  </channel>
</rss>
""".encode("utf-8")

# What a bot-block page looks like: HTML, HTTP 200, zero RSS entries.
BLOCK_HTML = b"<html><head><title>Access Denied</title></head><body>blocked</body></html>"


class FakeResponse:
    def __init__(self, status_code=200, content=b""):
        self.status_code = status_code
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error", response=self)


@pytest.fixture
def no_curl_cffi(monkeypatch):
    """Force the plain-requests fallback: locally curl_cffi IS installed, so
    without this the fetch would take the impersonated path and never hit the
    mocked requests.get."""
    monkeypatch.setattr(http_browser, "_load_curl_cffi", lambda: None)


def test_fetch_uses_browser_headers_and_returns_articles(monkeypatch, no_curl_cffi):
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append({"url": url, "headers": headers, "timeout": timeout})
        return FakeResponse(200, VALID_RSS)

    monkeypatch.setattr(http_browser.requests, "get", fake_get)

    articles, fetched_by_source = news.fetch_feed_articles(max_articles=10)

    assert len(articles) == 2  # same payload for every feed -> deduped by URL
    assert articles[0].title == "Islam Makhachev defiende el titulo"
    assert articles[0].url == "https://espndeportes.espn.com/mma/nota/_/id/1"
    # One explicit fetch per configured feed, with real-browser headers.
    assert len(calls) == len(news.RSS_FEEDS)
    headers = calls[0]["headers"]
    assert headers["User-Agent"].startswith("Mozilla/5.0")
    assert "feedparser" not in headers["User-Agent"]
    assert "application/rss+xml" in headers["Accept"]
    assert calls[0]["timeout"] == 20
    # Pre-dedup counts: every feed answered with the same 2 items.
    assert fetched_by_source == {source: 2 for source, _ in news.RSS_FEEDS}


def test_fetch_prefers_impersonated_client_when_available(monkeypatch):
    """With curl_cffi present the feed fetch must go through impersonate="chrome"
    (ESPN/ufcespanol block on TLS fingerprint) and never touch requests.get."""
    calls = []

    class FakeCurlRequests:
        @staticmethod
        def get(url, headers=None, timeout=None, impersonate=None):
            calls.append({"url": url, "headers": headers, "impersonate": impersonate})
            return FakeResponse(200, VALID_RSS)

    monkeypatch.setattr(http_browser, "_load_curl_cffi", lambda: FakeCurlRequests)
    monkeypatch.setattr(
        http_browser.requests,
        "get",
        lambda *args, **kwargs: pytest.fail("requests.get must not be used when curl_cffi is available"),
    )

    articles, _ = news.fetch_feed_articles(max_articles=10)

    assert len(articles) == 2
    assert len(calls) == len(news.RSS_FEEDS)
    assert all(call["impersonate"] == "chrome" for call in calls)
    # Impersonation owns the User-Agent; the Accept header still passes through.
    for call in calls:
        assert "User-Agent" not in (call["headers"] or {})
        assert "application/rss+xml" in call["headers"]["Accept"]


@pytest.mark.parametrize("body", [b"", BLOCK_HTML], ids=["empty-body", "block-html"])
def test_zero_articles_across_all_feeds_raises(monkeypatch, no_curl_cffi, body):
    monkeypatch.setattr(
        http_browser.requests,
        "get",
        lambda url, headers=None, timeout=None: FakeResponse(200, body),
    )
    with pytest.raises(RuntimeError, match="0 articles parsed from RSS feeds"):
        news.fetch_feed_articles(max_articles=10)


def test_one_feed_down_does_not_kill_the_rest(monkeypatch, no_curl_cffi, capsys):
    # ESPN blocked with a 403 while the others still answer: the run must
    # survive on the healthy feeds (per-feed tolerance + global zero guard),
    # and the dead feed must be visible as a GitHub Actions ::warning::.
    def fake_get(url, headers=None, timeout=None):
        if "espn" in url:
            return FakeResponse(403)
        return FakeResponse(200, VALID_RSS)

    monkeypatch.setattr(http_browser.requests, "get", fake_get)

    articles, fetched_by_source = news.fetch_feed_articles(max_articles=10)

    assert len(articles) == 2
    assert fetched_by_source == {"ESPN Deportes": 0, "Marca": 2, "UFC Español": 2}
    out = capsys.readouterr().out
    assert "::warning::Fuente ESPN Deportes devolvió 0 artículos" in out
    assert "Marca devolvió" not in out
    assert "UFC Español devolvió" not in out


def test_all_feeds_failing_raises_after_warning_each_source(monkeypatch, no_curl_cffi, capsys):
    monkeypatch.setattr(
        http_browser.requests,
        "get",
        lambda url, headers=None, timeout=None: FakeResponse(403),
    )
    with pytest.raises(RuntimeError, match="0 articles parsed from RSS feeds"):
        news.fetch_feed_articles(max_articles=10)
    out = capsys.readouterr().out
    for source, _ in news.RSS_FEEDS:
        assert f"::warning::Fuente {source} devolvió 0 artículos" in out


def test_ufcespanol_feed_parses_spanish_dates_and_has_no_feed_images(monkeypatch, no_curl_cffi):
    def fake_get(url, headers=None, timeout=None):
        if "ufcespanol" in url:
            return FakeResponse(200, UFC_ESPANOL_RSS)
        return FakeResponse(403)

    monkeypatch.setattr(http_browser.requests, "get", fake_get)

    articles, fetched_by_source = news.fetch_feed_articles(max_articles=10)

    assert fetched_by_source == {"ESPN Deportes": 0, "Marca": 0, "UFC Español": 3}
    assert [a.source for a in articles] == ["UFC Español"] * 3
    by_url = {a.url: a for a in articles}
    topuria = by_url["https://www.ufcespanol.com/news/topuria-madrid"]
    assert topuria.published_at == datetime(2026, 7, 19, 5, 44, 56, tzinfo=timezone.utc)
    moreno = by_url["https://www.ufcespanol.com/news/moreno-titulo"]
    assert moreno.published_at == datetime(2026, 1, 18, 10, 0, 0, tzinfo=timezone.utc)
    # Broken date: the ARTICLE stays, only the date is dropped.
    broken = by_url["https://www.ufcespanol.com/news/fecha-rota"]
    assert broken.published_at is None
    # The feed ships no images; production falls back to fetch_og_image.
    assert all(a.image_url is None for a in articles)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Real shape observed in the feed: Spanish day, single-digit hour.
        ("Dom, 19 Jul 2026 5:44:56 GMT", datetime(2026, 7, 19, 5, 44, 56, tzinfo=timezone.utc)),
        # Accented and unaccented day forms.
        ("Sáb, 18 Ene 2026 10:00:00 GMT", datetime(2026, 1, 18, 10, 0, 0, tzinfo=timezone.utc)),
        ("Sab, 18 Ene 2026 10:00:00 GMT", datetime(2026, 1, 18, 10, 0, 0, tzinfo=timezone.utc)),
        ("Mié, 15 Abr 2026 08:30:00 GMT", datetime(2026, 4, 15, 8, 30, 0, tzinfo=timezone.utc)),
        ("Mie, 15 Abr 2026 08:30:00 GMT", datetime(2026, 4, 15, 8, 30, 0, tzinfo=timezone.utc)),
        # "Mar" is Tuesday before the comma but March in the month slot: the
        # translation must convert the day WITHOUT corrupting the month.
        ("Mar, 03 Mar 2026 12:00:00 GMT", datetime(2026, 3, 3, 12, 0, 0, tzinfo=timezone.utc)),
        ("Lun, 10 Ago 2026 23:59:59 GMT", datetime(2026, 8, 10, 23, 59, 59, tzinfo=timezone.utc)),
        ("Vie, 25 Dic 2026 07:00:00 GMT", datetime(2026, 12, 25, 7, 0, 0, tzinfo=timezone.utc)),
        ("Jue, 12 Nov 2026 18:15:00 GMT", datetime(2026, 11, 12, 18, 15, 0, tzinfo=timezone.utc)),
        # English dates (ESPN/Marca) must keep parsing unchanged.
        ("Wed, 01 Jul 2026 12:00:00 GMT", datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)),
    ],
)
def test_parse_published_at_spanish_and_english(raw, expected):
    assert news._parse_published_at(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "esto no es una fecha", "Xyz, 99 Foo 20xx"])
def test_parse_published_at_tolerant_fallback(raw):
    # Unparseable -> None (article kept, published_at nullable), never raises.
    assert news._parse_published_at(raw) is None


def test_build_summary_includes_per_source_breakdown():
    counts = Counter(
        {
            "fetched": 41,
            "fetched_ESPN Deportes": 38,
            "fetched_Marca": 3,
            "fetched_UFC Español": 0,
            "stored": 5,
        }
    )
    summary = json.loads(news._build_summary(counts))
    assert summary["fetched_by_source"] == {
        "ESPN Deportes": 38,
        "Marca": 3,
        "UFC Español": 0,
    }
    assert summary["fetched"] == 41


# ---------------------------------------------------------------- imagenes
#
# 🪤 EL RSS DE MARCA MIENTE A VECES. El 15-ago-2026 la portada publicaba una
# noticia titulada "Asi fue el ultimo e intenso cara a cara entre Makhachev y
# Garry antes del UFC 330" ILUSTRADA CON UNA FOTO DE UN PARTIDO DE FUTBOL. La
# foto no era un error nuestro: el propio <media:content> del feed de Marca
# apuntaba a /imagenes/2018/05/27/15274426266232.jpg, una imagen de hace ocho
# anos. La pagina del articulo declaraba la correcta en su og:image.
#
# El scraper hacia `article.image_url or fetch_og_image(article.url)`: se
# quedaba con la del feed y NUNCA llegaba a mirar la buena. Ahora es al reves.
# El og:image es lo que el medio declara como imagen DE ESE articulo; el feed es
# la reserva para cuando la pagina no lo trae.


def test_la_imagen_sale_del_og_image_y_no_de_la_del_feed(monkeypatch):
    """El caso real de Marca: el feed trae una foto de 2018, la pagina la buena."""
    del monkeypatch  # la funcion bajo prueba no toca red: se le inyecta el fetcher
    DEL_FEED = "https://objetos.estaticos-marca.com/assets/multimedia/imagenes/2018/05/27/15274426266232.jpg"
    DE_LA_PAGINA = "https://objetos-xlk.estaticos-marca.com/files/article_main_microformat/uploads/2026/08/15/6a800c1f79554.jpeg"

    elegida = news.pick_image_url(
        feed_image=DEL_FEED,
        url="https://www.marca.com/combates-ufc/2026/08/15/x.html",
        fetch_og=lambda _url: DE_LA_PAGINA,
    )
    assert elegida == DE_LA_PAGINA


def test_si_la_pagina_no_declara_imagen_se_usa_la_del_feed():
    """La reserva. Sin esto, quitar la preferencia dejaria noticias sin foto."""
    DEL_FEED = "https://ejemplo/foto.jpg"
    assert news.pick_image_url(feed_image=DEL_FEED, url="https://x/y", fetch_og=lambda _u: None) == DEL_FEED


def test_sin_ninguna_de_las_dos_no_se_inventa_nada():
    assert news.pick_image_url(feed_image=None, url="https://x/y", fetch_og=lambda _u: None) is None


def test_si_el_feed_no_trae_imagen_igual_se_pide_la_pagina():
    """Es lo que ya pasaba con UFC Espanol, cuyo feed no lleva imagenes."""
    assert (
        news.pick_image_url(feed_image=None, url="https://x/y", fetch_og=lambda _u: "https://ok/f.jpg")
        == "https://ok/f.jpg"
    )


def test_un_og_image_roto_no_tumba_la_noticia():
    """fetch_og_image ya traga sus excepciones, pero si alguna se escapa la
    noticia tiene que entrar igual con la imagen del feed en vez de perderse."""
    def revienta(_url):
        raise RuntimeError("WAF 403")

    assert news.pick_image_url(feed_image="https://ejemplo/foto.jpg", url="https://x/y", fetch_og=revienta) == "https://ejemplo/foto.jpg"

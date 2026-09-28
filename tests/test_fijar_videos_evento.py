# -*- coding: utf-8 -*-
"""La correccion manual de los videos de un evento, con red debajo.

QUE SE FIJA AQUI. `scripts/fijar_videos_evento.py` es la unica via soportada
para corregir el careo o el pesaje de un evento, porque
`set_event_faceoff_video` es first-writer-wins y no puede sobrescribir. Un
script que escribe a pelo en produccion se prueba ENTERO o no se prueba:

  1. el id se valida de forma ANTES de gastar una peticion o abrir la base;
  2. el titulo se PIDE al oembed, nunca se teclea;
  3. un video que no resuelve NO se escribe (salvo --forzar);
  4. un short de menos de 75 s se avisa bien visible — es el fallo del 1090;
  5. sin --aplicar NO se escribe ni una linea;
  6. el id y su titulo viajan juntos: id nuevo con titulo viejo es el rotulo
     que miente, y la migracion 029 existe para cerrarlo;
  7. correrlo dos veces no escribe la segunda;
  8. el directo (migracion 027) tiene tres palancas: --directo lo fija con su
     titulo real, --quitar-directo lo deja a NULL y --ocultar-directo escribe
     el interruptor 'off' que apaga la web sin desplegar.

NI RED NI POSTGRES. El `fetch` se inyecta y la conexion se falsea con el
`fakedb` de conftest, que anota el SQL que se habria ejecutado. La trampa que
esto evita esta escrita en el propio conftest: una bateria que abre un socket a
Neon con un .env delante escribe en PRODUCCION.

LOS CUATRO VIDEOS son los reales, verificados con el oembed el 18-sep-2026.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from scripts import fijar_videos_evento as fijar


# --- Los cuatro videos del caso, con sus datos reales --------------------
CAREO = "MQLCbgV5rhc"  # "#CryptoCom #UFC331: Careos Conferencia de Prensa", 3:15
CAREO_TITULO = "#CryptoCom #UFC331: Careos Conferencia de Prensa"
CAREO_SEGUNDOS = 195

PESAJE = "enkyfSnB0r0"  # "UFC 331: Official Weigh-Ins" (TheMacLife), 17:20
PESAJE_TITULO = "UFC 331: Official Weigh-Ins"
PESAJE_SEGUNDOS = 1040

SHORT = "0xTX8Aut0VY"  # "what are these faceoffs saying?! #ufc331", 0:17. EL MALO
SHORT_TITULO = "what are these faceoffs saying?! #ufc331"
SHORT_SEGUNDOS = 17

CAREO_VIEJO_LEGITIMO = "qpjPPGtcS3I"  # "Noche UFC: Weigh-In Faceoffs", 2023

# El directo de ejemplo de la migracion 027 (UFC 330, evento 1088, canal
# ufcespanol). Titulo resuelto por oembed el 28-sep-2026. Los 0 segundos son lo
# que da /watch MIENTRAS emite o esta programado; hoy, ya terminado, da 3600.
# Aqui se simula la noche de la velada, que es cuando se usa la palanca.
DIRECTO = "qM-h-OudTqM"
DIRECTO_TITULO = "UFC 330 | Previa del Evento ¡EN VIVO!"
DIRECTO_SEGUNDOS_EN_EMISION = 0

EVENTO = 1090

TITULOS = {
    CAREO: CAREO_TITULO,
    PESAJE: PESAJE_TITULO,
    SHORT: SHORT_TITULO,
    CAREO_VIEJO_LEGITIMO: "Noche UFC: Weigh-In Faceoffs",
    DIRECTO: DIRECTO_TITULO,
}
DURACIONES = {
    CAREO: CAREO_SEGUNDOS,
    PESAJE: PESAJE_SEGUNDOS,
    SHORT: SHORT_SEGUNDOS,
    CAREO_VIEJO_LEGITIMO: 127,  # 2:07, medido contra YouTube el 18-sep-2026
    DIRECTO: DIRECTO_SEGUNDOS_EN_EMISION,
}


def cuerpo_oembed(video_id: str) -> str:
    """La respuesta del oembed, con la forma que devuelve YouTube de verdad."""
    return json.dumps(
        {
            "title": TITULOS[video_id],
            "author_name": "UFC",
            "author_url": "https://www.youtube.com/@UFC",
            "type": "video",
            "height": 113,
            "width": 200,
            "version": "1.0",
            "provider_name": "YouTube",
            "provider_url": "https://www.youtube.com/",
            "thumbnail_url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            "html": f'<iframe src="https://www.youtube.com/embed/{video_id}"></iframe>',
        }
    )


def cuerpo_watch(video_id: str) -> str:
    """Un trozo del HTML de /watch con el `videoDetails` donde vive la duracion."""
    return (
        'var ytInitialPlayerResponse = {"videoDetails":{"videoId":"%s",'
        '"title":"%s","lengthSeconds":"%s","isOwnerViewing":false,'
        '"isCrawlable":true}};'
        % (video_id, TITULOS[video_id], DURACIONES[video_id])
    )


class YouTubeFalso:
    """Sustituye a `fetch_text` y anota que URLs se pidieron. Cero red."""

    def __init__(self, *, sin_oembed: set[str] = frozenset(), sin_watch: set[str] = frozenset()):
        self.pedidas: list[str] = []
        self._sin_oembed = set(sin_oembed)
        self._sin_watch = set(sin_watch)

    def __call__(self, url: str) -> str | None:
        self.pedidas.append(url)
        for video_id in TITULOS:
            if video_id not in url:
                continue
            if "oembed" in url:
                return None if video_id in self._sin_oembed else cuerpo_oembed(video_id)
            return None if video_id in self._sin_watch else cuerpo_watch(video_id)
        return None


# --- La base falsa: anota el SQL y refleja el UPDATE en la fila ----------
FILA_1090_TRAS_EL_ARREGLO_A_MANO = {
    "id": EVENTO,
    "name": "Crypto.com UFC 331: Van vs. Pantoja 2",
    "event_date": "2026-09-19",
    "status": "upcoming",
    "faceoff_video_id": CAREO,  # ya corregido a mano en produccion
    "faceoff_video_title": None,  # pero SIN titulo: eso es lo que falta
    "weighin_video_id": None,
    "weighin_video_title": None,
    "live_video_id": None,
    "live_video_title": None,
}


class BaseFalsa:
    """Responder para `RecordingConnection`: information_schema, SELECT y UPDATE.

    El UPDATE se aplica sobre la fila en memoria a proposito: asi el segundo
    `read_event` (la comprobacion en caliente) ve lo escrito, y el test de
    idempotencia puede volver a correr `main` contra el estado resultante.
    """

    def __init__(self, fila: dict | None = None, columnas_presentes=None):
        self.fila = dict(fila) if fila is not None else None
        self.columnas = (
            list(fijar.REQUIRED_COLUMNS)
            if columnas_presentes is None
            else list(columnas_presentes)
        )
        self.updates: list[tuple[str, tuple]] = []
        self.lecturas = 0
        # Lo que `ejecutar` apunta de cada pasada: las conexiones abiertas (para
        # contar los commit) y los POST a /api/revalidate (con que secreto).
        self.conexiones: list = []
        self.revalidaciones: list[str] = []

    @property
    def commits(self) -> int:
        return sum(conn.commits for conn in self.conexiones)

    def __call__(self, sql, params=None):
        plano = " ".join(sql.split()).lower()
        if "information_schema.columns" in plano:
            return [(c,) for c in self.columnas]
        if plano.startswith("select") and "from events" in plano:
            self.lecturas += 1
            if self.fila is None:
                return []
            return [tuple(self.fila[c] for c in fijar.EVENT_COLUMNS)]
        if plano.startswith("update events"):
            self.updates.append((sql, params))
            columnas = [
                trozo.split("=")[0].strip()
                for trozo in sql.split("SET", 1)[1].split("WHERE")[0].split(",")
            ]
            for columna, valor in zip(columnas, params):
                self.fila[columna] = valor
            return []
        raise AssertionError(f"SQL inesperado en el test: {sql}")


@pytest.fixture
def ejecutar(monkeypatch, fakedb):
    """Corre `main()` sin red y sin Postgres. Devuelve (codigo, base, youtube).

    Tampoco toca la WEB: el POST a /api/revalidate se sustituye por uno que
    solo apunta en `base.revalidaciones`, y el secreto no se lee del .env de
    verdad (por defecto no hay; `secreto=` lo pone).
    """

    def correr(
        *argv,
        base: BaseFalsa | None = None,
        youtube: YouTubeFalso | None = None,
        secreto: str | None = None,
        http_revalidate: int | None = 200,
    ):
        base = base if base is not None else BaseFalsa(FILA_1090_TRAS_EL_ARREGLO_A_MANO)
        youtube = youtube if youtube is not None else YouTubeFalso()
        conexiones: list = []

        def conectar(_dsn):
            conn = fakedb.Connection(base)
            conexiones.append(conn)
            base.conexiones.append(conn)
            return conn

        def revalidar(secret):
            base.revalidaciones.append(secret)
            return http_revalidate

        monkeypatch.setattr(fijar, "_dsn", lambda: "postgresql://falsa/no-existe")
        monkeypatch.setattr(fijar, "_connect", conectar)
        monkeypatch.setattr(fijar, "fetch_text", youtube)
        monkeypatch.setattr(fijar, "revalidate_secret", lambda: secreto)
        monkeypatch.setattr(fijar, "post_revalidate", revalidar)

        codigo = fijar.main(list(argv))
        escrituras = [
            s for conn in conexiones for s in fakedb.mutating_statements(conn)
        ]
        return codigo, base, youtube, escrituras

    return correr


# --- 1. Forma del id ------------------------------------------------------
@pytest.mark.parametrize("video_id", [CAREO, PESAJE, SHORT, CAREO_VIEJO_LEGITIMO])
def test_los_cuatro_ids_reales_tienen_forma_valida(video_id):
    assert fijar.is_valid_video_id(video_id)


@pytest.mark.parametrize(
    "malo",
    [
        None,
        "",
        "MQLCbgV5rh",  # 10: uno de menos
        "MQLCbgV5rhcX",  # 12: uno de mas
        "MQLCbgV5rh!",  # caracter fuera del alfabeto
        "MQLCbg/5rhc",  # una barra: es una URL, no un id
        "https://www.youtube.com/watch?v=MQLCbgV5rhc",  # la URL entera
        " MQLCbgV5rhc",  # con espacio delante
    ],
)
def test_un_id_que_no_lo_es_se_rechaza(malo):
    assert not fijar.is_valid_video_id(malo)


# --- 2. El titulo se lee del oembed, no se teclea -------------------------
def test_el_titulo_sale_del_oembed_tal_cual():
    assert fijar.parse_oembed_title(cuerpo_oembed(CAREO)) == CAREO_TITULO
    assert fijar.parse_oembed_title(cuerpo_oembed(PESAJE)) == PESAJE_TITULO


@pytest.mark.parametrize(
    "cuerpo",
    [
        None,
        "",
        "<html>404 Not Found</html>",  # lo que devuelve un id inexistente
        "[1, 2, 3]",  # JSON valido pero no un objeto
        '{"author_name": "UFC"}',  # sin title
        '{"title": "   "}',  # title vacio
    ],
)
def test_sin_oembed_utilizable_no_hay_titulo(cuerpo):
    assert fijar.parse_oembed_title(cuerpo) is None


# --- 3. La duracion sale del HTML de /watch -------------------------------
def test_la_duracion_sale_de_lengthseconds():
    assert fijar.parse_length_seconds(cuerpo_watch(CAREO)) == CAREO_SEGUNDOS
    assert fijar.parse_length_seconds(cuerpo_watch(SHORT)) == SHORT_SEGUNDOS


def test_sin_lengthseconds_la_duracion_es_desconocida_y_no_cero():
    """None y no 0: un 0 pasaria por short y dispararia el aviso en falso."""
    assert fijar.parse_length_seconds("<html>consent page</html>") is None
    assert fijar.parse_length_seconds(None) is None


# --- 4. resolve_video: junta las dos peticiones ---------------------------
def test_resolve_video_pide_oembed_y_watch_y_no_toca_la_red():
    youtube = YouTubeFalso()

    info = fijar.resolve_video(CAREO, fetch=youtube)

    assert info.title == CAREO_TITULO
    assert info.length_seconds == CAREO_SEGUNDOS
    assert info.resolved and not info.looks_like_a_short
    assert len(youtube.pedidas) == 2, youtube.pedidas
    assert "oembed" in youtube.pedidas[0] and CAREO in youtube.pedidas[0]
    assert youtube.pedidas[1] == fijar.watch_url(CAREO)


def test_resolve_video_marca_el_short_de_17_segundos():
    """El caso del 1090: 17 s < 75 s, el mismo umbral que ya usa la web."""
    info = fijar.resolve_video(SHORT, fetch=YouTubeFalso())

    assert info.length_seconds == SHORT_SEGUNDOS
    assert info.looks_like_a_short


def test_resolve_video_sin_oembed_no_inventa_titulo():
    info = fijar.resolve_video(CAREO, fetch=YouTubeFalso(sin_oembed={CAREO}))

    assert info.title is None and not info.resolved
    assert info.error and "oembed" in info.error


def test_resolve_video_con_id_invalido_ni_lo_intenta():
    youtube = YouTubeFalso()

    info = fijar.resolve_video("no-soy-un-id", fetch=youtube)

    assert not info.resolved
    assert youtube.pedidas == [], "no se gasta una peticion en un id mal formado"


# --- 5. El parseo de argumentos ------------------------------------------
def test_sin_evento_el_parser_no_deja_seguir():
    with pytest.raises(SystemExit):
        fijar.build_parser().parse_args(["--careo", CAREO])


def test_por_defecto_no_aplica_ni_fuerza():
    args = fijar.build_parser().parse_args(["--evento", "1090", "--careo", CAREO])

    assert args.evento == 1090
    assert args.careo == CAREO
    assert args.pesaje is None
    assert args.aplicar is False, "escribir tiene que ser explicito"
    assert args.forzar is False


def test_se_pueden_fijar_los_dos_actos_de_una_vez():
    args = fijar.build_parser().parse_args(
        ["--evento", "1090", "--careo", CAREO, "--pesaje", PESAJE, "--aplicar"]
    )

    assert (args.careo, args.pesaje, args.aplicar) == (CAREO, PESAJE, True)


# --- 6. El plan: id y titulo viajan juntos --------------------------------
def test_el_plan_escribe_el_id_y_su_titulo():
    actual = dict(FILA_1090_TRAS_EL_ARREGLO_A_MANO, weighin_video_id=None)
    info = fijar.VideoInfo(PESAJE, title=PESAJE_TITULO, length_seconds=PESAJE_SEGUNDOS)

    cambios = {c.column: c.after for c in fijar.plan_changes(actual, {"pesaje": info})}

    assert cambios == {
        "weighin_video_id": PESAJE,
        "weighin_video_title": PESAJE_TITULO,
    }


def test_un_id_nuevo_sin_titulo_BORRA_el_titulo_viejo():
    """La regla que cierra la mentira: nunca el rotulo de antes sobre el video de ahora."""
    actual = dict(
        FILA_1090_TRAS_EL_ARREGLO_A_MANO,
        faceoff_video_id=SHORT,
        faceoff_video_title=SHORT_TITULO,
    )
    info = fijar.VideoInfo(CAREO, title=None, error="el oembed no contesto")

    cambios = {c.column: c.after for c in fijar.plan_changes(actual, {"careo": info})}

    assert cambios["faceoff_video_id"] == CAREO
    assert cambios["faceoff_video_title"] is None


def test_si_la_base_ya_dice_eso_no_hay_cambios():
    actual = dict(
        FILA_1090_TRAS_EL_ARREGLO_A_MANO,
        faceoff_video_id=CAREO,
        faceoff_video_title=CAREO_TITULO,
    )
    info = fijar.VideoInfo(CAREO, title=CAREO_TITULO, length_seconds=CAREO_SEGUNDOS)

    assert fijar.plan_changes(actual, {"careo": info}) == []


def test_el_update_SOBRESCRIBE_sin_guarda_is_null():
    """Lo contrario que set_event_faceoff_video, y es justo el motivo del script."""
    cambios = [
        fijar.Change("faceoff_video_id", SHORT, CAREO),
        fijar.Change("faceoff_video_title", None, CAREO_TITULO),
    ]

    sql, params = fijar.build_update(EVENTO, cambios)

    plano = " ".join(sql.split()).lower()
    assert "is null" not in plano, "con la guarda del cron no se podria corregir nada"
    assert "faceoff_video_id = %s" in plano and "faceoff_video_title = %s" in plano
    assert "where id = %s" in plano
    assert params == (CAREO, CAREO_TITULO, EVENTO)
    assert "updated_at" not in plano, "events NO tiene esa columna (ver db/migrations)"


# --- 7. main(): lo que escribe y, sobre todo, lo que no ------------------
def test_sin_aplicar_no_escribe_NADA(ejecutar):
    codigo, base, _youtube, escrituras = ejecutar(
        "--evento", str(EVENTO), "--pesaje", PESAJE
    )

    assert codigo == 0
    assert escrituras == [], f"la simulacion escribio: {escrituras}"
    assert base.updates == []
    assert base.fila["weighin_video_id"] is None


def test_con_aplicar_escribe_id_y_titulo(ejecutar):
    codigo, base, _youtube, escrituras = ejecutar(
        "--evento", str(EVENTO), "--pesaje", PESAJE, "--aplicar"
    )

    assert codigo == 0
    assert len(escrituras) == 1, escrituras
    assert base.fila["weighin_video_id"] == PESAJE
    assert base.fila["weighin_video_title"] == PESAJE_TITULO
    _sql, params = base.updates[0]
    assert params[-1] == EVENTO


def test_correrlo_dos_veces_no_escribe_la_segunda(ejecutar):
    """Idempotente: el segundo pase ve la base ya igual y no toca nada."""
    argv = ("--evento", str(EVENTO), "--pesaje", PESAJE, "--aplicar")
    _codigo, base, _youtube, primeras = ejecutar(*argv)
    assert len(primeras) == 1

    codigo, base, _youtube, segundas = ejecutar(*argv, base=base)

    assert codigo == 0
    assert segundas == [], f"la segunda pasada volvio a escribir: {segundas}"


def test_un_id_mal_formado_aborta_sin_pedir_nada_ni_abrir_la_base(ejecutar):
    """Corta en la forma, antes de la red y antes de la base.

    OJO CON EL CASO DE PRUEBA, que ya mordio al escribir esto: "no-es-un-id"
    tiene EXACTAMENTE 11 caracteres del alfabeto de YouTube, asi que PASA la
    validacion de forma y llega al oembed. Un id inventado puede ser
    indistinguible de uno real de puro mirarlo — por eso la segunda red
    (que el oembed conteste) no sobra.
    """
    codigo, base, youtube, escrituras = ejecutar(
        "--evento", str(EVENTO), "--careo", "ESTO_NO_ES_UN_ID", "--aplicar"
    )

    assert codigo == 1
    assert escrituras == []
    assert youtube.pedidas == [], "ni una peticion por un id que no tiene forma"


def test_un_id_con_forma_valida_pero_inventado_lo_para_el_oembed(ejecutar):
    """La otra mitad: 11 caracteres correctos no prueban que el video exista."""
    codigo, base, youtube, escrituras = ejecutar(
        "--evento", str(EVENTO), "--careo", "no-es-un-id", "--aplicar"
    )

    assert fijar.is_valid_video_id("no-es-un-id"), "son 11 chars validos, y engana"
    assert codigo == 1, "lo tiene que parar el oembed, no la forma"
    assert escrituras == []
    assert base.fila["faceoff_video_id"] == CAREO, "el careo bueno sigue en su sitio"


def test_sin_careo_ni_pesaje_no_hay_nada_que_hacer(ejecutar):
    codigo, _base, _youtube, escrituras = ejecutar("--evento", str(EVENTO), "--aplicar")

    assert codigo == 2
    assert escrituras == []


def test_un_video_que_no_resuelve_no_se_escribe(ejecutar):
    """El guardarrail del reproductor muerto: sin oembed no hay escritura."""
    codigo, base, _youtube, escrituras = ejecutar(
        "--evento",
        str(EVENTO),
        "--pesaje",
        PESAJE,
        "--aplicar",
        youtube=YouTubeFalso(sin_oembed={PESAJE}),
    )

    assert codigo == 1
    assert escrituras == []
    assert base.fila["weighin_video_id"] is None


def test_con_forzar_el_id_si_se_escribe_y_el_titulo_queda_a_null(ejecutar, capsys):
    codigo, base, _youtube, escrituras = ejecutar(
        "--evento",
        str(EVENTO),
        "--pesaje",
        PESAJE,
        "--aplicar",
        "--forzar",
        youtube=YouTubeFalso(sin_oembed={PESAJE}),
    )

    assert codigo == 0
    assert len(escrituras) == 1
    assert base.fila["weighin_video_id"] == PESAJE
    assert base.fila["weighin_video_title"] is None, (
        "sin titulo confirmado la columna va a NULL: la web no pinta rotulo"
    )


def test_el_short_se_avisa_bien_visible_antes_de_escribir(ejecutar, capsys):
    """El caso de test del expediente: 0xTX8Aut0VY, 17 s, careo del 1090."""
    ejecutar("--evento", str(EVENTO), "--careo", SHORT)

    salida = capsys.readouterr().out
    assert "SHORT" in salida.upper()
    assert "17" in salida
    assert str(fijar.MIN_DURATION_SECONDS) in salida


def test_el_careo_bueno_no_dispara_ningun_aviso_de_short(ejecutar, capsys):
    """Los 3:15 del careo oficial pasan de largo: el aviso no puede ser ruido."""
    ejecutar("--evento", str(EVENTO), "--careo", CAREO)

    assert "SHORT" not in capsys.readouterr().out.upper()


def test_si_falta_la_migracion_029_aborta_diciendolo(ejecutar, capsys):
    base = BaseFalsa(FILA_1090_TRAS_EL_ARREGLO_A_MANO, columnas_presentes=[])

    codigo, base, _youtube, escrituras = ejecutar(
        "--evento", str(EVENTO), "--pesaje", PESAJE, "--aplicar", base=base
    )

    assert codigo == 1
    assert escrituras == []
    assert "029" in capsys.readouterr().out


def test_un_evento_que_no_existe_aborta(ejecutar):
    codigo, _base, _youtube, escrituras = ejecutar(
        "--evento", "999999", "--careo", CAREO, "--aplicar", base=BaseFalsa(None)
    )

    assert codigo == 1
    assert escrituras == []


def test_el_informe_ensena_el_antes_y_el_despues(ejecutar, capsys):
    ejecutar("--evento", str(EVENTO), "--pesaje", PESAJE)

    salida = capsys.readouterr().out
    assert "antes" in salida and "despues" in salida
    assert "weighin_video_id" in salida and "weighin_video_title" in salida
    assert PESAJE_TITULO in salida


# --- 8. Importar el modulo no puede hacer trabajo ------------------------
def test_importar_el_modulo_no_importa_psycopg2_ni_requests():
    """La red basta de `test_scripts_importables`, afinada a este script.

    `requests` y `psycopg2` se importan DENTRO de las funciones a proposito.
    Subirlos al nivel de modulo no rompe este script, pero es la primera mitad
    del fallo que ese test persigue: trabajo al importar. Esto lo comprueba en
    la fuente, que es donde se ve la intencion.
    """
    fuente = Path(fijar.__file__).read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    modulos: list[str] = []
    for nodo in arbol.body:
        if isinstance(nodo, ast.Import):
            modulos += [alias.name for alias in nodo.names]
        elif isinstance(nodo, ast.ImportFrom) and nodo.module:
            modulos.append(nodo.module)

    assert not {"requests", "psycopg2"} & set(modulos), modulos


# --- 9. Titulos con emoji: el fallo que se comio el informe entero --------
def test_la_salida_esta_reconfigurada_a_utf8():
    """Sin esto el script MUERE antes de ensenar nada, y pasa de verdad.

    Todo lo que imprime este script es un titulo de YouTube, y los del canal de
    la UFC llevan emoji a diario ("Rosas Jr and Font Face-off! 💥 #ufc326"). En
    Windows stdout sale en cp1252, asi que ese `print` levanta un
    UnicodeEncodeError en la linea "titulo ->": reproducido el 18-sep-2026
    contra el script real, que se caia ANTES de imprimir el informe y antes de
    tocar la base. No se pierde ninguna escritura, pero el operador se queda sin
    la unica pantalla que le deja comprobar el video antes de escribirlo.

    Se comprueba en la fuente, como el test de arriba, porque bajo pytest
    `sys.stdout` ya no es el de la consola y el sintoma no se puede reproducir
    dentro de la bateria. Los otros cuatro scripts de operador del repo llevan
    esta misma guarda (event_readiness, live_watch, post_event_review,
    repair_incomplete_round_stats).
    """
    fuente = Path(fijar.__file__).read_text(encoding="utf-8")

    assert 'sys.stdout.reconfigure(encoding="utf-8"' in fuente
    assert 'errors="replace"' in fuente, (
        "sin errors='replace' un caracter raro vuelve a tumbar el informe"
    )


def test_un_titulo_con_emoji_no_rompe_el_informe(ejecutar, capsys):
    """La otra mitad: que el titulo llegue entero al informe y al plan.

    Aqui no se prueba la codificacion (pytest captura en utf-8 y el fallo no
    aparece): se prueba que nada por el camino recorta, escapa o revienta con un
    titulo fuera de ASCII, que es lo que YouTube devuelve la mitad de las veces.
    """
    titulo = "UFC 331: Careos 💥 con acentuación"
    TITULOS[SHORT] = titulo
    try:
        ejecutar("--evento", str(EVENTO), "--pesaje", SHORT)
        salida = capsys.readouterr().out
    finally:
        TITULOS[SHORT] = SHORT_TITULO

    assert titulo in salida


# --- 10. El directo: fijarlo, quitarlo y apagarlo ------------------------
FILA_CON_DIRECTO_FIJADO = dict(
    FILA_1090_TRAS_EL_ARREGLO_A_MANO,
    live_video_id=DIRECTO,
    live_video_title=DIRECTO_TITULO,
)

URL_WATCH = "https://www.youtube.com/watch?v=qM-h-OudTqM"
URL_CORTA = "https://youtu.be/qM-h-OudTqM?si=Xy12_abcDEF"  # boton Compartir
URL_LIVE = "https://www.youtube.com/live/qM-h-OudTqM?si=Xy12_abcDEF"


def columnas_del_update(sql: str) -> set[str]:
    """Las columnas del SET de un UPDATE: para ver que viajan en la MISMA sentencia."""
    return {
        trozo.split("=")[0].strip()
        for trozo in sql.split("SET", 1)[1].split("WHERE")[0].split(",")
    }


def test_directo_escribe_id_y_titulo_a_la_vez(ejecutar):
    codigo, base, _youtube, escrituras = ejecutar(
        "--evento", str(EVENTO), "--directo", DIRECTO, "--aplicar"
    )

    assert codigo == 0
    assert len(escrituras) == 1, "id y titulo en UN solo UPDATE, no en dos"
    sql, params = base.updates[0]
    assert columnas_del_update(sql) == {"live_video_id", "live_video_title"}, (
        "el directo no puede tocar careo ni pesaje"
    )
    assert params[-1] == EVENTO
    assert base.fila["live_video_id"] == DIRECTO
    assert base.fila["live_video_title"] == DIRECTO_TITULO, "el titulo sale del oembed"


def test_directo_con_lengthseconds_0_no_avisa_de_short(ejecutar, capsys):
    """Un directo en emision da 0 s: avisar ahi seria avisar SIEMPRE."""
    info = fijar.resolve_video(DIRECTO, fetch=YouTubeFalso())
    assert info.length_seconds == 0 and info.looks_like_a_short, (
        "el 0 tiene que llegar: lo que calla el aviso es el acto, no un dato perdido"
    )

    codigo, _base, _youtube, _escrituras = ejecutar(
        "--evento", str(EVENTO), "--directo", DIRECTO
    )

    salida = capsys.readouterr().out
    assert codigo == 0
    assert "SHORT" not in salida.upper()
    assert "AVISO GORDO" not in salida


def test_los_mismos_0_segundos_en_un_careo_si_avisan(ejecutar, capsys):
    """La otra mitad: el silencio es SOLO del directo, el aviso sigue vivo."""
    ejecutar("--evento", str(EVENTO), "--careo", DIRECTO)

    assert "SHORT" in capsys.readouterr().out.upper()


def test_quitar_directo_pone_los_dos_a_null(ejecutar):
    codigo, base, youtube, escrituras = ejecutar(
        "--evento",
        str(EVENTO),
        "--quitar-directo",
        "--aplicar",
        base=BaseFalsa(FILA_CON_DIRECTO_FIJADO),
    )

    assert codigo == 0
    assert len(escrituras) == 1
    assert columnas_del_update(base.updates[0][0]) == {
        "live_video_id",
        "live_video_title",
    }
    assert base.fila["live_video_id"] is None
    assert base.fila["live_video_title"] is None
    assert youtube.pedidas == [], "quitar no es un video: no hay nada que preguntar"


def test_ocultar_directo_escribe_off_y_null(ejecutar):
    """El interruptor: 'off' en el id apaga lo automatico de la web sin desplegar."""
    codigo, base, youtube, escrituras = ejecutar(
        "--evento",
        str(EVENTO),
        "--ocultar-directo",
        "--aplicar",
        base=BaseFalsa(FILA_CON_DIRECTO_FIJADO),
    )

    assert fijar.LIVE_VIDEO_OFF == "off", "es el literal que compara la web"
    assert codigo == 0
    assert len(escrituras) == 1
    assert columnas_del_update(base.updates[0][0]) == {
        "live_video_id",
        "live_video_title",
    }
    assert base.fila["live_video_id"] == "off"
    assert base.fila["live_video_title"] is None, "el titulo viejo no se queda"
    assert youtube.pedidas == []


def test_ocultar_directo_sin_aplicar_no_escribe_nada(ejecutar, capsys):
    codigo, base, _youtube, escrituras = ejecutar(
        "--evento",
        str(EVENTO),
        "--ocultar-directo",
        base=BaseFalsa(FILA_CON_DIRECTO_FIJADO),
    )

    assert codigo == 0
    assert escrituras == []
    assert base.fila["live_video_id"] == DIRECTO
    assert "SIMULACION" in capsys.readouterr().out


@pytest.mark.parametrize(
    "palanca",
    [("--directo", DIRECTO), ("--quitar-directo",), ("--ocultar-directo",)],
    ids=["directo", "quitar", "ocultar"],
)
def test_si_falta_la_migracion_027_aborta_diciendolo(ejecutar, capsys, palanca):
    """Aborta ANTES de leer el evento, y nombra solo la migracion que falta."""
    base = BaseFalsa(
        FILA_CON_DIRECTO_FIJADO, columnas_presentes=fijar.COLUMNS_FROM_MIGRATION_029
    )

    codigo, base, _youtube, escrituras = ejecutar(
        "--evento", str(EVENTO), *palanca, "--aplicar", base=base
    )

    salida = capsys.readouterr().out
    assert codigo == 1
    assert escrituras == []
    assert base.lecturas == 0, "no se lee una fila con columnas que no existen"
    assert "027_events_live_video.sql" in salida
    assert "029" not in salida, "la 029 esta aplicada: nombrarla despista"


@pytest.mark.parametrize(
    "palanca, fila_inicial",
    [
        (("--directo", DIRECTO), FILA_1090_TRAS_EL_ARREGLO_A_MANO),
        (("--directo", URL_LIVE), FILA_1090_TRAS_EL_ARREGLO_A_MANO),
        (("--quitar-directo",), FILA_CON_DIRECTO_FIJADO),
        (("--ocultar-directo",), FILA_CON_DIRECTO_FIJADO),
    ],
    ids=["directo", "directo-por-url", "quitar", "ocultar"],
)
def test_cada_palanca_del_directo_es_idempotente(ejecutar, palanca, fila_inicial):
    argv = ("--evento", str(EVENTO), *palanca, "--aplicar")
    _codigo, base, _youtube, primeras = ejecutar(*argv, base=BaseFalsa(fila_inicial))
    assert len(primeras) == 1, "si la primera no escribe, esto no prueba nada"

    codigo, _base, _youtube, segundas = ejecutar(*argv, base=base)

    assert codigo == 0
    assert segundas == [], f"la segunda pasada volvio a escribir: {segundas}"


@pytest.mark.parametrize(
    "pegado",
    [
        URL_WATCH,
        "https://www.youtube.com/watch?v=qM-h-OudTqM&t=120s&ab_channel=ufcespanol",
        "https://www.youtube.com/watch?app=desktop&v=qM-h-OudTqM",  # v no va primero
        "https://m.youtube.com/watch?v=qM-h-OudTqM",
        "youtube.com/watch?v=qM-h-OudTqM",  # sin esquema
        "https://youtu.be/qM-h-OudTqM",
        URL_CORTA,
        "https://www.youtube.com/live/qM-h-OudTqM",
        URL_LIVE,
        "  qM-h-OudTqM  ",  # el id pelado, con los espacios del copiar y pegar
    ],
)
def test_del_directo_se_saca_el_id_de_la_url(pegado):
    assert fijar.extract_video_id(pegado) == DIRECTO


@pytest.mark.parametrize(
    "pegado",
    [
        None,
        "",
        "off",
        # La pagina en vivo del CANAL no es UN video: el sabado que viene
        # apuntaria a otro. Por eso no se acepta.
        "https://www.youtube.com/@ufcespanol/live",
        "https://www.youtube.com/watch?v=qM-h-OudTq",  # id de 10
        "https://www.youtube.com/watch?list=PL1234567890",  # sin v
        "https://www.youtube.com/live/",
        "https://www.youtube.com/shorts/qM-h-OudTqM",  # un short nunca es el directo
        "https://vimeo.com/live/qM-h-OudTqM",  # otro sitio, aunque el id encaje
        "https://[youtube.com/watch?v=qM-h-OudTqM",  # host roto: urlsplit revienta
    ],
)
def test_lo_que_no_es_una_url_de_video_de_youtube_no_da_id(pegado):
    assert fijar.extract_video_id(pegado) is None


@pytest.mark.parametrize(
    "url", [URL_WATCH, URL_CORTA, URL_LIVE], ids=["watch", "youtu.be", "live"]
)
def test_directo_con_url_escribe_el_id_pelado(ejecutar, url):
    codigo, base, youtube, escrituras = ejecutar(
        "--evento", str(EVENTO), "--directo", url, "--aplicar"
    )

    assert codigo == 0
    assert len(escrituras) == 1
    assert base.fila["live_video_id"] == DIRECTO, "en la base va el id, nunca la URL"
    assert base.fila["live_video_title"] == DIRECTO_TITULO
    assert youtube.pedidas and all(DIRECTO in pedida for pedida in youtube.pedidas)


def test_directo_con_la_url_del_canal_aborta_sin_pedir_nada(ejecutar):
    codigo, base, youtube, escrituras = ejecutar(
        "--evento",
        str(EVENTO),
        "--directo",
        "https://www.youtube.com/@ufcespanol/live",
        "--aplicar",
    )

    assert codigo == 1
    assert escrituras == []
    assert youtube.pedidas == []
    assert base.lecturas == 0


def test_directo_off_no_cuela_y_senala_el_interruptor(ejecutar, capsys):
    """'off' solo lo escribe --ocultar-directo: por --directo no pasa la forma."""
    codigo, _base, youtube, escrituras = ejecutar(
        "--evento", str(EVENTO), "--directo", "off", "--aplicar"
    )

    assert codigo == 1
    assert escrituras == []
    assert youtube.pedidas == []
    assert "--ocultar-directo" in capsys.readouterr().out


def test_careo_y_pesaje_siguen_sin_aceptar_urls(ejecutar):
    """La URL solo se acepta en el directo; el careo sigue como estaba."""
    codigo, _base, youtube, escrituras = ejecutar(
        "--evento", str(EVENTO), "--careo", fijar.watch_url(CAREO), "--aplicar"
    )

    assert codigo == 1
    assert escrituras == []
    assert youtube.pedidas == []


@pytest.mark.parametrize(
    "juntas",
    [
        ["--directo", DIRECTO, "--quitar-directo"],
        ["--directo", DIRECTO, "--ocultar-directo"],
        ["--quitar-directo", "--ocultar-directo"],
    ],
)
def test_las_palancas_del_directo_no_se_combinan(juntas):
    with pytest.raises(SystemExit):
        fijar.build_parser().parse_args(["--evento", "1090", *juntas])


def test_por_defecto_el_directo_no_se_toca():
    args = fijar.build_parser().parse_args(["--evento", "1090", "--careo", CAREO])

    assert args.directo is None
    assert args.quitar_directo is False
    assert args.ocultar_directo is False


def test_el_help_explica_las_tres_palancas():
    ayuda = " ".join(fijar.build_parser().format_help().split())

    for palanca in ("--directo", "--quitar-directo", "--ocultar-directo"):
        assert palanca in ayuda
    assert "'off'" in ayuda, "el interruptor tiene que verse en el --help"
    assert "desplegar" in ayuda
    assert "youtu.be" in ayuda and "/live/" in ayuda


# --- 11. Un directo sin titulo no se escribe, ni con --forzar -------------
def test_directo_sin_titulo_no_se_escribe_ni_con_forzar(ejecutar, capsys):
    """Forzarlo era un --ocultar-directo disfrazado que ademas decia "ok".

    La web no pinta un directo sin titulo (EventLiveEmbed devuelve null), y
    como la columna manda, un id a mano sin titulo ademas apaga lo automatico:
    ni se detecta el directo ni sale UFC TV en la portada.
    """
    codigo, base, _youtube, escrituras = ejecutar(
        "--evento",
        str(EVENTO),
        "--directo",
        DIRECTO,
        "--aplicar",
        "--forzar",
        youtube=YouTubeFalso(sin_oembed={DIRECTO}),
    )

    salida = capsys.readouterr().out
    assert codigo == 1
    assert escrituras == []
    assert base.commits == 0
    assert base.fila["live_video_id"] is None
    assert "--forzar no vale" in salida
    assert "--ocultar-directo" in salida, "si lo que quiere es apagarlo, que lo diga"
    assert base.revalidaciones == [], "sin escribir no hay nada que refrescar"


def test_directo_sin_titulo_no_recomienda_forzar(ejecutar, capsys):
    """El ABORTA de careo y pesaje sugiere --forzar; para el directo seria mentir."""
    codigo, _base, _youtube, escrituras = ejecutar(
        "--evento",
        str(EVENTO),
        "--directo",
        DIRECTO,
        "--aplicar",
        youtube=YouTubeFalso(sin_oembed={DIRECTO}),
    )

    salida = capsys.readouterr().out
    assert codigo == 1
    assert escrituras == []
    assert "SI se puede escribir con --forzar" not in salida


def test_careo_sin_titulo_con_forzar_sigue_escribiendose(ejecutar):
    """La otra mitad: el careo SI se pinta sin titulo, alli --forzar vale."""
    codigo, base, _youtube, escrituras = ejecutar(
        "--evento",
        str(EVENTO),
        "--careo",
        SHORT,
        "--aplicar",
        "--forzar",
        youtube=YouTubeFalso(sin_oembed={SHORT}),
    )

    assert codigo == 0
    assert len(escrituras) == 1
    assert base.fila["faceoff_video_id"] == SHORT
    assert base.fila["faceoff_video_title"] is None


# --- 12. El commit: sin el, el UPDATE se pierde callado -------------------
@pytest.mark.parametrize(
    "palanca, fila_inicial",
    [
        (("--directo", DIRECTO), FILA_1090_TRAS_EL_ARREGLO_A_MANO),
        (("--quitar-directo",), FILA_CON_DIRECTO_FIJADO),
        (("--ocultar-directo",), FILA_CON_DIRECTO_FIJADO),
        (("--pesaje", PESAJE), FILA_1090_TRAS_EL_ARREGLO_A_MANO),
    ],
    ids=["directo", "quitar", "ocultar", "pesaje"],
)
def test_con_aplicar_hace_UN_commit_y_sin_aplicar_ninguno(
    ejecutar, palanca, fila_inicial
):
    """psycopg2 no hace autocommit: un UPDATE sin commit desaparece al cerrar.

    El dueno creeria que ha apagado el reproductor y seguiria encendido. El
    comentario del script lo explica; esto es la red.
    """
    _codigo, simulada, _youtube, _escrituras = ejecutar(
        "--evento", str(EVENTO), *palanca, base=BaseFalsa(fila_inicial)
    )
    assert simulada.commits == 0, "la simulacion no confirma nada"

    codigo, base, _youtube, escrituras = ejecutar(
        "--evento", str(EVENTO), *palanca, "--aplicar", base=BaseFalsa(fila_inicial)
    )
    assert codigo == 0
    assert len(escrituras) == 1
    assert base.commits == 1, "el UPDATE se escribio pero nadie lo confirmo"

    _codigo, base, _youtube, segundas = ejecutar(
        "--evento", str(EVENTO), *palanca, "--aplicar", base=base
    )
    assert segundas == []
    assert base.commits == 1, "la pasada idempotente no confirma nada nuevo"


# --- 13. La portada: sin desplegar no es al instante ----------------------
@pytest.mark.parametrize(
    "palanca, fila_inicial",
    [
        (("--directo", DIRECTO), FILA_1090_TRAS_EL_ARREGLO_A_MANO),
        (("--quitar-directo",), FILA_CON_DIRECTO_FIJADO),
        (("--ocultar-directo",), FILA_CON_DIRECTO_FIJADO),
    ],
    ids=["directo", "quitar", "ocultar"],
)
def test_tocar_el_directo_revalida_la_portada_si_hay_secreto(
    ejecutar, capsys, palanca, fila_inicial
):
    """La portada lee la columna de una cache de 30 min: sin esto, el interruptor
    'off' en plena velada dejaba el video malo media hora mas en la portada."""
    codigo, base, _youtube, _escrituras = ejecutar(
        "--evento",
        str(EVENTO),
        *palanca,
        "--aplicar",
        base=BaseFalsa(fila_inicial),
        secreto="s3cr3t",
    )

    assert codigo == 0
    assert base.revalidaciones == ["s3cr3t"]
    assert "revalidada" in capsys.readouterr().out


def test_sin_secreto_no_llama_y_avisa_del_retraso_con_el_curl(ejecutar, capsys):
    codigo, base, _youtube, _escrituras = ejecutar(
        "--evento",
        str(EVENTO),
        "--ocultar-directo",
        "--aplicar",
        base=BaseFalsa(FILA_CON_DIRECTO_FIJADO),
    )

    salida = capsys.readouterr().out
    assert codigo == 0
    assert base.revalidaciones == []
    assert "%d min" % fijar.PORTADA_CACHE_MINUTOS in salida
    assert "curl -X POST " + fijar.REVALIDATE_URL in salida


@pytest.mark.parametrize("http", [None, 401, 503], ids=["sin-respuesta", "401", "503"])
def test_si_la_revalidacion_falla_lo_dice(ejecutar, capsys, http):
    codigo, base, _youtube, _escrituras = ejecutar(
        "--evento",
        str(EVENTO),
        "--ocultar-directo",
        "--aplicar",
        base=BaseFalsa(FILA_CON_DIRECTO_FIJADO),
        secreto="s3cr3t",
        http_revalidate=http,
    )

    salida = capsys.readouterr().out
    assert codigo == 0, "lo escrito en la base sigue escrito: no es un fallo del script"
    assert base.revalidaciones == ["s3cr3t"]
    assert "AVISO" in salida and "revalidada" not in salida


def test_careo_simulacion_o_nada_que_cambiar_no_revalidan(ejecutar, capsys):
    """Revalidar tambien vacia las caches de UFC TV: solo cuando hace falta."""
    _codigo, pesaje, _youtube, _e = ejecutar(
        "--evento", str(EVENTO), "--pesaje", PESAJE, "--aplicar", secreto="s3cr3t"
    )
    _codigo, simulacion, _youtube, _e = ejecutar(
        "--evento",
        str(EVENTO),
        "--ocultar-directo",
        base=BaseFalsa(FILA_CON_DIRECTO_FIJADO),
        secreto="s3cr3t",
    )
    ya_apagado = BaseFalsa(
        dict(FILA_CON_DIRECTO_FIJADO, live_video_id="off", live_video_title=None)
    )
    _codigo, idempotente, _youtube, _e = ejecutar(
        "--evento",
        str(EVENTO),
        "--ocultar-directo",
        "--aplicar",
        base=ya_apagado,
        secreto="s3cr3t",
    )

    assert pesaje.revalidaciones == []
    assert simulacion.revalidaciones == []
    assert idempotente.revalidaciones == []
    assert "PORTADA" not in capsys.readouterr().out


def test_el_secreto_sale_del_entorno_o_del_env_y_si_no_hay_es_none(
    monkeypatch, tmp_path
):
    """Sin tocar el .env de verdad: RAIZ apunta a una carpeta temporal."""
    monkeypatch.setattr(fijar, "RAIZ", str(tmp_path))
    monkeypatch.delenv("REVALIDATE_SECRET", raising=False)
    assert fijar.revalidate_secret() is None, "sin .env ni entorno: None"

    (tmp_path / ".env").write_text(
        'DATABASE_URL=postgresql://x\nREVALIDATE_SECRET="del-env"\n', encoding="utf-8"
    )
    assert fijar.revalidate_secret() == "del-env"

    monkeypatch.setenv("REVALIDATE_SECRET", "del-entorno")
    assert fijar.revalidate_secret() == "del-entorno", "el entorno manda sobre el .env"


def test_el_help_avisa_de_la_espera_de_la_portada():
    ayuda = " ".join(fijar.build_parser().format_help().split())

    assert "REVALIDATE_SECRET" in ayuda
    assert "%d min" % fijar.PORTADA_CACHE_MINUTOS in ayuda

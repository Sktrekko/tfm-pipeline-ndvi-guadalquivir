"""Precipitacion y temperatura diarias de las estaciones de AEMET.

Este modulo es el equivalente de `catalog.py` para la otra fuente del proyecto.
Igual que aquel traduce el catalogo STAC a un modelo propio para que nadie mas
sepa como es un item de STAC, este traduce las respuestas de AEMET a filas
limpias para que nadie mas sepa que AEMET responde en dos pasos, con comas
decimales y en latin-1.

Por que existe esta fuente
--------------------------
El resto del pipeline demuestra que la vegetacion de la cuenca se aparto de lo
normal en la primavera de 2023. Eso solo, sin embargo, no dice por que. Un
descenso de NDVI podria venir de un cambio de cultivo, de un incendio o de un
error del propio pipeline. Cruzarlo con la lluvia que registro la agencia
meteorologica convierte una anomalia en un mecanismo: fallaron las lluvias de
otono, la vegetacion aguanto el invierno porque consume poco, y el dano estallo
en la primavera siguiente.

La forma de la API, y por que condiciona el diseno
--------------------------------------------------
AEMET no devuelve los datos en la respuesta. Devuelve un JSON pequeno con la
URL donde estan, y hay que ir a buscarlos ahi. Esa segunda URL es temporal, asi
que no sirve de nada guardarla: hay que consumirla en el momento.

El limite de fechas depende de a quien preguntes, y la diferencia es la decision
de diseno principal de este modulo. Medido el 27 de agosto de 2026 contra la API
real:

- Preguntando por **una estacion**, el tope es de **6 meses**. Cubrir 2018-2026
  para las 114 estaciones de la cuenca serian 18 ventanas por estacion, o sea
  **2.052 peticiones**.
- Preguntando por **todas las estaciones**, el tope baja a **15 dias**, pero
  hacen falta solo **211 peticiones** para el mismo periodo.

Se elige la segunda. La razon no es el volumen sino el numero de llamadas: AEMET
limita las peticiones seguidas y corta la conexion sin avisar cuando se le
aprieta, cosa que se comprobo por las malas durante estas mismas pruebas. Diez
veces menos llamadas es diez veces menos superficie para ese fallo.

El precio es traerse las 852 estaciones de Espana cuando solo interesan las 114
de la cuenca, o sea un 87% de descarga desaprovechada. Sale a cuenta: una
ventana de 15 dias pesa 6 MB y tarda 2,45 s, de modo que la serie entera son
unos 9 minutos y 1,3 GB. Frente a las quince horas que costo la carga de
imagenes, es un paseo. El filtro se aplica al llegar y al disco solo baja lo de
la cuenca.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta

from .config import AemetSettings, Settings, get_settings

logger = logging.getLogger(__name__)


class EmptyWindowError(Exception):
    """AEMET no tiene datos para ese periodo.

    No es un fallo. Pasa con los dias mas recientes, porque la validacion
    climatologica de AEMET va con semanas de retraso, y con estaciones que aun
    no existian. Se separa de los fallos de red a proposito: confundir las dos
    cosas es lo que convierte un corte de conexion en un hueco silencioso de
    quince dias en la serie. Es justo el error que este proyecto no puede
    permitirse, y que de hecho ocurrio una vez durante el desarrollo.
    """


#: Dias por peticion cuando se piden todas las estaciones. Lo impone AEMET.
WINDOW_DAYS = 15

#: Pausa antes de cada peticion. AEMET estrangula las llamadas seguidas, y su
#: forma de hacerlo es la peor posible para quien programa: no devuelve un error
#: legible ni un codigo 429, simplemente deja la conexion colgada hasta que vence
#: el tiempo de espera. Desde fuera se parece a un corte de red.
#:
#: La pausa va **antes** de la peticion y no despues, cosa que parece igual y no
#: lo es: la primera llamada de una ejecucion suele venir pegada al inventario de
#: estaciones, y sin pausa por delante esa segunda llamada es justo la que cae.
#:
#: El valor sale de medirlo el 27 de agosto de 2026, seis ventanas seguidas por
#: cada pausa: con 1,5 s salieron bien 2 de 6, con 4 s salieron 5 de 6. Se elige
#: 4 s porque volver a duplicarlo duplicaria tambien el tiempo total a cambio de
#: poco. **Ni con 4 s se llega al 100%**, y ese es el dato que importa: el
#: reintento de mas abajo no es un adorno defensivo, es la pieza que hace viable
#: la descarga.
REQUEST_PAUSE_S = 4.0

#: Reintentos ante un corte, con espera creciente.
MAX_RETRIES = 5

#: Tiempo de espera por peticion. Corto a proposito. Cuando AEMET estrangula no
#: responde tarde, no responde nunca, asi que esperar dos minutos no mejora las
#: probabilidades: solo encarece el fallo. Mas vale rendirse a los treinta
#: segundos y volver a intentarlo.
REQUEST_TIMEOUT_S = 30

#: Lluvia inapreciable. AEMET no escribe un numero sino la marca "Ip" cuando
#: llovio menos de 0,1 mm. Las dos traducciones faciles son malas: convertirlo a
#: dato ausente borra un dia en el que si llovio, y convertirlo a cero afirma lo
#: contrario de lo que midieron. Se traduce a la mitad del umbral, que es la
#: convencion habitual y ademas es el valor esperado si la lluvia se reparte
#: uniformemente por debajo de el. En una serie sobre sequia la diferencia entre
#: "no llovio" y "llovio una gota" no es un detalle cosmetico.
TRACE_PRECIPITATION_MM = 0.05

#: Valores no numericos que AEMET usa en los campos de medida y que no son
#: lluvia inapreciable. Se traducen a dato ausente, no a cero.
_MISSING_MARKS = frozenset({"", "-", "Acum", "Varias"})


@dataclass(frozen=True)
class Station:
    """Una estacion meteorologica de AEMET.

    Vista reducida del inventario, igual que `Scene` lo es del item STAC. Las
    coordenadas ya vienen convertidas a grados decimales, que es lo unico que
    necesita el resto del proyecto.
    """

    station_id: str
    name: str
    province: str
    longitude: float
    latitude: float
    altitude_m: float | None = None


@dataclass(frozen=True)
class DailyWeather:
    """Lo que midio una estacion un dia.

    Se guardan la lluvia y las tres temperaturas y se descarta el resto (viento,
    presion, insolacion, humedad), que vienen en la misma respuesta pero no
    entran en el analisis. La temperatura se conserva junto a la lluvia porque
    llega gratis en la misma peticion y porque el episodio de 2023 no se explica
    solo con la lluvia que falto: hubo ademas un abril excepcionalmente calido
    que remato el cereal de secano.
    """

    station_id: str
    observed_on: date
    precipitation_mm: float | None
    temp_mean_c: float | None
    temp_max_c: float | None
    temp_min_c: float | None


# ---------------------------------------------------------------------------
# Traduccion de los campos
# ---------------------------------------------------------------------------


def parse_decimal(raw: str | None) -> float | None:
    """Convierte un numero de AEMET, que viene como texto y con coma decimal.

    Devuelve `None` para las marcas de dato ausente y para cualquier texto que
    no sepa leer, dejando aviso en el log. Se prefiere avisar antes que callar:
    una marca nueva que apareciese en el futuro se convertiria en silencio en un
    hueco de la serie, y los huecos silenciosos son justo lo que este proyecto
    se ha propuesto no tener.
    """
    if raw is None:
        return None
    text = raw.strip()
    if text in _MISSING_MARKS:
        return None
    try:
        return float(text.replace(",", "."))
    except ValueError:
        logger.warning("Valor no numerico de AEMET sin traduccion: %r", text)
        return None


def parse_precipitation(raw: str | None) -> float | None:
    """Igual que `parse_decimal`, pero entendiendo la lluvia inapreciable."""
    if raw is not None and raw.strip() == "Ip":
        return TRACE_PRECIPITATION_MM
    return parse_decimal(raw)


def parse_coordinate(raw: str) -> float:
    """Convierte las coordenadas del inventario a grados decimales.

    AEMET las publica en grados, minutos y segundos pegados y con la letra del
    hemisferio al final: `370924N` son 37 grados, 9 minutos y 24 segundos norte.
    """
    sign = -1.0 if raw[-1] in "WS" else 1.0
    degrees, minutes, seconds = int(raw[:2]), int(raw[2:4]), int(raw[4:6])
    return sign * (degrees + minutes / 60 + seconds / 3600)


# ---------------------------------------------------------------------------
# Acceso a la API
# ---------------------------------------------------------------------------


def _request(
    path: str, settings: AemetSettings, *, timeout: int = REQUEST_TIMEOUT_S,
) -> list[dict]:
    """Resuelve una peticion de AEMET, con sus dos pasos.

    La primera llamada devuelve un sobre con el estado y la URL donde estan los
    datos; la segunda los trae. Se separa aqui para que el resto del modulo no
    tenga que saberlo.

    El contenido viene en latin-1 y no en UTF-8, cosa que solo se nota en los
    nombres de estacion con tilde. Decodificarlo mal no rompe nada visible, deja
    "CÓRDOBA" escrito de forma rara en una tabla que despues se cruza por nombre.
    """
    if not settings.configured:
        raise RuntimeError(
            "Falta AEMET_API_KEY. Se pide gratis en "
            "https://opendata.aemet.es/centrodedescargas/altaUsuario y va en .env"
        )

    request = urllib.request.Request(
        f"{settings.api_url}{path}", headers={"api_key": settings.api_key}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        envelope = json.loads(response.read())

    estado, descripcion = envelope.get("estado"), envelope.get("descripcion") or ""
    if estado == 404:
        # AEMET usa el mismo codigo para "no hay nada que darte" y para "me has
        # pedido mal el rango". Solo el primero es aceptable, y se distingue por
        # el texto porque la API no ofrece otra cosa.
        if "no hay datos" in descripcion.lower():
            raise EmptyWindowError(descripcion)
        raise RuntimeError(f"AEMET rechazo la peticion: {descripcion}")
    if estado != 200:
        raise RuntimeError(f"AEMET respondio {estado}: {descripcion}")

    with urllib.request.urlopen(envelope["datos"], timeout=timeout) as response:
        return json.loads(response.read().decode("latin-1"))


def _fetch_window(
    path: str,
    settings: AemetSettings,
    *,
    pause_s: float = REQUEST_PAUSE_S,
    retries: int = MAX_RETRIES,
) -> list[dict]:
    """Pide una ventana, esperando antes y reintentando si la conexion se corta.

    La distincion que hace esta funcion es la importante del modulo. Un
    `EmptyWindowError` sube tal cual, porque significa que AEMET no tiene nada y no
    hay nada que reintentar. Un corte se reintenta con espera creciente, y si
    se agotan los intentos **falla**, no se traga.

    Esa ultima parte es deliberada y corrige un error de la primera version, que
    apuntaba el corte en el log y seguia. Con doscientas once ventanas, un corte
    tragado son quince dias de lluvia que desaparecen de la serie sin que nadie
    se entere, y el analisis de sequia se hace despues sobre esos huecos. Mas
    vale que la descarga entera se pare y haya que relanzarla.
    """
    for attempt in range(1, retries + 1):
        time.sleep(pause_s)
        try:
            return _request(path, settings)
        except EmptyWindowError:
            raise
        except (TimeoutError, OSError, urllib.error.URLError) as exc:
            if attempt == retries:
                raise RuntimeError(
                    f"AEMET no responde tras {retries} intentos. Suele ser su "
                    f"limite de peticiones: subir REQUEST_PAUSE_S y relanzar. "
                    f"Ultimo fallo: {exc}"
                ) from exc
            espera = pause_s * 2 ** attempt
            logger.warning(
                "Corte en el intento %d de %d, esperando %.0f s: %s",
                attempt, retries, espera, exc,
            )
            time.sleep(espera)
    raise AssertionError("inalcanzable")


def list_stations(settings: Settings | None = None) -> list[Station]:
    """Inventario completo de estaciones climatologicas de AEMET."""
    settings = settings or get_settings()
    raw = _request(
        "/api/valores/climatologicos/inventarioestaciones/todasestaciones",
        settings.aemet,
    )
    stations = [
        Station(
            station_id=entry["indicativo"],
            name=entry["nombre"],
            province=entry["provincia"],
            longitude=parse_coordinate(entry["longitud"]),
            latitude=parse_coordinate(entry["latitud"]),
            altitude_m=parse_decimal(entry.get("altitud")),
        )
        for entry in raw
    ]
    logger.info("Inventario de AEMET: %d estaciones", len(stations))
    return stations


def windows(start: date, end: date, *, days: int = WINDOW_DAYS) -> Iterator[tuple[date, date]]:
    """Trocea un intervalo en ventanas del tamano que admite la API.

    Se saca a funcion propia para poder probarla sin red: es donde vive el
    unico error de aritmetica de fechas posible, el de dejar un dia fuera entre
    dos ventanas consecutivas.
    """
    cursor = start
    while cursor <= end:
        last = min(cursor + timedelta(days=days - 1), end)
        yield cursor, last
        cursor = last + timedelta(days=1)


def daily_weather(
    start: date,
    end: date,
    *,
    settings: Settings | None = None,
    station_ids: set[str] | None = None,
    pause_s: float = REQUEST_PAUSE_S,
) -> Iterator[DailyWeather]:
    """Recorre el intervalo pidiendo ventana a ventana y va soltando filas.

    Devuelve un iterador y no una lista porque la serie completa son mas de un
    millon de registros antes de filtrar. Quien llama decide si los acumula o
    los va escribiendo por lotes, igual que hace el pipeline de imagenes.

    Args:
        start: primer dia, inclusivo.
        end: ultimo dia, inclusivo.
        settings: configuracion del proyecto.
        station_ids: si se indica, solo se emiten esas estaciones. El filtro se
            aplica aqui, nada mas llegar, porque de las 852 que responden solo
            interesan las 114 de la cuenca y no tiene sentido arrastrar el resto.
        pause_s: espera entre peticiones. Ver `REQUEST_PAUSE_S`.
    """
    settings = settings or get_settings()
    total = 0

    for index, (first, last) in enumerate(windows(start, end), start=1):
        path = (
            f"/api/valores/climatologicos/diarios/datos"
            f"/fechaini/{first.isoformat()}T00:00:00UTC"
            f"/fechafin/{last.isoformat()}T23:59:59UTC"
            f"/todasestaciones"
        )
        try:
            raw = _fetch_window(path, settings.aemet, pause_s=pause_s)
        except EmptyWindowError:
            logger.info("[ventana %d] %s..%s  sin datos en AEMET", index, first, last)
            continue

        emitted = 0
        for entry in raw:
            if station_ids is not None and entry["indicativo"] not in station_ids:
                continue
            yield DailyWeather(
                station_id=entry["indicativo"],
                observed_on=date.fromisoformat(entry["fecha"]),
                precipitation_mm=parse_precipitation(entry.get("prec")),
                temp_mean_c=parse_decimal(entry.get("tmed")),
                temp_max_c=parse_decimal(entry.get("tmax")),
                temp_min_c=parse_decimal(entry.get("tmin")),
            )
            emitted += 1

        total += emitted
        logger.info(
            "[ventana %d] %s..%s  %d registros de %d  (acumulado %d)",
            index, first, last, emitted, len(raw), total,
        )

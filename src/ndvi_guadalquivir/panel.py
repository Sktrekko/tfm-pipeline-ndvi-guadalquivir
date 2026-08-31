"""Lo que el panel necesita saber, separado de como lo pinta.

Este modulo no importa Streamlit ni Folium a proposito. Todo lo que hay aqui son
consultas y transformaciones que devuelven DataFrames, de modo que se pueden
probar sin levantar una interfaz y sin un navegador. La parte visual vive en
`app.py`, en la raiz, y lo unico que hace es llamar a estas funciones y dibujar
lo que devuelven.

La separacion no es purismo. Un panel es la pieza mas facil de romper sin
enterarse, porque nadie escribe tests contra una pagina web, y si la logica vive
mezclada con los widgets no hay forma de comprobarla. Sacandola aqui, el
proyecto puede seguir teniendo el mismo tipo de garantia que tiene el resto.

De donde lee el panel, y por que de ahi
---------------------------------------
De dos sitios, y ninguno de los dos es el almacen Iceberg.

Los **numeros** salen de `data/warehouse.duckdb`, que es el fichero donde dbt
materializa la capa gold. La razon es que el panel hace siempre las mismas
preguntas (que anomalia tuvo cada municipio en tal semana) y esas respuestas ya
estan calculadas ahi. Ir a Iceberg significaria recalcular ocho anos de
climatologia en cada clic para llegar al mismo numero.

La **geometria** sale del GeoPackage que produce `zones.py`. Podria salir
tambien de la tabla `bronze.zones`, pero entonces el panel necesitaria el
catalogo y MinIO encendidos solo para dibujar un mapa. Leyendo del fichero, el
panel arranca con Docker apagado, cosa que importa para grabar el video de la
defensa y para que cualquiera pueda abrirlo sin montar la infraestructura
entera.

El precio es que el panel muestra lo que habia la ultima vez que corrio
`dbt build`, no lo que hay en Iceberg ahora mismo. Para este proyecto es lo
correcto: la serie tiene ocho anos y crece un dia cada cinco.

Por que la geometria se simplifica antes de dibujarla
-----------------------------------------------------
Los limites del IGN se eligieron por precision (431 vertices de mediana, ver la
tabla de decisiones) porque el borde decide que pixeles entran en la media de
cada municipio. Para dibujar no hace falta ni de lejos tanto: los 445 municipios
en crudo son 9,8 MB de GeoJSON, y eso metido en una pagina web la ahoga.

Medido sobre la capa real, simplificando con distintas tolerancias:

===========  ==========  ============  ============
Tolerancia   GeoJSON     Del original  Error de area
===========  ==========  ============  ============
nativo       9,83 MB     100%          -
20 m         3,09 MB     31,4%         -0,00%
50 m         1,83 MB     18,6%          0,00%
100 m        1,17 MB     11,9%         -0,00%
200 m        0,72 MB      7,3%         -0,03%
500 m        0,38 MB      3,9%         -0,03%
===========  ==========  ============  ============

Se eligen 100 m, que dejan el fichero en un octavo sin mover el area ni una
centesima de punto porcentual. El numero no es redondo por casualidad: es la
resolucion objetivo del proyecto. Dibujar el borde mas fino que el pixel con el
que se calculo el valor que ese borde colorea no anade informacion, solo bytes.

Lo que si cambia al simplificar es que dos municipios vecinos pueden dejar de
casar exactamente y aparece una rendija de un pixel entre ellos. Es un defecto
cosmetico del mapa y no toca ningun numero, porque las medias ya estaban
calculadas con la geometria buena mucho antes de llegar aqui.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb
import geopandas as gpd
import pandas as pd

logger = logging.getLogger(__name__)

#: Fichero DuckDB donde dbt deja la capa gold.
DEFAULT_WAREHOUSE = Path("data/warehouse.duckdb")

#: Capa de municipios que produce `zones.build_zone_layer`.
DEFAULT_ZONES = Path("data/zones/zones_municipios.gpkg")

#: Donde se guarda la version simplificada, para no recalcularla en cada arranque.
DEFAULT_GEOMETRY_CACHE = Path("data/zones/zones_panel.geojson")

#: Tolerancia de simplificacion, en metros. Ver el docstring del modulo.
SIMPLIFY_TOLERANCE_M = 100

#: CRS metrico de la peninsula. La tolerancia de simplificacion se expresa en
#: metros, asi que hay que salir de grados antes de aplicarla: en latitud 37 un
#: grado son unos 89 km, y una tolerancia de 100 en grados borraria la provincia.
METRIC_CRS = 25830

#: Esquemas que dbt genera. No se llaman `gold` y `silver` sino con el esquema
#: base por delante; es la trampa 15 del proyecto y aqui se escribe una sola vez.
GOLD = "main_gold"
SILVER = "main_silver"


def open_warehouse(path: Path | str = DEFAULT_WAREHOUSE) -> duckdb.DuckDBPyConnection:
    """Abre el fichero de dbt en solo lectura.

    En solo lectura por dos motivos. Uno, el panel no tiene nada que escribir.
    Y dos, DuckDB permite varios lectores a la vez pero un solo escritor: si el
    panel abriera en modo escritura, lanzar `dbt build` con el panel encendido
    fallaria con un error de fichero bloqueado que no sugiere en absoluto que la
    culpa sea de tener una pestana abierta.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"No existe {path}. La capa gold la construye dbt: "
            "cd dbt && DBT_PROFILES_DIR=. uv run dbt build"
        )
    return duckdb.connect(str(path), read_only=True)


def load_map_geometry(
    zones_path: Path | str = DEFAULT_ZONES,
    *,
    cache_path: Path | str | None = DEFAULT_GEOMETRY_CACHE,
    tolerance_m: int = SIMPLIFY_TOLERANCE_M,
) -> gpd.GeoDataFrame:
    """Devuelve los municipios con la geometria ya aligerada para el navegador.

    Guarda el resultado en disco la primera vez. Simplificar 445 poligonos de
    431 vertices cuesta un par de segundos, que no es mucho, pero es un par de
    segundos en cada arranque del panel y a cambio de nada: la capa de zonas no
    cambia salvo que se reconstruya a proposito.

    Args:
        zones_path: GeoPackage de municipios que produce `zones.py`.
        cache_path: donde dejar la version simplificada. `None` la recalcula
            siempre, que es lo que quieren los tests.
        tolerance_m: tolerancia de Douglas-Peucker en metros.

    Returns:
        GeoDataFrame en EPSG:4326 con `zone_id`, `zone_name`, `province_code`,
        `area_km2`, `basin_overlap_fraction` y la geometria simplificada.
    """
    if cache_path is not None:
        cache_path = Path(cache_path)
        if cache_path.exists():
            logger.debug("Geometria del panel leida de %s", cache_path)
            return gpd.read_file(cache_path)

    zones = gpd.read_file(zones_path)
    columnas = [
        c for c in ("zone_id", "zone_name", "province_code", "area_km2", "basin_overlap_fraction")
        if c in zones
    ]

    simplificada = zones.to_crs(METRIC_CRS).simplify(tolerance_m).to_crs(4326)
    resultado = gpd.GeoDataFrame(
        zones[columnas].copy(), geometry=simplificada.to_numpy(), crs=4326
    )

    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        resultado.to_file(cache_path, driver="GeoJSON")
        logger.info("Geometria del panel simplificada a %s m y guardada en %s",
                    tolerance_m, cache_path)
    return resultado


def available_weeks(connection: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Semanas que tienen dato, de la mas reciente a la mas antigua.

    Alimenta el deslizador temporal. Se devuelve tambien cuantos municipios
    cubre cada semana, porque no todas cubren la cuenca entera: en invierno las
    nubes dejan semanas con menos de la mitad, y quien mire el mapa tiene
    derecho a saber si esta viendo la cuenca o un trozo.
    """
    return connection.execute(f"""
        SELECT anio,
               semana,
               count(*)              AS municipios,
               avg(anomalia_ndvi)    AS anomalia_media,
               min(primera_fecha)    AS desde,
               max(ultima_fecha)     AS hasta
        FROM {GOLD}.ndvi_anomaly
        GROUP BY anio, semana
        ORDER BY anio DESC, semana DESC
    """).df()


def anomaly_snapshot(
    connection: duckdb.DuckDBPyConnection, anio: int, semana: int
) -> pd.DataFrame:
    """Lo que le paso a cada municipio en una semana concreta.

    Es lo que pinta el mapa. Se pasan ano y semana como parametros y no
    interpolados en el texto de la consulta: un panel es una entrada de usuario
    como cualquier otra, y aunque aqui los valores vengan de un desplegable, la
    costumbre de no construir SQL pegando cadenas es de las que conviene no
    perder nunca.
    """
    return connection.execute(f"""
        SELECT zone_id,
               zone_name,
               ndvi_observado,
               ndvi_normal,
               anomalia_ndvi,
               anomalia_sigmas,
               categoria,
               normal_fiable,
               observaciones,
               fraccion_util_media
        FROM {GOLD}.ndvi_anomaly
        WHERE anio = ? AND semana = ?
        ORDER BY anomalia_sigmas
    """, [anio, semana]).df()


def municipality_series(
    connection: duckdb.DuckDBPyConnection, zone_id: str
) -> pd.DataFrame:
    """Serie semanal completa de un municipio, con su normal al lado.

    Se devuelve la normal junto al valor observado porque la grafica interesante
    es la de los dos juntos. Una linea de NDVI sola sube en primavera y baja en
    verano todos los anos, y eso no informa de nada. Contra su propia normal, en
    cambio, se ve de un vistazo que anos se salieron.
    """
    return connection.execute(f"""
        SELECT anio,
               semana,
               make_date(anio, 1, 1) + to_days(((semana - 1) * 7)::INTEGER) AS fecha,
               ndvi_observado,
               ndvi_normal,
               anomalia_ndvi,
               anomalia_sigmas,
               categoria,
               observaciones
        FROM {GOLD}.ndvi_anomaly
        WHERE zone_id = ?
        ORDER BY anio, semana
    """, [zone_id]).df()


def municipality_options(
    connection: duckdb.DuckDBPyConnection, zones: gpd.GeoDataFrame | pd.DataFrame
) -> pd.DataFrame:
    """Municipios con dato, con su provincia, para el selector.

    Montar esto cuesta un rodeo que conviene explicar, porque lo evidente no
    funciona. Lo evidente seria preguntarle a `stg_zones`, que tiene el nombre
    de la provincia ya resuelto. Pero las vistas silver de dbt no son tablas
    sino consultas que apuntan a Iceberg, asi que preguntarles obliga a tener el
    catalogo y MinIO encendidos, y eso rompe la promesa de arrancar sin Docker.

    Lo segundo evidente seria copiar aqui la lista de provincias. Tampoco: ya
    esta escrita en `stg_zones.sql` y dos copias de la misma tabla acaban
    separandose el dia que alguien toque una.

    El rodeo es juntar dos piezas que si estan a mano. La capa de zonas trae el
    codigo de provincia de cada municipio, y `ndvi_monthly_by_province`, que es
    una tabla gold de verdad y por tanto vive dentro del fichero DuckDB, trae la
    correspondencia entre codigo y nombre. Uniendolas sale lo mismo sin duplicar
    nada y sin encender un contenedor.
    """
    con_dato = connection.execute(f"""
        SELECT zone_id,
               any_value(zone_name) AS zone_name,
               count(*)             AS semanas_con_dato
        FROM {GOLD}.ndvi_anomaly
        GROUP BY zone_id
    """).df()

    nombres = connection.execute(f"""
        SELECT DISTINCT province_code, province_name
        FROM {GOLD}.ndvi_monthly_by_province
    """).df()

    columnas = [c for c in ("zone_id", "province_code", "area_km2") if c in zones]
    opciones = con_dato.merge(pd.DataFrame(zones[columnas]), on="zone_id", how="left")

    if "province_code" in opciones:
        opciones = opciones.merge(nombres, on="province_code", how="left")
        opciones["province_name"] = opciones["province_name"].fillna("Desconocida")
        return opciones.sort_values(["province_name", "zone_name"], ignore_index=True)
    return opciones.sort_values("zone_name", ignore_index=True)


def basin_monthly(connection: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """La serie del cruce: vegetacion y lluvia de la cuenca, mes a mes.

    Sale tal cual del modelo `ndvi_weather_monthly`, sin tocar nada. El panel no
    recalcula: si un numero de la pantalla no cuadra con la memoria, el sitio
    donde mirar es el SQL de dbt y no este fichero.
    """
    return connection.execute(f"""
        SELECT mes_fecha,
               anio,
               mes,
               anomalia_ndvi,
               fraccion_municipios_por_debajo,
               lluvia_mm,
               lluvia_normal_mm,
               anomalia_lluvia_mm,
               ratio_lluvia,
               anomalia_lluvia_3m_mm,
               anomalia_lluvia_6m_mm,
               municipios,
               estaciones
        FROM {GOLD}.ndvi_weather_monthly
        ORDER BY mes_fecha
    """).df()


def headline_numbers(connection: duckdb.DuckDBPyConnection) -> dict[str, object]:
    """Las cuatro cifras que resumen el almacen, para la cabecera del panel.

    Existen para que quien abra el panel sepa en un segundo sobre cuanto dato
    esta mirando. Un mapa bonito sin saber si detras hay una semana o nueve anos
    no dice nada.
    """
    filas, municipios, desde, hasta = connection.execute(f"""
        SELECT count(*), count(DISTINCT zone_id), min(primera_fecha), max(ultima_fecha)
        FROM {GOLD}.ndvi_anomaly
    """).fetchone()
    peor = connection.execute(f"""
        SELECT zone_name, anio, semana, anomalia_sigmas
        FROM {GOLD}.ndvi_anomaly
        WHERE anomalia_sigmas IS NOT NULL
        ORDER BY anomalia_sigmas
        LIMIT 1
    """).fetchone()
    return {
        "observaciones": filas,
        "municipios": municipios,
        "desde": desde,
        "hasta": hasta,
        "peor_semana": peor,
    }


#: Cortes y colores del mapa. Son los mismos de la columna `categoria` de
#: `ndvi_anomaly`, y estan aqui repetidos a proposito para que el panel no
#: invente una escala propia: si manana se cambian los cortes en dbt, el mapa
#: tiene que cambiar con ellos y esta lista es donde se ve que hay que tocar.
#:
#: La rampa va de marron a verde y no de rojo a azul. En un mapa de vegetacion
#: el verde ya significa algo para quien lo mira, y pelearse con esa intuicion
#: para ganar contraste es perder mas de lo que se gana.
CATEGORY_COLORS: dict[str, str] = {
    "muy por debajo": "#8C510A",
    "por debajo":     "#D8B365",
    "normal":         "#F5F5F5",
    "por encima":     "#7FBC41",
    "muy por encima": "#276419",
    "sin referencia": "#BDBDBD",
}

#: Orden en que se listan las categorias en la leyenda. De peor a mejor, que es
#: como se lee un mapa de dano.
CATEGORY_ORDER: list[str] = [
    "muy por debajo",
    "por debajo",
    "normal",
    "por encima",
    "muy por encima",
    "sin referencia",
]


def category_color(categoria: object) -> str:
    """Color de una categoria, con gris para lo que no reconozca.

    Devolver gris en vez de reventar es deliberado: si algun dia dbt anade una
    categoria nueva, el mapa la pinta de gris y sigue funcionando, en lugar de
    dejar la pagina en blanco con un KeyError.
    """
    return CATEGORY_COLORS.get(str(categoria), CATEGORY_COLORS["sin referencia"])

"""Escritura de la capa bronze en el lakehouse.

El pipeline produce un DataFrame por fecha. Este modulo lo persiste como tabla
Apache Iceberg cuyos ficheros viven en MinIO, que habla el protocolo S3.

Por que Iceberg y no dejar los Parquet sueltos en una carpeta, que es la
pregunta natural y la que hay que saber responder en la defensa:

**La escritura es atomica.** Guardar una fecha significa escribir uno o varios
ficheros y despues registrar en el catalogo que ahora forman parte de la tabla.
Hasta ese ultimo paso nadie los ve. Si el proceso muere a mitad, la tabla queda
como estaba en lugar de quedar con media fecha dentro. En una carga historica
de ocho anos que se interrumpe y se reanuda, esa garantia es la diferencia
entre poder confiar en el resultado y no poder.

**Cada version queda registrada.** Cada escritura crea una instantanea, y se
puede consultar la tabla tal y como estaba en cualquiera de ellas. Los numeros
que aparezcan en la memoria se pueden anclar a una instantanea concreta, de modo
que sigan siendo reproducibles aunque despues se recargue mas dato.

**El particionado es cosa de la tabla, no del que consulta.** La tabla se
particiona por mes, pero quien pregunta escribe un `WHERE` normal sobre la
fecha y el motor descarta solo los ficheros que no hacen falta. Con Parquet en
carpetas, esa estructura se filtra a todas las consultas y cambiarla obliga a
reescribir todo lo que lea de ahi.

**Portabilidad.** MinIO expone el mismo protocolo que Amazon S3. Para desplegar
en AWS basta con vaciar `S3_ENDPOINT_URL` y poner credenciales reales.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterable, Sequence
from datetime import date

import duckdb
import pandas as pd
import pyarrow as pa
from pyiceberg.catalog import Catalog
from pyiceberg.catalog.rest import RestCatalog
from pyiceberg.exceptions import NoSuchTableError
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.table import Table
from pyiceberg.transforms import MonthTransform, YearTransform
from pyiceberg.types import (
    DateType,
    DoubleType,
    IntegerType,
    LongType,
    NestedField,
    StringType,
    TimestamptzType,
)

from .config import Settings, get_settings
from .schemas import DailyZonalNdviSchema

logger = logging.getLogger(__name__)

#: Nombre de la tabla de la capa bronze.
DAILY_TABLE = "ndvi_zonal_daily"

#: Tabla de control: una fila por fecha intentada, con lo que paso.
LOG_TABLE = "ingestion_log"

#: Tabla de dimension: los municipios y sus atributos.
ZONES_TABLE = "zones"

#: Precipitacion y temperatura diarias de las estaciones de AEMET dentro de la
#: cuenca. Es la segunda fuente del proyecto y vive en la misma capa bronze que
#: el NDVI, sin mezclarse con el: son observaciones de cosas distintas, medidas
#: por instrumentos distintos y con grano espacial distinto (114 puntos frente a
#: 445 poligonos). Cruzarlas es trabajo de la capa gold, en SQL, donde la
#: decision de como se cruzan queda a la vista y se puede discutir.
WEATHER_TABLE = "weather_station_daily"

#: Marca de tiempo de la carga. No viene del satelite sino del proceso, y sirve
#: para saber que ejecucion escribio cada fila cuando algo no cuadre.
INGESTED_AT = "ingested_at"

#: Estados posibles de una fecha en el registro de ingesta.
STATUS_OK = "ok"          # se escribieron filas
STATUS_EMPTY = "empty"    # se proceso, pero ninguna zona alcanzo el minimo
STATUS_ERROR = "error"    # fallo la lectura o el calculo

#: Estados que cuentan como fecha ya resuelta y que no hay que reintentar.
SETTLED_STATUSES = frozenset({STATUS_OK, STATUS_EMPTY})


def daily_schema() -> Schema:
    """Esquema de la tabla diaria de NDVI por zona.

    Los identificadores de campo son permanentes: Iceberg sigue una columna por
    su numero y no por su nombre, de modo que renombrarla mas adelante no
    invalida los ficheros ya escritos. Por eso se declaran explicitos.
    """
    return Schema(
        NestedField(1, "zone_id", StringType(), required=True),
        NestedField(2, "zone_name", StringType(), required=True),
        NestedField(3, "acquisition_date", DateType(), required=True),
        NestedField(4, "tile_ids", StringType(), required=True),
        NestedField(5, "scene_ids", StringType(), required=True),
        NestedField(6, "scene_count", IntegerType(), required=True),
        NestedField(7, "mean_cloud_cover", DoubleType(), required=True),
        NestedField(8, "ndvi_mean", DoubleType(), required=True),
        NestedField(9, "ndvi_median", DoubleType(), required=True),
        NestedField(10, "ndvi_std", DoubleType(), required=True),
        NestedField(11, "ndvi_p10", DoubleType(), required=True),
        NestedField(12, "ndvi_p90", DoubleType(), required=True),
        NestedField(13, "pixel_count", LongType(), required=True),
        NestedField(14, "valid_pixel_count", LongType(), required=True),
        NestedField(15, "valid_pixel_fraction", DoubleType(), required=True),
        NestedField(16, INGESTED_AT, TimestamptzType(), required=True),
        identifier_field_ids=[1, 3],
    )


def daily_partition_spec() -> PartitionSpec:
    """Particionado por mes de adquisicion.

    El mes es el grano que iguala las dos formas de usar la tabla. Al escribir,
    cada fecha cae en una sola particion, asi que anadir un dia no reescribe
    nada de lo que ya hay. Al consultar, ocho anos se reparten en unas noventa
    y seis particiones de unas siete mil filas: suficientes para que filtrar por
    fecha descarte casi todo, y no tantas como para llenar el catalogo de
    ficheros diminutos.
    """
    return PartitionSpec(
        PartitionField(
            source_id=3, field_id=1000, transform=MonthTransform(),
            name="acquisition_month",
        )
    )


def get_catalog(settings: Settings | None = None) -> Catalog:
    """Abre la conexion con el catalogo de tablas.

    Las credenciales del almacen se le pasan al catalogo porque es el quien
    escribe y lee los ficheros de datos en MinIO.
    """
    settings = settings or get_settings()
    store, lake = settings.store, settings.lakehouse
    return RestCatalog(
        name=lake.catalog_name,
        uri=lake.catalog_uri,
        warehouse=store.warehouse_uri,
        **{
            "s3.endpoint": store.endpoint_url,
            "s3.access-key-id": store.access_key,
            "s3.secret-access-key": store.secret_key,
            "s3.region": store.region,
        },
    )


def ensure_daily_table(
    catalog: Catalog | None = None,
    *,
    settings: Settings | None = None,
) -> Table:
    """Devuelve la tabla diaria, creandola la primera vez.

    Es idempotente a proposito: tanto la carga historica como la ejecucion
    diaria empiezan llamando aqui, y ninguna de las dos deberia tener que saber
    si la tabla ya existia.
    """
    settings = settings or get_settings()
    catalog = catalog or get_catalog(settings)
    namespace = settings.lakehouse.bronze_namespace
    identifier = f"{namespace}.{DAILY_TABLE}"

    catalog.create_namespace_if_not_exists(namespace)
    try:
        return catalog.load_table(identifier)
    except NoSuchTableError:
        logger.info("Creando la tabla %s", identifier)
        return catalog.create_table(
            identifier,
            schema=daily_schema(),
            partition_spec=daily_partition_spec(),
            properties={
                "write.parquet.compression-codec": "zstd",
                "comment": (
                    "NDVI medio por municipio y fecha de adquisicion, "
                    "derivado de Sentinel-2 L2A"
                ),
            },
        )


def to_arrow(frame: pd.DataFrame, table: Table) -> pa.Table:
    """Adapta el DataFrame al esquema exacto de la tabla.

    Iceberg no acepta un lote cuyo tipo no coincida con el declarado, y hace
    bien: aceptarlo en silencio dejaria la tabla con columnas de tipo distinto
    segun el dia en que se escribieron. Aqui se convierte de forma explicita, de
    modo que cualquier discrepancia salte al escribir y no meses despues al
    consultar.

    Args:
        frame: salida de `zonal_ndvi_daily`, ya validada.
        table: tabla de destino, de la que se toma el esquema.

    Returns:
        Tabla de PyArrow lista para anadir.

    Raises:
        ValueError: si al DataFrame le falta alguna columna del esquema.
    """
    arrow_schema = table.schema().as_arrow()
    prepared = frame.copy()

    if INGESTED_AT not in prepared.columns:
        prepared[INGESTED_AT] = pd.Timestamp.now(tz="UTC")

    missing = [name for name in arrow_schema.names if name not in prepared.columns]
    if missing:
        raise ValueError(
            f"Al lote le faltan las columnas {missing} que exige la tabla "
            f"{table.name()}"
        )

    # `acquisition_date` llega como objeto `date` de Python; el resto de tipos
    # los resuelve PyArrow con la conversion directa desde pandas.
    prepared["acquisition_date"] = pd.to_datetime(prepared["acquisition_date"]).dt.date

    columns = [
        pa.array(prepared[field.name], type=field.type) for field in arrow_schema
    ]
    return pa.Table.from_arrays(columns, schema=arrow_schema)


def append_daily(
    frame: pd.DataFrame,
    *,
    table: Table | None = None,
    validate: bool = True,
) -> int:
    """Anade a la tabla el resultado de una o varias fechas.

    La validacion de Pandera se aplica aqui, en la frontera entre el calculo y
    el almacen, y no antes ni despues. Antes seria pronto, porque el DataFrame
    todavia se esta construyendo; despues seria tarde, porque el dato malo ya
    estaria escrito. Este es el ultimo punto en el que un lote defectuoso se
    puede rechazar sin que nadie lo haya leido.

    Args:
        frame: filas a escribir, con el esquema de `zonal.DAILY_COLUMNS`.
        table: tabla de destino; por defecto la de la configuracion.
        validate: aplicar el contrato de Pandera antes de escribir.

    Returns:
        Numero de filas escritas.
    """
    if frame.empty:
        logger.info("Lote vacio: no se escribe nada")
        return 0

    if validate:
        frame = DailyZonalNdviSchema.validate(frame)

    table = table or ensure_daily_table()
    table.append(to_arrow(frame, table))

    logger.info(
        "Escritas %d filas de %d fechas en %s",
        len(frame), frame["acquisition_date"].nunique(), table.name(),
    )
    return len(frame)


def existing_dates(table: Table | None = None) -> set[date]:
    """Fechas que ya estan cargadas en la tabla.

    Es lo que hace reanudable la carga historica: al arrancar se consulta que
    hay y se procesa solo lo que falta. Se lee unicamente la columna de fecha,
    asi que el coste no depende de lo ancha que sea la tabla.
    """
    table = table or ensure_daily_table()
    if table.current_snapshot() is None:
        return set()

    scanned = table.scan(selected_fields=("acquisition_date",)).to_arrow()
    return set(scanned.column("acquisition_date").to_pylist())


def replace_dates(
    frame: pd.DataFrame,
    dates: Iterable[date],
    *,
    table: Table | None = None,
) -> int:
    """Reescribe por completo unas fechas concretas.

    Reprocesar una fecha con `append_daily` la duplicaria. Esta operacion borra
    y escribe dentro de la misma instantanea, de forma que quien consulte vea
    la version vieja o la nueva, nunca las dos ni ninguna.

    Args:
        frame: filas nuevas de esas fechas.
        dates: fechas a sustituir.
        table: tabla de destino.

    Returns:
        Numero de filas escritas.
    """
    from pyiceberg.expressions import EqualTo, Or

    dates = list(dates)
    if not dates:
        return 0

    table = table or ensure_daily_table()
    condition = EqualTo("acquisition_date", dates[0])
    for day in dates[1:]:
        condition = Or(condition, EqualTo("acquisition_date", day))

    with table.transaction() as transaction:
        transaction.delete(delete_filter=condition)
        if not frame.empty:
            transaction.append(to_arrow(DailyZonalNdviSchema.validate(frame), table))

    logger.info("Reemplazadas %d fechas (%d filas)", len(dates), len(frame))
    return len(frame)


# ---------------------------------------------------------------------------
# Meteorologia
# ---------------------------------------------------------------------------


def weather_schema() -> Schema:
    """Esquema de la tabla diaria de estaciones meteorologicas.

    Las medidas van como opcionales y el resto como obligatorias, y esa
    distincion no es un descuido sino el contrato. Una estacion puede tener el
    pluviometro averiado una semana y el termometro bien, o al reves. Forzar un
    valor donde no lo hubo obligaria a inventarse un cero, que en una serie
    sobre sequia es exactamente la mentira que mas dano hace: un cero de lluvia
    y un "no se midio" cuentan historias opuestas y no se pueden confundir.

    La estacion y el dia forman la clave: una estacion no mide dos veces el
    mismo dia.
    """
    return Schema(
        NestedField(1, "station_id", StringType(), required=True),
        NestedField(2, "observed_on", DateType(), required=True),
        NestedField(3, "precipitation_mm", DoubleType(), required=False),
        NestedField(4, "temp_mean_c", DoubleType(), required=False),
        NestedField(5, "temp_max_c", DoubleType(), required=False),
        NestedField(6, "temp_min_c", DoubleType(), required=False),
        NestedField(7, "station_name", StringType(), required=True),
        NestedField(8, "province", StringType(), required=True),
        NestedField(9, "longitude", DoubleType(), required=True),
        NestedField(10, "latitude", DoubleType(), required=True),
        NestedField(11, "altitude_m", DoubleType(), required=False),
        NestedField(12, INGESTED_AT, TimestamptzType(), required=True),
        identifier_field_ids=[1, 2],
    )


def weather_partition_spec() -> PartitionSpec:
    """Particionado por ano de observacion, no por mes.

    Aqui el grano correcto es mas grueso que en la tabla de NDVI, y por una
    razon de tamano. Son 114 estaciones por unos 3.100 dias, del orden de
    350.000 filas en total: repartidas por mes darian particiones de tres mil
    filas, que es el tamano al que empiezan a estorbar mas de lo que ayudan.
    Por ano salen nueve particiones de unas cuarenta mil, que es razonable.
    """
    return PartitionSpec(
        PartitionField(
            source_id=2, field_id=1000, transform=YearTransform(),
            name="observed_year",
        )
    )


def ensure_weather_table(
    catalog: Catalog | None = None,
    *,
    settings: Settings | None = None,
) -> Table:
    """Devuelve la tabla de meteorologia, creandola la primera vez."""
    settings = settings or get_settings()
    catalog = catalog or get_catalog(settings)
    namespace = settings.lakehouse.bronze_namespace
    identifier = f"{namespace}.{WEATHER_TABLE}"

    catalog.create_namespace_if_not_exists(namespace)
    try:
        return catalog.load_table(identifier)
    except NoSuchTableError:
        logger.info("Creando la tabla %s", identifier)
        return catalog.create_table(
            identifier,
            schema=weather_schema(),
            partition_spec=weather_partition_spec(),
            properties={
                "write.parquet.compression-codec": "zstd",
                "comment": (
                    "Precipitacion y temperatura diarias de las estaciones de "
                    "AEMET dentro de la cuenca del Guadalquivir"
                ),
            },
        )


def append_weather(frame: pd.DataFrame, *, table: Table | None = None) -> int:
    """Anade filas de meteorologia a la tabla."""
    if frame.empty:
        logger.info("Lote de meteorologia vacio: no se escribe nada")
        return 0

    table = table or ensure_weather_table()
    frame = frame.copy()
    frame[INGESTED_AT] = pd.Timestamp.now(tz="UTC")

    arrow_schema = table.schema().as_arrow()
    table.append(pa.Table.from_arrays(
        [pa.array(frame[field.name], type=field.type) for field in arrow_schema],
        schema=arrow_schema,
    ))
    logger.info("Escritas %d filas de meteorologia en %s", len(frame), table.name())
    return len(frame)


# ---------------------------------------------------------------------------
# Registro de ingesta
# ---------------------------------------------------------------------------


def log_schema() -> Schema:
    """Esquema de la tabla de control de la carga.

    Existe por dos razones distintas que resulta que piden lo mismo.

    La primera es reanudar. Una carga de ocho anos tarda horas y se interrumpe:
    hay que saber por donde iba. La tabla de datos no basta como marca, porque
    una fecha completamente nublada se procesa entera y no escribe ni una fila,
    de modo que al reanudar volveria a intentarse una y otra vez.

    La segunda es medir. Cuanto tarda una fecha, que porcentaje del archivo
    resulta aprovechable, cuantas fechas fallan y por que. Son las cifras de la
    seccion 7.2 de la memoria, y salen gratis por llevar el registro.
    """
    return Schema(
        NestedField(1, "acquisition_date", DateType(), required=True),
        NestedField(2, "status", StringType(), required=True),
        NestedField(3, "scene_count", IntegerType(), required=True),
        NestedField(4, "row_count", IntegerType(), required=True),
        NestedField(5, "duration_seconds", DoubleType(), required=True),
        NestedField(6, "message", StringType(), required=False),
        NestedField(7, INGESTED_AT, TimestamptzType(), required=True),
    )


def ensure_log_table(
    catalog: Catalog | None = None,
    *,
    settings: Settings | None = None,
) -> Table:
    """Devuelve la tabla de control, creandola la primera vez.

    No se particiona: son del orden de mil quinientas filas para los ocho anos
    completos, y particionar algo tan pequeno solo generaria ficheros diminutos
    que despues hay que compactar.
    """
    settings = settings or get_settings()
    catalog = catalog or get_catalog(settings)
    namespace = settings.lakehouse.bronze_namespace
    identifier = f"{namespace}.{LOG_TABLE}"

    catalog.create_namespace_if_not_exists(namespace)
    try:
        return catalog.load_table(identifier)
    except NoSuchTableError:
        logger.info("Creando la tabla %s", identifier)
        return catalog.create_table(
            identifier,
            schema=log_schema(),
            properties={"comment": "Control de la carga historica: una fila por intento"},
        )


def append_log(records: Sequence[dict], *, table: Table | None = None) -> int:
    """Anade entradas al registro de ingesta."""
    if not records:
        return 0

    table = table or ensure_log_table()
    frame = pd.DataFrame.from_records(list(records))
    frame[INGESTED_AT] = pd.Timestamp.now(tz="UTC")
    frame["acquisition_date"] = pd.to_datetime(frame["acquisition_date"]).dt.date
    if "message" not in frame.columns:
        frame["message"] = None

    arrow_schema = table.schema().as_arrow()
    table.append(pa.Table.from_arrays(
        [pa.array(frame[field.name], type=field.type) for field in arrow_schema],
        schema=arrow_schema,
    ))
    return len(frame)


def settled_dates(table: Table | None = None) -> set[date]:
    """Fechas que ya no hay que volver a procesar.

    El registro es de solo anadir, asi que una fecha que fallo y luego salio
    bien tiene dos entradas. Cuenta como resuelta si alguna de ellas lo dice:
    reintentar algo que ya salio bien seria trabajo tirado.
    """
    table = table or ensure_log_table()
    if table.current_snapshot() is None:
        return set()

    scanned = table.scan(
        selected_fields=("acquisition_date", "status")
    ).to_arrow().to_pydict()
    return {
        day
        for day, status in zip(scanned["acquisition_date"], scanned["status"], strict=True)
        if status in SETTLED_STATUSES
    }


# ---------------------------------------------------------------------------
# Dimension de zonas
# ---------------------------------------------------------------------------


def zones_schema() -> Schema:
    """Esquema de la tabla de municipios.

    Es una tabla de dimension: describe las zonas, no mide nada. Existe para
    que las consultas no tengan que abrir un fichero cartografico. Sin ella,
    agregar por provincia obligaria a cruzar el almacen con un GeoPackage
    desde fuera, y dbt no sabe leer geometrias.

    La geometria se guarda como texto en formato WKT. No es la forma mas
    compacta, pero es la unica que entiende cualquier herramienta sin
    extensiones: con eso el panel puede dibujar el mapa leyendo solo del
    almacen, igual que lee las series.
    """
    return Schema(
        NestedField(1, "zone_id", StringType(), required=True),
        NestedField(2, "zone_name", StringType(), required=True),
        NestedField(3, "province_code", StringType(), required=True),
        NestedField(4, "area_km2", DoubleType(), required=True),
        NestedField(5, "basin_overlap_fraction", DoubleType(), required=True),
        NestedField(6, "geometry_wkt", StringType(), required=True),
        NestedField(7, INGESTED_AT, TimestamptzType(), required=True),
        identifier_field_ids=[1],
    )


def write_zones(zones, *, catalog: Catalog | None = None,
                settings: Settings | None = None) -> int:
    """Vuelca la capa de zonas al almacen, reemplazando lo que hubiera.

    Se reescribe entera en lugar de anadir porque es una dimension pequena y
    congelada: si algun dia cambia la capa, lo que se quiere es sustituirla, no
    acumular dos versiones que despues habria que desambiguar.

    Args:
        zones: GeoDataFrame con las columnas de `aoi.load_zones`.
        catalog: catalogo de tablas.
        settings: configuracion del proyecto.

    Returns:
        Numero de municipios escritos.
    """
    settings = settings or get_settings()
    catalog = catalog or get_catalog(settings)
    namespace = settings.lakehouse.bronze_namespace
    identifier = f"{namespace}.{ZONES_TABLE}"

    catalog.create_namespace_if_not_exists(namespace)
    with contextlib.suppress(NoSuchTableError):
        catalog.drop_table(identifier)
    table = catalog.create_table(identifier, schema=zones_schema())

    frame = pd.DataFrame({
        "zone_id": zones["zone_id"].astype(str),
        "zone_name": zones["zone_name"].astype(str),
        "province_code": zones["province_code"].astype(str),
        "area_km2": zones["area_km2"].astype(float),
        "basin_overlap_fraction": zones["basin_overlap_fraction"].astype(float),
        "geometry_wkt": zones.geometry.to_wkt(),
        INGESTED_AT: pd.Timestamp.now(tz="UTC"),
    })

    arrow_schema = table.schema().as_arrow()
    table.append(pa.Table.from_arrays(
        [pa.array(frame[field.name], type=field.type) for field in arrow_schema],
        schema=arrow_schema,
    ))
    logger.info("Escritos %d municipios en %s", len(frame), identifier)
    return len(frame)


def duckdb_connection(
    settings: Settings | None = None,
    *,
    alias: str = "lake",
    database: str | None = None,
) -> duckdb.DuckDBPyConnection:
    """Abre DuckDB con el catalogo del lakehouse ya montado.

    Es el otro extremo del almacen. PyIceberg escribe; DuckDB lee. Con el
    catalogo montado, las tablas Iceberg se consultan con SQL corriente
    (`SELECT ... FROM lake.bronze.ndvi_zonal_daily`) y el motor se encarga de
    descartar las particiones que el `WHERE` no necesita.

    Dos detalles que cuesta adivinar y conviene tener escritos:

    El secreto de S3 pide `URL_STYLE 'path'`. MinIO no usa subdominios por
    bucket como hace Amazon, asi que sin eso DuckDB construiria una URL que en
    local no resuelve.

    El catalogo pide `AUTHORIZATION_TYPE 'none'`. DuckDB asume OAuth2 al montar
    un catalogo REST y aborta si no encuentra credenciales; el catalogo de
    desarrollo no las usa. Al desplegar en la nube, esta es justo la linea que
    cambia.

    Args:
        settings: configuracion del proyecto.
        alias: nombre con el que queda montado el catalogo.
        database: fichero DuckDB donde persistir vistas y tablas propias. Por
            defecto en memoria, que basta para consultar.

    Returns:
        Conexion lista para consultar.
    """
    settings = settings or get_settings()
    store, lake = settings.store, settings.lakehouse

    connection = duckdb.connect(database or ":memory:")
    connection.execute("INSTALL iceberg; LOAD iceberg; INSTALL httpfs; LOAD httpfs;")
    connection.execute(
        """
        CREATE OR REPLACE SECRET object_store (
            TYPE s3, KEY_ID ?, SECRET ?, ENDPOINT ?, REGION ?,
            URL_STYLE 'path', USE_SSL false
        )
        """,
        [store.access_key, store.secret_key,
         store.endpoint_url.removeprefix("http://").removeprefix("https://"),
         store.region],
    )
    connection.execute(
        f"ATTACH '{store.bucket}' AS {alias} "
        f"(TYPE ICEBERG, ENDPOINT '{lake.catalog_uri}', AUTHORIZATION_TYPE 'none')"
    )
    logger.debug("DuckDB conectado al catalogo %s como %s", lake.catalog_uri, alias)
    return connection


def replace_log_entries(
    records: Sequence[dict],
    dates: Iterable[date],
    *,
    table: Table | None = None,
) -> int:
    """Sustituye las entradas del registro de unas fechas concretas.

    El registro es de solo anadir mientras la carga avanza, y esta bien que lo
    sea: cada intento queda documentado. Pero al recuperar una fecha que se
    proceso con codigo defectuoso, la entrada vieja no es historia util sino
    una mentira operativa. Mientras siga ahi con estado `ok` o `empty`,
    `settled_dates` la da por resuelta y ninguna ejecucion futura volvera a
    intentarla.

    Se borra y se escribe en la misma instantanea por el mismo motivo que en
    `replace_dates`: entre las dos operaciones, una fecha sin entrada y sin
    datos es un hueco que la siguiente ejecucion rellenaria por su cuenta, pero
    una fecha marcada dos veces obliga a desempatar.

    Args:
        records: entradas nuevas, en el formato de `DateOutcome.as_log_record`.
        dates: fechas cuyas entradas anteriores hay que retirar.
        table: tabla de destino.

    Returns:
        Numero de entradas escritas.
    """
    from pyiceberg.expressions import EqualTo, Or

    dates = list(dates)
    if not dates:
        return 0

    table = table or ensure_log_table()
    condition = EqualTo("acquisition_date", dates[0])
    for day in dates[1:]:
        condition = Or(condition, EqualTo("acquisition_date", day))

    frame = pd.DataFrame.from_records(list(records))
    frame[INGESTED_AT] = pd.Timestamp.now(tz="UTC")
    frame["acquisition_date"] = pd.to_datetime(frame["acquisition_date"]).dt.date
    if "message" not in frame.columns:
        frame["message"] = None

    arrow_schema = table.schema().as_arrow()
    batch = pa.Table.from_arrays(
        [pa.array(frame[field.name], type=field.type) for field in arrow_schema],
        schema=arrow_schema,
    )

    with table.transaction() as transaction:
        transaction.delete(delete_filter=condition)
        transaction.append(batch)

    logger.info("Reemplazadas %d entradas del registro", len(frame))
    return len(frame)

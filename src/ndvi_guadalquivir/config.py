"""Configuracion central del pipeline.

Todos los parametros se leen de variables de entorno con valores por defecto
razonables, de forma que el proyecto funcione recien clonado sin configurar
nada, pero se pueda apuntar a otra infraestructura (por ejemplo S3 de AWS en
lugar de MinIO local) sin tocar codigo.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Raiz del repositorio, calculada desde la ubicacion de este fichero.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


#: Fichero de variables locales, no versionado. Docker Compose lo lee solo; el
#: codigo de Python no, y hasta ahora no hacia falta porque todos los ajustes
#: tenian un valor por defecto razonable. La clave de AEMET no puede tenerlo,
#: porque es una credencial, asi que se carga desde aqui.
ENV_FILE = PROJECT_ROOT / ".env"


def load_env_file(path: Path | None = None) -> None:
    """Lleva a `os.environ` lo que haya en `.env`, sin pisar lo ya definido.

    Se escribe a mano en lugar de traer `python-dotenv` porque son quince lineas
    y una dependencia menos que justificar en la memoria.

    El orden de precedencia importa y es el habitual: lo que ya esta en el
    entorno gana. Asi, una ejecucion en Airflow o en integracion continua puede
    apuntar a otra infraestructura exportando la variable, sin que el fichero de
    desarrollo del portatil se imponga por detras.
    """
    path = path or ENV_FILE
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip())


load_env_file()


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class StacSettings:
    """Acceso al catalogo de imagenes.

    Se usa el catalogo de Element84, que expone los productos Sentinel-2 de
    Copernicus como COG (Cloud Optimized GeoTIFF) en AWS Open Data y permite
    lectura anonima por ventanas.

    La coleccion por defecto es `sentinel-2-l2a` y no `sentinel-2-c1-l2a`, que
    era la eleccion inicial, por una razon que solo aparecio al medir la
    cobertura ano por ano: **el reprocesado Collection-1 no tiene ni una escena
    de 2022** sobre la cuenca, ni siquiera sin filtrar por nubes, mientras que
    2021 y 2023 traen del orden de 130 cada quincena.

    Collection-1 es tecnicamente mejor, con correccion geometrica mas fina en
    zonas de relieve como Sierra Morena. Pero un hueco de un ano entero es
    inaceptable en una serie destinada a detectar anomalias, y ademas 2022 es
    precisamente el ano de la sequia que el trabajo quiere documentar. Una
    correccion geometrica algo peor se puede justificar en la memoria; un
    agujero en el ano central del analisis, no.

    El cambio no toca ni una linea de codigo: es el valor de una variable de
    entorno.
    """

    api_url: str = field(default_factory=lambda: _env(
        "STAC_API_URL", "https://earth-search.aws.element84.com/v1"))
    collection: str = field(default_factory=lambda: _env(
        "STAC_COLLECTION", "sentinel-2-l2a"))
    max_cloud_cover: float = field(default_factory=lambda: float(_env(
        "STAC_MAX_CLOUD_COVER", "20")))


@dataclass(frozen=True)
class AemetSettings:
    """Acceso al servicio de datos abiertos de AEMET.

    Sirve para contrastar la anomalia de vegetacion con la lluvia que de verdad
    falto. Sin ese contraste el proyecto solo puede afirmar que detecta una
    anomalia; con el puede afirmar que detecta la sequia que registro la agencia
    meteorologica del Estado, que es una conclusion de otro orden.

    Se eligio AEMET y no un reanalisis como ERA5 precisamente por eso. ERA5 es
    una rejilla y evitaria tener que interpolar desde estaciones sueltas, pero
    es un modelo que reconstruye el pasado, no una observacion. El argumento que
    sostiene la memoria necesita la fuente oficial.

    La clave se pide gratis en el portal de AEMET y llega por correo. No tiene
    valor por defecto a proposito: es una credencial y debe salir del entorno,
    nunca del codigo.
    """

    api_url: str = field(default_factory=lambda: _env(
        "AEMET_API_URL", "https://opendata.aemet.es/opendata"))
    api_key: str = field(default_factory=lambda: _env("AEMET_API_KEY", ""))

    @property
    def configured(self) -> bool:
        """Si hay clave. Permite que el resto del proyecto siga funcionando sin ella."""
        return bool(self.api_key)


@dataclass(frozen=True)
class ObjectStoreSettings:
    """Almacen de objetos compatible con S3.

    En desarrollo apunta a MinIO levantado con Docker Compose. Para desplegar
    en AWS basta con vaciar `endpoint_url` y usar credenciales reales: el
    codigo de la aplicacion no cambia.
    """

    endpoint_url: str = field(default_factory=lambda: _env(
        "S3_ENDPOINT_URL", "http://localhost:9000"))
    access_key: str = field(default_factory=lambda: _env(
        "S3_ACCESS_KEY", "minioadmin"))
    secret_key: str = field(default_factory=lambda: _env(
        "S3_SECRET_KEY", "minioadmin"))
    bucket: str = field(default_factory=lambda: _env("S3_BUCKET", "lakehouse"))
    region: str = field(default_factory=lambda: _env("S3_REGION", "us-east-1"))

    @property
    def warehouse_uri(self) -> str:
        """Raiz del almacen de tablas dentro del bucket."""
        return f"s3://{self.bucket}/warehouse"


@dataclass(frozen=True)
class ProcessingSettings:
    """Parametros del calculo.

    `target_resolution_m` fija la resolucion de trabajo. Sentinel-2 entrega las
    bandas rojo e infrarrojo cercano a 10 m, pero para series temporales
    agregadas por municipio no aporta precision y multiplica por 100 el coste
    de computo frente a 100 m. Es el principal mando de escala del proyecto.
    """

    target_resolution_m: int = field(default_factory=lambda: int(_env(
        "TARGET_RESOLUTION_M", "100")))
    min_valid_pixel_fraction: float = field(default_factory=lambda: float(_env(
        "MIN_VALID_PIXEL_FRACTION", "0.30")))
    max_workers: int = field(default_factory=lambda: int(_env(
        "MAX_WORKERS", "4")))


@dataclass(frozen=True)
class LakehouseSettings:
    """Catalogo de tablas del lakehouse.

    El catalogo y el almacen son dos servicios distintos a proposito. MinIO
    guarda los bytes; el catalogo guarda que ficheros componen cada tabla en
    cada momento. Esa separacion es lo que permite que una escritura a medias
    nunca sea visible y que dos procesos escriban a la vez sin corromper nada.
    """

    catalog_uri: str = field(default_factory=lambda: _env(
        "ICEBERG_CATALOG_URI", "http://localhost:8181"))
    catalog_name: str = field(default_factory=lambda: _env(
        "ICEBERG_CATALOG_NAME", "lakehouse"))
    #: Capa de datos tal y como los produce el calculo en Python, sin agregar.
    #: Las capas silver y gold las construye dbt a partir de esta.
    bronze_namespace: str = field(default_factory=lambda: _env(
        "ICEBERG_BRONZE_NAMESPACE", "bronze"))


@dataclass(frozen=True)
class Settings:
    stac: StacSettings = field(default_factory=StacSettings)
    aemet: AemetSettings = field(default_factory=AemetSettings)
    store: ObjectStoreSettings = field(default_factory=ObjectStoreSettings)
    processing: ProcessingSettings = field(default_factory=ProcessingSettings)
    lakehouse: LakehouseSettings = field(default_factory=LakehouseSettings)
    data_dir: Path = field(default_factory=lambda: Path(
        _env("DATA_DIR", str(PROJECT_ROOT / "data"))))

    @property
    def warehouse_path(self) -> Path:
        """Ruta del almacen DuckDB usado por dbt y por el panel."""
        return self.data_dir / "warehouse.duckdb"


def get_settings() -> Settings:
    """Punto unico de acceso a la configuracion."""
    return Settings()

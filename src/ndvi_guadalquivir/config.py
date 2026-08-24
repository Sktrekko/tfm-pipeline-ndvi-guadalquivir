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


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class StacSettings:
    """Acceso al catalogo de imagenes.

    Se usa el catalogo de Element84, que expone los productos Sentinel-2 de
    Copernicus como COG (Cloud Optimized GeoTIFF) en AWS Open Data y permite
    lectura anonima por ventanas. La coleccion `sentinel-2-c1-l2a` corresponde
    al reprocesado Collection-1, con correccion geometrica mejorada en zonas
    de relieve como Sierra Morena o Sierra Nevada.
    """

    api_url: str = field(default_factory=lambda: _env(
        "STAC_API_URL", "https://earth-search.aws.element84.com/v1"))
    collection: str = field(default_factory=lambda: _env(
        "STAC_COLLECTION", "sentinel-2-c1-l2a"))
    max_cloud_cover: float = field(default_factory=lambda: float(_env(
        "STAC_MAX_CLOUD_COVER", "20")))


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

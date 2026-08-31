"""Contratos de datos validados con Pandera.

La validacion se aplica en la frontera entre etapas: lo que sale del calculo
en Python queda garantizado antes de escribirse en el almacen, de modo que los
modelos de dbt aguas abajo pueden asumir un esquema estable. Es la primera de
las dos capas de calidad del proyecto; la segunda son los tests de dbt sobre
los modelos SQL.
"""

from __future__ import annotations

import pandera.pandas as pa
from pandera.typing import Series

#: Rango teorico del NDVI. Un valor fuera de aqui delata un error de calculo,
#: tipicamente un offset de reflectancia mal aplicado o una division por cero.
NDVI_MIN, NDVI_MAX = -1.0, 1.0

#: Desviacion tipica maxima admisible dentro de una zona. El NDVI vive en un
#: intervalo de longitud 2, asi que una desviacion mayor que 1 seria imposible.
NDVI_STD_MAX = 1.0


class _NdviStatsSchema(pa.DataFrameModel):
    """Parte comun de las tablas de NDVI zonal.

    No se valida directamente: existe para que las reglas de rango y de
    coherencia entre estadisticos se declaren una sola vez y las hereden los
    dos esquemas concretos.
    """

    zone_id: Series[str] = pa.Field(nullable=False)
    zone_name: Series[str] = pa.Field(nullable=False)

    ndvi_mean: Series[float] = pa.Field(ge=NDVI_MIN, le=NDVI_MAX, nullable=False)
    ndvi_median: Series[float] = pa.Field(ge=NDVI_MIN, le=NDVI_MAX, nullable=False)
    ndvi_std: Series[float] = pa.Field(ge=0.0, le=NDVI_STD_MAX, nullable=False)
    ndvi_p10: Series[float] = pa.Field(ge=NDVI_MIN, le=NDVI_MAX, nullable=False)
    ndvi_p90: Series[float] = pa.Field(ge=NDVI_MIN, le=NDVI_MAX, nullable=False)

    pixel_count: Series[int] = pa.Field(gt=0)
    valid_pixel_count: Series[int] = pa.Field(ge=0)
    valid_pixel_fraction: Series[float] = pa.Field(ge=0.0, le=1.0)

    class Config:
        strict = False       # se toleran columnas extra de trazabilidad
        coerce = True

    # Las comprobaciones de DataFrame reciben `cls` sin llevar `@classmethod`
    # encima: es Pandera quien las convierte al construir la clase. mypy no lo
    # sabe y las lee como metodos de instancia mal escritos, de ahi el
    # `type: ignore` que llevan todas. Ponerles un `@classmethod` a mano
    # romperia el orden de decoradores que espera Pandera.
    @pa.dataframe_check
    def percentiles_ordered(cls, df) -> Series[bool]:  # type: ignore[misc]
        """El percentil 10 nunca puede superar al 90."""
        return df["ndvi_p10"] <= df["ndvi_p90"]

    @pa.dataframe_check
    def median_within_percentiles(cls, df) -> Series[bool]:  # type: ignore[misc]
        """La mediana cae necesariamente entre los percentiles 10 y 90."""
        return (df["ndvi_median"] >= df["ndvi_p10"]) & (df["ndvi_median"] <= df["ndvi_p90"])

    @pa.dataframe_check
    def valid_pixels_not_exceeding_total(cls, df) -> Series[bool]:  # type: ignore[misc]
        """No puede haber mas pixeles validos que pixeles totales."""
        return df["valid_pixel_count"] <= df["pixel_count"]


class DailyZonalNdviSchema(_NdviStatsSchema):
    """Tabla de salida del pipeline: una fila por zona y fecha de adquisicion.

    Es el contrato que la capa Python garantiza a los modelos de dbt.
    """

    acquisition_date: Series[pa.DateTime] = pa.Field(nullable=False)
    tile_ids: Series[str] = pa.Field(nullable=False)
    scene_ids: Series[str] = pa.Field(nullable=False)
    scene_count: Series[int] = pa.Field(gt=0)
    mean_cloud_cover: Series[float] = pa.Field(ge=0.0, le=100.0)

    class Config:
        strict = False
        coerce = True

    @pa.dataframe_check
    def one_row_per_zone_and_date(cls, df) -> bool:  # type: ignore[misc]
        """La combinacion zona-fecha identifica la fila de forma univoca."""
        return not df.duplicated(subset=["zone_id", "acquisition_date"]).any()


class SceneZonalNdviSchema(_NdviStatsSchema):
    """Tabla de diagnostico: una fila por zona y escena individual."""

    scene_id: Series[str] = pa.Field(nullable=False)
    acquired_at: Series[pa.DateTime] = pa.Field(nullable=False)
    tile_id: Series[str] = pa.Field(nullable=False)
    scene_cloud_cover: Series[float] = pa.Field(ge=0.0, le=100.0)

    class Config:
        strict = False
        coerce = True

    @pa.dataframe_check
    def one_row_per_zone_and_scene(cls, df) -> bool:  # type: ignore[misc]
        """La combinacion zona-escena identifica la fila de forma univoca."""
        return not df.duplicated(subset=["zone_id", "scene_id"]).any()

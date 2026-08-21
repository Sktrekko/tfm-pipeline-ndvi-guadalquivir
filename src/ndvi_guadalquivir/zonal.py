"""Agregacion de raster a poligonos (estadistica zonal).

Convierte un raster continuo de NDVI en una fila por zona, que es el formato
tabular que consumen DuckDB, dbt y el panel. Se implementa rasterizando los
identificadores de zona sobre la misma malla del NDVI y agrupando con numpy,
lo que evita dependencias adicionales y recorre el raster una sola vez.

El pipeline agrega a nivel de *fecha* (`zonal_ndvi_daily`, sobre un mosaico);
la version por escena suelta (`zonal_ndvi_scene`) se conserva porque es util
para inspeccionar un granulo concreto durante el diagnostico.
"""

from __future__ import annotations

import logging

import geopandas as gpd
import numpy as np
import pandas as pd
import xarray as xr
from rasterio.features import rasterize

from .mosaic import NdviMosaic
from .raster import NdviRaster

logger = logging.getLogger(__name__)

#: Valor de relleno para los pixeles que no caen en ninguna zona.
_NO_ZONE = -1

#: Estadisticos calculados para cada zona.
_STAT_COLUMNS = [
    "ndvi_mean", "ndvi_median", "ndvi_std", "ndvi_p10", "ndvi_p90",
    "pixel_count", "valid_pixel_count", "valid_pixel_fraction",
]

#: Orden canonico de columnas de la tabla diaria. Se fija explicitamente para
#: que un DataFrame vacio tenga el mismo esquema que uno con datos y la
#: concatenacion aguas abajo nunca falle.
DAILY_COLUMNS = [
    "zone_id", "zone_name", "acquisition_date", "tile_ids", "scene_ids",
    "scene_count", "mean_cloud_cover", *_STAT_COLUMNS,
]

SCENE_COLUMNS = [
    "zone_id", "zone_name", "scene_id", "acquired_at", "acquisition_date",
    "tile_id", "scene_cloud_cover", *_STAT_COLUMNS,
]


def _rasterize_zones(zones: gpd.GeoDataFrame, template: xr.DataArray) -> np.ndarray:
    """Pinta el indice posicional de cada zona sobre la malla del raster."""
    zones_projected = zones.to_crs(template.rio.crs)
    shapes = (
        (geometry, index)
        for index, geometry in enumerate(zones_projected.geometry)
        if geometry is not None and not geometry.is_empty
    )
    return rasterize(
        shapes,
        out_shape=template.shape[-2:],
        transform=template.rio.transform(),
        fill=_NO_ZONE,
        dtype="int32",
        all_touched=False,
    )


def _aggregate(
    data: xr.DataArray,
    zones: gpd.GeoDataFrame,
    min_valid_pixel_fraction: float,
    context: str,
) -> list[dict]:
    """Calcula los estadisticos de NDVI de cada zona sobre un raster.

    Devuelve una lista de diccionarios con `zone_id`, `zone_name` y los
    estadisticos, omitiendo las zonas sin cobertura o con demasiados huecos.
    """
    zone_index = _rasterize_zones(zones, data)
    values = np.asarray(data.values, dtype=np.float32)
    finite = np.isfinite(values)

    rows: list[dict] = []
    for position, zone in zones.reset_index(drop=True).iterrows():
        in_zone = zone_index == position
        total = int(in_zone.sum())
        if total == 0:
            continue  # la zona queda fuera de la ventana leida

        usable = in_zone & finite
        valid = int(usable.sum())
        fraction = valid / total

        if fraction < min_valid_pixel_fraction:
            logger.debug(
                "Zona %s descartada en %s: solo %.1f%% de pixeles validos",
                zone["zone_id"], context, 100 * fraction,
            )
            continue

        sample = values[usable]
        rows.append({
            "zone_id": str(zone["zone_id"]),
            "zone_name": zone["zone_name"],
            "ndvi_mean": float(np.mean(sample)),
            "ndvi_median": float(np.median(sample)),
            "ndvi_std": float(np.std(sample)),
            "ndvi_p10": float(np.percentile(sample, 10)),
            "ndvi_p90": float(np.percentile(sample, 90)),
            "pixel_count": total,
            "valid_pixel_count": valid,
            "valid_pixel_fraction": float(fraction),
        })

    logger.info(
        "Estadistica zonal de %s: %d zonas de %d con dato suficiente",
        context, len(rows), len(zones),
    )
    return rows


def zonal_ndvi_daily(
    mosaic: NdviMosaic,
    zones: gpd.GeoDataFrame,
    *,
    min_valid_pixel_fraction: float = 0.30,
) -> pd.DataFrame:
    """Resume por zona el mosaico de NDVI de una fecha.

    Esta es la granularidad de salida del pipeline: una fila por zona y dia
    de adquisicion. Los identificadores de los granulos que han intervenido se
    conservan en texto para poder trazar cualquier valor hasta su origen.

    Args:
        mosaic: mosaico diario ya construido.
        zones: poligonos de agregacion, con `zone_id` y `zone_name`.
        min_valid_pixel_fraction: fraccion minima de pixeles utiles exigida a
            una zona para generar fila. Evita medias enganosas bajo nubes.

    Returns:
        DataFrame con el esquema `DAILY_COLUMNS`.
    """
    rows = _aggregate(
        mosaic.data, zones, min_valid_pixel_fraction,
        context=f"mosaico {mosaic.acquisition_date}",
    )
    for row in rows:
        row.update({
            "acquisition_date": mosaic.acquisition_date,
            "tile_ids": ",".join(mosaic.tile_ids),
            "scene_ids": ",".join(mosaic.scene_ids),
            "scene_count": mosaic.scene_count,
            "mean_cloud_cover": mosaic.mean_cloud_cover,
        })
    return pd.DataFrame.from_records(rows, columns=DAILY_COLUMNS)


def zonal_ndvi_scene(
    raster: NdviRaster,
    zones: gpd.GeoDataFrame,
    *,
    min_valid_pixel_fraction: float = 0.30,
) -> pd.DataFrame:
    """Resume por zona el NDVI de una escena individual.

    Util para diagnosticar un granulo concreto. Para la serie temporal del
    proyecto se usa `zonal_ndvi_daily`, que evita partir una zona entre tiles.

    Returns:
        DataFrame con el esquema `SCENE_COLUMNS`.
    """
    rows = _aggregate(
        raster.data, zones, min_valid_pixel_fraction,
        context=raster.scene.item_id,
    )
    for row in rows:
        row.update({
            "scene_id": raster.scene.item_id,
            "acquired_at": raster.scene.acquired_at,
            "acquisition_date": raster.scene.acquisition_date,
            "tile_id": raster.scene.tile_id,
            "scene_cloud_cover": raster.scene.cloud_cover,
        })
    return pd.DataFrame.from_records(rows, columns=SCENE_COLUMNS)

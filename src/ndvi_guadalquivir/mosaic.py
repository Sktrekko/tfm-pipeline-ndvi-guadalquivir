"""Mosaico diario de escenas.

Una zona de estudio raramente cae dentro de un unico granulo Sentinel-2: la
malla MGRS divide el territorio en tiles de 110 km, y la cuenca del
Guadalquivir necesita del orden de una docena. Ademas los tiles adyacentes se
solapan varios kilometros.

Por eso la unidad de trabajo del pipeline no es la escena sino la *fecha*:
todas las escenas de una misma pasada se combinan en un unico raster continuo
antes de calcular la estadistica zonal. Sin este paso, un municipio a caballo
entre dos tiles produciria dos medias parciales en lugar de una correcta.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np
import rioxarray  # noqa: F401
import xarray as xr
from affine import Affine
from rasterio.enums import Resampling
from rasterio.warp import transform_bounds

from .catalog import Scene
from .raster import NdviRaster, read_ndvi

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NdviMosaic:
    """NDVI combinado de todas las escenas de una fecha."""

    acquisition_date: date
    data: xr.DataArray
    scene_ids: tuple[str, ...]
    tile_ids: tuple[str, ...]
    mean_cloud_cover: float

    @property
    def scene_count(self) -> int:
        return len(self.scene_ids)


def dominant_crs(scenes: Sequence[Scene]) -> str:
    """CRS de destino del mosaico: el codigo EPSG mas frecuente entre las escenas.

    Reproyectar es caro, asi que se elige la proyeccion que ya comparte la
    mayoria de los granulos y solo se reproyectan los minoritarios. En la
    cuenca del Guadalquivir esto suele resolverse en EPSG:32630 (UTM 30N).
    """
    if not scenes:
        raise ValueError("No hay escenas de las que deducir el CRS")
    counts: dict[int, int] = {}
    for scene in scenes:
        counts[scene.epsg] = counts.get(scene.epsg, 0) + 1
    epsg = max(counts, key=lambda code: (counts[code], -code))
    return f"EPSG:{epsg}"


def build_target_grid(
    bounds_wgs84: tuple[float, float, float, float],
    target_crs: str,
    resolution_m: float,
) -> xr.DataArray:
    """Construye la malla canonica de destino del mosaico.

    Es una pieza central del diseno. La malla se deriva de la zona de estudio
    y de la resolucion de trabajo, nunca de la extension del primer granulo
    que se lea. Esto tiene dos consecuencias importantes:

    1. El mosaico cubre toda la zona pedida aunque ningun tile individual la
       cubra entera, que es el caso habitual cuando una comarca queda a
       caballo entre dos granulos MGRS.
    2. Todas las fechas comparten exactamente la misma malla, de modo que la
       serie temporal es comparable pixel a pixel y las variaciones que se
       observan son del terreno, no del remuestreo.

    Args:
        bounds_wgs84: zona de estudio en grados `(oeste, sur, este, norte)`.
        target_crs: proyeccion de destino, tipicamente UTM.
        resolution_m: tamano de pixel en metros.

    Returns:
        DataArray vacio (todo NaN) georreferenciado, listo para recibir datos.
    """
    west, south, east, north = transform_bounds("EPSG:4326", target_crs, *bounds_wgs84)

    # Se ancla la malla a multiplos de la resolucion para que dos ejecuciones
    # con ventanas ligeramente distintas produzcan pixeles alineados.
    west = math.floor(west / resolution_m) * resolution_m
    south = math.floor(south / resolution_m) * resolution_m
    east = math.ceil(east / resolution_m) * resolution_m
    north = math.ceil(north / resolution_m) * resolution_m

    width = max(1, round((east - west) / resolution_m))
    height = max(1, round((north - south) / resolution_m))

    grid = xr.DataArray(
        np.full((height, width), np.nan, dtype=np.float32),
        coords={
            "y": north - resolution_m * (np.arange(height) + 0.5),
            "x": west + resolution_m * (np.arange(width) + 0.5),
        },
        dims=("y", "x"),
        name="ndvi",
    )
    grid.rio.write_crs(target_crs, inplace=True)
    grid.rio.write_transform(
        Affine(resolution_m, 0.0, west, 0.0, -resolution_m, north), inplace=True
    )
    grid.rio.write_nodata(np.nan, inplace=True)
    return grid


def merge_rasters(
    rasters: Sequence[NdviRaster],
    *,
    target_grid: xr.DataArray | None = None,
    target_crs: str | None = None,
) -> xr.DataArray:
    """Combina varios rasteres de NDVI sobre una malla comun.

    En las franjas de solape entre tiles adyacentes se promedian los valores
    disponibles en lugar de quedarse con el primero: ambos son observaciones
    validas de la misma pasada y promediarlas reduce el ruido del sensor.

    Args:
        rasters: rasteres a combinar, no vacio.
        target_grid: malla de destino. Si se omite se usa la del primer
            raster, lo que solo es correcto cuando este ya cubre toda la zona.
        target_crs: proyeccion de destino cuando no se pasa `target_grid`.

    Returns:
        DataArray con el mosaico, con NaN donde ningun raster aporta dato.

    Raises:
        ValueError: si no se pasa ningun raster.
    """
    if not rasters:
        raise ValueError("Se necesita al menos un raster para construir el mosaico")

    if target_grid is None:
        reference = rasters[0].data
        crs = target_crs or str(reference.rio.crs)
        if str(reference.rio.crs) != crs:
            reference = reference.rio.reproject(crs, resampling=Resampling.bilinear)
        if len(rasters) == 1:
            return reference
    else:
        reference = target_grid

    aligned = [
        raster.data.rio.reproject_match(reference, resampling=Resampling.bilinear)
        for raster in rasters
    ]

    stacked = xr.concat(aligned, dim="_scene")
    # nanmean sobre el eje de escenas: promedia solapes e ignora huecos.
    with np.errstate(invalid="ignore"):
        merged = stacked.mean(dim="_scene", skipna=True)

    merged.rio.write_crs(reference.rio.crs, inplace=True)
    merged.rio.write_nodata(np.nan, inplace=True)
    merged.name = "ndvi"
    return merged


def build_daily_mosaic(
    scenes: Sequence[Scene],
    bounds_wgs84: tuple[float, float, float, float],
    *,
    target_resolution_m: int | None = None,
) -> NdviMosaic:
    """Lee y combina todas las escenas de una fecha sobre una ventana.

    Args:
        scenes: escenas de la misma fecha de adquisicion.
        bounds_wgs84: ventana en grados `(oeste, sur, este, norte)`.
        target_resolution_m: resolucion de trabajo del mosaico.

    Returns:
        El mosaico diario.

    Raises:
        ValueError: si la lista esta vacia o ninguna escena se pudo leer.
    """
    if not scenes:
        raise ValueError("No hay escenas para mosaicar")

    target_crs = dominant_crs(scenes)
    resolution = float(target_resolution_m or 10)
    grid = build_target_grid(bounds_wgs84, target_crs, resolution)
    rasters: list[NdviRaster] = []

    for scene in scenes:
        try:
            rasters.append(
                read_ndvi(scene, bounds_wgs84, target_resolution_m=target_resolution_m)
            )
        except Exception as exc:
            # Un granulo corrupto o una ventana que no interseca el tile no
            # deben impedir que el resto de la pasada se procese.
            logger.warning("No se pudo leer %s: %s", scene.item_id, exc)

    if not rasters:
        raise ValueError(
            f"Ninguna de las {len(scenes)} escenas de "
            f"{scenes[0].acquisition_date} pudo leerse"
        )

    merged = merge_rasters(rasters, target_grid=grid)
    used = [raster.scene for raster in rasters]

    logger.info(
        "Mosaico de %s: %d tiles (%s), %.1f%% de pixeles con dato",
        used[0].acquisition_date, len(used),
        ", ".join(sorted({s.tile_id for s in used})),
        100 * float(np.isfinite(merged.values).mean()),
    )

    return NdviMosaic(
        acquisition_date=used[0].acquisition_date,
        data=merged,
        scene_ids=tuple(s.item_id for s in used),
        tile_ids=tuple(sorted({s.tile_id for s in used})),
        mean_cloud_cover=float(np.mean([s.cloud_cover for s in used])),
    )

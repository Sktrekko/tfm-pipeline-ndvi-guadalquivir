"""Lectura de bandas por ventana espacial.

La clave de eficiencia del proyecto esta aqui. Los productos Sentinel-2 se
publican como COG (Cloud Optimized GeoTIFF), un formato organizado en teselas
internas que permite pedir por HTTP unicamente los bytes correspondientes a
una region concreta. Leer una comarca cuesta unos pocos megabytes frente a los
mas de 100 MB que ocupa una banda completa de 10 980 x 10 980 pixeles.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import rioxarray
import xarray as xr
from rasterio.enums import Resampling
from rasterio.warp import transform_bounds

from .catalog import Scene
from .indices import compute_ndvi

logger = logging.getLogger(__name__)

#: Opciones de GDAL que evitan lecturas inutiles al abrir COG remotos.
GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "2",
}


@dataclass(frozen=True)
class NdviRaster:
    """Resultado del calculo de NDVI para una escena y una ventana."""

    scene: Scene
    data: xr.DataArray

    @property
    def values(self) -> np.ndarray:
        return self.data.values


def _open_window(href: str, bounds_wgs84: tuple[float, float, float, float]) -> xr.DataArray:
    """Abre un COG remoto y recorta la ventana indicada.

    Args:
        href: URL del COG.
        bounds_wgs84: `(oeste, sur, este, norte)` en grados.

    Returns:
        DataArray bidimensional con el recorte, en el CRS nativo del producto.
    """
    array = rioxarray.open_rasterio(href, masked=True)
    bounds_native = transform_bounds("EPSG:4326", array.rio.crs, *bounds_wgs84)
    return array.rio.clip_box(*bounds_native).squeeze(drop=True)


def read_ndvi(
    scene: Scene,
    bounds_wgs84: tuple[float, float, float, float],
    *,
    target_resolution_m: int | None = None,
) -> NdviRaster:
    """Calcula el NDVI de una escena sobre una ventana geografica.

    La banda SCL se distribuye a 20 m y las bandas espectrales a 10 m, asi que
    hay que llevarlas a una malla comun antes de combinarlas. Se remuestrea
    SCL con vecino mas proximo, que es lo correcto para un dato categorico:
    interpolar clases produciria categorias inexistentes.

    Args:
        scene: escena localizada en el catalogo.
        bounds_wgs84: ventana en grados `(oeste, sur, este, norte)`.
        target_resolution_m: si se indica, se degrada la resolucion a ese
            tamano de pixel antes de calcular, promediando los valores.

    Returns:
        El NDVI como `NdviRaster`, con NaN en los pixeles invalidos.
    """
    with rioxarray.set_options(export_grid_mapping=False):
        import rasterio

        with rasterio.Env(**GDAL_ENV):
            red = _open_window(scene.red_href, bounds_wgs84)
            nir = _open_window(scene.nir_href, bounds_wgs84)
            scl = _open_window(scene.scl_href, bounds_wgs84)

    # SCL (20 m) a la malla de las bandas espectrales (10 m).
    scl = scl.rio.reproject_match(red, resampling=Resampling.nearest)

    ndvi_values = compute_ndvi(red.values, nir.values, scl.values)
    ndvi = xr.DataArray(
        ndvi_values,
        coords=red.coords,
        dims=red.dims,
        name="ndvi",
        attrs={
            "long_name": "Normalized Difference Vegetation Index",
            "scene_id": scene.item_id,
            "acquired_at": scene.acquired_at.isoformat(),
            "valid_range": (-1.0, 1.0),
        },
    )
    ndvi.rio.write_crs(red.rio.crs, inplace=True)
    ndvi.rio.write_nodata(np.nan, inplace=True)

    if target_resolution_m:
        ndvi = downsample(ndvi, target_resolution_m)

    logger.debug(
        "NDVI de %s: %s pixeles, %.1f%% validos",
        scene.item_id, ndvi.shape,
        100 * float(np.isfinite(ndvi.values).mean()),
    )
    return NdviRaster(scene=scene, data=ndvi)


def downsample(array: xr.DataArray, target_resolution_m: int) -> xr.DataArray:
    """Reduce la resolucion promediando bloques de pixeles.

    Promediar NDVI ya calculado (y no las bandas antes del cociente) es lo
    adecuado cuando el objetivo es una estadistica zonal: el valor resultante
    es la media del indice en el area, que es justo lo que se quiere reportar.

    Args:
        array: raster de entrada con informacion de resolucion.
        target_resolution_m: tamano de pixel deseado, en metros.

    Returns:
        El raster degradado, o el original si ya es mas grosero que el objetivo.
    """
    current = abs(float(array.rio.resolution()[0]))
    factor = round(target_resolution_m / current)
    if factor <= 1:
        return array

    y_dim, x_dim = array.dims[-2], array.dims[-1]
    return (
        array.coarsen({y_dim: factor, x_dim: factor}, boundary="trim")
        .mean()
        .rio.write_crs(array.rio.crs)
    )

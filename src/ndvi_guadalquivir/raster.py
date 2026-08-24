"""Lectura de bandas por ventana espacial y por nivel de piramide.

La clave de eficiencia del proyecto esta aqui, y son dos optimizaciones que se
componen.

La primera es la lectura por ventana. Los productos Sentinel-2 se publican como
COG (Cloud Optimized GeoTIFF), un formato organizado en teselas internas que
permite pedir por HTTP unicamente los bytes correspondientes a una region
concreta. Leer una comarca cuesta unos pocos megabytes frente a los mas de
100 MB que ocupa una banda completa de 10 980 x 10 980 pixeles.

La segunda es la lectura por overview. Ademas de los pixeles a resolucion
nativa, un COG guarda una piramide de versiones reducidas del mismo raster.
Si el analisis trabaja a 100 m, pedir los pixeles de 10 m para promediarlos
despues es tirar el trabajo que el productor ya hizo: basta con pedir
directamente el nivel de la piramide cuya resolucion ya es suficiente. Medido
sobre una ventana de 35 x 33 km en la campina de Cordoba, la banda roja pasa de
48,8 MB y 16,1 s a 0,8 MB y 0,8 s.

Que esa segunda optimizacion sea licita depende de con que algoritmo se
construyeron las piramides, y eso no se supone: se consulta. Los productos de
la coleccion declaran `OVR_RESAMPLING_ALG` en sus metadatos, con dos valores
distintos que son justo los correctos:

    bandas espectrales (B04, B08)   AVERAGE   promedio, adecuado para reflectancia
    clasificacion de escena (SCL)   MODE      voto mayoritario, adecuado para clases

El voto mayoritario es lo que hace utilizable la piramide de SCL: promediar
clases produciria categorias inexistentes (la media de "nube" y "vegetacion" no
es ninguna clase), mientras que la moda devuelve siempre un valor del catalogo
original. Verificado leyendo la banda: en todos los niveles los valores siguen
siendo enteros del conjunto de clases valido.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import rasterio
import rioxarray
import xarray as xr
from rasterio.enums import Resampling
from rasterio.warp import transform_bounds

from .catalog import Scene
from .indices import compute_ndvi

logger = logging.getLogger(__name__)

#: Opciones de GDAL que evitan lecturas inutiles al abrir COG remotos.
#: `GDAL_DISABLE_READDIR_ON_OPEN` es la mas importante: sin ella GDAL lista el
#: directorio remoto entero antes de abrir un solo fichero.
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
    #: Tamano de pixel realmente obtenido, en metros. No tiene por que
    #: coincidir con el objetivo pedido: la piramide solo ofrece potencias de
    #: dos de la resolucion nativa, asi que para un objetivo de 100 m el nivel
    #: disponible mas proximo por debajo es 80 m.
    effective_resolution_m: float | None = None

    @property
    def values(self) -> np.ndarray:
        return self.data.values


def choose_overview_level(
    native_resolution_m: float,
    overview_factors: Sequence[int],
    target_resolution_m: float | None,
) -> int | None:
    """Elige el nivel de piramide mas grosero que no llegue a perder detalle.

    Se busca el mayor factor de decimacion cuya resolucion resultante siga
    siendo igual o mas fina que el objetivo. Nunca se elige un nivel mas
    grosero que el objetivo, porque eso obligaria despues a interpolar hacia
    arriba y a inventar detalle que ya no esta en el dato.

    Args:
        native_resolution_m: tamano de pixel del nivel de resolucion completa.
        overview_factors: factores de decimacion declarados por el fichero,
            en el orden en que los expone GDAL (tipicamente `[2, 4, 8, 16]`).
        target_resolution_m: resolucion de trabajo deseada. Si es `None` no se
            aplica reduccion alguna.

    Returns:
        Indice del nivel dentro de `overview_factors`, que es lo que esperan
        rasterio y rioxarray en su parametro `overview_level`, o `None` para
        leer a resolucion nativa.
    """
    if not target_resolution_m or native_resolution_m <= 0:
        return None

    chosen: int | None = None
    best_resolution = native_resolution_m
    for level, factor in enumerate(overview_factors):
        resolution = native_resolution_m * factor
        # `>` estricto: se descartan los niveles que ya superan el objetivo.
        if resolution > target_resolution_m:
            continue
        if resolution >= best_resolution:
            chosen, best_resolution = level, resolution

    return chosen


def _open_window(
    href: str,
    bounds_wgs84: tuple[float, float, float, float],
    *,
    target_resolution_m: float | None = None,
) -> xr.DataArray:
    """Abre un COG remoto por el nivel adecuado y recorta la ventana indicada.

    La resolucion nativa y los factores de la piramide son propiedades del
    fichero, no del proyecto, asi que se leen de su cabecera en lugar de
    darlos por sabidos. Eso mantiene el codigo valido si cambia la coleccion o
    si se apunta a otro proveedor. La consulta previa es gratis: GDAL conserva
    en cache los bloques ya descargados, de modo que la segunda apertura del
    mismo fichero cuesta milisegundos frente al segundo largo de la primera.

    Args:
        href: URL del COG.
        bounds_wgs84: `(oeste, sur, este, norte)` en grados.
        target_resolution_m: resolucion de trabajo con la que elegir el nivel.

    Returns:
        DataArray bidimensional con el recorte, en el CRS nativo del producto.
    """
    with rasterio.open(href) as dataset:
        native_resolution_m = abs(float(dataset.res[0]))
        overview_factors = dataset.overviews(1)

    level = choose_overview_level(
        native_resolution_m, overview_factors, target_resolution_m
    )
    if level is not None:
        logger.debug(
            "%s: nivel %d de %s (%.0f m -> %.0f m)",
            href.rsplit("/", 1)[-1], level, overview_factors,
            native_resolution_m, native_resolution_m * overview_factors[level],
        )

    array = rioxarray.open_rasterio(href, masked=True, overview_level=level)
    bounds_native = transform_bounds("EPSG:4326", array.rio.crs, *bounds_wgs84)
    return array.rio.clip_box(*bounds_native).squeeze(drop=True)


def read_ndvi(
    scene: Scene,
    bounds_wgs84: tuple[float, float, float, float],
    *,
    target_resolution_m: int | None = None,
) -> NdviRaster:
    """Calcula el NDVI de una escena sobre una ventana geografica.

    Cada banda elige su propio nivel de piramide, porque no parten de la misma
    resolucion nativa: las bandas espectrales se distribuyen a 10 m y la
    clasificacion de escena a 20 m. Al pedir a cada una el nivel adecuado para
    el mismo objetivo, ambas aterrizan en la misma resolucion y el
    remuestreo posterior se queda practicamente en nada. Aun asi se conserva,
    porque las mallas pueden diferir en el origen: se usa vecino mas proximo,
    que es lo unico correcto para un dato categorico.

    Args:
        scene: escena localizada en el catalogo.
        bounds_wgs84: ventana en grados `(oeste, sur, este, norte)`.
        target_resolution_m: si se indica, se lee por el nivel de piramide
            adecuado y, si aun queda un factor entero, se promedia hasta el
            tamano de pixel pedido.

    Returns:
        El NDVI como `NdviRaster`, con NaN en los pixeles invalidos.
    """
    with rioxarray.set_options(export_grid_mapping=False), rasterio.Env(**GDAL_ENV):
        red = _open_window(scene.red_href, bounds_wgs84,
                           target_resolution_m=target_resolution_m)
        nir = _open_window(scene.nir_href, bounds_wgs84,
                           target_resolution_m=target_resolution_m)
        scl = _open_window(scene.scl_href, bounds_wgs84,
                           target_resolution_m=target_resolution_m)

    # La clasificacion de escena, a la malla de las bandas espectrales.
    scl = scl.rio.reproject_match(red, resampling=Resampling.nearest)

    # El offset lo trae resuelto la escena: depende de su linea base de
    # procesado y de si el proveedor ya lo aplico dentro del fichero.
    ndvi_values = compute_ndvi(
        red.values, nir.values, scl.values, add_offset=scene.boa_offset
    )
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

    resolution = abs(float(ndvi.rio.resolution()[0]))
    logger.debug(
        "NDVI de %s: %s pixeles a %.0f m, %.1f%% validos",
        scene.item_id, ndvi.shape, resolution,
        100 * float(np.isfinite(ndvi.values).mean()),
    )
    return NdviRaster(scene=scene, data=ndvi, effective_resolution_m=resolution)


def downsample(array: xr.DataArray, target_resolution_m: int) -> xr.DataArray:
    """Reduce la resolucion promediando bloques de pixeles.

    Complementa a la lectura por overview: la piramide solo ofrece factores
    potencia de dos, asi que cubre el salto que quede entre el nivel elegido y
    el objetivo. Cuando ese salto es menor que un pixel, que es el caso
    habitual, no hace nada.

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

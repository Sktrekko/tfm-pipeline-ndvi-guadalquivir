"""Fixtures compartidas: construyen escenas y rasteres sinteticos para poder
testear la agregacion zonal sin depender del catalogo remoto."""

from __future__ import annotations

from datetime import UTC, datetime

import geopandas as gpd
import numpy as np
import pytest
import rioxarray  # noqa: F401
import xarray as xr
from affine import Affine
from shapely.geometry import box

from ndvi_guadalquivir.catalog import Scene
from ndvi_guadalquivir.raster import NdviRaster


@pytest.fixture
def scene() -> Scene:
    return Scene(
        item_id="S2A_T30SUH_20240601T105916_L2A",
        acquired_at=datetime(2024, 6, 1, 10, 59, 16, tzinfo=UTC),
        tile_id="30SUH",
        cloud_cover=1.5,
        epsg=32630,
        red_href="https://example.invalid/B04.tif",
        nir_href="https://example.invalid/B08.tif",
        scl_href="https://example.invalid/SCL.tif",
    )


def _grid(values: np.ndarray, *, origin=(0.0, 100.0), pixel=10.0) -> xr.DataArray:
    """Envuelve un array en un DataArray georreferenciado en EPSG:32630."""
    height, width = values.shape
    x = origin[0] + pixel * (np.arange(width) + 0.5)
    y = origin[1] - pixel * (np.arange(height) + 0.5)
    array = xr.DataArray(values, coords={"y": y, "x": x}, dims=("y", "x"), name="ndvi")
    array.rio.write_crs("EPSG:32630", inplace=True)
    array.rio.write_transform(Affine(pixel, 0, origin[0], 0, -pixel, origin[1]), inplace=True)
    return array


@pytest.fixture
def ndvi_raster(scene) -> NdviRaster:
    """Raster 10x10 con NDVI 0.8 en la mitad izquierda y 0.2 en la derecha."""
    values = np.full((10, 10), 0.2, dtype=np.float32)
    values[:, :5] = 0.8
    return NdviRaster(scene=scene, data=_grid(values))


@pytest.fixture
def zones() -> gpd.GeoDataFrame:
    """Dos zonas contiguas que parten el raster en dos mitades verticales."""
    return gpd.GeoDataFrame(
        {"zone_id": ["oeste", "este"], "zone_name": ["Zona Oeste", "Zona Este"]},
        geometry=[box(0, 0, 50, 100), box(50, 0, 100, 100)],
        crs="EPSG:32630",
    )


@pytest.fixture
def make_raster(scene):
    """Permite construir rasteres a medida dentro de un test."""
    def _factory(values: np.ndarray) -> NdviRaster:
        return NdviRaster(scene=scene, data=_grid(np.asarray(values, dtype=np.float32)))
    return _factory


@pytest.fixture
def make_mosaic(scene):
    """Construye un mosaico diario sintetico a partir de un array de NDVI."""
    from datetime import date as _date

    from ndvi_guadalquivir.mosaic import NdviMosaic

    def _factory(values, *, tiles=("30SUG", "30SUH"), day=_date(2024, 6, 1)):
        return NdviMosaic(
            acquisition_date=day,
            data=_grid(np.asarray(values, dtype=np.float32)),
            scene_ids=tuple(f"S2A_T{t}_20240601T105916_L2A" for t in tiles),
            tile_ids=tuple(tiles),
            mean_cloud_cover=1.5,
        )
    return _factory


@pytest.fixture
def ndvi_mosaic(make_mosaic):
    """Mosaico 10x10 con NDVI 0.8 al oeste y 0.2 al este."""
    values = np.full((10, 10), 0.2, dtype=np.float32)
    values[:, :5] = 0.8
    return make_mosaic(values)

"""Tests de la lectura por ventana y por nivel de piramide.

La eleccion de nivel es una funcion pura sobre tres numeros, asi que la mayor
parte se cubre sin tocar disco ni red. Para la parte que si abre ficheros se
fabrica un COG sintetico con piramides reales, que es la unica forma de
comprobar que el nivel elegido llega efectivamente a GDAL.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest
import rasterio
import rioxarray  # noqa: F401
import xarray as xr
from affine import Affine
from rasterio.enums import Resampling as RioResampling
from rasterio.warp import transform_bounds

from ndvi_guadalquivir.catalog import Scene
from ndvi_guadalquivir.raster import (
    _open_window,
    choose_overview_level,
    downsample,
    read_ndvi,
)

#: Piramide tipica de un producto Sentinel-2 en la coleccion de trabajo.
SENTINEL_FACTORS = [2, 4, 8, 16]


class TestChooseOverviewLevel:
    """Eleccion del nivel de piramide."""

    def test_banda_espectral_a_cien_metros(self):
        """Las bandas de 10 m resuelven en el nivel 2, que son 80 m.

        Es el caso central del proyecto: 80 m es el nivel mas grosero que
        todavia no supera el objetivo de 100 m.
        """
        assert choose_overview_level(10.0, SENTINEL_FACTORS, 100.0) == 2

    def test_clasificacion_de_escena_a_cien_metros(self):
        """SCL parte de 20 m, asi que llega a los mismos 80 m un nivel antes."""
        assert choose_overview_level(20.0, SENTINEL_FACTORS, 100.0) == 1

    def test_las_dos_bandas_aterrizan_en_la_misma_resolucion(self):
        """Que ambas coincidan es lo que hace casi gratuito el remuestreo."""
        espectral = choose_overview_level(10.0, SENTINEL_FACTORS, 100.0)
        categorica = choose_overview_level(20.0, SENTINEL_FACTORS, 100.0)
        assert 10.0 * SENTINEL_FACTORS[espectral] == 20.0 * SENTINEL_FACTORS[categorica]

    def test_nunca_elige_un_nivel_mas_grosero_que_el_objetivo(self):
        """Pasarse de grosero obligaria a interpolar hacia arriba.

        Con objetivo de 15 m el primer nivel ya son 20 m, demasiado: la
        respuesta correcta es leer a resolucion nativa.
        """
        assert choose_overview_level(10.0, SENTINEL_FACTORS, 15.0) is None

    def test_objetivo_exacto_en_un_nivel(self):
        """Si el objetivo cae justo en un nivel, se usa ese y no el anterior."""
        assert choose_overview_level(10.0, SENTINEL_FACTORS, 40.0) == 1

    def test_sin_objetivo_lee_nativo(self):
        assert choose_overview_level(10.0, SENTINEL_FACTORS, None) is None

    def test_fichero_sin_piramide(self):
        """Un GeoTIFF plano no ofrece niveles: hay que leerlo entero."""
        assert choose_overview_level(10.0, [], 100.0) is None

    def test_objetivo_muy_grosero_toma_el_ultimo_nivel(self):
        """Con un objetivo de 1 km el nivel util es el mas reducido que haya."""
        assert choose_overview_level(10.0, SENTINEL_FACTORS, 1000.0) == 3

    def test_factores_desordenados(self):
        """La eleccion depende de la resolucion, no del orden de la lista."""
        assert choose_overview_level(10.0, [8, 2, 4], 100.0) == 0

    def test_resolucion_nativa_invalida(self):
        """Una cabecera sin resolucion util no debe romper la lectura."""
        assert choose_overview_level(0.0, SENTINEL_FACTORS, 100.0) is None


# --------------------------------------------------------------------------
# COG sintetico
# --------------------------------------------------------------------------

#: Esquina superior izquierda de la ventana de pruebas, en UTM 30N sobre la
#: campina de Cordoba. Se usan coordenadas reales para que la reproyeccion a
#: grados que hace el codigo sea la misma que en produccion.
_ORIGIN_X, _ORIGIN_Y = 340_000.0, 4_190_000.0
_CRS = "EPSG:32630"


def _write_cog(path, values, pixel_size, resampling):
    """Escribe un GeoTIFF con piramides construidas como en el producto real."""
    height, width = values.shape
    transform = Affine(pixel_size, 0, _ORIGIN_X, 0, -pixel_size, _ORIGIN_Y)
    profile = {
        "driver": "GTiff", "height": height, "width": width, "count": 1,
        "dtype": values.dtype.name, "crs": _CRS, "transform": transform,
        "tiled": True, "blockxsize": 128, "blockysize": 128,
    }
    with rasterio.open(path, "w", **profile) as dataset:
        dataset.write(values, 1)
        dataset.build_overviews(SENTINEL_FACTORS, resampling)
        dataset.update_tags(ns="rio_overview", resampling=resampling.name)
    return str(path)


@pytest.fixture
def synthetic_scene(tmp_path):
    """Escena completa en disco: rojo y NIR a 10 m, SCL a 20 m, con piramides.

    El NDVI esperado es constante y conocido, de forma que cualquier nivel de
    piramide debe devolver el mismo valor: promediar bandas uniformes no
    cambia nada. Eso permite comprobar la resolucion sin que el valor estorbe.
    """
    size = 512
    # Reflectancia 0,10 en rojo y 0,50 en NIR con el offset de la linea base
    # 04.00 aplicado al reves, para que el codigo lo deshaga y salga NDVI 2/3.
    red = np.full((size, size), 0.10 * 10_000 + 1_000, dtype=np.uint16)
    nir = np.full((size, size), 0.50 * 10_000 + 1_000, dtype=np.uint16)
    scl = np.full((size // 2, size // 2), 4, dtype=np.uint8)  # todo vegetacion

    return Scene(
        item_id="S2A_T30SUH_20240601T105916_L2A",
        acquired_at=datetime(2024, 6, 1, 10, 59, 16, tzinfo=UTC),
        tile_id="30SUH",
        cloud_cover=1.5,
        epsg=32630,
        red_href=_write_cog(tmp_path / "red.tif", red, 10.0, RioResampling.average),
        nir_href=_write_cog(tmp_path / "nir.tif", nir, 10.0, RioResampling.average),
        scl_href=_write_cog(tmp_path / "scl.tif", scl, 20.0, RioResampling.mode),
    )


@pytest.fixture
def synthetic_bounds():
    """Ventana de lectura en grados, centrada en el COG sintetico."""
    side = 512 * 10.0
    return transform_bounds(
        _CRS, "EPSG:4326",
        _ORIGIN_X + 0.1 * side, _ORIGIN_Y - 0.9 * side,
        _ORIGIN_X + 0.9 * side, _ORIGIN_Y - 0.1 * side,
    )


class TestOpenWindow:
    """Apertura de un COG por el nivel adecuado."""

    def test_sin_objetivo_devuelve_resolucion_nativa(self, synthetic_scene, synthetic_bounds):
        array = _open_window(synthetic_scene.red_href, synthetic_bounds)
        assert abs(float(array.rio.resolution()[0])) == pytest.approx(10.0)

    def test_objetivo_de_cien_metros_lee_a_ochenta(self, synthetic_scene, synthetic_bounds):
        """La piramide solo ofrece potencias de dos: 80 m es el nivel util."""
        array = _open_window(
            synthetic_scene.red_href, synthetic_bounds, target_resolution_m=100.0
        )
        assert abs(float(array.rio.resolution()[0])) == pytest.approx(80.0)

    def test_scl_alcanza_la_misma_resolucion_desde_veinte_metros(
        self, synthetic_scene, synthetic_bounds
    ):
        array = _open_window(
            synthetic_scene.scl_href, synthetic_bounds, target_resolution_m=100.0
        )
        assert abs(float(array.rio.resolution()[0])) == pytest.approx(80.0)

    def test_la_piramide_de_scl_conserva_clases_enteras(
        self, synthetic_scene, synthetic_bounds
    ):
        """El voto mayoritario nunca inventa una clase que no existiera.

        Es la condicion que hace licito leer la mascara de calidad por
        overview: con promediado saldrian valores como 4,7, que no
        corresponden a ninguna categoria del catalogo SCL.
        """
        array = _open_window(
            synthetic_scene.scl_href, synthetic_bounds, target_resolution_m=100.0
        )
        values = array.values[np.isfinite(array.values)]
        assert np.array_equal(values, np.round(values))

    def test_leer_por_overview_mueve_muchos_menos_pixeles(
        self, synthetic_scene, synthetic_bounds
    ):
        """El ahorro es cuadratico en el factor de decimacion."""
        nativo = _open_window(synthetic_scene.red_href, synthetic_bounds)
        reducido = _open_window(
            synthetic_scene.red_href, synthetic_bounds, target_resolution_m=100.0
        )
        assert nativo.size > 50 * reducido.size


class TestReadNdvi:
    """Calculo de NDVI de extremo a extremo sobre el COG sintetico."""

    def test_valor_correcto_a_resolucion_nativa(self, synthetic_scene, synthetic_bounds):
        """Reflectancias 0,10 y 0,50 dan un NDVI de 0,4/0,6."""
        raster = read_ndvi(synthetic_scene, synthetic_bounds)
        assert float(np.nanmean(raster.values)) == pytest.approx(2 / 3, abs=1e-3)

    def test_el_overview_no_altera_el_valor(self, synthetic_scene, synthetic_bounds):
        """Sobre un campo uniforme, leer reducido debe dar lo mismo.

        Es la comprobacion de que la optimizacion no introduce sesgo por si
        misma: toda diferencia que aparezca en datos reales vendra de la
        heterogeneidad del terreno, no del mecanismo de lectura.
        """
        nativo = read_ndvi(synthetic_scene, synthetic_bounds)
        reducido = read_ndvi(
            synthetic_scene, synthetic_bounds, target_resolution_m=100
        )
        assert float(np.nanmean(reducido.values)) == pytest.approx(
            float(np.nanmean(nativo.values)), abs=1e-4
        )

    def test_registra_la_resolucion_realmente_obtenida(
        self, synthetic_scene, synthetic_bounds
    ):
        """El raster declara 80 m, no los 100 m pedidos.

        Guardar la resolucion efectiva evita que aguas abajo se documente una
        precision que el dato no tiene.
        """
        raster = read_ndvi(
            synthetic_scene, synthetic_bounds, target_resolution_m=100
        )
        assert raster.effective_resolution_m == pytest.approx(80.0)

    def test_la_mascara_scl_se_aplica(self, tmp_path, synthetic_scene, synthetic_bounds):
        """Una escena clasificada como nube alta no deja ningun pixel valido."""
        nublada = np.full((256, 256), 9, dtype=np.uint8)  # CLOUD_HIGH_PROBABILITY
        scene = Scene(
            **{
                **synthetic_scene.__dict__,
                "scl_href": _write_cog(
                    tmp_path / "scl_nube.tif", nublada, 20.0, RioResampling.mode
                ),
            }
        )
        raster = read_ndvi(scene, synthetic_bounds)
        assert not np.isfinite(raster.values).any()


class TestDownsample:
    """Promediado de bloques posterior a la lectura."""

    def _grid(self, values, pixel):
        height, width = values.shape
        array = xr.DataArray(
            values,
            coords={
                "y": _ORIGIN_Y - pixel * (np.arange(height) + 0.5),
                "x": _ORIGIN_X + pixel * (np.arange(width) + 0.5),
            },
            dims=("y", "x"),
            name="ndvi",
        )
        array.rio.write_crs(_CRS, inplace=True)
        array.rio.write_transform(
            Affine(pixel, 0, _ORIGIN_X, 0, -pixel, _ORIGIN_Y), inplace=True
        )
        return array

    def test_promedia_bloques_completos(self):
        values = np.arange(16, dtype=np.float32).reshape(4, 4)
        reducido = downsample(self._grid(values, 10.0), 20)
        assert reducido.shape == (2, 2)
        # El bloque superior izquierdo son los valores 0, 1, 4 y 5.
        assert float(reducido.values[0, 0]) == pytest.approx(2.5)

    def test_no_toca_un_raster_ya_mas_grosero(self):
        """El caso habitual tras leer por overview: no queda nada que hacer."""
        grid = self._grid(np.ones((4, 4), dtype=np.float32), 80.0)
        assert downsample(grid, 100) is grid

    def test_descarta_el_resto_que_no_completa_un_bloque(self):
        """Con `boundary='trim'` no se promedian bloques incompletos.

        Un bloque a medias tendria menos pixeles y su media seria menos fiable
        que la del resto, asi que se prefiere perder el borde.
        """
        grid = self._grid(np.ones((5, 5), dtype=np.float32), 10.0)
        assert downsample(grid, 20).shape == (2, 2)

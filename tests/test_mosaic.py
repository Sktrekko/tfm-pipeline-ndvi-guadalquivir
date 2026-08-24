"""Tests del mosaico diario."""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from ndvi_guadalquivir.mosaic import dominant_crs, merge_rasters


class TestDominantCrs:
    def test_toma_el_epsg_mayoritario(self, scene):
        escenas = [
            dataclasses.replace(scene, epsg=32630),
            dataclasses.replace(scene, epsg=32630),
            dataclasses.replace(scene, epsg=32629),
        ]
        assert dominant_crs(escenas) == "EPSG:32630"

    def test_una_sola_escena(self, scene):
        assert dominant_crs([scene]) == f"EPSG:{scene.epsg}"

    def test_desempate_determinista(self, scene):
        """Con igual frecuencia debe elegir siempre el mismo, no al azar."""
        escenas = [
            dataclasses.replace(scene, epsg=32630),
            dataclasses.replace(scene, epsg=32629),
        ]
        assert dominant_crs(escenas) == dominant_crs(list(reversed(escenas)))

    def test_lista_vacia_lanza_error(self):
        with pytest.raises(ValueError, match="No hay escenas"):
            dominant_crs([])


class TestMergeRasters:
    def test_un_solo_raster_se_devuelve_tal_cual(self, ndvi_raster):
        assert merge_rasters([ndvi_raster]) is ndvi_raster.data

    def test_rellena_los_huecos_del_primero_con_el_segundo(self, make_raster):
        """El caso real: dos tiles adyacentes que se complementan."""
        oeste = np.full((10, 10), np.nan, dtype=np.float32)
        oeste[:, :5] = 0.8
        este = np.full((10, 10), np.nan, dtype=np.float32)
        este[:, 5:] = 0.2

        combinado = merge_rasters([make_raster(oeste), make_raster(este)]).values
        assert np.isfinite(combinado).all()
        assert combinado[:, :5] == pytest.approx(0.8)
        assert combinado[:, 5:] == pytest.approx(0.2)

    def test_promedia_la_franja_de_solape(self, make_raster):
        a = np.full((10, 10), 0.4, dtype=np.float32)
        b = np.full((10, 10), 0.6, dtype=np.float32)
        combinado = merge_rasters([make_raster(a), make_raster(b)]).values
        assert combinado == pytest.approx(0.5, abs=1e-5)

    def test_los_huecos_comunes_siguen_siendo_nulos(self, make_raster):
        hueco = np.full((10, 10), np.nan, dtype=np.float32)
        hueco[0, 0] = 0.5
        combinado = merge_rasters([make_raster(hueco), make_raster(hueco)]).values
        assert np.isfinite(combinado[0, 0])
        assert np.isnan(combinado[1:, 1:]).all()

    def test_promedia_bien_muchas_escenas(self, make_raster):
        """El resultado no depende de cuantas escenas entren.

        La media se acumula escena a escena en lugar de apilarlas todas, que
        es lo que mantiene la memoria constante durante la carga historica.
        Este test fija que esa forma de calcularla sigue dando la media exacta.
        """
        valores = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
        rasteres = [make_raster(np.full((10, 10), v, dtype=np.float32)) for v in valores]
        combinado = merge_rasters(rasteres).values
        assert combinado == pytest.approx(float(np.mean(valores)), abs=1e-5)

    def test_cada_pixel_promedia_solo_sus_aportaciones(self, make_raster):
        """Un hueco en una escena no debe rebajar la media de ese pixel.

        La columna izquierda la ve una sola escena y la derecha las dos, asi
        que cada una tiene que promediarse sobre un numero distinto de valores.
        """
        una = np.full((4, 4), 0.6, dtype=np.float32)
        otra = np.full((4, 4), 0.2, dtype=np.float32)
        otra[:, :2] = np.nan
        combinado = merge_rasters([make_raster(una), make_raster(otra)]).values
        assert combinado[:, :2] == pytest.approx(0.6)
        assert combinado[:, 2:] == pytest.approx(0.4)

    def test_conserva_el_sistema_de_referencia(self, ndvi_raster, make_raster):
        otro = make_raster(np.full((10, 10), 0.3, dtype=np.float32))
        combinado = merge_rasters([ndvi_raster, otro])
        assert combinado.rio.crs is not None
        assert str(combinado.rio.crs) == str(ndvi_raster.data.rio.crs)

    def test_sin_rasteres_lanza_error(self):
        with pytest.raises(ValueError, match="al menos un raster"):
            merge_rasters([])


class TestBuildTargetGrid:
    """La malla canonica es lo que garantiza que el mosaico cubra toda la zona
    de estudio y que todas las fechas sean comparables entre si."""

    def test_cubre_toda_la_zona_pedida(self):
        from rasterio.warp import transform_bounds

        from ndvi_guadalquivir.mosaic import build_target_grid

        bounds = (-4.90, 37.80, -4.70, 37.95)
        grid = build_target_grid(bounds, "EPSG:32630", 100)
        esperado = transform_bounds("EPSG:4326", "EPSG:32630", *bounds)
        oeste, sur, este, norte = grid.rio.bounds()

        assert oeste <= esperado[0] and sur <= esperado[1]
        assert este >= esperado[2] and norte >= esperado[3]

    def test_resolucion_solicitada(self):
        from ndvi_guadalquivir.mosaic import build_target_grid

        grid = build_target_grid((-4.9, 37.8, -4.7, 37.95), "EPSG:32630", 100)
        assert abs(grid.rio.resolution()[0]) == pytest.approx(100.0)

    def test_arranca_vacia(self):
        from ndvi_guadalquivir.mosaic import build_target_grid

        grid = build_target_grid((-4.9, 37.8, -4.7, 37.95), "EPSG:32630", 100)
        assert np.isnan(grid.values).all()

    def test_es_reproducible_entre_llamadas(self):
        """Dos fechas distintas deben caer sobre pixeles identicos."""
        from ndvi_guadalquivir.mosaic import build_target_grid

        bounds = (-4.9, 37.8, -4.7, 37.95)
        a = build_target_grid(bounds, "EPSG:32630", 100)
        b = build_target_grid(bounds, "EPSG:32630", 100)
        assert a.shape == b.shape
        assert a.rio.bounds() == b.rio.bounds()

    def test_ventanas_ligeramente_distintas_quedan_alineadas(self):
        """El anclaje a multiplos de la resolucion evita desplazamientos."""
        from ndvi_guadalquivir.mosaic import build_target_grid

        a = build_target_grid((-4.900, 37.80, -4.70, 37.95), "EPSG:32630", 100)
        b = build_target_grid((-4.899, 37.80, -4.70, 37.95), "EPSG:32630", 100)
        desfase_x = (a.rio.bounds()[0] - b.rio.bounds()[0]) % 100
        assert desfase_x == pytest.approx(0.0, abs=1e-6)

    def test_el_mosaico_sobre_malla_canonica_conserva_zonas_de_ambos_tiles(self, make_raster):
        """Regresion: con la malla del primer tile se perdia media zona."""
        from ndvi_guadalquivir.mosaic import merge_rasters

        norte = np.full((10, 10), np.nan, dtype=np.float32)
        norte[:5, :] = 0.7
        sur = np.full((10, 10), np.nan, dtype=np.float32)
        sur[5:, :] = 0.3

        combinado = merge_rasters([make_raster(norte), make_raster(sur)])
        assert np.isfinite(combinado.values).all()

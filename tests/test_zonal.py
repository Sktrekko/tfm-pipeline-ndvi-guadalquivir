"""Tests de la estadistica zonal."""

from __future__ import annotations

import numpy as np
import pytest

from ndvi_guadalquivir.zonal import (
    DAILY_COLUMNS,
    SCENE_COLUMNS,
    zonal_ndvi_daily,
    zonal_ndvi_scene,
)


class TestZonalNdviScene:
    def test_una_fila_por_zona(self, ndvi_raster, zones):
        resultado = zonal_ndvi_scene(ndvi_raster, zones)
        assert len(resultado) == 2
        assert set(resultado["zone_id"]) == {"oeste", "este"}

    def test_las_medias_corresponden_a_cada_mitad(self, ndvi_raster, zones):
        resultado = zonal_ndvi_scene(ndvi_raster, zones).set_index("zone_id")
        assert resultado.loc["oeste", "ndvi_mean"] == pytest.approx(0.8, abs=1e-5)
        assert resultado.loc["este", "ndvi_mean"] == pytest.approx(0.2, abs=1e-5)

    def test_arrastra_los_metadatos_de_la_escena(self, ndvi_raster, zones, scene):
        resultado = zonal_ndvi_scene(ndvi_raster, zones)
        assert (resultado["scene_id"] == scene.item_id).all()
        assert (resultado["tile_id"] == scene.tile_id).all()

    def test_esquema_de_columnas_estable(self, ndvi_raster, zones):
        assert list(zonal_ndvi_scene(ndvi_raster, zones).columns) == SCENE_COLUMNS

    def test_zona_demasiado_nublada_se_descarta(self, make_raster, zones):
        """Si la mitad oeste esta tapada, solo debe salir la zona este."""
        values = np.full((10, 10), 0.5, dtype=np.float32)
        values[:, :5] = np.nan
        resultado = zonal_ndvi_scene(make_raster(values), zones, min_valid_pixel_fraction=0.30)
        assert resultado["zone_id"].tolist() == ["este"]

    def test_umbral_permisivo_conserva_la_zona_parcial(self, make_raster, zones):
        values = np.full((10, 10), 0.5, dtype=np.float32)
        values[:, :4] = np.nan          # 20% de pixeles validos en el oeste
        resultado = zonal_ndvi_scene(make_raster(values), zones, min_valid_pixel_fraction=0.10)
        assert set(resultado["zone_id"]) == {"oeste", "este"}

    def test_raster_totalmente_nulo_devuelve_tabla_vacia_con_esquema(self, make_raster, zones):
        vacio = np.full((10, 10), np.nan, dtype=np.float32)
        resultado = zonal_ndvi_scene(make_raster(vacio), zones)
        assert resultado.empty
        assert list(resultado.columns) == SCENE_COLUMNS

    def test_percentiles_ordenados(self, ndvi_raster, zones):
        resultado = zonal_ndvi_scene(ndvi_raster, zones)
        assert (resultado["ndvi_p10"] <= resultado["ndvi_p90"]).all()

    def test_conteo_de_pixeles_coherente(self, ndvi_raster, zones):
        resultado = zonal_ndvi_scene(ndvi_raster, zones)
        assert (resultado["valid_pixel_count"] <= resultado["pixel_count"]).all()
        assert (resultado["pixel_count"] == 50).all()

    def test_zona_fuera_del_raster_no_genera_fila(self, ndvi_raster, zones):
        import geopandas as gpd
        from shapely.geometry import box
        lejos = gpd.GeoDataFrame(
            {"zone_id": ["fuera"], "zone_name": ["Fuera de rango"]},
            geometry=[box(10_000, 10_000, 10_100, 10_100)], crs="EPSG:32630",
        )
        assert zonal_ndvi_scene(ndvi_raster, lejos).empty


class TestZonalNdviDaily:
    """La granularidad de salida del pipeline: una fila por zona y fecha."""

    def test_una_fila_por_zona(self, ndvi_mosaic, zones):
        resultado = zonal_ndvi_daily(ndvi_mosaic, zones)
        assert len(resultado) == 2

    def test_esquema_de_columnas_estable(self, ndvi_mosaic, zones):
        assert list(zonal_ndvi_daily(ndvi_mosaic, zones).columns) == DAILY_COLUMNS

    def test_conserva_la_trazabilidad_de_los_granulos(self, ndvi_mosaic, zones):
        resultado = zonal_ndvi_daily(ndvi_mosaic, zones)
        assert (resultado["tile_ids"] == "30SUG,30SUH").all()
        assert (resultado["scene_count"] == 2).all()

    def test_no_hay_zonas_duplicadas_en_una_fecha(self, ndvi_mosaic, zones):
        resultado = zonal_ndvi_daily(ndvi_mosaic, zones)
        assert not resultado.duplicated(subset=["zone_id", "acquisition_date"]).any()

    def test_el_mosaico_cubre_zonas_que_una_escena_sola_no_cubriria(
        self, make_mosaic, make_raster, zones
    ):
        """Motivo de ser del mosaico: un tile deja media zona sin dato."""
        import numpy as np

        solo_oeste = np.full((10, 10), np.nan, dtype=np.float32)
        solo_oeste[:, :5] = 0.7
        completo = np.full((10, 10), 0.7, dtype=np.float32)

        por_escena = zonal_ndvi_scene(make_raster(solo_oeste), zones)
        por_mosaico = zonal_ndvi_daily(make_mosaic(completo), zones)

        assert len(por_escena) == 1     # solo la zona oeste tiene dato
        assert len(por_mosaico) == 2    # el mosaico cubre las dos

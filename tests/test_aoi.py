"""Tests de la carga y validacion de la capa de zonas."""

from __future__ import annotations

import geopandas as gpd
import pytest
from shapely.geometry import box

from ndvi_guadalquivir.aoi import (
    GUADALQUIVIR_BBOX,
    basin_bbox,
    basin_geometry,
    bounds_of,
    load_zones,
)


@pytest.fixture
def capa(tmp_path):
    """Escribe una capa de zonas valida en disco y devuelve su ruta."""
    def _write(zones: gpd.GeoDataFrame, name="zonas.geojson"):
        path = tmp_path / name
        zones.to_file(path, driver="GeoJSON")
        return path
    return _write


@pytest.fixture
def zonas_validas():
    return gpd.GeoDataFrame(
        {"zone_id": ["14021", "41091"], "zone_name": ["Cordoba", "Sevilla"]},
        geometry=[box(-4.85, 37.85, -4.75, 37.90), box(-6.00, 37.35, -5.90, 37.40)],
        crs="EPSG:4326",
    )


class TestBasin:
    def test_la_caja_de_la_cuenca_es_coherente(self):
        oeste, sur, este, norte = basin_bbox()
        assert oeste < este and sur < norte

    def test_la_cuenca_cubre_cordoba_y_sevilla(self):
        oeste, sur, este, norte = GUADALQUIVIR_BBOX
        for lon, lat in [(-4.78, 37.89), (-5.99, 37.39), (-3.79, 37.77)]:
            assert oeste <= lon <= este and sur <= lat <= norte

    def test_la_geometria_de_la_cuenca_es_una_sola_zona(self):
        cuenca = basin_geometry()
        assert len(cuenca) == 1
        assert cuenca.crs.to_string() == "EPSG:4326"


class TestLoadZones:
    def test_carga_una_capa_valida(self, capa, zonas_validas):
        zonas = load_zones(capa(zonas_validas))
        assert len(zonas) == 2
        assert list(zonas.columns) == ["zone_id", "zone_name", "geometry"]

    def test_reproyecta_a_coordenadas_geodesicas(self, capa, zonas_validas):
        zonas = load_zones(capa(zonas_validas.to_crs("EPSG:25830")))
        assert zonas.crs.to_string() == "EPSG:4326"

    def test_el_identificador_se_normaliza_a_texto(self, capa, zonas_validas):
        numericas = zonas_validas.assign(zone_id=[14021, 41091])
        assert load_zones(capa(numericas))["zone_id"].tolist() == ["14021", "41091"]

    def test_descarta_zonas_fuera_de_la_cuenca(self, capa, zonas_validas):
        import pandas as pd
        fuera = gpd.GeoDataFrame(
            {"zone_id": ["28079"], "zone_name": ["Madrid"]},
            geometry=[box(-3.75, 40.40, -3.65, 40.45)], crs="EPSG:4326",
        )
        mezcla = gpd.GeoDataFrame(
            pd.concat([zonas_validas, fuera], ignore_index=True), crs="EPSG:4326"
        )
        assert "28079" not in load_zones(capa(mezcla))["zone_id"].tolist()

    def test_se_puede_desactivar_el_recorte(self, capa, zonas_validas):
        zonas = load_zones(capa(zonas_validas), clip_to_basin=False)
        assert len(zonas) == 2

    def test_fichero_inexistente_da_mensaje_util(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="zones download"):
            load_zones(tmp_path / "no-existe.geojson")

    def test_faltan_columnas_obligatorias(self, capa, zonas_validas):
        sin_nombre = zonas_validas.drop(columns=["zone_name"])
        with pytest.raises(ValueError, match="zone_name"):
            load_zones(capa(sin_nombre))

    def test_capa_enteramente_fuera_de_la_cuenca(self, capa):
        madrid = gpd.GeoDataFrame(
            {"zone_id": ["28079"], "zone_name": ["Madrid"]},
            geometry=[box(-3.75, 40.40, -3.65, 40.45)], crs="EPSG:4326",
        )
        with pytest.raises(ValueError, match="no contiene zonas"):
            load_zones(capa(madrid))


class TestBoundsOf:
    def test_envuelve_todas_las_zonas(self, zonas_validas):
        oeste, sur, este, norte = bounds_of(zonas_validas, buffer_deg=0.0)
        total = zonas_validas.total_bounds
        assert (oeste, sur, este, norte) == pytest.approx(tuple(total))

    def test_el_margen_ensancha_la_caja(self, zonas_validas):
        sin = bounds_of(zonas_validas, buffer_deg=0.0)
        con = bounds_of(zonas_validas, buffer_deg=0.05)
        assert con[0] < sin[0] and con[2] > sin[2]

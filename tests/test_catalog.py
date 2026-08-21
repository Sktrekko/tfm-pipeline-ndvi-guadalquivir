"""Tests de la traduccion de items STAC al modelo interno.

Se construyen items sinteticos en lugar de llamar al catalogo real, de modo
que la suite es determinista y no depende de la red.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pystac import Asset, Item

from ndvi_guadalquivir.catalog import Scene, group_by_date


def _item(**overrides) -> Item:
    """Item STAC minimo equivalente a los que devuelve Element84."""
    props = {
        "eo:cloud_cover": 3.4,
        "proj:epsg": 32630,
        "grid:code": "MGRS-30SUH",
        **overrides.pop("properties", {}),
    }
    item = Item(
        id=overrides.pop("id", "S2A_T30SUH_20240601T105916_L2A"),
        geometry=None,
        bbox=[-5.0, 37.5, -4.0, 38.5],
        datetime=overrides.pop("datetime", datetime(2024, 6, 1, 10, 59, tzinfo=timezone.utc)),
        properties=props,
    )
    for name in overrides.pop("assets", ["red", "nir", "scl"]):
        item.add_asset(name, Asset(href=f"https://example.invalid/{name}.tif"))
    return item


class TestSceneFromStacItem:
    def test_extrae_los_campos_esperados(self):
        scene = Scene.from_stac_item(_item())
        assert scene.item_id == "S2A_T30SUH_20240601T105916_L2A"
        assert scene.tile_id == "30SUH"
        assert scene.epsg == 32630
        assert scene.cloud_cover == pytest.approx(3.4)

    def test_apunta_a_las_tres_bandas(self):
        scene = Scene.from_stac_item(_item())
        assert scene.red_href.endswith("red.tif")
        assert scene.nir_href.endswith("nir.tif")
        assert scene.scl_href.endswith("scl.tif")

    def test_quita_el_prefijo_mgrs_del_tile(self):
        assert Scene.from_stac_item(_item()).tile_id == "30SUH"

    def test_compone_el_tile_desde_sus_partes_si_falta_grid_code(self):
        item = _item(properties={
            "grid:code": None, "mgrs:utm_zone": 30,
            "mgrs:latitude_band": "S", "mgrs:grid_square": "UG",
        })
        assert Scene.from_stac_item(item).tile_id == "30SUG"

    def test_acepta_proj_code_en_lugar_de_proj_epsg(self):
        item = _item(properties={"proj:epsg": None, "proj:code": "EPSG:32629"})
        assert Scene.from_stac_item(item).epsg == 32629

    def test_falta_una_banda_lanza_error_explicito(self):
        with pytest.raises(KeyError, match="scl"):
            Scene.from_stac_item(_item(assets=["red", "nir"]))

    def test_la_fecha_de_adquisicion_deriva_del_instante(self):
        scene = Scene.from_stac_item(_item())
        assert scene.acquisition_date.isoformat() == "2024-06-01"

    def test_es_inmutable(self):
        """`Scene` es un dataclass congelado: nadie muta una escena por error."""
        scene = Scene.from_stac_item(_item())
        with pytest.raises(Exception):
            scene.cloud_cover = 99.0


class TestGroupByDate:
    def test_agrupa_los_tiles_de_una_misma_pasada(self):
        escenas = [
            Scene.from_stac_item(_item(id="a", properties={"grid:code": "MGRS-30SUH"})),
            Scene.from_stac_item(_item(id="b", properties={"grid:code": "MGRS-30SUG"})),
            Scene.from_stac_item(_item(
                id="c",
                datetime=datetime(2024, 6, 6, 10, 59, tzinfo=timezone.utc),
            )),
        ]
        grupos = dict(group_by_date(escenas))
        assert len(grupos) == 2
        assert len(grupos[escenas[0].acquisition_date]) == 2

    def test_devuelve_las_fechas_en_orden(self):
        escenas = [
            Scene.from_stac_item(_item(
                id=str(day),
                datetime=datetime(2024, 6, day, 10, 0, tzinfo=timezone.utc),
            ))
            for day in (10, 3, 7)
        ]
        fechas = [day for day, _ in group_by_date(escenas)]
        assert fechas == sorted(fechas)

    def test_sin_escenas_no_produce_grupos(self):
        assert list(group_by_date([])) == []


@pytest.mark.integration
class TestBusquedaReal:
    """Contrato con el catalogo remoto. Se ejecuta con `pytest -m integration`."""

    def test_encuentra_escenas_sobre_el_guadalquivir(self):
        from ndvi_guadalquivir.catalog import search_scenes

        escenas = search_scenes(
            (-4.90, 37.80, -4.70, 37.95), "2024-06-01", "2024-06-30",
            max_cloud_cover=10,
        )
        assert escenas, "el catalogo deberia devolver escenas para junio de 2024"
        assert all(s.cloud_cover < 10 for s in escenas)
        assert all(s.red_href.startswith("https://") for s in escenas)

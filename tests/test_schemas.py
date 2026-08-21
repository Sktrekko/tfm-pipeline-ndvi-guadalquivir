"""Tests del contrato de datos: comprueban que el esquema rechaza lo que debe."""

from __future__ import annotations

import pandas as pd
import pandera.errors
import pytest

from ndvi_guadalquivir.schemas import DailyZonalNdviSchema, SceneZonalNdviSchema
from ndvi_guadalquivir.zonal import zonal_ndvi_daily, zonal_ndvi_scene


@pytest.fixture
def tabla_valida(ndvi_mosaic, zones) -> pd.DataFrame:
    return zonal_ndvi_daily(ndvi_mosaic, zones)


class TestDailyZonalNdviSchema:
    def test_la_salida_real_del_pipeline_valida(self, tabla_valida):
        """El contrato y el productor de datos no se han desincronizado."""
        DailyZonalNdviSchema.validate(tabla_valida)

    def test_rechaza_ndvi_fuera_de_rango(self, tabla_valida):
        corrupta = tabla_valida.copy()
        corrupta.loc[0, "ndvi_mean"] = 1.5
        with pytest.raises(pandera.errors.SchemaError):
            DailyZonalNdviSchema.validate(corrupta)

    def test_rechaza_ndvi_muy_negativo(self, tabla_valida):
        corrupta = tabla_valida.copy()
        corrupta.loc[0, "ndvi_mean"] = -115.0     # el bug del prototipo
        with pytest.raises(pandera.errors.SchemaError):
            DailyZonalNdviSchema.validate(corrupta)

    def test_rechaza_percentiles_invertidos(self, tabla_valida):
        corrupta = tabla_valida.copy()
        corrupta.loc[0, ["ndvi_p10", "ndvi_p90"]] = [0.9, 0.1]
        with pytest.raises(pandera.errors.SchemaError):
            DailyZonalNdviSchema.validate(corrupta)

    def test_rechaza_mas_validos_que_totales(self, tabla_valida):
        corrupta = tabla_valida.copy()
        corrupta.loc[0, "valid_pixel_count"] = corrupta.loc[0, "pixel_count"] + 1
        with pytest.raises(pandera.errors.SchemaError):
            DailyZonalNdviSchema.validate(corrupta)

    def test_rechaza_duplicados_de_zona_y_fecha(self, tabla_valida):
        duplicada = pd.concat([tabla_valida, tabla_valida], ignore_index=True)
        with pytest.raises(pandera.errors.SchemaError):
            DailyZonalNdviSchema.validate(duplicada)

    def test_rechaza_cobertura_nubosa_imposible(self, tabla_valida):
        corrupta = tabla_valida.copy()
        corrupta.loc[0, "mean_cloud_cover"] = 150.0
        with pytest.raises(pandera.errors.SchemaError):
            DailyZonalNdviSchema.validate(corrupta)


class TestSceneZonalNdviSchema:
    def test_la_salida_por_escena_valida(self, ndvi_raster, zones):
        SceneZonalNdviSchema.validate(zonal_ndvi_scene(ndvi_raster, zones))

    def test_rechaza_duplicados_de_zona_y_escena(self, ndvi_raster, zones):
        tabla = zonal_ndvi_scene(ndvi_raster, zones)
        with pytest.raises(pandera.errors.SchemaError):
            SceneZonalNdviSchema.validate(pd.concat([tabla, tabla], ignore_index=True))

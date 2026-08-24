"""Tests de la escritura en el lakehouse.

El almacen de produccion son dos contenedores, pero Iceberg no depende de
ellos: la misma tabla se puede materializar en un catalogo SQLite con los
ficheros en una carpeta temporal. Eso permite probar de verdad la escritura,
el particionado y la sustitucion de fechas sin levantar nada.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.exceptions import NoSuchTableError
from pyiceberg.transforms import MonthTransform

from ndvi_guadalquivir.lakehouse import (
    DAILY_TABLE,
    INGESTED_AT,
    append_daily,
    daily_partition_spec,
    daily_schema,
    ensure_daily_table,
    existing_dates,
    replace_dates,
    to_arrow,
)
from ndvi_guadalquivir.zonal import DAILY_COLUMNS


@pytest.fixture
def catalog(tmp_path):
    """Catalogo local equivalente al de produccion, sin contenedores."""
    warehouse = tmp_path / "warehouse"
    warehouse.mkdir()
    return SqlCatalog(
        "pruebas",
        uri=f"sqlite:///{tmp_path / 'catalogo.db'}",
        warehouse=f"file://{warehouse}",
    )


@pytest.fixture
def table(catalog):
    return ensure_daily_table(catalog)


@pytest.fixture
def make_frame():
    """Fabrica lotes con el esquema exacto que produce `zonal_ndvi_daily`."""
    def _factory(day: date, *, zones: int = 3, ndvi: float = 0.5) -> pd.DataFrame:
        values = np.full(zones, ndvi, dtype=float)
        return pd.DataFrame({
            "zone_id": [f"1400{i}" for i in range(zones)],
            "zone_name": [f"Municipio {i}" for i in range(zones)],
            "acquisition_date": [day] * zones,
            "tile_ids": ["30SUG,30SUH"] * zones,
            "scene_ids": ["S2A_uno,S2B_dos"] * zones,
            "scene_count": [2] * zones,
            "mean_cloud_cover": [3.5] * zones,
            "ndvi_mean": values,
            "ndvi_median": values,
            "ndvi_std": np.full(zones, 0.08),
            "ndvi_p10": np.clip(values - 0.1, -1, 1),
            "ndvi_p90": np.clip(values + 0.1, -1, 1),
            "pixel_count": [4000] * zones,
            "valid_pixel_count": [3800] * zones,
            "valid_pixel_fraction": [0.95] * zones,
        })[DAILY_COLUMNS]
    return _factory


class TestSchema:
    """Contrato de la tabla."""

    def test_declara_todas_las_columnas_del_pipeline(self):
        """Nada de lo que calcula el pipeline se pierde al guardarlo."""
        campos = {field.name for field in daily_schema().fields}
        assert set(DAILY_COLUMNS) <= campos

    def test_anade_la_marca_de_carga(self):
        """`ingested_at` no viene del satelite: lo pone el proceso."""
        assert INGESTED_AT in {field.name for field in daily_schema().fields}

    def test_zona_y_fecha_identifican_la_fila(self):
        """Es la misma regla que ya impone Pandera, declarada en la tabla."""
        schema = daily_schema()
        identificadores = {
            schema.find_field(fid).name for fid in schema.identifier_field_ids
        }
        assert identificadores == {"zone_id", "acquisition_date"}

    def test_ninguna_columna_admite_nulos(self):
        assert all(field.required for field in daily_schema().fields)

    def test_se_particiona_por_mes_de_adquisicion(self):
        campo = daily_partition_spec().fields[0]
        assert isinstance(campo.transform, MonthTransform)
        assert campo.source_id == daily_schema().find_field("acquisition_date").field_id


class TestEnsureDailyTable:
    def test_crea_la_tabla_la_primera_vez(self, catalog):
        with pytest.raises(NoSuchTableError):
            catalog.load_table(f"bronze.{DAILY_TABLE}")
        assert ensure_daily_table(catalog) is not None

    def test_llamarla_dos_veces_no_falla(self, catalog):
        """La carga historica y la diaria empiezan las dos por aqui."""
        primera = ensure_daily_table(catalog)
        segunda = ensure_daily_table(catalog)
        assert primera.name() == segunda.name()

    def test_la_tabla_nace_particionada(self, table):
        assert len(table.spec().fields) == 1


class TestToArrow:
    def test_respeta_el_esquema_de_la_tabla(self, table, make_frame):
        arrow = to_arrow(make_frame(date(2024, 4, 10)), table)
        assert arrow.schema.equals(table.schema().as_arrow())

    def test_rellena_la_marca_de_carga_si_falta(self, table, make_frame):
        arrow = to_arrow(make_frame(date(2024, 4, 10)), table)
        assert arrow.column(INGESTED_AT).null_count == 0

    def test_avisa_si_falta_una_columna(self, table, make_frame):
        incompleto = make_frame(date(2024, 4, 10)).drop(columns=["ndvi_p90"])
        with pytest.raises(ValueError, match="ndvi_p90"):
            to_arrow(incompleto, table)


class TestAppendDaily:
    def test_escribe_las_filas(self, table, make_frame):
        assert append_daily(make_frame(date(2024, 4, 10)), table=table) == 3
        assert len(table.refresh().scan().to_pandas()) == 3

    def test_los_valores_sobreviven_al_viaje(self, table, make_frame):
        append_daily(make_frame(date(2024, 4, 10), ndvi=0.63), table=table)
        leido = table.refresh().scan().to_pandas()
        assert leido["ndvi_mean"].tolist() == pytest.approx([0.63] * 3)
        assert leido["acquisition_date"].tolist() == [date(2024, 4, 10)] * 3

    def test_un_lote_vacio_no_hace_nada(self, table):
        assert append_daily(pd.DataFrame(columns=DAILY_COLUMNS), table=table) == 0

    def test_rechaza_un_ndvi_imposible(self, table, make_frame):
        """La validacion actua antes de escribir, que es el ultimo momento util."""
        from pandera.errors import SchemaError

        malo = make_frame(date(2024, 4, 10))
        malo.loc[0, "ndvi_mean"] = 1.5
        with pytest.raises(SchemaError):
            append_daily(malo, table=table)

    def test_un_lote_rechazado_no_deja_rastro(self, table, make_frame):
        """Si la validacion corta, la tabla queda como estaba."""
        from pandera.errors import SchemaError

        append_daily(make_frame(date(2024, 4, 10)), table=table)
        malo = make_frame(date(2024, 4, 22))
        malo.loc[0, "ndvi_p10"] = 0.99  # por encima del percentil 90
        with pytest.raises(SchemaError):
            append_daily(malo, table=table)
        assert len(table.refresh().scan().to_pandas()) == 3

    def test_varias_fechas_reparten_particiones(self, table, make_frame):
        """Dos meses distintos deben acabar en dos particiones distintas."""
        for day in (date(2024, 4, 10), date(2024, 4, 22), date(2024, 5, 7)):
            append_daily(make_frame(day), table=table)
        particiones = table.refresh().inspect.partitions().to_pylist()
        assert len(particiones) == 2
        assert sorted(p["record_count"] for p in particiones) == [3, 6]


class TestExistingDates:
    def test_una_tabla_recien_creada_no_tiene_fechas(self, table):
        assert existing_dates(table) == set()

    def test_devuelve_lo_ya_cargado(self, table, make_frame):
        dias = {date(2024, 4, 10), date(2024, 5, 7), date(2023, 6, 1)}
        for day in dias:
            append_daily(make_frame(day), table=table)
        assert existing_dates(table.refresh()) == dias


class TestReplaceDates:
    def test_sustituye_sin_duplicar(self, table, make_frame):
        """Reprocesar una fecha con `append` la duplicaria; con esto no."""
        append_daily(make_frame(date(2024, 4, 10), ndvi=0.4), table=table)
        replace_dates(
            make_frame(date(2024, 4, 10), ndvi=0.8), [date(2024, 4, 10)], table=table
        )
        leido = table.refresh().scan().to_pandas()
        assert len(leido) == 3
        assert leido["ndvi_mean"].tolist() == pytest.approx([0.8] * 3)

    def test_no_toca_las_demas_fechas(self, table, make_frame):
        append_daily(make_frame(date(2024, 4, 10), ndvi=0.4), table=table)
        append_daily(make_frame(date(2024, 4, 22), ndvi=0.5), table=table)
        replace_dates(
            make_frame(date(2024, 4, 10), ndvi=0.9), [date(2024, 4, 10)], table=table
        )
        leido = table.refresh().scan().to_pandas()
        intacta = leido[leido["acquisition_date"] == date(2024, 4, 22)]
        assert intacta["ndvi_mean"].tolist() == pytest.approx([0.5] * 3)

    def test_sin_fechas_no_hace_nada(self, table, make_frame):
        assert replace_dates(make_frame(date(2024, 4, 10)), [], table=table) == 0

    def test_borrar_una_fecha_sin_reemplazo(self, table, make_frame):
        """Con un lote vacio queda solo el borrado, util para purgar."""
        append_daily(make_frame(date(2024, 4, 10)), table=table)
        replace_dates(
            pd.DataFrame(columns=DAILY_COLUMNS), [date(2024, 4, 10)], table=table
        )
        assert len(table.refresh().scan().to_pandas()) == 0

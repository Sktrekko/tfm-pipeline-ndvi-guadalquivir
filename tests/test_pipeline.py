"""Tests de la orquestacion de la carga historica.

Lo que se prueba aqui no es el calculo, que ya tiene sus propios tests, sino
las promesas del pipeline: que reanuda donde iba, que una fecha con error no
tumba la ejecucion ni se da por resuelta, y que lo escrito y lo registrado
son coherentes. Las tablas Iceberg son reales sobre un catalogo SQLite en un
directorio temporal; la red se sustituye por dobles.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest
from pyiceberg.catalog.sql import SqlCatalog

from ndvi_guadalquivir import pipeline
from ndvi_guadalquivir.catalog import Scene
from ndvi_guadalquivir.lakehouse import (
    STATUS_EMPTY,
    STATUS_ERROR,
    STATUS_OK,
    ensure_daily_table,
    ensure_log_table,
    existing_dates,
    settled_dates,
)
from ndvi_guadalquivir.pipeline import (
    BackfillReport,
    DateOutcome,
    process_date,
    run_backfill,
)
from ndvi_guadalquivir.zonal import DAILY_COLUMNS


def _scene(day: date, tile: str = "30SUH") -> Scene:
    return Scene(
        item_id=f"S2A_{tile}_{day:%Y%m%d}_0_L2A",
        acquired_at=datetime(day.year, day.month, day.day, 11, 0, tzinfo=UTC),
        tile_id=tile,
        cloud_cover=2.0,
        epsg=32630,
        red_href="https://example.invalid/red.tif",
        nir_href="https://example.invalid/nir.tif",
        scl_href="https://example.invalid/scl.tif",
    )


def _frame(day: date, zones: int = 3) -> pd.DataFrame:
    """Lote con el esquema exacto que produce la estadistica zonal."""
    values = np.full(zones, 0.5)
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
        "ndvi_p10": values - 0.1,
        "ndvi_p90": values + 0.1,
        "pixel_count": [4000] * zones,
        "valid_pixel_count": [3800] * zones,
        "valid_pixel_fraction": [0.95] * zones,
    })[DAILY_COLUMNS]


DAYS = [date(2024, 6, 1), date(2024, 6, 6), date(2024, 6, 11)]


@pytest.fixture
def catalog(tmp_path):
    warehouse = tmp_path / "warehouse"
    warehouse.mkdir()
    return SqlCatalog(
        "pruebas",
        uri=f"sqlite:///{tmp_path / 'catalogo.db'}",
        warehouse=f"file://{warehouse}",
    )


@pytest.fixture
def backfill_env(catalog, zones, monkeypatch):
    """Deja `run_backfill` funcionando contra tablas locales y sin red.

    El catalogo remoto se sustituye por tres fechas sinteticas y el proceso
    de cada fecha por una funcion que fabrica filas al instante. Lo unico
    real es la escritura Iceberg, que es justo lo que interesa vigilar.
    """
    daily = ensure_daily_table(catalog)
    log = ensure_log_table(catalog)
    monkeypatch.setattr(pipeline, "ensure_daily_table", lambda **kw: daily)
    monkeypatch.setattr(pipeline, "ensure_log_table", lambda **kw: log)
    monkeypatch.setattr(
        pipeline, "search_scenes",
        lambda *a, **kw: [_scene(day) for day in DAYS],
    )

    def fake_process(day, scenes, zonas, *, settings=None):
        return _frame(day), DateOutcome(day, STATUS_OK, len(scenes), 3, 0.1)

    monkeypatch.setattr(pipeline, "process_date", fake_process)
    return {"daily": daily, "log": log, "zones": zones}


class TestProcessDate:
    """La unidad de trabajo: una fecha entra, filas y desenlace salen."""

    def test_exito_produce_filas_y_estado_ok(self, zones, monkeypatch):
        day = DAYS[0]
        monkeypatch.setattr(pipeline, "build_daily_mosaic", lambda *a, **kw: "mosaico")
        monkeypatch.setattr(pipeline, "zonal_ndvi_daily", lambda *a, **kw: _frame(day))

        frame, outcome = process_date(day, [_scene(day)], zones)
        assert outcome.status == STATUS_OK
        assert outcome.row_count == len(frame) == 3
        assert outcome.scene_count == 1

    def test_sin_zonas_con_dato_es_empty_no_error(self, zones, monkeypatch):
        """Una fecha nublada no es un fallo: es un hueco legitimo en la serie."""
        monkeypatch.setattr(pipeline, "build_daily_mosaic", lambda *a, **kw: "mosaico")
        monkeypatch.setattr(
            pipeline, "zonal_ndvi_daily",
            lambda *a, **kw: pd.DataFrame(columns=DAILY_COLUMNS),
        )
        frame, outcome = process_date(DAYS[0], [_scene(DAYS[0])], zones)
        assert outcome.status == STATUS_EMPTY
        assert frame.empty

    def test_una_excepcion_se_convierte_en_desenlace_error(self, zones, monkeypatch):
        """El fallo queda anotado con su motivo en vez de propagarse: con mil
        quinientas fechas, dejar que una tumbe el resto seria carisimo."""
        def boom(*a, **kw):
            raise ConnectionError("granulo corrupto" + "x" * 600)

        monkeypatch.setattr(pipeline, "build_daily_mosaic", boom)
        frame, outcome = process_date(DAYS[0], [_scene(DAYS[0])], zones)
        assert outcome.status == STATUS_ERROR
        assert frame.empty
        assert "granulo corrupto" in outcome.message
        assert len(outcome.message) <= 500  # el registro no almacena novelas

    def test_el_desenlace_es_serializable_al_registro(self):
        record = DateOutcome(DAYS[0], STATUS_OK, 2, 30, 1.5).as_log_record()
        assert record["acquisition_date"] == DAYS[0]
        assert isinstance(record["scene_count"], int)
        assert isinstance(record["duration_seconds"], float)
        assert record["message"] is None


class TestRunBackfill:
    """Las promesas de la carga historica, contra tablas Iceberg reales."""

    def test_escribe_datos_y_registro_coherentes(self, backfill_env):
        report = run_backfill("2024-06-01", "2024-06-30", backfill_env["zones"])

        assert report.requested_dates == len(DAYS)
        assert report.written_rows == 3 * len(DAYS)
        assert existing_dates(backfill_env["daily"]) == set(DAYS)
        assert settled_dates(backfill_env["log"]) == set(DAYS)

    def test_reanudar_no_repite_lo_ya_resuelto(self, backfill_env):
        run_backfill("2024-06-01", "2024-06-30", backfill_env["zones"])
        segunda = run_backfill("2024-06-01", "2024-06-30", backfill_env["zones"])

        assert segunda.skipped_dates == len(DAYS)
        assert segunda.outcomes == []
        # Y el almacen no tiene filas duplicadas.
        assert len(backfill_env["daily"].scan().to_arrow()) == 3 * len(DAYS)

    def test_sin_reanudar_vuelve_a_procesar_todo(self, backfill_env):
        run_backfill("2024-06-01", "2024-06-30", backfill_env["zones"])
        tercera = run_backfill(
            "2024-06-01", "2024-06-30", backfill_env["zones"], resume=False,
        )
        assert tercera.skipped_dates == 0
        assert len(tercera.outcomes) == len(DAYS)

    def test_max_dates_limita_el_trabajo(self, backfill_env):
        report = run_backfill(
            "2024-06-01", "2024-06-30", backfill_env["zones"], max_dates=1,
        )
        assert len(report.outcomes) == 1
        assert len(settled_dates(backfill_env["log"])) == 1

    def test_una_fecha_con_error_no_tumba_ni_se_da_por_resuelta(
        self, backfill_env, monkeypatch,
    ):
        """La promesa doble: el resto continua, y la fecha fallida queda fuera
        del registro de resueltas para que el siguiente intento la recoja."""
        def fragil(day, scenes, zonas, *, settings=None):
            if day == DAYS[1]:
                return (
                    pd.DataFrame(columns=DAILY_COLUMNS),
                    DateOutcome(day, STATUS_ERROR, len(scenes), 0, 0.1, "se rompio"),
                )
            return _frame(day), DateOutcome(day, STATUS_OK, len(scenes), 3, 0.1)

        monkeypatch.setattr(pipeline, "process_date", fragil)
        report = run_backfill("2024-06-01", "2024-06-30", backfill_env["zones"])

        assert [o.acquisition_date for o in report.failed] == [DAYS[1]]
        assert settled_dates(backfill_env["log"]) == {DAYS[0], DAYS[2]}
        # Al reintentar, las dos resueltas se saltan y solo entra la fallida.
        segunda = run_backfill("2024-06-01", "2024-06-30", backfill_env["zones"])
        assert segunda.skipped_dates == 2
        assert [o.acquisition_date for o in segunda.outcomes] == [DAYS[1]]

    def test_fecha_nublada_queda_resuelta_sin_filas(self, backfill_env, monkeypatch):
        """Una fecha sin dato util no escribe filas pero si se registra: sin
        esa anotacion se reintentaria en cada ejecucion, para nada."""
        def nublado(day, scenes, zonas, *, settings=None):
            return (
                pd.DataFrame(columns=DAILY_COLUMNS),
                DateOutcome(day, STATUS_EMPTY, len(scenes), 0, 0.1),
            )

        monkeypatch.setattr(pipeline, "process_date", nublado)
        report = run_backfill("2024-06-01", "2024-06-30", backfill_env["zones"])

        assert report.written_rows == 0
        assert existing_dates(backfill_env["daily"]) == set()
        assert settled_dates(backfill_env["log"]) == set(DAYS)


class TestBackfillReport:
    def test_el_resumen_cuadra_las_cuentas(self):
        report = BackfillReport(requested_dates=5, skipped_dates=2)
        report.outcomes = [
            DateOutcome(DAYS[0], STATUS_OK, 2, 30, 1.0),
            DateOutcome(DAYS[1], STATUS_EMPTY, 2, 0, 1.0),
            DateOutcome(DAYS[2], STATUS_ERROR, 2, 0, 1.0, "x"),
        ]
        report.elapsed_seconds = 60.0

        assert report.written_rows == 30
        assert len(report.failed) == 1
        assert len(report.empty) == 1
        resumen = report.summary()
        assert "5 fechas" in resumen
        assert "1 con error" in resumen

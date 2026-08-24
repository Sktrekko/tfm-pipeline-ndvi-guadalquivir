"""Ejecucion del pipeline: de un intervalo de fechas a filas en el almacen.

Los modulos anteriores resuelven cada uno una parte (buscar, leer, calcular,
agregar, guardar). Este los encadena y se ocupa de lo que solo aparece cuando
el trabajo pasa de una fecha de prueba a ocho anos seguidos:

**Que no haya que empezar de cero.** Una carga historica completa tarda horas.
Se corta la luz, se cierra el portatil, salta un error de red. Al volver a
lanzarla, solo se procesa lo que falta. La marca de por donde iba no es un
fichero aparte sino el propio registro de ingesta, que es la unica fuente de
verdad que no se puede desincronizar de los datos.

**Que un fallo no tumbe la ejecucion.** Con casi mil quinientas fechas, alguna
va a fallar: un granulo corrupto, un corte de red, una ventana que no interseca
ningun tile. Cada fecha se procesa aislada, y la que falla queda anotada con su
motivo mientras el resto continua.

**Que se use la maquina.** Casi todo el tiempo se va en esperar bytes por la
red, no en calcular. Varias fechas a la vez multiplican el rendimiento sin
tocar el algoritmo. El calculo va en paralelo pero la escritura no: los hilos
producen tablas y un unico punto las guarda, de forma que no compitan por
confirmar la misma version.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date

import geopandas as gpd
import pandas as pd

from .aoi import bounds_of
from .catalog import Scene, group_by_date, search_scenes
from .config import Settings, get_settings
from .lakehouse import (
    STATUS_EMPTY,
    STATUS_ERROR,
    STATUS_OK,
    append_daily,
    append_log,
    ensure_daily_table,
    ensure_log_table,
    settled_dates,
)
from .mosaic import build_daily_mosaic
from .zonal import DAILY_COLUMNS, zonal_ndvi_daily

logger = logging.getLogger(__name__)

#: Fechas que se acumulan antes de escribir. Escribir una a una generaria un
#: fichero minusculo por fecha y una version de la tabla por cada uno, que es
#: el problema clasico de los muchos ficheros pequenos. Agrupar de diez en diez
#: da ficheros de tamano razonable sin arriesgar mas de diez fechas de trabajo
#: si el proceso muere entre dos escrituras.
DEFAULT_BATCH_DATES = 10


@dataclass(frozen=True)
class DateOutcome:
    """Que paso con una fecha."""

    acquisition_date: date
    status: str
    scene_count: int
    row_count: int
    duration_seconds: float
    message: str | None = None

    def as_log_record(self) -> dict:
        return {
            "acquisition_date": self.acquisition_date,
            "status": self.status,
            "scene_count": int(self.scene_count),
            "row_count": int(self.row_count),
            "duration_seconds": float(self.duration_seconds),
            "message": self.message,
        }


@dataclass
class BackfillReport:
    """Resumen de una ejecucion completa."""

    requested_dates: int = 0
    skipped_dates: int = 0
    outcomes: list[DateOutcome] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    @property
    def written_rows(self) -> int:
        return sum(o.row_count for o in self.outcomes)

    @property
    def failed(self) -> list[DateOutcome]:
        return [o for o in self.outcomes if o.status == STATUS_ERROR]

    @property
    def empty(self) -> list[DateOutcome]:
        return [o for o in self.outcomes if o.status == STATUS_EMPTY]

    def summary(self) -> str:
        procesadas = len(self.outcomes)
        correctas = procesadas - len(self.failed) - len(self.empty)
        media = (
            sum(o.duration_seconds for o in self.outcomes) / procesadas
            if procesadas else 0.0
        )
        return (
            f"{self.requested_dates} fechas en el intervalo, "
            f"{self.skipped_dates} ya cargadas, {procesadas} procesadas "
            f"({correctas} con dato, {len(self.empty)} sin dato util, "
            f"{len(self.failed)} con error). "
            f"{self.written_rows} filas escritas en {self.elapsed_seconds / 60:.1f} min, "
            f"{media:.1f} s por fecha."
        )


def process_date(
    day: date,
    scenes: list[Scene],
    zones: gpd.GeoDataFrame,
    *,
    settings: Settings | None = None,
) -> tuple[pd.DataFrame, DateOutcome]:
    """Convierte todas las escenas de una fecha en filas por zona.

    Es el trabajo de una unidad completa: mosaicar los granulos de esa pasada
    sobre la malla canonica y resumir el resultado por municipio. No escribe
    nada, para que se pueda ejecutar en paralelo sin coordinacion.

    Args:
        day: fecha de adquisicion.
        scenes: escenas de esa fecha.
        zones: capa de zonas de agregacion.
        settings: configuracion del proyecto.

    Returns:
        El DataFrame de resultados (vacio si ninguna zona alcanzo el minimo de
        pixeles utiles) y el desenlace de la fecha.
    """
    settings = settings or get_settings()
    processing = settings.processing
    started = time.perf_counter()

    try:
        mosaic = build_daily_mosaic(
            scenes,
            bounds_of(zones),
            target_resolution_m=processing.target_resolution_m,
        )
        frame = zonal_ndvi_daily(
            mosaic,
            zones,
            min_valid_pixel_fraction=processing.min_valid_pixel_fraction,
        )
    except Exception as exc:
        elapsed = time.perf_counter() - started
        logger.warning("Fecha %s descartada: %s", day, exc)
        return (
            pd.DataFrame(columns=DAILY_COLUMNS),
            DateOutcome(day, STATUS_ERROR, len(scenes), 0, elapsed, str(exc)[:500]),
        )

    elapsed = time.perf_counter() - started
    status = STATUS_OK if not frame.empty else STATUS_EMPTY
    return frame, DateOutcome(day, status, len(scenes), len(frame), elapsed)


def run_backfill(
    start: date | str,
    end: date | str,
    zones: gpd.GeoDataFrame,
    *,
    settings: Settings | None = None,
    resume: bool = True,
    max_workers: int | None = None,
    batch_dates: int = DEFAULT_BATCH_DATES,
    max_dates: int | None = None,
) -> BackfillReport:
    """Carga en el almacen todas las fechas de un intervalo.

    Args:
        start: fecha inicial, inclusiva.
        end: fecha final, inclusiva.
        zones: capa de zonas de agregacion.
        settings: configuracion del proyecto.
        resume: saltar las fechas que el registro da por resueltas.
        max_workers: fechas simultaneas. Cada una necesita del orden de medio
            giga de memoria mientras mosaica, asi que el limite util no es el
            numero de nucleos sino la memoria disponible.
        batch_dates: fechas que se acumulan antes de cada escritura.
        max_dates: tope de fechas a procesar, util para una prueba corta.

    Returns:
        El resumen de la ejecucion.
    """
    settings = settings or get_settings()
    workers = max_workers or settings.processing.max_workers
    started = time.perf_counter()

    daily_table = ensure_daily_table(settings=settings)
    log_table = ensure_log_table(settings=settings)

    logger.info("Consultando el catalogo entre %s y %s", start, end)
    scenes = search_scenes(bounds_of(zones), start, end, settings=settings.stac)
    by_date = dict(group_by_date(scenes))

    report = BackfillReport(requested_dates=len(by_date))

    pending = sorted(by_date)
    if resume:
        already = settled_dates(log_table)
        pending = [day for day in pending if day not in already]
        report.skipped_dates = report.requested_dates - len(pending)
    if max_dates is not None:
        pending = pending[:max_dates]

    logger.info(
        "%d fechas en el intervalo, %d ya resueltas, %d por procesar con %d hilos",
        report.requested_dates, report.skipped_dates, len(pending), workers,
    )
    if not pending:
        report.elapsed_seconds = time.perf_counter() - started
        return report

    frames: list[pd.DataFrame] = []
    records: list[dict] = []

    def flush() -> None:
        """Escribe lo acumulado y vacia el lote.

        Los datos van antes que el registro a proposito. Si el proceso muere
        entre las dos escrituras, la fecha se queda sin marcar y se reprocesa
        en la siguiente ejecucion, que es molesto pero inofensivo. Al reves se
        daria por cargada una fecha que no lo esta, y eso es un hueco silencioso
        en la serie.
        """
        if frames:
            append_daily(pd.concat(frames, ignore_index=True), table=daily_table)
            frames.clear()
        if records:
            append_log(records, table=log_table)
            records.clear()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(process_date, day, by_date[day], zones, settings=settings): day
            for day in pending
        }
        for done, future in enumerate(as_completed(futures), start=1):
            frame, outcome = future.result()
            report.outcomes.append(outcome)
            records.append(outcome.as_log_record())
            if not frame.empty:
                frames.append(frame)

            logger.info(
                "[%d/%d] %s  %-5s  %d escenas  %d zonas  %.1f s",
                done, len(pending), outcome.acquisition_date, outcome.status,
                outcome.scene_count, outcome.row_count, outcome.duration_seconds,
            )
            if len(records) >= batch_dates:
                flush()

    flush()
    report.elapsed_seconds = time.perf_counter() - started
    logger.info("Carga terminada. %s", report.summary())
    return report

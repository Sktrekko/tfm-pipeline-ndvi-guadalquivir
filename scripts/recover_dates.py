"""Recupera las fechas que la carga historica proceso con la deduplicacion rota.

La carga del 25 de agosto corrio con el codigo anterior al commit `b70d60d`.
Aquella version elegia siempre la linea base de procesado mas alta, incluso
cuando esa version apuntaba al bucket antiguo de JP2 que no es publico, de modo
que se llevaba por delante a la unica copia legible del granulo. El efecto no
es uniforme y ahi esta la trampa:

- Si la fecha tenia una sola escena, no quedo nada que leer y fallo entera.
- Si tenia varias, se mosaico con las que sobrevivieron. Esas fechas quedaron
  anotadas como `ok` o como `empty`, indistinguibles de las correctas: el
  registro de ingesta no sabe cuantas escenas *deberia* haber habido.

Por eso las fechas a recuperar no salen del registro sino del log de la carga,
que es donde quedo escrito cada granulo que no se pudo abrir.

La recuperacion no reanuda ni reintenta: sustituye. Se reprocesan las fechas
con el codigo actual y el resultado reemplaza en una sola instantanea lo que
hubiera, datos y registro. Volver a lanzarlo dos veces deja el almacen igual.

Uso:
    uv run python scripts/recover_dates.py --dry-run   # ver que haria
    uv run python scripts/recover_dates.py
    uv run python scripts/recover_dates.py --dates 2019-03-26 2022-05-09
"""

from __future__ import annotations

import argparse
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pandas as pd

from ndvi_guadalquivir.aoi import bounds_of, load_zones
from ndvi_guadalquivir.catalog import group_by_date, search_scenes
from ndvi_guadalquivir.config import get_settings
from ndvi_guadalquivir.lakehouse import (
    ensure_daily_table,
    ensure_log_table,
    replace_dates,
    replace_log_entries,
)
from ndvi_guadalquivir.pipeline import process_date
from ndvi_guadalquivir.zonal import DAILY_COLUMNS

ZONES_PATH = Path("data/zones/zones_municipios.gpkg")
BACKFILL_LOG = Path("data/backfill.log")
RECOVERY_LOG = Path("data/recover.log")

#: Linea que suelta el lector cuando un granulo no se puede abrir. El
#: identificador de la escena lleva la fecha de adquisicion en el tercer campo.
UNREADABLE = re.compile(r"No se pudo leer S2[AB]_[0-9A-Z]+_(\d{8})_")

logger = logging.getLogger("recover")


def dates_from_log(path: Path) -> list[date]:
    """Fechas que perdieron algun granulo por la deduplicacion rota.

    Se leen del log y no del registro de ingesta a proposito: una fecha que
    perdio una escena de tres sigue figurando como `ok`, y es justo la que hay
    que recuperar sin que nada en el almacen la delate.
    """
    found = {
        date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8]))
        for stamp in UNREADABLE.findall(path.read_text(errors="replace"))
    }
    return sorted(found)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dates", nargs="*", help="fechas concretas, en ISO")
    parser.add_argument("--log", type=Path, default=BACKFILL_LOG,
                        help="log de la carga del que sacar las fechas")
    parser.add_argument("--workers", type=int, default=None,
                        help="fechas simultaneas; el trabajo es esperar red")
    parser.add_argument("--dry-run", action="store_true",
                        help="listar las fechas y salir sin tocar el almacen")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(RECOVERY_LOG), logging.StreamHandler()],
    )
    for noisy in ("rasterio.session", "rasterio._env", "botocore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.dates:
        days = sorted(date.fromisoformat(value) for value in args.dates)
    else:
        days = dates_from_log(args.log)

    logger.info("Fechas a recuperar: %d", len(days))
    if args.dry_run:
        for day in days:
            print(day.isoformat())
        return
    if not days:
        return

    settings = get_settings()
    workers = args.workers or settings.processing.max_workers
    zones = load_zones(ZONES_PATH)
    bbox = bounds_of(zones)
    started = time.perf_counter()

    # Una consulta por fecha en vez de una sola de ocho anos. Suena peor y es
    # mucho mejor: el catalogo tarda dos segundos en resolver un dia y casi
    # veinte minutos en resolver el intervalo completo, que es lo que costo
    # arrancar la carga de anoche.
    def scenes_of(day: date) -> tuple[date, list]:
        found = search_scenes(bbox, day, day)
        grouped = dict(group_by_date(found))
        return day, grouped.get(day, [])

    with ThreadPoolExecutor(max_workers=workers) as pool:
        by_date = dict(pool.map(scenes_of, days))

    sin_escenas = [day for day, scenes in by_date.items() if not scenes]
    if sin_escenas:
        logger.warning(
            "%d fechas sin ninguna escena en el catalogo: %s",
            len(sin_escenas), ", ".join(d.isoformat() for d in sin_escenas),
        )

    frames: list[pd.DataFrame] = []
    outcomes = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(process_date, day, by_date[day], zones, settings=settings)
            for day in days if by_date[day]
        ]
        for done, future in enumerate(futures, start=1):
            frame, outcome = future.result()
            outcomes.append(outcome)
            if not frame.empty:
                frames.append(frame)
            logger.info(
                "[%d/%d] %s  %-5s  %d escenas  %d zonas  %.1f s",
                done, len(futures), outcome.acquisition_date, outcome.status,
                outcome.scene_count, outcome.row_count, outcome.duration_seconds,
            )

    combined = (
        pd.concat(frames, ignore_index=True) if frames
        else pd.DataFrame(columns=DAILY_COLUMNS)
    )

    # Los datos primero y el registro despues, igual que en la carga: si el
    # proceso muere entre ambos, la fecha queda sin marcar y se vuelve a
    # recuperar, que es molesto pero no deja un hueco silencioso.
    replace_dates(combined, days, table=ensure_daily_table(settings=settings))
    replace_log_entries(
        [outcome.as_log_record() for outcome in outcomes],
        days,
        table=ensure_log_table(settings=settings),
    )

    correctas = sum(1 for o in outcomes if o.row_count)
    logger.info(
        "Recuperacion terminada. %d fechas reprocesadas (%d con dato), "
        "%d filas escritas en %.1f min.",
        len(outcomes), correctas, len(combined),
        (time.perf_counter() - started) / 60,
    )


if __name__ == "__main__":
    main()

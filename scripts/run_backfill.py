"""Lanza la carga historica completa.

Es un script y no parte de la libreria porque mezcla decisiones de operacion
(el intervalo, donde escribir el log, que hacer al terminar) que no deben
quedar cableadas en el paquete. La libreria ofrece `run_backfill`; este
fichero decide como usarla esta noche.

Uso:
    uv run python scripts/run_backfill.py                # 2018 hasta hoy
    uv run python scripts/run_backfill.py --max-dates 3  # ensayo corto

Es reanudable: si se corta, el siguiente lanzamiento continua donde iba
gracias al registro de ingesta. La salida detallada va a data/backfill.log
para esquivar el ruido que suelta GDAL al cerrar el interprete.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path

from ndvi_guadalquivir.aoi import load_zones
from ndvi_guadalquivir.pipeline import run_backfill

ZONES_PATH = Path("data/zones/zones_municipios.gpkg")
LOG_PATH = Path("data/backfill.log")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2018-01-01")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--max-dates", type=int, default=None,
                        help="tope de fechas, para un ensayo corto")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()],
    )
    # Rasterio anuncia por INFO cada apertura de un COG remoto. Son dos lineas
    # por granulo, unas cuarenta mil en una carga completa, y sepultan el
    # progreso real. Se suben a WARNING para que el log siga siendo legible.
    for noisy in ("rasterio.session", "rasterio._env", "botocore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    zones = load_zones(ZONES_PATH)
    report = run_backfill(args.start, args.end, zones, max_dates=args.max_dates)
    print(report.summary())
    for outcome in report.failed:
        print(f"  fallo {outcome.acquisition_date}: {outcome.message}")


if __name__ == "__main__":
    main()

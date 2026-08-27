"""Descarga la serie de precipitacion y temperatura de AEMET para la cuenca.

Es el equivalente de `run_backfill.py` para la segunda fuente del proyecto, y
es un script por el mismo motivo: mezcla decisiones de operacion (que intervalo,
cada cuanto escribe, donde deja el log) que no deben quedar cableadas en la
libreria.

La diferencia de escala con la carga de imagenes conviene tenerla presente. Esta
son unos diez minutos; aquella fueron quince horas. No hace falta reanudar ni
paralelizar nada: si se corta, se vuelve a lanzar entero.

Uso:
    uv run python scripts/fetch_weather.py                 # 2018 hasta hoy
    uv run python scripts/fetch_weather.py --start 2023-01-01 --end 2023-06-30
    uv run python scripts/fetch_weather.py --dry-run       # sin escribir nada
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import asdict
from datetime import date
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

from ndvi_guadalquivir.aoi import basin_geometry
from ndvi_guadalquivir.lakehouse import append_weather, ensure_weather_table
from ndvi_guadalquivir.weather import Station, daily_weather, list_stations

LOG_PATH = Path("data/weather.log")

#: Filas que se acumulan antes de cada escritura. Mismo criterio que en la carga
#: de imagenes: escribir una ventana cada vez generaria doscientos ficheros
#: diminutos y doscientas versiones de la tabla.
BATCH_ROWS = 20_000


def basin_stations(stations: list[Station]) -> list[Station]:
    """Se queda con las estaciones que caen dentro de la cuenca.

    El recorte se hace contra la misma geometria de HydroBASINS que define el
    area de estudio del resto del proyecto, y no contra una lista de provincias.
    Por provincias entrarian estaciones de Malaga o de Almeria que vierten al
    Mediterraneo, y quedarian fuera las de Badajoz o Albacete que si vierten al
    Guadalquivir. La cuenca es una consecuencia del relieve, no de la politica.
    """
    puntos = gpd.GeoDataFrame(
        [asdict(s) for s in stations],
        geometry=[Point(s.longitude, s.latitude) for s in stations],
        crs="EPSG:4326",
    )
    cuenca = basin_geometry().to_crs("EPSG:4326").union_all()
    dentro = puntos[puntos.within(cuenca)]
    return [s for s in stations if s.station_id in set(dentro["station_id"])]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2018-01-01")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--dry-run", action="store_true",
                        help="descarga y resume, pero no escribe en el almacen")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()],
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logger = logging.getLogger("weather")

    todas = list_stations()
    dentro = basin_stations(todas)
    por_id = {s.station_id: s for s in dentro}
    logger.info("Estaciones dentro de la cuenca: %d de %d", len(dentro), len(todas))

    table = None if args.dry_run else ensure_weather_table()
    pendientes: list[dict] = []
    escritas = 0

    def volcar() -> None:
        nonlocal escritas, pendientes
        if not pendientes:
            return
        frame = pd.DataFrame(pendientes)
        if table is not None:
            append_weather(frame, table=table)
        escritas += len(frame)
        pendientes = []

    for fila in daily_weather(
        date.fromisoformat(args.start),
        date.fromisoformat(args.end),
        station_ids=set(por_id),
    ):
        estacion = por_id[fila.station_id]
        pendientes.append({
            "station_id": fila.station_id,
            "observed_on": fila.observed_on,
            "precipitation_mm": fila.precipitation_mm,
            "temp_mean_c": fila.temp_mean_c,
            "temp_max_c": fila.temp_max_c,
            "temp_min_c": fila.temp_min_c,
            "station_name": estacion.name,
            "province": estacion.province,
            "longitude": estacion.longitude,
            "latitude": estacion.latitude,
            "altitude_m": estacion.altitude_m,
        })
        if len(pendientes) >= BATCH_ROWS:
            volcar()

    volcar()
    logger.info("Terminado: %d filas%s", escritas, " (simulacro)" if args.dry_run else "")


if __name__ == "__main__":
    main()

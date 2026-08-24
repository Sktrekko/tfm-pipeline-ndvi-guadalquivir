"""Zonas de estudio.

La unidad de analisis del proyecto no es el pixel sino el poligono. Agregar
el NDVI a recintos administrativos reduce el volumen en varios ordenes de
magnitud y produce justo el dato que consume un analista: la evolucion del
vigor vegetal de un termino municipal a lo largo del tiempo.
"""

from __future__ import annotations

import logging
from pathlib import Path

import geopandas as gpd
from shapely.geometry import box

logger = logging.getLogger(__name__)

#: Caja envolvente aproximada de la cuenca hidrografica del Guadalquivir,
#: desde la cabecera en Sierra de Cazorla hasta la desembocadura en Sanlucar.
GUADALQUIVIR_BBOX: tuple[float, float, float, float] = (-6.55, 36.70, -2.35, 38.75)

#: Sistema de referencia de trabajo para las geometrias de entrada.
GEODETIC_CRS = "EPSG:4326"

#: Columnas que debe exponer una capa de zonas para el resto del pipeline.
REQUIRED_ZONE_COLUMNS = ("zone_id", "zone_name", "geometry")


def basin_bbox() -> tuple[float, float, float, float]:
    """Devuelve la caja envolvente de la cuenca en grados."""
    return GUADALQUIVIR_BBOX


def basin_geometry() -> gpd.GeoDataFrame:
    """Cuenca como una unica geometria rectangular, util para el catalogo."""
    return gpd.GeoDataFrame(
        {"zone_id": ["guadalquivir"], "zone_name": ["Cuenca del Guadalquivir"]},
        geometry=[box(*GUADALQUIVIR_BBOX)],
        crs=GEODETIC_CRS,
    )


def load_zones(path: str | Path, *, clip_to_basin: bool = True) -> gpd.GeoDataFrame:
    """Carga la capa de zonas de agregacion desde disco.

    Acepta cualquier formato que sepa leer GDAL (GeoJSON, GeoPackage,
    shapefile). La capa se reproyecta a coordenadas geodesicas y, por defecto,
    se recorta a la cuenca para descartar recintos que quedan fuera del area
    de estudio.

    Args:
        path: ruta al fichero vectorial.
        clip_to_basin: filtrar las zonas que no intersecan la cuenca.

    Returns:
        GeoDataFrame con las columnas `zone_id`, `zone_name` y `geometry`.

    Raises:
        FileNotFoundError: si el fichero no existe.
        ValueError: si faltan columnas obligatorias o la capa queda vacia.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"No se encuentra la capa de zonas en {path}. "
            f"Ejecuta `ndvi-pipeline zones download` para descargarla."
        )

    zones = gpd.read_file(path)
    missing = [c for c in REQUIRED_ZONE_COLUMNS if c not in zones.columns]
    if missing:
        raise ValueError(
            f"A la capa {path} le faltan las columnas {missing}; "
            f"tiene {list(zones.columns)}"
        )

    if zones.crs is None:
        raise ValueError(f"La capa {path} no declara sistema de referencia")
    if zones.crs.to_string() != GEODETIC_CRS:
        zones = zones.to_crs(GEODETIC_CRS)

    if clip_to_basin:
        before = len(zones)
        zones = zones[zones.intersects(box(*GUADALQUIVIR_BBOX))].copy()
        logger.info("Zonas dentro de la cuenca: %d de %d", len(zones), before)

    if zones.empty:
        raise ValueError(f"La capa {path} no contiene zonas dentro de la cuenca")

    zones["zone_id"] = zones["zone_id"].astype(str)

    # Las columnas obligatorias van primero y el resto se conserva. Esos
    # extras (provincia, superficie, solape con la cuenca) no los usa el
    # calculo del NDVI, pero son justo lo que permite a dbt subir de grano
    # despues sin tener que volver a leer una sola imagen.
    extra = [c for c in zones.columns if c not in REQUIRED_ZONE_COLUMNS]
    ordered = ["zone_id", "zone_name", *extra, "geometry"]
    return zones.reset_index(drop=True)[ordered]


def bounds_of(
    zones: gpd.GeoDataFrame,
    *,
    buffer_deg: float = 0.01,
) -> tuple[float, float, float, float]:
    """Caja envolvente de un conjunto de zonas, con un margen de seguridad.

    El margen evita que el recorte del raster corte justo en el borde de un
    poligono y deje pixeles fronterizos sin dato.
    """
    west, south, east, north = zones.total_bounds
    return (
        west - buffer_deg,
        south - buffer_deg,
        east + buffer_deg,
        north + buffer_deg,
    )

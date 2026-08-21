"""Descubrimiento de escenas Sentinel-2 en el catalogo STAC.

STAC (SpatioTemporal Asset Catalog) es el estandar que permite consultar que
imagenes existen para una zona y una fecha sin descargar ni un byte de pixel.
Cada resultado es un *item* que apunta, mediante URL, a las bandas alojadas
como COG.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from pystac import Item
from pystac_client import Client

from .config import StacSettings

logger = logging.getLogger(__name__)

#: Nombres de asset que usa la coleccion de Element84 para cada banda.
ASSET_RED = "red"      # B04, 10 m
ASSET_NIR = "nir"      # B08, 10 m
ASSET_SCL = "scl"      # Scene Classification Layer, 20 m


@dataclass(frozen=True)
class Scene:
    """Una escena Sentinel-2 localizada en el catalogo.

    Es una vista reducida del item STAC con solo lo que necesita el pipeline,
    de forma que el resto del codigo no dependa de la estructura interna de
    pystac ni del proveedor de catalogo concreto.
    """

    item_id: str
    acquired_at: datetime
    tile_id: str
    cloud_cover: float
    epsg: int
    red_href: str
    nir_href: str
    scl_href: str

    @property
    def acquisition_date(self) -> date:
        return self.acquired_at.date()

    @classmethod
    def from_stac_item(cls, item: Item) -> Scene:
        """Traduce un item STAC al modelo interno.

        Raises:
            KeyError: si el item no expone las bandas necesarias.
        """
        for asset in (ASSET_RED, ASSET_NIR, ASSET_SCL):
            if asset not in item.assets:
                raise KeyError(
                    f"El item {item.id} no expone la banda '{asset}'; "
                    f"disponibles: {sorted(item.assets)}"
                )
        if item.datetime is None:
            raise KeyError(f"El item {item.id} no tiene fecha de adquisicion")

        props = item.properties
        # El identificador de tile MGRS llega troceado en sus tres componentes.
        tile_id = props.get("grid:code") or "{:02d}{}{}".format(
            int(props.get("mgrs:utm_zone", 0)),
            props.get("mgrs:latitude_band", ""),
            props.get("mgrs:grid_square", ""),
        )

        return cls(
            item_id=item.id,
            acquired_at=item.datetime,
            tile_id=str(tile_id).removeprefix("MGRS-"),
            cloud_cover=float(props.get("eo:cloud_cover", 0.0)),
            epsg=int(props.get("proj:epsg") or props.get("proj:code", "EPSG:4326").split(":")[-1]),
            red_href=item.assets[ASSET_RED].href,
            nir_href=item.assets[ASSET_NIR].href,
            scl_href=item.assets[ASSET_SCL].href,
        )


def search_scenes(
    bbox: Sequence[float],
    start: date | str,
    end: date | str,
    *,
    settings: StacSettings | None = None,
    max_cloud_cover: float | None = None,
    limit: int | None = None,
) -> list[Scene]:
    """Busca escenas que cubran una zona y un intervalo temporal.

    Args:
        bbox: caja envolvente en grados `[oeste, sur, este, norte]` (EPSG:4326).
        start: fecha inicial, inclusiva.
        end: fecha final, inclusiva.
        settings: configuracion del catalogo; por defecto la del proyecto.
        max_cloud_cover: cobertura nubosa maxima en porcentaje. Filtra en el
            servidor, no en cliente, para no traerse metadatos inutiles.
        limit: numero maximo de escenas a devolver, util en pruebas.

    Returns:
        Escenas ordenadas cronologicamente.
    """
    settings = settings or StacSettings()
    threshold = (
        settings.max_cloud_cover if max_cloud_cover is None else max_cloud_cover
    )

    client = Client.open(settings.api_url)
    search = client.search(
        collections=[settings.collection],
        bbox=list(bbox),
        datetime=f"{_as_iso(start)}/{_as_iso(end)}",
        query={"eo:cloud_cover": {"lt": threshold}},
    )

    scenes: list[Scene] = []
    for item in search.items():
        try:
            scenes.append(Scene.from_stac_item(item))
        except KeyError as exc:
            # Un item incompleto no debe tumbar la ejecucion entera.
            logger.warning("Se descarta el item %s: %s", item.id, exc)

    scenes.sort(key=lambda scene: (scene.acquired_at, scene.tile_id))
    if limit is not None:
        scenes = scenes[:limit]

    logger.info(
        "Encontradas %d escenas de %s entre %s y %s (nubes < %s%%)",
        len(scenes), settings.collection, _as_iso(start), _as_iso(end), threshold,
    )
    return scenes


def group_by_date(scenes: Sequence[Scene]) -> Iterator[tuple[date, list[Scene]]]:
    """Agrupa escenas por fecha de adquisicion.

    Una misma pasada del satelite genera varios tiles MGRS contiguos; para
    construir un mosaico diario conviene tratarlos juntos.
    """
    grouped: dict[date, list[Scene]] = {}
    for scene in scenes:
        grouped.setdefault(scene.acquisition_date, []).append(scene)
    for day in sorted(grouped):
        yield day, grouped[day]


def _as_iso(value: date | str) -> str:
    return value if isinstance(value, str) else value.isoformat()

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
from .indices import BOA_ADD_OFFSET

logger = logging.getLogger(__name__)

#: Nombres de asset que usa la coleccion de Element84 para cada banda.
ASSET_RED = "red"      # B04, 10 m
ASSET_NIR = "nir"      # B08, 10 m
ASSET_SCL = "scl"      # Scene Classification Layer, 20 m

#: Primera linea base de procesado que incorpora el offset radiometrico de
#: -1000. Desde ella el valor fisico es (DN - 1000) / 10000; antes, DN / 10000.
_OFFSET_SINCE_BASELINE = (4, 0)


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
    #: Version del algoritmo de la ESA que genero el producto ("05.00").
    #: Decide el offset y sirve para elegir entre versiones duplicadas.
    processing_baseline: str = "00.00"
    #: Offset aditivo que este pipeline debe aplicar a los enteros crudos.
    #: Se resuelve aqui, al traducir el item, porque depende del proveedor:
    #: Element84 resta el de la linea base 04.00 dentro del propio COG en la
    #: mayoria de las escenas y lo declara en `earthsearch:boa_offset_applied`.
    #: Aplicarlo dos veces hundiria la reflectancia tanto como ignorarlo.
    boa_offset: float = 0.0

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

        baseline = str(props.get("s2:processing_baseline") or "00.00")

        return cls(
            item_id=item.id,
            acquired_at=item.datetime,
            tile_id=str(tile_id).removeprefix("MGRS-"),
            cloud_cover=float(props.get("eo:cloud_cover", 0.0)),
            epsg=int(props.get("proj:epsg") or props.get("proj:code", "EPSG:4326").split(":")[-1]),
            red_href=item.assets[ASSET_RED].href,
            nir_href=item.assets[ASSET_NIR].href,
            scl_href=item.assets[ASSET_SCL].href,
            processing_baseline=baseline,
            boa_offset=_resolve_boa_offset(
                baseline, bool(props.get("earthsearch:boa_offset_applied"))
            ),
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

    # La limpieza se hace aqui, en la frontera con el proveedor, para que
    # ningun consumidor tenga que saber que el catalogo trae duplicados.
    scenes = deduplicate_scenes(scenes)
    if limit is not None:
        scenes = scenes[:limit]

    logger.info(
        "Encontradas %d escenas de %s entre %s y %s (nubes < %s%%)",
        len(scenes), settings.collection, _as_iso(start), _as_iso(end), threshold,
    )
    return scenes


def deduplicate_scenes(scenes: Sequence[Scene]) -> list[Scene]:
    """Se queda con una version de cada observacion (tile y fecha).

    La ESA reprocesa periodicamente su archivo con algoritmos mejorados y el
    catalogo conserva las dos versiones del mismo granulo. Medido sobre la
    cuenca: en 2019-2021 casi la mitad de las escenas son la version antigua
    de otra que tambien esta, y leer ambas duplica el tiempo de esos anos sin
    aportar informacion.

    Sobrevive la linea base de procesado mas alta, que es la correccion mas
    moderna. A igualdad de linea base decide el identificador, que lleva un
    numero de secuencia creciente, de modo que el resultado no depende del
    orden en que el catalogo devuelva los items.
    """
    survivors: dict[tuple[str, date], Scene] = {}
    for scene in scenes:
        key = (scene.tile_id, scene.acquisition_date)
        rival = survivors.get(key)
        if rival is None or _dedup_rank(scene) > _dedup_rank(rival):
            survivors[key] = scene

    kept = sorted(survivors.values(), key=lambda s: (s.acquired_at, s.tile_id))
    dropped = len(scenes) - len(kept)
    if dropped:
        logger.info(
            "Descartadas %d escenas duplicadas de %d (%.0f%%)",
            dropped, len(scenes), 100 * dropped / len(scenes),
        )
    return kept


def _dedup_rank(scene: Scene) -> tuple[tuple[int, ...], str]:
    return _baseline_tuple(scene.processing_baseline), scene.item_id


def _baseline_tuple(baseline: str) -> tuple[int, ...]:
    """Convierte "05.00" en (5, 0) para poder comparar lineas base.

    Compararlas como texto funcionaria hoy, pero "10.00" quedaria por debajo
    de "05.00" en cuanto la ESA pase de dos digitos.
    """
    try:
        return tuple(int(part) for part in baseline.split("."))
    except ValueError:
        return (0,)


def _resolve_boa_offset(baseline: str, provider_applied: bool) -> float:
    """Decide que offset debe aplicar el pipeline a una escena.

    Tres casos, verificados contra el catalogo escena a escena:

    - Lineas base anteriores a la 04.00: el producto nunca tuvo offset.
    - Desde la 04.00, si el proveedor declara haberlo restado ya en el COG
      (`earthsearch:boa_offset_applied`), aplicarlo otra vez lo duplicaria.
    - Desde la 04.00 sin esa declaracion, lo aplica el pipeline. Es el caso
      de unas pocas escenas de 2022 en Element84 y de toda la coleccion
      Collection-1, que no usa la propiedad.
    """
    if _baseline_tuple(baseline) < _OFFSET_SINCE_BASELINE or provider_applied:
        return 0.0
    return BOA_ADD_OFFSET


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

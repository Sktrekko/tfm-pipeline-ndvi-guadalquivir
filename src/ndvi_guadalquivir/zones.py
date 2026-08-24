"""Construccion de la capa de zonas de agregacion.

Hasta ahora el pipeline agregaba sobre rectangulos de prueba. Este modulo los
sustituye por la capa real, y lo hace combinando dos fuentes oficiales que
responden a dos preguntas distintas:

    donde esta la cuenca      HydroBASINS (WWF / BfG), nivel 4
    donde estan los municipios  Lineas Limite Municipales del IGN

Son preguntas distintas porque una cuenca hidrografica es un objeto fisico,
definido por la divisoria de aguas, mientras que un municipio es un objeto
administrativo. El area de estudio del proyecto es la primera; la unidad de
agregacion es la segunda. Cruzarlas es justo el trabajo de este modulo.

Cuatro decisiones que conviene tener escritas, porque un tribunal las va a
preguntar:

1. **La cuenca se toma de HydroBASINS y no de un rectangulo.** El rectangulo
   con el que arranco el proyecto metia dentro media Extremadura y la costa de
   Malaga, que vierten a otro sitio. HydroBASINS delimita por divisoria de
   aguas sobre un modelo digital del terreno, y su unidad 2040018360 mide
   57 026 km2 frente a los 57 527 km2 que publica la Confederacion
   Hidrografica del Guadalquivir: menos de un 1 % de diferencia.

2. **Los municipios se toman del IGN y no de la capa europea LAU.** Ambas
   coinciden en superficie, pero LAU esta generalizada a escala 1:1 000 000 y
   describe un municipio medio con 31 vertices, mientras que el IGN usa 431 y
   declara precision decametrica. Como el pixel de trabajo mide 80 m, una
   generalizacion kilometrica desplazaria el borde varios pixeles.

3. **Se incluye el municipio entero, no la parte que cae dentro de la cuenca.**
   Recortar produciria recintos que ya no son el municipio y que no se podrian
   cruzar con ninguna estadistica del INE. A cambio se exige que la mayor parte
   del termino este dentro y se guarda que fraccion exactamente, de modo que
   aguas abajo se pueda filtrar mas fino o ponderar.

4. **Los limites se congelan en una unica edicion para los ocho anos.** Un
   municipio que cambia de contorno a mitad de la serie produciria un salto de
   NDVI que no viene del terreno sino del mapa. Como el objetivo es detectar
   anomalias, cualquier discontinuidad artificial es exactamente lo que no se
   quiere.
"""

from __future__ import annotations

import logging
import shutil
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
from pyogrio import read_info
from rasterio.warp import transform_bounds

from .aoi import GEODETIC_CRS, GUADALQUIVIR_BBOX

logger = logging.getLogger(__name__)

#: Proyeccion equivalente para Europa (Lambert azimutal de igual area). Las
#: superficies se calculan aqui: hacerlo en grados daria areas sin sentido y
#: hacerlo en UTM las deformaria en los bordes del huso.
EQUAL_AREA_CRS = "EPSG:3035"


@dataclass(frozen=True)
class VectorSource:
    """Fichero vectorial publicado dentro de un ZIP remoto."""

    url: str
    #: Ruta del shapefile dentro del ZIP.
    member: str
    #: Nombre con el que se guarda la descarga en la cache local.
    archive_name: str
    #: De donde sale el dato, para poder citarlo en la memoria.
    attribution: str


BASIN_SOURCE = VectorSource(
    url="https://data.hydrosheds.org/file/hydrobasins/standard/hybas_eu_lev04_v1c.zip",
    member="hybas_eu_lev04_v1c.shp",
    archive_name="hybas_eu_lev04_v1c.zip",
    attribution="HydroBASINS v1.c (Lehner y Grill, 2013), nivel 4",
)

MUNICIPALITY_SOURCE = VectorSource(
    url="https://centrodedescargas.cnig.es/CentroDescargas/documentos/lineas_limite.zip",
    member=(
        "SIGLIM_Publico_INSPIRE/recintos_municipales_inspire_peninbal_etrs89/"
        "recintos_municipales_inspire_peninbal_etrs89.shp"
    ),
    archive_name="lineas_limite.zip",
    attribution="Lineas Limite Municipales, Instituto Geografico Nacional (CC BY 4.0)",
)

#: Identificador de la cuenca del Guadalquivir en HydroBASINS nivel 4.
GUADALQUIVIR_HYBAS_ID = 2_040_018_360

#: Fraccion minima del termino municipal que debe caer dentro de la cuenca
#: para que el municipio entre en el estudio.
DEFAULT_MIN_OVERLAP = 0.5

#: Columnas de la capa de zonas, mas alla de las tres obligatorias. Se
#: conservan porque son la materia prima de las agregaciones de dbt: la
#: provincia permite subir de grano sin volver a tocar el raster.
ZONE_EXTRA_COLUMNS = ("province_code", "area_km2", "basin_overlap_fraction")


def _download(url: str, dest: Path) -> Path:
    """Descarga un fichero si no esta ya en la cache local.

    Las fuentes cartograficas pesan mas de 100 MB y no cambian entre
    ejecuciones, asi que se descargan una sola vez. Se escribe primero en un
    fichero temporal y se renombra al final, de modo que una descarga
    interrumpida no deje en la cache un ZIP a medias que parezca valido.
    """
    if dest.exists():
        logger.info("Ya en cache: %s (%.1f MB)", dest.name, dest.stat().st_size / 1e6)
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".part")
    logger.info("Descargando %s", url)
    with urllib.request.urlopen(url) as response, partial.open("wb") as handle:
        shutil.copyfileobj(response, handle)
    partial.rename(dest)
    logger.info("Descargado %s (%.1f MB)", dest.name, dest.stat().st_size / 1e6)
    return dest


def _extract_shapefile(archive: Path, member: str, dest_dir: Path) -> Path:
    """Extrae un shapefile y sus ficheros acompanantes de un ZIP.

    Un shapefile no es un fichero sino media docena que comparten nombre, asi
    que hay que sacarlos todos. Se descartan a proposito los indices
    espaciales `.sbn` y `.sbx`: son un formato propietario antiguo y en
    HydroBASINS vienen corruptos, de forma que GDAL aborta la lectura al
    intentar usarlos. Sin ellos recurre a un barrido secuencial, que para unos
    miles de recintos es instantaneo.
    """
    stem = Path(member).stem
    prefix = str(Path(member).parent / stem)
    dest_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(archive) as zf:
        members = [
            name for name in zf.namelist()
            if name.startswith(prefix) and not name.lower().endswith((".sbn", ".sbx"))
        ]
        if not any(name.lower().endswith(".shp") for name in members):
            raise FileNotFoundError(f"{archive.name} no contiene el shapefile {member}")
        for name in members:
            target = dest_dir / Path(name).name
            if not target.exists():
                with zf.open(name) as source, target.open("wb") as handle:
                    shutil.copyfileobj(source, handle)

    return dest_dir / f"{stem}.shp"


def _bbox_in_file_crs(shapefile: Path, bbox):
    """Traduce una caja en grados al sistema de coordenadas del fichero.

    El filtro por caja de GDAL trabaja siempre en las coordenadas del propio
    fichero, no en las que se le pasen, y no avisa cuando no coinciden: si la
    capa viniera proyectada en metros y se le diera una caja en grados,
    devolveria cero recintos sin lanzar ningun error. Los recintos del IGN se
    publican en ETRS89 geodesico, donde la caja funciona tal cual, pero eso es
    una propiedad de esta descarga concreta y no algo en lo que apoyarse.

    Leer la cabecera para averiguar el sistema cuesta unos milisegundos y deja
    la funcion valida para cualquier capa vectorial, venga como venga.
    """
    if bbox is None:
        return None
    crs = read_info(shapefile)["crs"]
    if crs is None:
        return bbox
    return transform_bounds(GEODETIC_CRS, crs, *bbox)


def load_basin(shapefile: Path, *, hybas_id: int = GUADALQUIVIR_HYBAS_ID) -> gpd.GeoDataFrame:
    """Extrae de HydroBASINS el poligono de la cuenca de estudio.

    Args:
        shapefile: capa de cuencas ya extraida del ZIP.
        hybas_id: identificador de la cuenca dentro de la capa.

    Returns:
        GeoDataFrame de una sola fila en coordenadas geodesicas.

    Raises:
        ValueError: si el identificador no aparece en la capa.
    """
    basins = gpd.read_file(shapefile)
    basin = basins[basins["HYBAS_ID"] == hybas_id]
    if basin.empty:
        raise ValueError(
            f"La cuenca {hybas_id} no esta en {shapefile.name}; "
            f"la capa contiene {len(basins)} cuencas"
        )
    basin = basin.to_crs(GEODETIC_CRS).reset_index(drop=True)
    area = float(basin.to_crs(EQUAL_AREA_CRS).geometry.area.iloc[0] / 1e6)
    logger.info("Cuenca %s: %.0f km2", hybas_id, area)
    return basin


def load_municipalities(shapefile: Path, *, bbox=GUADALQUIVIR_BBOX) -> gpd.GeoDataFrame:
    """Carga los recintos municipales del IGN y normaliza sus columnas.

    El IGN identifica cada recinto con un `NATCODE` de once digitos con la
    estructura `34` + comunidad (2) + provincia (2) + codigo INE (5). Los cinco
    ultimos se adoptan como `zone_id` porque son el codigo del INE, que es el
    que usa cualquier estadistica oficial espanola: eso hace la capa cruzable
    con datos de superficie cultivada, poblacion o produccion sin trabajo extra.

    La provincia se lee de su propio campo y no de los dos primeros digitos del
    codigo INE, aunque en la inmensa mayoria de los casos coincidan. La razon
    esta en la propia capa: contiene condominios, terrenos comunales que
    pertenecen a varios municipios a la vez y que el INE numera en el rango
    53xxx, ajeno a la serie provincial. Deducir de ahi la provincia inventaria
    una provincia 53 que no existe. Son solo dos recintos en toda la cuenca,
    pero son suelo real con vegetacion real, asi que se conservan bien
    clasificados en lugar de descartarlos.

    Args:
        shapefile: capa de recintos municipales ya extraida del ZIP.
        bbox: caja previa de lectura, para no cargar toda la peninsula.

    Returns:
        GeoDataFrame con `zone_id`, `zone_name`, `province_code` y `geometry`.
    """
    municipalities = gpd.read_file(shapefile, bbox=_bbox_in_file_crs(shapefile, bbox))
    if municipalities.crs is None:
        raise ValueError(f"La capa {shapefile.name} no declara sistema de referencia")

    natcode = municipalities["NATCODE"].astype(str)
    normalized = gpd.GeoDataFrame(
        {
            "zone_id": natcode.str[-5:],
            "zone_name": municipalities["NAMEUNIT"].astype(str),
            "province_code": natcode.str[4:6],
        },
        geometry=municipalities.geometry,
        crs=municipalities.crs,
    ).to_crs(GEODETIC_CRS)

    logger.info("Municipios leidos del IGN: %d", len(normalized))
    return normalized.reset_index(drop=True)


def select_basin_municipalities(
    municipalities: gpd.GeoDataFrame,
    basin: gpd.GeoDataFrame,
    *,
    min_overlap_fraction: float = DEFAULT_MIN_OVERLAP,
) -> gpd.GeoDataFrame:
    """Se queda con los municipios que pertenecen mayoritariamente a la cuenca.

    La pertenencia se mide por superficie, no por si el centroide cae dentro:
    un municipio alargado puede tener el centroide fuera de la cuenca y estar
    casi entero dentro, o al reves. La fraccion se calcula en una proyeccion
    equivalente, que es la unica en la que comparar areas tiene sentido.

    La geometria que se devuelve es la del municipio completo, sin recortar.
    El recorte daria recintos mas fieles a la cuenca pero dejaria de ser
    municipios, y con ello se perderia la posibilidad de cruzar la serie con
    cualquier otra fuente administrativa.

    Args:
        municipalities: capa normalizada de municipios.
        basin: poligono de la cuenca, una sola fila.
        min_overlap_fraction: fraccion minima del termino dentro de la cuenca.

    Returns:
        Los municipios seleccionados, con `area_km2` y
        `basin_overlap_fraction`, ordenados por identificador.

    Raises:
        ValueError: si la seleccion queda vacia.
    """
    projected = municipalities.to_crs(EQUAL_AREA_CRS)
    basin_geometry = basin.to_crs(EQUAL_AREA_CRS).geometry.union_all()

    total_area = projected.geometry.area
    inside_area = projected.geometry.intersection(basin_geometry).area
    overlap = (inside_area / total_area).fillna(0.0)

    selected = municipalities.assign(
        area_km2=(total_area / 1e6).to_numpy(),
        basin_overlap_fraction=overlap.to_numpy(),
    )
    selected = selected[selected["basin_overlap_fraction"] >= min_overlap_fraction]

    if selected.empty:
        raise ValueError(
            f"Ningun municipio alcanza el {100 * min_overlap_fraction:.0f}% "
            f"de superficie dentro de la cuenca"
        )

    selected = selected.sort_values("zone_id").reset_index(drop=True)
    logger.info(
        "Municipios en la cuenca: %d de %d (solape >= %.0f%%), %.0f km2 en total",
        len(selected), len(municipalities), 100 * min_overlap_fraction,
        float(selected["area_km2"].sum()),
    )
    return selected


def build_zone_layer(
    *,
    cache_dir: Path,
    dest: Path,
    min_overlap_fraction: float = DEFAULT_MIN_OVERLAP,
) -> Path:
    """Descarga las fuentes y escribe la capa de zonas lista para el pipeline.

    Args:
        cache_dir: directorio donde se guardan las descargas originales.
        dest: fichero de salida, en cualquier formato que escriba GDAL.
        min_overlap_fraction: criterio de pertenencia a la cuenca.

    Returns:
        La ruta del fichero escrito.
    """
    raw = cache_dir / "raw"
    work = cache_dir / "work"

    basin_archive = _download(BASIN_SOURCE.url, raw / BASIN_SOURCE.archive_name)
    basin_shp = _extract_shapefile(basin_archive, BASIN_SOURCE.member, work / "basin")
    basin = load_basin(basin_shp)

    muni_archive = _download(MUNICIPALITY_SOURCE.url, raw / MUNICIPALITY_SOURCE.archive_name)
    muni_shp = _extract_shapefile(muni_archive, MUNICIPALITY_SOURCE.member, work / "municipios")
    municipalities = load_municipalities(muni_shp)

    zones = select_basin_municipalities(
        municipalities, basin, min_overlap_fraction=min_overlap_fraction
    )

    dest.parent.mkdir(parents=True, exist_ok=True)
    zones.to_file(dest, driver="GPKG", layer="zones")
    basin.to_file(dest.with_name("guadalquivir_basin.gpkg"), driver="GPKG", layer="basin")

    logger.info("Capa de zonas escrita en %s (%d municipios)", dest, len(zones))
    return dest

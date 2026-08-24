"""Algebra de bandas y enmascarado de calidad.

Este modulo contiene funciones puras que operan sobre arrays de numpy: no
leen ficheros ni acceden a la red. Esa separacion es deliberada, porque es lo
que permite testear la parte delicada del calculo (offsets de reflectancia,
mascaras, division por cero) de forma rapida y determinista.
"""

from __future__ import annotations

from enum import IntEnum

import numpy as np
import numpy.typing as npt

# A partir de la linea base de procesado 04.00, los productos Sentinel-2 L2A
# almacenan la reflectancia como entero con un desplazamiento de -1000, de modo
# que el valor fisico es (DN + BOA_ADD_OFFSET) / QUANTIFICATION_VALUE. Ignorar
# el offset sesga el NDVI de forma sistematica.
QUANTIFICATION_VALUE = 10_000.0
BOA_ADD_OFFSET = -1_000.0

# Umbral por debajo del cual el denominador (NIR + Rojo) se considera nulo.
# Sin esta guarda, los pixeles de agua profunda o de relleno producen valores
# de NDVI de varios ordenes de magnitud.
_DENOMINATOR_EPS = 1e-6


class SceneClass(IntEnum):
    """Clases de la banda SCL (Scene Classification Layer) de Sentinel-2 L2A."""

    NO_DATA = 0
    SATURATED_OR_DEFECTIVE = 1
    CAST_SHADOW = 2
    CLOUD_SHADOW = 3
    VEGETATION = 4
    NOT_VEGETATED = 5
    WATER = 6
    UNCLASSIFIED = 7
    CLOUD_MEDIUM_PROBABILITY = 8
    CLOUD_HIGH_PROBABILITY = 9
    THIN_CIRRUS = 10
    SNOW_OR_ICE = 11


#: Clases que invalidan un pixel para el calculo de vegetacion. Se descartan
#: nubes y sus sombras, nieve, pixeles saturados y zonas sin dato. El agua se
#: conserva porque su NDVI negativo es informativo (lamina de embalse).
INVALID_SCENE_CLASSES: frozenset[int] = frozenset({
    SceneClass.NO_DATA,
    SceneClass.SATURATED_OR_DEFECTIVE,
    SceneClass.CAST_SHADOW,
    SceneClass.CLOUD_SHADOW,
    SceneClass.CLOUD_MEDIUM_PROBABILITY,
    SceneClass.CLOUD_HIGH_PROBABILITY,
    SceneClass.THIN_CIRRUS,
    SceneClass.SNOW_OR_ICE,
})


def to_reflectance(
    digital_numbers: npt.NDArray,
    *,
    quantification_value: float = QUANTIFICATION_VALUE,
    add_offset: float = BOA_ADD_OFFSET,
) -> npt.NDArray[np.float32]:
    """Convierte los enteros crudos de una banda en reflectancia fisica [0, 1].

    Args:
        digital_numbers: valores tal y como vienen en el COG.
        quantification_value: divisor de cuantificacion del producto.
        add_offset: desplazamiento aditivo de la linea base de procesado.

    Returns:
        Array de float32 con la reflectancia en la parte alta de la atmosfera
        corregida a superficie (BOA).
    """
    values = np.asarray(digital_numbers, dtype=np.float32)
    return (values + np.float32(add_offset)) / np.float32(quantification_value)


def scl_valid_mask(
    scl: npt.NDArray,
    *,
    invalid_classes: frozenset[int] = INVALID_SCENE_CLASSES,
) -> npt.NDArray[np.bool_]:
    """Construye la mascara de pixeles utilizables a partir de la banda SCL.

    Args:
        scl: banda de clasificacion de escena, ya remuestreada a la malla de
            las bandas espectrales.
        invalid_classes: clases a descartar.

    Returns:
        Array booleano, `True` donde el pixel es utilizable.
    """
    scl_array = np.asarray(scl)
    mask = np.ones(scl_array.shape, dtype=bool)
    for class_value in invalid_classes:
        mask &= scl_array != class_value
    # Los NaN aparecen cuando el recorte espacial cae fuera del granulo.
    if np.issubdtype(scl_array.dtype, np.floating):
        mask &= np.isfinite(scl_array)
    return mask


def normalized_difference(
    first: npt.NDArray,
    second: npt.NDArray,
) -> npt.NDArray[np.float32]:
    """Indice de diferencia normalizada `(a - b) / (a + b)`.

    Los pixeles cuyo denominador es practicamente cero se devuelven como NaN
    en lugar de propagar infinitos, y el resultado se restringe al rango
    teorico [-1, 1].
    """
    a = np.asarray(first, dtype=np.float32)
    b = np.asarray(second, dtype=np.float32)

    denominator = a + b
    with np.errstate(invalid="ignore", divide="ignore"):
        index = np.where(
            np.abs(denominator) > _DENOMINATOR_EPS,
            (a - b) / denominator,
            np.nan,
        ).astype(np.float32)

    out_of_range = np.isfinite(index) & ((index < -1.0) | (index > 1.0))
    index[out_of_range] = np.nan
    return index


def compute_ndvi(
    red: npt.NDArray,
    nir: npt.NDArray,
    scl: npt.NDArray | None = None,
    *,
    apply_reflectance_scaling: bool = True,
    add_offset: float = BOA_ADD_OFFSET,
) -> npt.NDArray[np.float32]:
    """Calcula el NDVI a partir de las bandas roja e infrarroja cercana.

    Args:
        red: banda B04 (rojo, 10 m).
        nir: banda B08 (infrarrojo cercano, 10 m).
        scl: banda de clasificacion de escena remuestreada a la malla de
            `red`. Si se omite no se aplica enmascarado de nubes.
        apply_reflectance_scaling: convertir de enteros a reflectancia antes
            del calculo. Se desactiva cuando las bandas ya vienen escaladas.
        add_offset: offset aditivo que corresponde a esta escena concreta.
            Depende de la linea base de procesado y de si el proveedor ya lo
            resto en el fichero, asi que lo decide quien conoce la escena
            (ver `catalog.Scene.boa_offset`), no esta funcion.

    Returns:
        NDVI en float32, con NaN en los pixeles invalidos.

    Raises:
        ValueError: si las bandas no comparten forma.
    """
    if red.shape != nir.shape:
        raise ValueError(
            f"Las bandas roja e infrarroja deben tener la misma forma: "
            f"{red.shape} != {nir.shape}"
        )
    if scl is not None and scl.shape != red.shape:
        raise ValueError(
            f"La banda SCL debe estar remuestreada a la malla de las bandas "
            f"espectrales: {scl.shape} != {red.shape}"
        )

    if apply_reflectance_scaling:
        red_values = to_reflectance(red, add_offset=add_offset)
        nir_values = to_reflectance(nir, add_offset=add_offset)
    else:
        red_values = np.asarray(red, dtype=np.float32)
        nir_values = np.asarray(nir, dtype=np.float32)

    ndvi = normalized_difference(nir_values, red_values)

    if scl is not None:
        ndvi = np.where(scl_valid_mask(scl), ndvi, np.nan).astype(np.float32)

    return ndvi


def valid_fraction(values: npt.NDArray) -> float:
    """Proporcion de pixeles no nulos de un array, en el intervalo [0, 1].

    Se usa como criterio de aceptacion: una escena mayoritariamente cubierta
    de nubes no debe generar un registro en la serie temporal.
    """
    array = np.asarray(values, dtype=np.float32)
    if array.size == 0:
        return 0.0
    return float(np.count_nonzero(np.isfinite(array)) / array.size)

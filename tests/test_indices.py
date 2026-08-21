"""Tests del algebra de bandas y el enmascarado de calidad.

Son tests deterministas sobre arrays sinteticos: no tocan red ni disco, de
forma que la parte mas delicada del calculo queda cubierta y la suite corre
en milisegundos.
"""

from __future__ import annotations

import numpy as np
import pytest

from ndvi_guadalquivir.indices import (
    BOA_ADD_OFFSET,
    INVALID_SCENE_CLASSES,
    QUANTIFICATION_VALUE,
    SceneClass,
    compute_ndvi,
    normalized_difference,
    scl_valid_mask,
    to_reflectance,
    valid_fraction,
)


class TestToReflectance:
    def test_aplica_offset_y_cuantificacion(self):
        # Un DN de 3000 con offset -1000 equivale a 0.2 de reflectancia.
        assert to_reflectance(np.array([3000])) == pytest.approx(0.2)

    def test_devuelve_float32(self):
        assert to_reflectance(np.array([1500], dtype="uint16")).dtype == np.float32

    def test_dn_igual_al_offset_da_reflectancia_nula(self):
        dn = np.array([-BOA_ADD_OFFSET])
        assert to_reflectance(dn) == pytest.approx(0.0)

    def test_ignorar_el_offset_sesga_el_resultado(self):
        """Documenta por que el offset importa: sin el, el valor se infla."""
        dn = np.array([3000])
        sin_offset = dn / QUANTIFICATION_VALUE
        con_offset = to_reflectance(dn)
        assert sin_offset > con_offset


class TestNormalizedDifference:
    def test_caso_conocido(self):
        # (0.4 - 0.1) / (0.4 + 0.1) = 0.6
        resultado = normalized_difference(np.array([0.4]), np.array([0.1]))
        assert resultado == pytest.approx(0.6)

    def test_bandas_iguales_dan_cero(self):
        valores = np.array([0.25, 0.5, 0.75])
        assert normalized_difference(valores, valores) == pytest.approx(0.0)

    def test_denominador_nulo_da_nan_y_no_infinito(self):
        """La proteccion que motivo el bug de NDVI = 1.4e6 en el prototipo."""
        resultado = normalized_difference(np.array([0.0]), np.array([0.0]))
        assert np.isnan(resultado).all()
        assert not np.isinf(resultado).any()

    def test_denominador_casi_nulo_da_nan(self):
        resultado = normalized_difference(np.array([1e-9]), np.array([-1e-9]))
        assert np.isnan(resultado).all()

    def test_resultado_siempre_en_rango_teorico(self):
        rng = np.random.default_rng(42)
        a = rng.uniform(-0.2, 1.2, size=5_000)
        b = rng.uniform(-0.2, 1.2, size=5_000)
        resultado = normalized_difference(a, b)
        finitos = resultado[np.isfinite(resultado)]
        assert finitos.size > 0
        assert np.all((finitos >= -1.0) & (finitos <= 1.0))


class TestSclValidMask:
    def test_marca_invalidas_las_clases_de_nube(self):
        scl = np.array([
            SceneClass.VEGETATION,
            SceneClass.CLOUD_HIGH_PROBABILITY,
            SceneClass.NOT_VEGETATED,
            SceneClass.CLOUD_SHADOW,
        ])
        assert scl_valid_mask(scl).tolist() == [True, False, True, False]

    def test_el_agua_se_conserva(self):
        """El NDVI negativo del agua es informativo, no un error."""
        assert scl_valid_mask(np.array([SceneClass.WATER])).all()

    def test_nodata_se_descarta(self):
        assert not scl_valid_mask(np.array([SceneClass.NO_DATA])).any()

    @pytest.mark.parametrize("clase", sorted(INVALID_SCENE_CLASSES))
    def test_todas_las_clases_invalidas_se_enmascaran(self, clase):
        assert not scl_valid_mask(np.array([clase])).any()


class TestComputeNdvi:
    def test_vegetacion_sana_da_ndvi_alto(self):
        # Rojo bajo e infrarrojo alto es la firma espectral de la vegetacion.
        red = np.array([[1500]])   # 0.05 tras el offset
        nir = np.array([[4000]])   # 0.30 tras el offset
        assert compute_ndvi(red, nir)[0, 0] == pytest.approx(0.714, abs=1e-3)

    def test_suelo_desnudo_da_ndvi_bajo(self):
        red = np.array([[3000]])   # 0.20
        nir = np.array([[3500]])   # 0.25
        assert 0.0 < compute_ndvi(red, nir)[0, 0] < 0.2

    def test_la_mascara_scl_anula_los_pixeles_cubiertos(self):
        red = np.full((2, 2), 1500)
        nir = np.full((2, 2), 4000)
        scl = np.array([
            [SceneClass.VEGETATION, SceneClass.CLOUD_HIGH_PROBABILITY],
            [SceneClass.CLOUD_SHADOW, SceneClass.NOT_VEGETATED],
        ])
        resultado = compute_ndvi(red, nir, scl)
        assert np.isfinite(resultado[0, 0]) and np.isfinite(resultado[1, 1])
        assert np.isnan(resultado[0, 1]) and np.isnan(resultado[1, 0])

    def test_sin_escalado_usa_los_valores_tal_cual(self):
        red = np.array([[0.1]])
        nir = np.array([[0.4]])
        resultado = compute_ndvi(red, nir, apply_reflectance_scaling=False)
        assert resultado[0, 0] == pytest.approx(0.6)

    def test_formas_incompatibles_lanzan_error(self):
        with pytest.raises(ValueError, match="misma forma"):
            compute_ndvi(np.zeros((2, 2)), np.zeros((3, 3)))

    def test_scl_sin_remuestrear_lanza_error(self):
        """SCL viene a 20 m; olvidar el remuestreo debe fallar pronto."""
        with pytest.raises(ValueError, match="remuestreada"):
            compute_ndvi(np.zeros((4, 4)), np.zeros((4, 4)), np.zeros((2, 2)))

    def test_escena_totalmente_nublada_no_deja_dato(self):
        red = np.full((3, 3), 1500)
        nir = np.full((3, 3), 4000)
        scl = np.full((3, 3), SceneClass.CLOUD_HIGH_PROBABILITY)
        assert np.isnan(compute_ndvi(red, nir, scl)).all()


class TestValidFraction:
    def test_sin_nan_es_uno(self):
        assert valid_fraction(np.ones((4, 4))) == 1.0

    def test_todo_nan_es_cero(self):
        assert valid_fraction(np.full((4, 4), np.nan)) == 0.0

    def test_mitad_nan(self):
        valores = np.array([1.0, np.nan, 2.0, np.nan])
        assert valid_fraction(valores) == 0.5

    def test_array_vacio_no_lanza_division_por_cero(self):
        assert valid_fraction(np.array([])) == 0.0

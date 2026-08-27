"""Tests de la fuente de precipitacion de AEMET.

Todo corre sin red. Lo que se prueba son las dos cosas que pueden estropear la
serie en silencio: la traduccion de los valores, donde una marca mal entendida
convierte lluvia en un hueco, y el troceado del intervalo, donde un dia perdido
entre dos ventanas no da error y no se ve hasta que alguien busca esa fecha.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from ndvi_guadalquivir.config import AemetSettings, Settings
from ndvi_guadalquivir.weather import (
    TRACE_PRECIPITATION_MM,
    WINDOW_DAYS,
    DailyWeather,
    EmptyWindowError,
    Station,
    daily_weather,
    list_stations,
    parse_coordinate,
    parse_decimal,
    parse_precipitation,
    windows,
)


class TestTraduccionDeValores:
    def test_la_coma_decimal_se_entiende(self):
        assert parse_decimal("12,4") == pytest.approx(12.4)

    def test_los_negativos_tambien(self):
        assert parse_decimal("-3,2") == pytest.approx(-3.2)

    def test_las_marcas_de_ausencia_dan_none(self):
        for marca in ("", "  ", "-", "Acum", "Varias"):
            assert parse_decimal(marca) is None

    def test_un_texto_desconocido_avisa_en_vez_de_callar(self, caplog):
        assert parse_decimal("loquesea") is None
        assert "sin traduccion" in caplog.text

    def test_la_lluvia_inapreciable_no_es_cero_ni_es_ausencia(self):
        """La distincion entre no llover y llover una gota importa en una sequia."""
        assert parse_precipitation("Ip") == TRACE_PRECIPITATION_MM
        assert parse_precipitation("Ip") > 0
        assert parse_precipitation("0,0") == 0.0
        assert parse_precipitation("") is None

    def test_las_coordenadas_pegadas_se_desmontan(self):
        assert parse_coordinate("370924N") == pytest.approx(37 + 9 / 60 + 24 / 3600)

    def test_oeste_y_sur_salen_negativos(self):
        assert parse_coordinate("052309W") < 0
        assert parse_coordinate("052309E") > 0


class TestTroceadoDelIntervalo:
    def test_ninguna_ventana_pasa_del_tope_de_la_api(self):
        tramos = list(windows(date(2018, 1, 1), date(2018, 12, 31)))
        assert all((fin - ini).days + 1 <= WINDOW_DAYS for ini, fin in tramos)

    def test_no_se_pierde_ni_un_dia_entre_ventanas(self):
        """El fallo que este test existe para cazar no da error, deja un hueco."""
        ini, fin = date(2023, 1, 1), date(2023, 3, 15)
        cubiertos: set[date] = set()
        for primero, ultimo in windows(ini, fin):
            dia = primero
            while dia <= ultimo:
                assert dia not in cubiertos, f"{dia} sale en dos ventanas"
                cubiertos.add(dia)
                dia += timedelta(days=1)
        assert len(cubiertos) == (fin - ini).days + 1
        assert min(cubiertos) == ini
        assert max(cubiertos) == fin

    def test_un_solo_dia_da_una_ventana(self):
        assert list(windows(date(2023, 5, 1), date(2023, 5, 1))) == [
            (date(2023, 5, 1), date(2023, 5, 1))
        ]

    def test_la_serie_completa_cabe_en_las_ventanas_previstas(self):
        """211 ventanas es el numero que gobierna el coste de la descarga."""
        tramos = list(windows(date(2018, 1, 1), date(2026, 8, 27)))
        assert len(tramos) == 211


def _settings() -> Settings:
    return Settings(aemet=AemetSettings(api_url="https://ejemplo.invalido", api_key="clave"))


class TestDescarga:
    """La red se sustituye por un doble que devuelve lo que devolveria AEMET."""

    @pytest.fixture
    def respuesta(self, monkeypatch):
        llamadas: list[str] = []

        def falso_request(path, settings, timeout=120):
            llamadas.append(path)
            return [
                {"indicativo": "5402", "fecha": "2023-04-01", "prec": "0,0",
                 "tmed": "17,4", "tmax": "26,4", "tmin": "8,3"},
                {"indicativo": "9999X", "fecha": "2023-04-01", "prec": "Ip",
                 "tmed": "12,0", "tmax": "15,0", "tmin": "9,0"},
            ]

        monkeypatch.setattr("ndvi_guadalquivir.weather._request", falso_request)
        return llamadas

    def test_traduce_las_filas(self, respuesta, monkeypatch):
        monkeypatch.setattr("ndvi_guadalquivir.weather.time.sleep", lambda _: None)
        filas = list(daily_weather(date(2023, 4, 1), date(2023, 4, 1), settings=_settings()))
        assert filas[0] == DailyWeather(
            station_id="5402", observed_on=date(2023, 4, 1),
            precipitation_mm=0.0, temp_mean_c=17.4, temp_max_c=26.4, temp_min_c=8.3,
        )

    def test_el_filtro_de_estaciones_se_aplica_al_llegar(self, respuesta, monkeypatch):
        monkeypatch.setattr("ndvi_guadalquivir.weather.time.sleep", lambda _: None)
        filas = list(daily_weather(
            date(2023, 4, 1), date(2023, 4, 1),
            settings=_settings(), station_ids={"5402"},
        ))
        assert [f.station_id for f in filas] == ["5402"]

    def test_una_ventana_vacia_se_salta_y_la_descarga_sigue(self, monkeypatch):
        """AEMET no tiene los ultimos dias porque los valida con retraso."""
        def sin_datos_en_la_segunda(path, settings, timeout=120):
            if "2023-04-16" in path:
                raise EmptyWindowError("No hay datos que satisfagan esos criterios")
            return [{"indicativo": "5402", "fecha": "2023-04-01", "prec": "1,2"}]

        monkeypatch.setattr("ndvi_guadalquivir.weather._request", sin_datos_en_la_segunda)
        monkeypatch.setattr("ndvi_guadalquivir.weather.time.sleep", lambda _: None)
        filas = list(daily_weather(date(2023, 4, 1), date(2023, 4, 30), settings=_settings()))
        # Abril son 30 dias, o sea dos ventanas de quince. La segunda esta vacia
        # y la primera entrega su fila igualmente.
        assert len(filas) == 1

    def test_un_corte_de_red_se_reintenta(self, monkeypatch):
        intentos = []

        def falla_y_luego_va(path, settings, timeout=120):
            intentos.append(path)
            if len(intentos) < 3:
                raise TimeoutError("conexion colgada")
            return [{"indicativo": "5402", "fecha": "2023-04-01", "prec": "1,2"}]

        monkeypatch.setattr("ndvi_guadalquivir.weather._request", falla_y_luego_va)
        monkeypatch.setattr("ndvi_guadalquivir.weather.time.sleep", lambda _: None)
        filas = list(daily_weather(date(2023, 4, 1), date(2023, 4, 5), settings=_settings()))
        assert len(intentos) == 3
        assert len(filas) == 1

    def test_si_los_reintentos_se_agotan_la_descarga_falla_en_vez_de_callar(
        self, monkeypatch
    ):
        """Tragarse el corte dejaria quince dias de lluvia fuera de la serie.

        Es la version meteorologica de la trampa 14: un hueco que nada delata.
        Mas vale parar la descarga entera y relanzarla.
        """
        def siempre_falla(path, settings, timeout=120):
            raise TimeoutError("conexion colgada")

        monkeypatch.setattr("ndvi_guadalquivir.weather._request", siempre_falla)
        monkeypatch.setattr("ndvi_guadalquivir.weather.time.sleep", lambda _: None)
        with pytest.raises(RuntimeError, match="limite de peticiones"):
            list(daily_weather(date(2023, 4, 1), date(2023, 4, 5), settings=_settings()))

    def test_sin_clave_falla_con_un_mensaje_que_dice_que_hacer(self):
        vacia = Settings(aemet=AemetSettings(api_key=""))
        with pytest.raises(RuntimeError, match="AEMET_API_KEY"):
            list_stations(vacia)


class TestEstacion:
    def test_es_inmutable_como_el_resto_de_modelos_del_proyecto(self):
        estacion = Station("5402", "CORDOBA", "CORDOBA", -4.85, 37.84)
        with pytest.raises(AttributeError):
            estacion.name = "otro"

"""Tests de la capa de datos del panel.

El almacen real pesa 35 MB y tarda en construirse quince horas, asi que aqui se
fabrica uno de juguete con las mismas tablas y las mismas columnas. Es la misma
tactica que en `test_zones.py`: no se prueba contra el dato de verdad sino
contra algo que tiene su forma, y asi los tests corren en milisegundos y sin
depender de que alguien haya lanzado `dbt build` antes.

Lo que se comprueba aqui son justo las cosas que un panel rompe en silencio: que
una consulta devuelva las columnas que la pantalla va a pintar, que el filtro por
semana filtre de verdad, y que la geometria simplificada siga siendo la misma
geometria. Un fallo en cualquiera de esas tres no da error en Streamlit, da una
pagina con datos equivocados.
"""

from __future__ import annotations

import duckdb
import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Polygon, box

from ndvi_guadalquivir.panel import (
    CATEGORY_COLORS,
    CATEGORY_ORDER,
    anomaly_snapshot,
    available_weeks,
    basin_monthly,
    category_color,
    headline_numbers,
    load_map_geometry,
    municipality_options,
    municipality_series,
    open_warehouse,
)


@pytest.fixture
def warehouse(tmp_path):
    """Un almacen de juguete con la forma del que construye dbt.

    Tres municipios, dos semanas y dos meses. Suficiente para que cada consulta
    tenga algo que devolver y algo que descartar, que es lo unico que hace falta
    para saber si el filtro funciona.
    """
    ruta = tmp_path / "warehouse.duckdb"
    con = duckdb.connect(str(ruta))
    con.execute("CREATE SCHEMA main_gold")

    con.execute("""
        CREATE TABLE main_gold.ndvi_anomaly AS
        SELECT * FROM (VALUES
            ('14001', 'Adamuz',   2023, 18, DATE '2023-05-01', DATE '2023-05-03',
             2, 0.93, 0.31, 0.42, 0.04, 6, true, -0.11, -2.75, 'muy por debajo'),
            ('14002', 'Baena',    2023, 18, DATE '2023-05-02', DATE '2023-05-02',
             1, 0.88, 0.40, 0.41, 0.03, 6, true, -0.01, -0.33, 'normal'),
            ('14003', 'Cabra',    2023, 18, DATE '2023-05-02', DATE '2023-05-02',
             1, 0.91, 0.50, 0.44, 0.02, 6, true,  0.06,  3.00, 'muy por encima'),
            ('14001', 'Adamuz',   2024, 18, DATE '2024-05-01', DATE '2024-05-01',
             1, 0.95, 0.45, 0.42, 0.04, 6, true,  0.03,  0.75, 'normal')
        ) AS t(zone_id, zone_name, anio, semana, primera_fecha, ultima_fecha,
               observaciones, fraccion_util_media, ndvi_observado, ndvi_normal,
               ndvi_desviacion, anios_observados, normal_fiable,
               anomalia_ndvi, anomalia_sigmas, categoria)
    """)

    con.execute("""
        CREATE TABLE main_gold.ndvi_monthly_by_province AS
        SELECT * FROM (VALUES ('14', 'Cordoba', 2023, 5, 0.42))
        AS t(province_code, province_name, anio, mes, ndvi_ponderado_area)
    """)

    con.execute("""
        CREATE TABLE main_gold.ndvi_weather_monthly AS
        SELECT * FROM (VALUES
            (DATE '2023-04-01', 2023, 4, -0.081, 0.99, 2.2, 47.5, -45.3, 0.02,
             -144.6, -144.6, 445, 90),
            (DATE '2023-05-01', 2023, 5, -0.088, 0.99, 78.8, 47.5, 31.3, 2.37,
             -85.6, -85.6, 285, 84)
        ) AS t(mes_fecha, anio, mes, anomalia_ndvi, fraccion_municipios_por_debajo,
               lluvia_mm, lluvia_normal_mm, anomalia_lluvia_mm, ratio_lluvia,
               anomalia_lluvia_3m_mm, anomalia_lluvia_6m_mm, municipios, estaciones)
    """)
    con.close()
    return ruta


@pytest.fixture
def zones(tmp_path):
    """Tres municipios cuadrados, con las columnas que trae el GeoPackage real."""
    ruta = tmp_path / "zones.gpkg"
    capa = gpd.GeoDataFrame(
        {
            "zone_id": ["14001", "14002", "14003"],
            "zone_name": ["Adamuz", "Baena", "Cabra"],
            "province_code": ["14", "14", "14"],
            "area_km2": [335.0, 292.0, 227.0],
            "basin_overlap_fraction": [1.0, 1.0, 0.8],
        },
        geometry=[box(-5, 37, -4.9, 37.1), box(-4.9, 37, -4.8, 37.1),
                  box(-4.8, 37, -4.7, 37.1)],
        crs="EPSG:4326",
    )
    capa.to_file(ruta, driver="GPKG")
    return ruta


# ---------------------------------------------------------------------------
# Apertura del almacen
# ---------------------------------------------------------------------------


def test_open_warehouse_explica_que_falta_dbt(tmp_path):
    """El error dice como arreglarlo, no solo que fallo.

    Es el fallo que va a tener cualquiera que clone el repositorio y lance el
    panel antes de construir nada. Un `FileNotFoundError` pelado le dejaria
    buscando; el mensaje lleva el comando dentro.
    """
    with pytest.raises(FileNotFoundError, match="dbt build"):
        open_warehouse(tmp_path / "no_existe.duckdb")


def test_open_warehouse_abre_en_solo_lectura(warehouse):
    con = open_warehouse(warehouse)
    with pytest.raises(duckdb.Error):
        con.execute("CREATE TABLE main_gold.intruso (x INTEGER)")


# ---------------------------------------------------------------------------
# Consultas
# ---------------------------------------------------------------------------


def test_available_weeks_ordena_de_lo_mas_reciente_a_lo_mas_antiguo(warehouse):
    semanas = available_weeks(open_warehouse(warehouse))
    assert list(semanas.anio) == [2024, 2023]
    assert semanas.iloc[0].municipios == 1
    assert semanas.iloc[1].municipios == 3


def test_anomaly_snapshot_filtra_por_semana(warehouse):
    """Que el filtro filtre. Sin esto el mapa pintaria los nueve anos a la vez."""
    datos = anomaly_snapshot(open_warehouse(warehouse), 2023, 18)
    assert len(datos) == 3
    assert set(datos.zone_id) == {"14001", "14002", "14003"}

    otra = anomaly_snapshot(open_warehouse(warehouse), 2024, 18)
    assert list(otra.zone_id) == ["14001"]


def test_anomaly_snapshot_ordena_por_gravedad(warehouse):
    """El peor primero, porque la tabla de la pantalla muestra los quince peores."""
    datos = anomaly_snapshot(open_warehouse(warehouse), 2023, 18)
    assert list(datos.zone_name) == ["Adamuz", "Baena", "Cabra"]
    assert datos.anomalia_sigmas.is_monotonic_increasing


def test_anomaly_snapshot_trae_las_columnas_que_pinta_el_mapa(warehouse):
    datos = anomaly_snapshot(open_warehouse(warehouse), 2023, 18)
    for columna in ("zone_id", "zone_name", "anomalia_ndvi", "anomalia_sigmas",
                    "categoria"):
        assert columna in datos.columns


def test_municipality_series_devuelve_una_fecha_utilizable(warehouse):
    """La fecha se deriva de ano y semana, y ese calculo es facil de romper.

    DuckDB no deja sumar un entero a una fecha, hace falta convertirlo a dias.
    La primera version del panel lo hacia mal y la grafica no salia, asi que el
    caso se queda escrito.
    """
    serie = municipality_series(open_warehouse(warehouse), "14001")
    assert len(serie) == 2
    assert pd.api.types.is_datetime64_any_dtype(serie.fecha)
    assert serie.fecha.iloc[0].year == 2023
    assert serie.anio.is_monotonic_increasing


def test_municipality_series_de_un_municipio_sin_dato_sale_vacia(warehouse):
    serie = municipality_series(open_warehouse(warehouse), "99999")
    assert serie.empty


def test_municipality_options_resuelve_la_provincia(warehouse, zones):
    """El nombre de la provincia sale de dbt, no de una lista copiada aqui."""
    capa = load_map_geometry(zones, cache_path=None)
    opciones = municipality_options(open_warehouse(warehouse), capa)

    assert len(opciones) == 3
    assert set(opciones.province_name) == {"Cordoba"}
    assert opciones.loc[opciones.zone_id == "14001", "semanas_con_dato"].iloc[0] == 2


def test_municipality_options_no_pierde_municipios_sin_provincia(warehouse, zones):
    """Un municipio cuyo codigo no este en la tabla de provincias sigue saliendo.

    Es el caso de los condominios de la trampa 4. Preferimos verlos como
    "Desconocida" a que desaparezcan del selector sin que nadie lo note.
    """
    capa = load_map_geometry(zones, cache_path=None)
    capa.loc[capa.zone_id == "14003", "province_code"] = "53"
    opciones = municipality_options(open_warehouse(warehouse), capa)

    assert len(opciones) == 3
    assert "Desconocida" in set(opciones.province_name)


def test_basin_monthly_sale_ordenado_en_el_tiempo(warehouse):
    mensual = basin_monthly(open_warehouse(warehouse))
    assert len(mensual) == 2
    assert mensual.mes_fecha.is_monotonic_increasing
    assert "anomalia_lluvia_6m_mm" in mensual.columns


def test_headline_numbers_resume_el_almacen(warehouse):
    cifras = headline_numbers(open_warehouse(warehouse))
    assert cifras["observaciones"] == 4
    assert cifras["municipios"] == 3
    assert str(cifras["desde"]) == "2023-05-01"
    assert cifras["peor_semana"][0] == "Adamuz"


# ---------------------------------------------------------------------------
# Geometria
# ---------------------------------------------------------------------------


def test_load_map_geometry_conserva_municipios_y_columnas(zones):
    capa = load_map_geometry(zones, cache_path=None)
    assert len(capa) == 3
    assert capa.crs.to_epsg() == 4326
    for columna in ("zone_id", "zone_name", "province_code", "area_km2"):
        assert columna in capa.columns


def test_load_map_geometry_no_mueve_el_area(zones):
    """Simplificar puede aligerar el dibujo, no cambiar el territorio.

    Es la comprobacion que justifica la tolerancia elegida. Sobre la capa real
    el error medido es del 0,00%; aqui se exige menos del 1%, que sobre
    cuadrados de once kilometros de lado es holgado y sigue cazando una
    tolerancia disparatada.
    """
    original = gpd.read_file(zones).to_crs(25830).area.sum()
    simplificada = load_map_geometry(zones, cache_path=None).to_crs(25830).area.sum()
    assert abs(simplificada - original) / original < 0.01


def test_load_map_geometry_reduce_los_vertices(tmp_path):
    """Que simplifique de verdad, no que devuelva lo mismo.

    Un cuadrado no se puede simplificar mas, asi que el test necesita un
    poligono con vertices de sobra: un circulo de muchos lados, donde una
    tolerancia de cien metros si tiene algo que quitar.
    """
    import numpy as np

    angulos = np.linspace(0, 2 * np.pi, 400, endpoint=False)
    # Un grado de latitud son unos 111 km, asi que este radio da unos 5,5 km.
    puntos = [(-4.8 + 0.05 * np.cos(a), 37.5 + 0.05 * np.sin(a)) for a in angulos]

    ruta = tmp_path / "circulo.gpkg"
    gpd.GeoDataFrame(
        {"zone_id": ["14001"], "zone_name": ["Circulo"], "province_code": ["14"]},
        geometry=[Polygon(puntos)],
        crs="EPSG:4326",
    ).to_file(ruta, driver="GPKG")

    antes = len(gpd.read_file(ruta).geometry.iloc[0].exterior.coords)
    despues = len(load_map_geometry(ruta, cache_path=None).geometry.iloc[0].exterior.coords)
    assert despues < antes


def test_load_map_geometry_usa_la_cache_la_segunda_vez(zones, tmp_path):
    """La cache existe para no repetir el trabajo en cada arranque del panel."""
    cache = tmp_path / "cacheada.geojson"
    assert not cache.exists()

    primera = load_map_geometry(zones, cache_path=cache)
    assert cache.exists()

    # Se borra el origen: si la segunda llamada funciona, es que leyo la cache.
    zones.unlink()
    segunda = load_map_geometry(zones, cache_path=cache)
    assert len(segunda) == len(primera)


# ---------------------------------------------------------------------------
# Colores
# ---------------------------------------------------------------------------


def test_todas_las_categorias_tienen_color():
    """La leyenda y el mapa se dibujan de la misma lista, y tienen que casar."""
    assert set(CATEGORY_ORDER) == set(CATEGORY_COLORS)


def test_category_color_no_revienta_con_una_categoria_nueva():
    """Si dbt anade una categoria manana, el mapa la pinta gris y sigue vivo."""
    assert category_color("muy por debajo") == CATEGORY_COLORS["muy por debajo"]
    assert category_color("categoria inventada") == CATEGORY_COLORS["sin referencia"]
    assert category_color(None) == CATEGORY_COLORS["sin referencia"]

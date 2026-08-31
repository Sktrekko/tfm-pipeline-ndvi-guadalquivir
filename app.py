"""Panel de exploracion: el mismo dato del almacen, pero mirable.

Este fichero es solo la cara. Todas las consultas viven en
`ndvi_guadalquivir.panel`, que no importa Streamlit ni Folium y por eso se puede
probar sin navegador. Aqui no hay ni una regla de negocio: si un numero de la
pantalla no cuadra, el sitio donde mirar es el SQL de dbt, no esto.

Se lanza asi, y no hace falta Docker:

    uv run --group panel streamlit run app.py

Por que tres vistas y no un cuadro de mando
--------------------------------------------
La tentacion en un panel es meter todo lo que hay. Aqui hay tres pantallas
porque el proyecto responde a tres preguntas distintas y cada una necesita una
forma distinta de dibujo.

El **mapa** responde a donde. Es una anomalia con signo (por debajo o por encima
de lo normal), asi que la escala es divergente: dos colores opuestos con un
neutro en medio. Un arcoiris aqui seria un error, porque inventaria orden donde
lo que hay es distancia a un centro.

La **serie por municipio** responde a cuando. Dos lineas, lo observado y su
normal, porque una linea de NDVI sola sube en primavera y baja en verano todos
los anos y eso no informa de nada.

El **cruce** responde a por que. Y ahi va la unica decision de dibujo que merece
explicacion: son dos graficas apiladas y no una con dos ejes verticales. Meter
milimetros de lluvia y unidades de NDVI en el mismo eje doble deja que quien lo
dibuja decida, moviendo las escalas, si las dos curvas parecen ir juntas o no.
Apiladas y compartiendo el eje del tiempo se comparan igual de bien y no hay
forma de hacer trampa con el encuadre.
"""

from __future__ import annotations

import altair as alt
import folium
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

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

st.set_page_config(page_title="NDVI Guadalquivir", page_icon="🛰️", layout="wide")

#: Centro y zoom de partida del mapa. La cuenca es muy ancha y poco alta, asi
#: que el encuadre se fija a mano en vez de dejar que Folium lo deduzca.
BASIN_CENTER = (37.6, -4.8)
BASIN_ZOOM = 7


# ---------------------------------------------------------------------------
# Carga, cacheada
# ---------------------------------------------------------------------------
# Streamlit vuelve a ejecutar el fichero entero cada vez que se toca un widget.
# Sin cache, mover el deslizador releeria el GeoJSON de un mega y reabriria la
# base de datos en cada movimiento. `cache_resource` guarda objetos vivos como
# la conexion; `cache_data` guarda resultados que se pueden copiar.


@st.cache_resource
def _connection():
    return open_warehouse()


@st.cache_data
def _geometry():
    return load_map_geometry()


@st.cache_data
def _weeks():
    return available_weeks(_connection())


@st.cache_data
def _options():
    return municipality_options(_connection(), _geometry())


@st.cache_data
def _snapshot(anio: int, semana: int):
    return anomaly_snapshot(_connection(), anio, semana)


@st.cache_data
def _series(zone_id: str):
    return municipality_series(_connection(), zone_id)


@st.cache_data
def _monthly():
    return basin_monthly(_connection())


def _legend() -> None:
    """Leyenda con el nombre de cada categoria escrito al lado del color.

    No es decoracion. La escala divergente tiene poco contraste contra el fondo
    del mapa, asi que el color solo no basta para identificar una categoria:
    hace falta que el nombre este escrito. Es la misma razon por la que debajo
    del mapa hay una tabla con los numeros.
    """
    trozos = [
        f'<span style="display:inline-flex;align-items:center;gap:6px;margin-right:16px">'
        f'<span style="width:14px;height:14px;border-radius:3px;background:{CATEGORY_COLORS[c]};'
        f'border:1px solid rgba(0,0,0,.25)"></span>'
        f'<span style="font-size:13px">{c}</span></span>'
        for c in CATEGORY_ORDER
    ]
    st.markdown(
        f'<div style="display:flex;flex-wrap:wrap;margin:2px 0 10px">{"".join(trozos)}</div>',
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Cabecera
# ---------------------------------------------------------------------------
st.title("Vegetacion de la cuenca del Guadalquivir")

try:
    cifras = headline_numbers(_connection())
except FileNotFoundError as error:
    st.error(str(error))
    st.stop()

col1, col2, col3, col4 = st.columns(4)
col1.metric("Observaciones", f"{cifras['observaciones']:,}".replace(",", "."))
col2.metric("Municipios", cifras["municipios"])
col3.metric("Desde", str(cifras["desde"]))
col4.metric("Hasta", str(cifras["hasta"]))

st.caption(
    "NDVI semanal por municipio a partir de Sentinel-2, comparado con la normal "
    "de ese mismo municipio en esa misma semana del ano. Los datos salen de la "
    "capa gold que construye dbt."
)

mapa_tab, serie_tab, cruce_tab = st.tabs(
    ["Mapa de anomalia", "Serie por municipio", "Cruce con la lluvia"]
)


# ---------------------------------------------------------------------------
# 1. Mapa
# ---------------------------------------------------------------------------
with mapa_tab:
    semanas = _weeks()

    # La etiqueta se arma corta a proposito. El deslizador la escribe encima de
    # si mismo y una etiqueta larga se sale del contenedor por la izquierda, que
    # es un fallo que no se ve hasta que se abre la pagina de verdad.
    etiquetas = [
        f"{pd.Timestamp(fila.desde):%d/%m/%Y} · sem. {int(fila.semana)}"
        for fila in semanas.itertuples()
    ]
    posicion = st.select_slider(
        "Semana",
        options=list(range(len(etiquetas))),
        value=0,
        format_func=lambda i: etiquetas[i],
        help="La lista va de la semana mas reciente a la mas antigua.",
    )
    fila = semanas.iloc[posicion]
    anio, semana = int(fila.anio), int(fila.semana)

    datos = _snapshot(anio, semana)

    # Cuantos municipios cubre esta semana. Importa decirlo: en invierno las
    # nubes dejan semanas con menos de la mitad de la cuenca, y un mapa medio
    # vacio se lee mal si no se avisa de por que esta vacio.
    cobertura = len(datos) / cifras["municipios"]
    if cobertura < 0.6:
        st.warning(
            f"Esta semana solo tiene dato en {len(datos)} de los "
            f"{cifras['municipios']} municipios ({cobertura:.0%} de la cuenca). "
            "Las nubes tiraron el resto de observaciones."
        )

    izquierda, derecha = st.columns([3, 2])

    with izquierda:
        _legend()

        geometria = _geometry()
        dibujable = geometria[geometria.zone_id.isin(datos.zone_id)].copy()
        dibujable = dibujable.merge(
            datos[["zone_id", "anomalia_ndvi", "anomalia_sigmas", "categoria"]],
            on="zone_id",
            how="left",
        )
        dibujable["anomalia_ndvi"] = dibujable["anomalia_ndvi"].round(3)
        dibujable["anomalia_sigmas"] = dibujable["anomalia_sigmas"].round(2)

        # Mapa base de OpenStreetMap y no el de CartoDB, que es mas sobrio y
        # seria mejor fondo para un coropleto: desde hace poco pide clave de API
        # y sin ella estampa una marca de agua sobre el mapa entero. OSM no pide
        # nada y el proyecto no depende de una cuenta de un tercero.
        lienzo = folium.Map(
            location=BASIN_CENTER, zoom_start=BASIN_ZOOM, tiles="OpenStreetMap"
        )
        folium.GeoJson(
            dibujable,
            style_function=lambda rasgo: {
                "fillColor": category_color(rasgo["properties"]["categoria"]),
                "color": "#666666",
                "weight": 0.3,
                "fillOpacity": 0.85,
            },
            highlight_function=lambda _: {"weight": 2, "color": "#222222"},
            tooltip=folium.GeoJsonTooltip(
                fields=["zone_name", "anomalia_ndvi", "anomalia_sigmas", "categoria"],
                aliases=["Municipio", "Anomalia NDVI", "Desviaciones", "Estado"],
                sticky=True,
            ),
        ).add_to(lienzo)

        # `returned_objects=[]` evita que Streamlit vuelva a ejecutarlo todo
        # cada vez que el raton pasa por encima del mapa. Sin eso el panel se
        # arrastra, y el motivo no se adivina mirando el codigo.
        st_folium(lienzo, height=520, width=None, returned_objects=[])

    with derecha:
        st.markdown(f"**Los quince municipios peor parados** · {anio}, semana {semana}")
        st.caption(
            "La tabla existe porque el color solo no basta para leer un valor. "
            "Aqui estan los numeros exactos."
        )
        tabla = (
            datos[["zone_name", "anomalia_ndvi", "anomalia_sigmas", "categoria"]]
            .head(15)
            .rename(
                columns={
                    "zone_name": "Municipio",
                    "anomalia_ndvi": "Anomalia",
                    "anomalia_sigmas": "Sigmas",
                    "categoria": "Estado",
                }
            )
        )
        st.dataframe(
            tabla.style.format({"Anomalia": "{:.3f}", "Sigmas": "{:.2f}"}),
            hide_index=True,
            width="stretch",
        )

        reparto = (
            datos.categoria.value_counts()
            .reindex(CATEGORY_ORDER)
            .dropna()
            .astype(int)
            .rename("municipios")
            .reset_index()
        )
        reparto.columns = ["categoria", "municipios"]
        st.altair_chart(
            alt.Chart(reparto)
            .mark_bar(cornerRadiusEnd=4, height=18)
            .encode(
                x=alt.X("municipios:Q", title="Municipios"),
                y=alt.Y("categoria:N", sort=CATEGORY_ORDER, title=None),
                color=alt.Color(
                    "categoria:N",
                    scale=alt.Scale(
                        domain=CATEGORY_ORDER,
                        range=[CATEGORY_COLORS[c] for c in CATEGORY_ORDER],
                    ),
                    legend=None,
                ),
                tooltip=["categoria", "municipios"],
            )
            .properties(height=180, title="Como se reparte la cuenca esa semana"),
            width="stretch",
        )


# ---------------------------------------------------------------------------
# 2. Serie por municipio
# ---------------------------------------------------------------------------
with serie_tab:
    opciones = _options()
    etiqueta_municipio = {
        fila.zone_id: f"{fila.zone_name} ({fila.province_name})"
        for fila in opciones.itertuples()
    }

    elegido = st.selectbox(
        "Municipio",
        options=list(etiqueta_municipio),
        format_func=lambda z: etiqueta_municipio[z],
        index=0,
    )
    serie = _series(elegido)

    if serie.empty:
        st.info("Ese municipio no tiene observaciones.")
    else:
        largo = serie.melt(
            id_vars=["fecha"],
            value_vars=["ndvi_observado", "ndvi_normal"],
            var_name="serie",
            value_name="ndvi",
        )
        largo["serie"] = largo["serie"].map(
            {"ndvi_observado": "Observado", "ndvi_normal": "Normal de esa semana"}
        )

        seleccion = alt.selection_point(fields=["fecha"], nearest=True, on="pointerover",
                                        empty=False)

        lineas = (
            alt.Chart(largo)
            .mark_line(strokeWidth=2)
            .encode(
                x=alt.X("fecha:T", title=None),
                y=alt.Y("ndvi:Q", title="NDVI", scale=alt.Scale(zero=False)),
                color=alt.Color(
                    "serie:N",
                    title=None,
                    scale=alt.Scale(
                        domain=["Observado", "Normal de esa semana"],
                        range=["#276419", "#9E9E9E"],
                    ),
                    # Arriba y dentro del area de dibujo. Por defecto Altair la
                    # saca a la derecha, y con la grafica estirada al ancho del
                    # contenedor se queda fuera del recorte y no se ve ninguna.
                    # Con dos series la leyenda no es opcional: sin ella no hay
                    # forma de saber cual de las dos lineas es la normal.
                    legend=alt.Legend(orient="top", direction="horizontal"),
                ),
                strokeDash=alt.StrokeDash(
                    "serie:N",
                    scale=alt.Scale(
                        domain=["Observado", "Normal de esa semana"],
                        range=[[1, 0], [4, 3]],
                    ),
                    legend=None,
                ),
            )
        )
        puntos = (
            lineas.mark_point(size=60, filled=True)
            .encode(
                opacity=alt.condition(seleccion, alt.value(1), alt.value(0)),
                tooltip=[
                    alt.Tooltip("fecha:T", title="Semana"),
                    alt.Tooltip("serie:N", title=None),
                    alt.Tooltip("ndvi:Q", title="NDVI", format=".3f"),
                ],
            )
            .add_params(seleccion)
        )
        st.altair_chart(
            (lineas + puntos).properties(
                height=300, title=f"NDVI de {etiqueta_municipio[elegido]} frente a su normal"
            ),
            width="stretch",
        )

        # La anomalia va en su propia grafica y no encima de la anterior. Es la
        # misma regla que en la vista del cruce: dos magnitudes distintas, dos
        # graficas que comparten el eje del tiempo.
        # Barras y no un area rellena, aunque el area quede mas suave. Un area es
        # una sola figura y toma un unico color, asi que la regla de pintar de
        # marron lo que baja y de verde lo que sube no se aplicaba: salia todo
        # marron, incluidos los anos buenos. Cada barra es una figura aparte y
        # si puede llevar su propio color.
        st.altair_chart(
            alt.Chart(serie)
            .mark_bar(size=2)
            .encode(
                x=alt.X("fecha:T", title=None),
                y=alt.Y("anomalia_ndvi:Q", title="Anomalia"),
                color=alt.condition(
                    alt.datum.anomalia_ndvi < 0,
                    alt.value(CATEGORY_COLORS["muy por debajo"]),
                    alt.value(CATEGORY_COLORS["muy por encima"]),
                ),
                tooltip=[
                    alt.Tooltip("fecha:T", title="Semana"),
                    alt.Tooltip("anomalia_ndvi:Q", title="Anomalia", format=".3f"),
                    alt.Tooltip("anomalia_sigmas:Q", title="Sigmas", format=".2f"),
                    alt.Tooltip("categoria:N", title="Estado"),
                ],
            )
            .properties(height=170, title="Cuanto se aparta de lo normal"),
            width="stretch",
        )

        with st.expander("Ver los datos de este municipio"):
            st.dataframe(serie, hide_index=True, width="stretch")


# ---------------------------------------------------------------------------
# 3. El cruce con la lluvia
# ---------------------------------------------------------------------------
with cruce_tab:
    mensual = _monthly()

    st.markdown(
        "Dos instrumentos que no se hablan entre si: un sensor optico en orbita y "
        "109 pluviometros de AEMET en el suelo. Si senalan el mismo ano, la "
        "coincidencia no puede venir de un fallo compartido."
    )

    eje = alt.X("mes_fecha:T", title=None)
    ancho_barra = alt.value(6)

    arriba = (
        alt.Chart(mensual)
        .mark_bar(cornerRadiusEnd=3, size=6)
        .encode(
            x=eje,
            y=alt.Y("anomalia_ndvi:Q", title="Anomalia de NDVI"),
            color=alt.condition(
                alt.datum.anomalia_ndvi < 0,
                alt.value(CATEGORY_COLORS["muy por debajo"]),
                alt.value(CATEGORY_COLORS["muy por encima"]),
            ),
            tooltip=[
                alt.Tooltip("mes_fecha:T", title="Mes"),
                alt.Tooltip("anomalia_ndvi:Q", title="Anomalia NDVI", format=".4f"),
                alt.Tooltip("municipios:Q", title="Municipios"),
            ],
        )
        .properties(height=200, title="Vegetacion: cuanto se aparta de lo normal")
    )

    abajo = (
        alt.Chart(mensual)
        .mark_bar(cornerRadiusEnd=3, size=6)
        .encode(
            x=eje,
            y=alt.Y("anomalia_lluvia_mm:Q", title="Anomalia de lluvia (mm)"),
            color=alt.condition(
                alt.datum.anomalia_lluvia_mm < 0,
                alt.value("#8C510A"),
                alt.value("#01665E"),
            ),
            tooltip=[
                alt.Tooltip("mes_fecha:T", title="Mes"),
                alt.Tooltip("lluvia_mm:Q", title="Lluvia (mm)", format=".1f"),
                alt.Tooltip("lluvia_normal_mm:Q", title="Normal (mm)", format=".1f"),
                alt.Tooltip("anomalia_lluvia_mm:Q", title="Anomalia", format=".1f"),
                alt.Tooltip("estaciones:Q", title="Estaciones"),
            ],
        )
        .properties(height=200, title="Lluvia: cuanto se aparta de lo normal")
    )

    st.altair_chart(alt.vconcat(arriba, abajo).resolve_scale(x="shared"),
                    width="stretch")

    st.caption(
        "Dos graficas y no una con dos ejes verticales, a proposito: con dos "
        "escalas en el mismo dibujo, quien lo dibuja decide si las curvas parecen "
        "ir juntas o no. Compartiendo solo el eje del tiempo, eso no se puede hacer."
    )

    anual = (
        mensual.assign(anio=lambda d: pd.to_datetime(d.mes_fecha).dt.year)
        .groupby("anio")
        .agg(
            lluvia=("lluvia_mm", "sum"),
            normal=("lluvia_normal_mm", "sum"),
            anomalia_ndvi=("anomalia_ndvi", "mean"),
            meses=("mes_fecha", "count"),
        )
        .reset_index()
    )
    anual["ratio"] = (anual.lluvia / anual.normal).round(2)
    anual["anomalia_ndvi"] = anual.anomalia_ndvi.round(4)

    st.markdown("**Ano a ano, las dos columnas que no se hablan**")
    st.dataframe(
        anual[["anio", "meses", "lluvia", "ratio", "anomalia_ndvi"]].rename(
            columns={
                "anio": "Ano",
                "meses": "Meses con dato",
                "lluvia": "Lluvia (mm)",
                "ratio": "Sobre lo normal",
                "anomalia_ndvi": "Anomalia NDVI",
            }
        ),
        hide_index=True,
        width="stretch",
    )

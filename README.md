# Pipeline Geoespacial de Monitorización de Vegetación

Pipeline de ingeniería de datos que calcula series temporales del índice **NDVI**
sobre la **cuenca del Guadalquivir** a partir de imágenes **Sentinel-2** del
programa **Copernicus**, sin descargar las escenas completas.

Trabajo Fin de Máster. Máster en Big Data & Data Engineering, UCM / NTIC.
Autor: Darío Rodríguez González. Tutores: Jorge Centeno y Alberto González.

## El problema

Una sola escena Sentinel-2 ocupa más de 800 MB, y seguir una cuenca entera durante
varios años implica cientos de escenas. Descargarlas para procesarlas en local no
escala. Este proyecto usa el catálogo **STAC** para localizar imágenes y lee
directamente por HTTP **solo la ventana de píxeles necesaria** de los COG
(Cloud Optimized GeoTIFF) alojados en la nube.

Medido sobre una comarca de la campiña cordobesa: **8,4 MB leídos frente a los
120 MB que ocupa una banda completa.**

## Arquitectura de la capa de procesamiento

```
STAC API ──► catalog.py ──► raster.py ──► mosaic.py ──► zonal.py ──► schemas.py
 (busca)      (escenas)     (ventana)     (diario)     (polígonos)  (contrato)
                                │
                            indices.py
                         (NDVI + máscara SCL)
```

| Módulo | Responsabilidad |
|---|---|
| `catalog.py` | Descubrimiento de escenas vía STAC. Traduce items a un modelo interno. |
| `raster.py`  | Lectura de bandas por ventana espacial desde COG remotos. |
| `indices.py` | Álgebra de bandas y enmascarado de calidad. **Funciones puras**, sin E/S. |
| `mosaic.py`  | Combina los tiles MGRS de una misma fecha sobre una malla canónica. |
| `zonal.py`   | Agrega el ráster a polígonos: una fila por zona y fecha. |
| `schemas.py` | Contratos de datos validados con Pandera. |
| `aoi.py`     | Zonas de estudio y recorte a la cuenca. |
| `weather.py` | Precipitación y temperatura diarias de AEMET, la segunda fuente. |
| `panel.py`   | Consultas del panel. No importa Streamlit: la interfaz vive en `app.py`. |
| `config.py`  | Configuración por variables de entorno. |

## Decisiones de diseño relevantes

**Máscara SCL obligatoria.** El NDVI se calcula sobre reflectancia corregida
aplicando el desplazamiento `BOA_ADD_OFFSET = -1000` de la línea base de procesado
04.00, y se enmascaran nubes, sombras y nieve mediante la banda de clasificación
de escena. Sin proteger el denominador `NIR + Rojo`, los píxeles de relleno
producen valores de NDVI de varios órdenes de magnitud.

**La unidad de trabajo es la fecha, no la escena.** La malla MGRS divide el
territorio en tiles de 110 km y una comarca puede quedar a caballo entre dos.
Todas las escenas de una pasada se mosaican antes de agregar.

**Malla canónica derivada del área de estudio.** El mosaico se proyecta sobre una
malla anclada a múltiplos de la resolución y calculada a partir de la zona de
estudio, nunca de la extensión del primer tile. Así todas las fechas son
comparables píxel a píxel y las variaciones observadas son del terreno.

**Agregación a polígonos.** La salida no es un ráster sino una tabla de series
temporales por zona administrativa, que es el dato que consume un analista y lo
que hace manejable el volumen.

## Orquestación

Dos DAG de **Airflow 3** en `dags/`, y la relación entre ambos es la parte
interesante:

```
ndvi_ingest  (cada día a las 5:00)          ndvi_transform  (sin calendario)
  pending_dates                                dbt_build
       │                                          │
  ingest_date × una tarea por fecha           summarize
       │                                          │
  publish ──────► [ ndvi_zonal_daily ] ──────► [ ndvi_anomaly ]
                     asset de bronze              asset de gold
```

`ndvi_transform` no tiene hora. Se programa **sobre el asset** que produce la
ingesta, de modo que dbt corre cuando entra dato nuevo y no corre cuando no
entra. Sobre esta cuenca el satélite pasa cada cinco días, así que la mayoría de
las madrugadas no hay nada que recalcular.

Cada fecha de adquisición es **una tarea propia**, generada en el momento de
ejecutar. Si el día 12 falla por un corte de red, Airflow reintenta ese día y no
los que ya salieron bien.

```bash
./scripts/run_airflow.sh       # interfaz en http://localhost:8080
```

## Puesta en marcha

```bash
uv sync --all-groups           # crea el entorno con Python 3.12
uv run pytest -m "not integration"   # 251 tests, sin red
uv run pytest -m integration         # el que sí sale a internet: contrato con el catálogo
uv run ruff check .            # análisis estático
uv run mypy                    # tipos
```

Los cuatro comandos son los mismos que ejecuta la integración continua, que corre
en cada empujón y en cada pull request. El contrato con el catálogo remoto va en
un trabajo aparte y no bloquea: depende de que un servicio de terceros esté en
pie, y eso no puede frenar una rama.

### El entregable

```bash
uv build                                      # el paquete instalable
uv run python scripts/build_deliverable.py    # el zip que pide la guía del máster
```

El segundo arma `Dario_Rodriguez_Gonzalez_TFM.zip` con el código versionado (vía
`git archive`, que deja fuera el entorno, las cachés y el `.env`), el paquete ya
construido y la documentación. Si falta la memoria o el enlace del vídeo, lo dice
por su nombre en vez de dejar el hueco.

Incluye además los tres ficheros que el panel necesita para abrirse (unos 40 MB:
el almacén DuckDB y las dos capas de geometría), de modo que quien reciba el zip
pueda ejecutarlo sin levantar Docker ni repetir la carga histórica. Las descargas
en bruto de `data/raw` (124 MB) sí quedan fuera: son públicas y se rebajan solas.

### El panel

```bash
uv run --group panel streamlit run app.py     # se abre en localhost:8501
```

No necesita Docker. Lee la capa gold del fichero DuckDB que deja `dbt build` y
la geometría del GeoPackage de municipios, así que basta con haber construido
las dos cosas una vez. Tiene tres vistas: mapa de anomalía con deslizador
temporal, serie semanal de un municipio frente a su normal, y el cruce de la
vegetación con la lluvia de AEMET.

## Estado

- [x] Descubrimiento STAC, lectura por ventana, NDVI con máscara de calidad
- [x] Mosaico diario y estadística zonal
- [x] Contratos de datos con Pandera y suite de tests
- [x] Almacenamiento Iceberg sobre MinIO, con carga histórica 2018-2026
- [x] Modelado con dbt sobre DuckDB: climatología y anomalía semanal
- [x] Orquestación con Airflow 3, con programación por assets
- [x] Panel Streamlit + Folium: mapa de anomalía, serie por municipio y el cruce
- [x] CI en GitHub Actions, paquete instalable y script del entregable

## Fuentes de datos

**Imágenes.** Productos **Sentinel-2 L2A** del programa Copernicus, servidos como
COG a través del catálogo STAC de Element84 sobre AWS Open Data. Datos públicos y
gratuitos bajo licencia abierta de Copernicus.

**Meteorología.** Precipitación y temperatura diarias de las **114 estaciones de
AEMET** que caen dentro de la cuenca, vía AEMET OpenData. Sirven para contrastar
la anomalía de vegetación con la lluvia que de verdad faltó, que es lo que
convierte una anomalía en un mecanismo. Requiere una clave gratuita en
`AEMET_API_KEY`; sin ella el resto del pipeline funciona igual.

**Geometrías.** Cuenca de HydroBASINS y límites municipales del Instituto
Geográfico Nacional.

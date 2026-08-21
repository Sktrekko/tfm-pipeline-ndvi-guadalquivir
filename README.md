# Pipeline Geoespacial de Monitorización de Vegetación

Pipeline de ingeniería de datos que calcula series temporales del índice **NDVI**
sobre la **cuenca del Guadalquivir** a partir de imágenes **Sentinel-2** del
programa **Copernicus**, sin descargar las escenas completas.

Trabajo Fin de Máster — Máster en Big Data & Data Engineering, UCM / NTIC.
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

## Puesta en marcha

```bash
uv sync                        # crea el entorno con Python 3.12
uv run pytest                  # 95 tests, sin red
uv run pytest -m integration   # contrato con el catálogo remoto
uv run ruff check src tests    # análisis estático
```

## Estado

- [x] Descubrimiento STAC, lectura por ventana, NDVI con máscara de calidad
- [x] Mosaico diario y estadística zonal
- [x] Contratos de datos con Pandera y suite de tests
- [ ] Almacenamiento Iceberg sobre MinIO
- [ ] Modelado con dbt sobre DuckDB
- [ ] Orquestación con Airflow 3
- [ ] Panel Streamlit + Folium
- [ ] Empaquetado Docker Compose y CI en GitHub Actions

## Fuente de datos

Productos **Sentinel-2 L2A Collection-1** del programa Copernicus, servidos como
COG a través del catálogo STAC de Element84 sobre AWS Open Data. Datos públicos y
gratuitos bajo licencia abierta de Copernicus.

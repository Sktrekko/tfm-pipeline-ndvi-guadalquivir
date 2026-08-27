"""Los assets que enlazan los dos DAG del proyecto.

Un asset en Airflow 3 es el nombre de un dato, no de una tarea. Sirve para
declarar quien lo produce y quien lo consume, y que sea el propio Airflow quien
deduzca el orden. La diferencia con encadenar tareas dentro de un mismo DAG es
que el productor y el consumidor pueden vivir en DAG distintos, con calendarios
distintos, sin conocerse.

Esa es la razon concreta por la que este proyecto usa Airflow 3 y no la 2. La
transformacion de dbt no tiene por que correr a una hora fija: tiene que correr
**cuando llega dato nuevo**, que es algo que depende del satelite y no del
reloj. Un DAG a las tres de la manana recalcularia ocho anos de climatologia
tanto si esa noche entraron mil filas como si no entro ninguna. Declarando la
tabla bronze como asset, la ingesta la marca como actualizada solo si de verdad
escribio algo, y la transformacion arranca sola.

Los URI no apuntan a ningun servicio que Airflow vaya a abrir: son
identificadores. Se escriben con la forma `esquema://catalogo/espacio/tabla`
porque asi se lee de un vistazo de que almacen viene cada uno, que es lo que
mira alguien que abre la pantalla de assets sin conocer el proyecto.
"""

from __future__ import annotations

from airflow.sdk import Asset

#: Capa bronze: una fila por municipio y fecha de adquisicion, tal y como sale
#: del calculo en Python. La produce el DAG de ingesta.
BRONZE_DAILY = Asset(
    name="ndvi_zonal_daily",
    uri="iceberg://lakehouse/bronze/ndvi_zonal_daily",
    group="bronze",
)

#: Capa gold: la anomalia de cada municipio y semana frente a su propia normal
#: historica. La produce dbt y la consume el panel. Se declara como asset para
#: que el panel pueda, mas adelante, colgarse de ella igual que dbt cuelga de
#: bronze, sin que nadie tenga que acordarse de refrescar nada a mano.
GOLD_ANOMALY = Asset(
    name="ndvi_anomaly",
    uri="duckdb://warehouse/gold/ndvi_anomaly",
    group="gold",
)

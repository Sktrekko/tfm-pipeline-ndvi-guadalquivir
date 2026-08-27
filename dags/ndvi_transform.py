"""DAG de transformacion: reconstruye las capas silver y gold con dbt.

Este DAG no tiene calendario. Se despierta cuando el de ingesta declara que ha
escrito filas nuevas en la capa bronze, y solo entonces. Esa es la diferencia
practica entre orquestar por reloj y orquestar por dato.

Con calendario habria que elegir una hora y aceptar sus dos fallos, que son
simetricos y ninguno tiene arreglo: si corre antes de que termine la ingesta
transforma dato viejo, y si corre despues gasta el trabajo de recalcular ocho
anos de climatologia en dias en los que no ha entrado ni una fila. Sobre esta
cuenca el satelite pasa cada cinco dias, asi que la mayoria de los dias no hay
nada nuevo. Declarando la dependencia sobre el dato, Airflow arranca cuando hay
motivo y no arranca cuando no lo hay.

Por que `dbt build` y no `dbt run` seguido de `dbt test`
-------------------------------------------------------
Parece lo mismo y no lo es. `run` construye todos los modelos y `test` los
comprueba despues, de modo que un modelo con un defecto alimenta a los que
dependen de el antes de que salte su comprobacion. `build` intercala: construye
un modelo, lo comprueba, y solo si pasa sigue con los que se apoyan en el. Es la
diferencia entre revisar la obra al terminar el edificio o al terminar cada
planta. Con treinta y siete comprobaciones encadenadas sobre seis modelos, la
segunda es la unica que sirve de algo.

Por que es una tarea de shell y no codigo Python
------------------------------------------------
dbt tiene interfaz de Python, pero llamarla desde dentro del proceso de Airflow
mezclaria dos cosas que conviene mantener separadas: el estado del orquestador y
el del transformador. Lanzandolo como un mandato se obtiene ademas la propiedad
util de que la misma linea que corre el DAG es la que Dario escribe en su
terminal, asi que lo que falla en produccion se reproduce en local copiando y
pegando.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import dag, task

from ndvi_assets import BRONZE_DAILY, GOLD_ANOMALY

logger = logging.getLogger(__name__)

#: Ver la nota equivalente en `ndvi_ingest.py`.
REPO_ROOT = Path(__file__).resolve().parents[1]
DBT_DIR = REPO_ROOT / "dbt"


@dag(
    dag_id="ndvi_transform",
    description="Modelos dbt de climatologia y anomalia sobre la capa bronze",
    # Sin cron. La lista de assets es el calendario: este DAG corre cuando la
    # tabla bronze cambia.
    schedule=[BRONZE_DAILY],
    catchup=False,
    max_active_runs=1,
    default_args={
        "retries": 1,
        "retry_delay": timedelta(minutes=1),
    },
    tags=["ndvi", "dbt", "transformacion"],
    doc_md=__doc__,
)
def ndvi_transform():
    # Un fallo de dbt casi nunca es transitorio: o el SQL esta mal o una
    # comprobacion no pasa, y en los dos casos reintentar da el mismo
    # resultado. El unico reintento que se concede cubre el caso real de que el
    # catalogo Iceberg tarde un instante en publicar la version recien escrita.
    dbt_build = BashOperator(
        task_id="dbt_build",
        bash_command="DBT_PROFILES_DIR=. dbt build --fail-fast",
        cwd=str(DBT_DIR),
        outlets=[GOLD_ANOMALY],
        doc_md=(
            "Construye los dos modelos silver y los cuatro gold, y ejecuta las "
            "comprobaciones de cada uno segun se van construyendo. "
            "`--fail-fast` detiene la ejecucion en la primera comprobacion que "
            "falle, en vez de seguir construyendo sobre un modelo ya sospechoso."
        ),
    )

    @task(retries=0)
    def summarize() -> dict:
        """Comprueba contra el almacen que la transformacion dejo algo usable.

        Que dbt termine en verde dice que las consultas corrieron y que las
        comprobaciones pasaron. No dice cuanta serie hay, hasta que dia llega ni
        cuantos municipios estan en alarma, y esas son las tres cosas que de
        verdad interesan al mirar por la manana la ejecucion de anoche. La tarea
        las deja escritas en el log de Airflow, que es donde se van a buscar
        cuando algo parezca raro dentro de un mes.

        Es tambien la unica comprobacion de punta a punta que hay: lee el
        fichero de DuckDB por el mismo camino que usara el panel, asi que si dbt
        lo dejo a medias se entera aqui y no el dia de la demostracion. Se abre
        en modo solo lectura para no quedarse con el fichero tomado si la tarea
        muriera a mitad, que dejaria a la ejecucion siguiente sin poder escribir.
        """
        import duckdb

        from ndvi_guadalquivir.config import get_settings

        # dbt-duckdb antepone el esquema base al de cada carpeta de modelos, asi
        # que la capa gold acaba llamandose `main_gold` y no `gold`. Es de las
        # cosas que solo se descubren mirando el fichero.
        with duckdb.connect(str(get_settings().warehouse_path), read_only=True) as conn:
            row = conn.execute(
                """
                SELECT count(*)                    AS filas,
                       count(DISTINCT zone_id)     AS municipios,
                       min(primera_fecha)          AS desde,
                       max(ultima_fecha)           AS hasta,
                       count(*) FILTER (WHERE anomalia_sigmas <= -2) AS muy_por_debajo
                FROM main_gold.ndvi_anomaly
                """
            ).fetchone()

        stats = {
            "filas": int(row[0]),
            "municipios": int(row[1]),
            "desde": str(row[2]),
            "hasta": str(row[3]),
            "muy_por_debajo": int(row[4]),
        }
        logger.info(
            "Capa gold: %(filas)d semanas-municipio, %(municipios)d municipios, "
            "de %(desde)s a %(hasta)s, %(muy_por_debajo)d por debajo de dos sigmas",
            stats,
        )
        return stats

    dbt_build >> summarize()


ndvi_transform()

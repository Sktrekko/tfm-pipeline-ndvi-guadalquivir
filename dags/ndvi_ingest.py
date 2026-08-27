"""DAG de ingesta incremental: mantiene la serie al dia.

Por que existe si ya esta `scripts/run_backfill.py`
---------------------------------------------------
Son dos trabajos distintos que se confunden con facilidad. El script es una
operacion de un solo uso: se lanza una vez, tarda quince horas y deja ocho anos
cargados. Este DAG resuelve lo contrario, el trabajo pequeno y repetido. El
satelite vuelve a pasar por la cuenca cada cinco dias, el archivo publica esas
escenas con retraso variable, y la serie se queda vieja sola sin que nadie haga
nada. Alguien tiene que despertarse cada dia, mirar si hay dato nuevo y
cargarlo. Eso es orquestacion, y es lo que no se puede hacer con un script que
haya que acordarse de ejecutar.

La forma del DAG
----------------
Tres pasos con una division de trabajo deliberada:

    pending_dates ─► ingest_date (una tarea por fecha) ─► publish

`pending_dates` pregunta al catalogo que dias hay en la ventana y descarta los
que el registro de ingesta ya da por resueltos. `ingest_date` no es una tarea
sino tantas como fechas haya, generadas en el momento de ejecutar (lo que
Airflow llama mapeo dinamico). `publish` marca el asset y con ello dispara la
transformacion de dbt.

Que una fecha sea una tarea y no un bucle dentro de una tarea es la decision de
diseno importante. Las fechas son independientes entre si, de modo que si el
dia 12 falla por un corte de red no tiene ningun sentido repetir tambien el 10
y el 11, que ya salieron bien. Con una tarea por fecha, Airflow reintenta solo
la que fallo, y la pantalla de la ejecucion se lee como lo que es: una rejilla
donde cada casilla es un dia del satelite.

Los numeros que gobiernan la configuracion
------------------------------------------
Ninguno esta puesto a ojo, todos salen de medidas del propio proyecto que estan
recogidas en `docs/metodologia-y-medidas.md`:

- **Cuatro fechas a la vez y no mas.** Sobre una fecha de dieciocho granulos, en
  serie tarda 81 s, con cuatro hilos 42 s, con seis 92 s y con diez 84 s. El
  servidor estrangula por encima de cuatro peticiones simultaneas, asi que
  pedirle mas es mas lento, no mas rapido. Ademas cada fecha necesita del orden
  de medio giga de memoria mientras mosaica.
- **Ventana de veinte dias.** Tiene que cubrir el ciclo de revisita de cinco
  dias con holgura, porque el archivo publica tarde y desigual, y porque el
  catalogo reprocesa escenas semanas despues (trampa 13). Veinte dias dan cuatro
  pasadas de margen. El coste de mirar hacia atras es cero: las fechas ya
  resueltas se descartan contra el registro sin leer una sola imagen.
- **Consultar acotado y no entero.** La consulta al catalogo completo tarda
  19 min 37 s y se paga en cada lanzamiento. Acotada a veinte dias baja a
  segundos. Un DAG diario no puede permitirse la primera.
- **Tres reintentos con espera creciente.** El modo de fallo medido es la red:
  el archivo esta en Oregon, cada peticion cruza el Atlantico y el cuello de
  botella es la latencia, no el ancho de banda. Los fallos de ese tipo se
  arreglan solos esperando, que es justo lo que hace el reintento con espera
  creciente, y no se arreglan repitiendo al instante.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

import pendulum
from airflow.sdk import dag, get_current_context, task
from airflow.sdk.exceptions import AirflowException, AirflowSkipException
from airflow.task.trigger_rule import TriggerRule

from ndvi_assets import BRONZE_DAILY

logger = logging.getLogger(__name__)

#: Dias hacia atras que revisa cada ejecucion. Ver el docstring de arriba.
LOOKBACK_DAYS = 20

#: Capa de zonas, relativa a la raiz del repositorio. No se resuelve aqui sino
#: dentro de cada tarea, porque el fichero del DAG lo relee el planificador cada
#: pocos segundos y no debe tocar el disco al hacerlo.
ZONES_PATH = "data/zones/zones_municipios.gpkg"


#: Raiz del repositorio. El DAG no puede suponer desde que directorio se
#: arranco Airflow, y las rutas de datos del proyecto son relativas a la raiz,
#: asi que se deduce de la ubicacion de este fichero: `dags/` cuelga de la raiz.
#: Se resuelve aqui y no dentro de las tareas porque no toca el disco, solo
#: manipula una cadena, y el planificador relee este fichero cada pocos segundos.
REPO_ROOT = Path(__file__).resolve().parents[1]


@dag(
    dag_id="ndvi_ingest",
    description="Carga incremental de NDVI por municipio desde el catalogo STAC",
    # Cada madrugada. La pasada sobre la peninsula es de media manana y el
    # archivo tarda horas en publicarla, asi que a las cinco ya esta disponible
    # lo del dia anterior y la maquina esta libre.
    schedule="0 5 * * *",
    start_date=pendulum.datetime(2026, 8, 27, tz="Europe/Madrid"),
    # Sin recuperacion de ejecuciones pasadas. La ventana de veinte dias ya
    # cubre cualquier hueco, y el registro de ingesta es la marca de por donde
    # va la serie. Dejar que Airflow reproduzca un ano de ejecuciones diarias
    # seria llevar la misma contabilidad dos veces, con el riesgo de que las dos
    # versiones no coincidan.
    catchup=False,
    # Dos ejecuciones a la vez se disputarian las mismas fechas y la misma tabla.
    max_active_runs=1,
    # El servidor del archivo estrangula por encima de cuatro peticiones a la
    # vez. Medido, no supuesto.
    max_active_tasks=4,
    default_args={
        "retries": 3,
        "retry_delay": timedelta(minutes=2),
        "retry_exponential_backoff": True,
        "max_retry_delay": timedelta(minutes=15),
    },
    tags=["ndvi", "ingesta", "sentinel-2"],
    doc_md=__doc__,
)
def ndvi_ingest():
    @task
    def pending_dates() -> list[str]:
        """Fechas de la ventana que todavia no estan resueltas.

        Devuelve cadenas ISO y no objetos `date` porque el valor viaja entre
        tareas por XCom, que serializa a JSON. Una fecha de Python no sobrevive
        a ese viaje; una cadena si, y ademas se lee en la interfaz.

        Si no queda ninguna fecha por procesar, la tarea se marca como saltada.
        Es lo normal: el satelite pasa cada cinco dias, asi que cuatro de cada
        cinco ejecuciones no tienen nada que hacer. Saltar es mas honesto que
        terminar en verde, porque en la lista de ejecuciones se distingue de un
        vistazo el dia que entro dato del que no.
        """
        from ndvi_guadalquivir.aoi import bounds_of, load_zones
        from ndvi_guadalquivir.catalog import group_by_date, search_scenes
        from ndvi_guadalquivir.lakehouse import ensure_log_table, settled_dates

        context = get_current_context()
        end = context["data_interval_end"].date()
        start = end - timedelta(days=LOOKBACK_DAYS)

        zones = load_zones(REPO_ROOT / ZONES_PATH)
        logger.info("Consultando el catalogo entre %s y %s", start, end)
        scenes = search_scenes(bounds_of(zones), start, end)
        available = {day for day, _ in group_by_date(scenes)}

        already = settled_dates(ensure_log_table())
        pending = sorted(available - already)

        logger.info(
            "%d fechas en la ventana, %d ya resueltas, %d por procesar",
            len(available), len(available & already), len(pending),
        )
        if not pending:
            raise AirflowSkipException("Nada nuevo en la ventana")
        return [day.isoformat() for day in pending]

    # El nombre de cada tarea mapeada es la fecha que procesa, en lugar del
    # indice 0, 1, 2 que pone Airflow por defecto. En la rejilla de la
    # ejecucion se ve directamente que dia fallo.
    @task(map_index_template="{{ task.op_kwargs['day'] }}")
    def ingest_date(day: str) -> int:
        """Procesa una fecha de adquisicion y la escribe en el almacen.

        Repite a proposito el orden de escritura del script de carga: primero
        los datos, despues el registro. Si el proceso muere entre las dos
        escrituras, la fecha se queda sin marcar y se reprocesa manana, que es
        molesto pero inofensivo. Al reves se daria por cargada una fecha que no
        lo esta, y eso es un hueco silencioso en la serie.

        Sobre los errores hay una diferencia deliberada con el script.
        `process_date` no lanza excepciones: atrapa el fallo y lo devuelve como
        un desenlace, porque en una carga de mil quinientas fechas un error no
        puede tumbar las catorce horas restantes. Aqui interesa lo contrario:
        que la tarea falle de verdad, para que Airflow la reintente. Asi que el
        desenlace se anota igual en el registro (es un hecho y el registro es de
        solo anadir) y despues se relanza como excepcion. Anotarlo no bloquea el
        reintento: solo `ok` y `empty` cuentan como fecha resuelta, `error` no.

        Returns:
            Filas escritas. Cero es un resultado legitimo: significa que ese dia
            la nubosidad dejo todos los municipios por debajo del minimo de
            pixeles utiles.
        """
        from datetime import date as date_type

        from ndvi_guadalquivir.aoi import bounds_of, load_zones
        from ndvi_guadalquivir.catalog import search_scenes
        from ndvi_guadalquivir.lakehouse import (
            STATUS_ERROR,
            append_daily,
            append_log,
            ensure_daily_table,
            ensure_log_table,
        )
        from ndvi_guadalquivir.pipeline import process_date

        target = date_type.fromisoformat(day)
        zones = load_zones(REPO_ROOT / ZONES_PATH)

        # Se vuelve a preguntar al catalogo por este dia suelto en vez de
        # arrastrar las escenas desde la tarea anterior. Un objeto `Scene` no
        # cabe en XCom sin inventarse una serializacion, y una consulta acotada
        # a un dia se resuelve en un par de segundos.
        scenes = search_scenes(bounds_of(zones), target, target)
        if not scenes:
            raise AirflowException(f"El catalogo no devuelve escenas para {day}")

        frame, outcome = process_date(target, scenes, zones)

        if not frame.empty:
            append_daily(frame, table=ensure_daily_table())
        append_log([outcome.as_log_record()], table=ensure_log_table())

        if outcome.status == STATUS_ERROR:
            raise AirflowException(f"{day}: {outcome.message}")

        logger.info(
            "%s  %s  %d escenas  %d filas  %.1f s",
            day, outcome.status, outcome.scene_count,
            outcome.row_count, outcome.duration_seconds,
        )
        return outcome.row_count

    @task(
        outlets=[BRONZE_DAILY],
        # Se ejecuta aunque alguna fecha haya fallado. Cinco dias buenos y uno
        # malo siguen siendo cinco dias de dato nuevo que dbt tiene que
        # incorporar; retenerlos por el sexto no arregla nada.
        trigger_rule=TriggerRule.ALL_DONE,
        # Publicar es contar filas: no hay nada que reintentar.
        retries=0,
    )
    def publish(written: list[int]) -> int:
        """Marca la capa bronze como actualizada, y solo si de verdad lo esta.

        Es la unica tarea que declara el asset de salida, de modo que es la que
        despierta al DAG de transformacion. Si la ejecucion no escribio ni una
        fila, se salta, y una tarea saltada no emite el evento: dbt no corre.

        Sin esta condicion el calendario se volveria absurdo. Cuatro de cada
        cinco dias no hay pasada del satelite, y recalcular la climatologia de
        ocho anos para incorporar cero filas es trabajo puro de calentar la
        maquina. Este es el motivo concreto por el que el proyecto usa assets y
        no una dependencia directa entre tareas: el disparo depende de que haya
        dato, no de que haya terminado un paso.
        """
        total = sum(count for count in written if count)
        if not total:
            raise AirflowSkipException("Ninguna fecha aporto filas")
        logger.info("Capa bronze actualizada con %d filas", total)
        return total

    publish(ingest_date.expand(day=pending_dates()))


ndvi_ingest()

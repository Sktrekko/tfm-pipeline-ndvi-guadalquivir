"""Tests de los DAG de Airflow.

Aqui no se ejecuta nada del pipeline: eso ya lo cubren los otros tests. Lo que
se comprueba es que los dos ficheros de `dags/` describen la orquestacion que
dicen describir, y hay dos razones concretas para molestarse.

La primera es que un DAG con un error de importacion no falla, desaparece. El
planificador lo anota en un rincon de la interfaz y sigue como si nada, asi que
la serie se queda sin actualizar durante dias sin que salte ninguna alarma. Un
test que abra los ficheros convierte ese silencio en un fallo de la bateria.

La segunda es el enlace entre los dos DAG. La ingesta produce un asset y la
transformacion se programa sobre ese mismo asset, pero cada uno vive en un
fichero distinto y lo unico que los une es que el nombre coincida. Cambiar ese
nombre en un sitio y no en el otro deja dos DAG que siguen funcionando por
separado, cada uno en verde, mientras la capa gold deja de refrescarse. Es
justo el tipo de averia que no se descubre hasta que alguien mira una cifra
vieja y la da por buena.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DAGS_DIR = REPO_ROOT / "dags"

# Airflow lee su configuracion al importarse y, si no encuentra un directorio de
# trabajo, se crea uno en el home del usuario. Se le indica el mismo que usa el
# proyecto (esta en .gitignore) para que la bateria de tests no deje ficheros
# sueltos fuera del repositorio.
os.environ.setdefault("AIRFLOW_HOME", str(REPO_ROOT / "airflow"))
os.environ.setdefault("AIRFLOW__CORE__DAGS_FOLDER", str(DAGS_DIR))
os.environ.setdefault("AIRFLOW__CORE__LOAD_EXAMPLES", "False")

# Airflow no es dependencia del paquete sino de un grupo aparte, porque pesa y
# el calculo no lo necesita para nada. Si no esta instalado, estos tests se
# saltan en lugar de fallar: un clon recien hecho debe poder correr la bateria
# sin instalar el orquestador.
pytest.importorskip("airflow", reason="el grupo 'airflow' no esta instalado")

# En ejecucion real es Airflow quien pone la carpeta de DAG en el path, al
# cargar el paquete de ficheros. Aqui hay que hacerlo a mano para poder importar
# `ndvi_assets`, que es de donde salen los nombres contra los que se comprueba.
if str(DAGS_DIR) not in sys.path:
    sys.path.insert(0, str(DAGS_DIR))

from airflow.dag_processing.dagbag import BundleDagBag  # noqa: E402


@pytest.fixture(scope="module")
def dagbag() -> BundleDagBag:
    return BundleDagBag(dag_folder=str(DAGS_DIR), bundle_path=DAGS_DIR)


class TestCarga:
    def test_los_ficheros_de_dags_se_importan_sin_error(self, dagbag):
        assert dagbag.import_errors == {}

    def test_estan_los_dos_dags(self, dagbag):
        assert set(dagbag.dags) == {"ndvi_ingest", "ndvi_transform"}


class TestIngesta:
    @pytest.fixture
    def ingest(self, dagbag):
        return dagbag.dags["ndvi_ingest"]

    def test_las_tres_tareas_estan_encadenadas(self, ingest):
        assert {t.task_id for t in ingest.tasks} == {
            "pending_dates", "ingest_date", "publish",
        }
        assert ingest.get_task("ingest_date").upstream_task_ids == {"pending_dates"}
        assert ingest.get_task("publish").upstream_task_ids == {"ingest_date"}

    def test_cada_fecha_es_una_tarea_propia(self, ingest):
        """El mapeo dinamico es lo que permite reintentar solo el dia que fallo."""
        assert ingest.get_task("ingest_date").is_mapped

    def test_no_se_piden_mas_de_cuatro_fechas_a_la_vez(self, ingest):
        """Medido: el servidor del archivo estrangula por encima de cuatro."""
        assert ingest.max_active_tasks == 4

    def test_una_sola_ejecucion_a_la_vez(self, ingest):
        """Dos se disputarian las mismas fechas y la misma tabla Iceberg."""
        assert ingest.max_active_runs == 1

    def test_sin_recuperacion_de_ejecuciones_pasadas(self, ingest):
        """La ventana y el registro de ingesta ya cubren los huecos."""
        assert ingest.catchup is False

    def test_la_lectura_remota_se_reintenta(self, ingest):
        """El fallo tipico es de red, y ese se arregla esperando."""
        task = ingest.get_task("ingest_date")
        assert task.retries >= 1
        # Airflow guarda aqui el multiplicador de la espera, no un booleano.
        assert task.retry_exponential_backoff

    def test_publicar_no_espera_a_que_todo_haya_ido_bien(self, ingest):
        """Cinco dias buenos y uno malo siguen siendo cinco dias de dato nuevo."""
        assert ingest.get_task("publish").trigger_rule == "all_done"


class TestTransformacion:
    @pytest.fixture
    def transform(self, dagbag):
        return dagbag.dags["ndvi_transform"]

    def test_no_tiene_calendario_propio(self, transform):
        """Corre cuando llega dato, no a una hora fija."""
        assert type(transform.timetable).__name__ == "AssetTriggeredTimetable"

    def test_dbt_va_antes_del_resumen(self, transform):
        assert {t.task_id for t in transform.tasks} == {"dbt_build", "summarize"}
        assert transform.get_task("summarize").upstream_task_ids == {"dbt_build"}


class TestEnlaceEntreDags:
    """El contrato que une los dos ficheros.

    Es el unico test de este modulo que protege algo que no se ve. Todo lo
    demas fallaria de forma ruidosa; esto fallaria en silencio.
    """

    def test_lo_que_publica_la_ingesta_es_lo_que_espera_la_transformacion(
        self, dagbag
    ):
        from ndvi_assets import BRONZE_DAILY

        publica = dagbag.dags["ndvi_ingest"].get_task("publish").outlets
        assert [asset.uri for asset in publica] == [BRONZE_DAILY.uri]

        espera = dagbag.dags["ndvi_transform"].timetable.asset_condition
        assert BRONZE_DAILY.uri in {asset.uri for asset in espera.objects}

    def test_dbt_declara_la_capa_gold_como_producto(self, dagbag):
        from ndvi_assets import GOLD_ANOMALY

        salidas = dagbag.dags["ndvi_transform"].get_task("dbt_build").outlets
        assert [asset.uri for asset in salidas] == [GOLD_ANOMALY.uri]

#!/usr/bin/env bash
# Arranca Airflow en local con la configuracion del proyecto.
#
# Existe por la misma razon que `run_backfill.py`: son decisiones de operacion
# (donde vive la base de datos de metadatos, que carpeta de DAG se lee, que
# ejecutor se usa) que no deben quedar cableadas en ningun sitio ni tener que
# recordarse de memoria antes de una demostracion.
#
# `standalone` levanta de una vez el planificador, el servidor web y el lector
# de DAG. Es el modo pensado para desarrollo y para ensenar el proyecto; un
# despliegue de verdad separaria los tres procesos.
#
# Uso:
#     ./scripts/run_airflow.sh          # interfaz en http://localhost:8080
#
# La primera vez tarda porque crea la base de datos. La contrasena del usuario
# admin la imprime por pantalla y la deja en airflow/simple_auth_manager_passwords.json
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export AIRFLOW_HOME="${ROOT}/airflow"
export AIRFLOW__CORE__DAGS_FOLDER="${ROOT}/dags"
# Los DAG de ejemplo que trae Airflow (unos cincuenta) enterrarian los dos del
# proyecto en la lista.
export AIRFLOW__CORE__LOAD_EXAMPLES=False
# LocalExecutor corre cada tarea en un proceso aparte de la misma maquina, que
# es lo que hace falta para que las fechas se procesen de verdad en paralelo.
export AIRFLOW__CORE__EXECUTOR=LocalExecutor

mkdir -p "${AIRFLOW_HOME}"

# `db migrate` es idempotente: la primera vez crea el esquema y las siguientes
# comprueba que esta al dia. Se lanza siempre para que arrancar tras actualizar
# la version de Airflow no exija acordarse de nada.
uv run --group airflow --group dev airflow db migrate

echo
echo "Airflow en http://localhost:8080   (Ctrl+C para parar)"
echo
exec uv run --group airflow --group dev airflow standalone

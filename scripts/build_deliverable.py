"""Arma el entregable que pide la guia del master.

La guia exige un unico fichero llamado `Dario_Rodriguez_Gonzalez_TFM.zip`, y
montarlo a mano el dia de la entrega es una forma estupenda de olvidarse de algo
a las once de la noche. Esto lo construye de una pieza y avisa de lo que falta en
vez de dejar un hueco silencioso.

Que entra y que no
------------------
Entra el codigo tal como esta versionado, y para eso se usa `git archive` en
lugar de copiar la carpeta. La diferencia importa: copiar la carpeta se llevaria
los 200 MB de `data/`, el entorno virtual, las cachés y el fichero `.env` con la
clave de AEMET dentro. `git archive` exporta exactamente lo que git conoce, que
es justamente lo que se quiere entregar.

Entra tambien el paquete instalable ya construido, porque el enunciado pide un
artefacto y un `.whl` es la forma estandar de entregarlo en Python.

Entran los tres ficheros que el panel necesita para abrirse (unos 40 MB): el
almacen DuckDB reconstruido con solo la capa gold y la geometria del panel. Sin ellos el zip
se lee pero no se ejecuta, y quien lo reciba tendria que levantar Docker y
repetir una carga de quince horas solo para ver el mapa. Se copian dentro de
`codigo/` porque es ahi donde el panel los busca.

No entran las descargas en bruto de `data/raw` (124 MB): son ficheros de origen
publicos, el pipeline los vuelve a bajar solo, y el panel no los toca.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

#: Nombre exacto que exige la guia. No se toca.
DELIVERABLE_NAME = "Dario_Rodriguez_Gonzalez_TFM"

#: Ficheros que la guia pide y que este script no puede generar. Se buscan, y si
#: no estan se avisa por su nombre en vez de entregar un zip incompleto sin
#: decir nada.
EXPECTED_DOCUMENTS = {
    "memoria": ("memoria.pdf", "Memoria.pdf", f"{DELIVERABLE_NAME}.pdf"),
    "video": ("video.txt", "VIDEO.txt", "enlace-video.txt"),
}


def repo_root() -> Path:
    """Raiz del repositorio, preguntandosela a git y no adivinandola."""
    salida = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=True,
    )
    return Path(salida.stdout.strip())


def git_is_clean(root: Path) -> bool:
    """Si hay cambios sin confirmar, lo entregado no es lo que hay en git."""
    salida = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    )
    return not salida.stdout.strip()


def export_source(root: Path, dest: Path) -> None:
    """Exporta el codigo versionado, sin datos, entorno ni secretos."""
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(root), "archive", "--format=tar", "HEAD"],
        check=True, stdout=subprocess.PIPE,
    )
    archivo = dest / "codigo.tar"
    with archivo.open("wb") as salida:
        subprocess.run(
            ["git", "-C", str(root), "archive", "--format=tar", "HEAD"],
            check=True, stdout=salida,
        )
    shutil.unpack_archive(archivo, dest / "codigo", format="tar")
    archivo.unlink()


def build_package(root: Path, dest: Path) -> list[Path]:
    """Construye el wheel y el sdist y los deja en el entregable."""
    subprocess.run(["uv", "build"], cwd=root, check=True)
    origen = root / "dist"
    dest.mkdir(parents=True, exist_ok=True)
    copiados = []
    for fichero in sorted(origen.glob("ndvi_guadalquivir-*")):
        destino = dest / fichero.name
        shutil.copy2(fichero, destino)
        copiados.append(destino)
    return copiados


#: Lo minimo para que el panel arranque sin reconstruir nada. Se copian dentro
#: de `codigo/` porque panel.py los busca por ruta relativa al directorio actual.
PANEL_DATA = (
    Path("data/zones/zones_panel.geojson"),
)

#: El almacen de trabajo pesa 34 MB porque arrastra espacio libre y las vistas
#: silver, que son vistas sobre Iceberg y sin Docker ni se leen. Reconstruirlo
#: con solo las tablas gold lo deja en 17 MB, y el campus limita la entrega a 20.
ALMACEN = Path("data/warehouse.duckdb")


def compactar_almacen(origen: Path, destino: Path) -> None:
    """Reescribe el almacen con solo las tablas gold, que es lo que lee el panel.

    Las tablas silver son vistas sobre el catalogo Iceberg: sin los contenedores
    levantados no devuelven nada, asi que ocupan sitio en el entregable sin dar
    nada a cambio. Copiar tabla a tabla ademas compacta el fichero.
    """
    import duckdb

    destino.parent.mkdir(parents=True, exist_ok=True)
    if destino.exists():
        destino.unlink()
    with duckdb.connect(str(destino)) as salida:
        salida.execute(f"ATTACH '{origen}' AS viejo (READ_ONLY)")
        salida.execute("CREATE SCHEMA IF NOT EXISTS main_gold")
        tablas = salida.execute(
            "SELECT table_name FROM duckdb_tables() "
            "WHERE database_name = 'viejo' AND schema_name = 'main_gold'"
        ).fetchall()
        for (tabla,) in tablas:
            salida.execute(
                f'CREATE TABLE main_gold."{tabla}" AS '
                f'SELECT * FROM viejo.main_gold."{tabla}"'
            )
        salida.execute("CHECKPOINT")


def copy_panel_data(root: Path, dest: Path) -> list[str]:
    """Copia los datos del panel dentro del codigo exportado. Devuelve lo que falte."""
    faltan = []
    if (root / ALMACEN).exists():
        compactar_almacen(root / ALMACEN, dest / ALMACEN)
    else:
        faltan.append(str(ALMACEN))
    for relativo in PANEL_DATA:
        origen = root / relativo
        if not origen.exists():
            faltan.append(str(relativo))
            continue
        destino = dest / relativo
        destino.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origen, destino)
    return faltan


def collect_documents(root: Path, dest: Path) -> list[str]:
    """Busca la memoria y el enlace del video. Devuelve lo que falte."""
    dest.mkdir(parents=True, exist_ok=True)
    faltan = []
    for etiqueta, nombres in EXPECTED_DOCUMENTS.items():
        encontrado = next(
            (c for nombre in nombres
             for c in (root / nombre, root / "docs" / nombre, root.parent / nombre)
             if c.exists()),
            None,
        )
        if encontrado is None:
            faltan.append(etiqueta)
        else:
            shutil.copy2(encontrado, dest / encontrado.name)
    return faltan


def write_manifest(dest: Path, paquetes: list[Path], faltan: list[str]) -> None:
    """Deja escrito que hay dentro y como se usa, para quien abra el zip."""
    lineas = [
        "Entregable del TFM",
        "Dario Rodriguez Gonzalez",
        "Master en Big Data y Data Engineering, UCM",
        "",
        "Contenido",
        "---------",
        "codigo/      El repositorio completo tal como esta versionado.",
        "paquete/     El paquete instalable ya construido (.whl y .tar.gz).",
        "docs/        Memoria y enlace al video.",
        "",
        "Como abrir el panel (no hace falta Docker ni reconstruir nada)",
        "--------------------------------------------------------------",
        "    cd codigo",
        "    uv sync --group panel",
        "    uv run --group panel streamlit run app.py      -> http://localhost:8501",
        "",
        "Los datos del panel ya van dentro, en codigo/data/.",
        "",
        "Como instalar el paquete",
        "------------------------",
    ]
    if paquetes:
        wheel = next((p.name for p in paquetes if p.suffix == ".whl"), paquetes[0].name)
        lineas.append(f"    pip install paquete/{wheel}")
    lineas += [
        "",
        "Como levantar el proyecto entero",
        "--------------------------------",
        "    cd codigo",
        "    uv sync",
        "    docker compose up -d",
        "    (el resto de comandos, en README.md)",
        "",
        "Repositorio",
        "-----------",
        "https://github.com/Sktrekko/tfm-pipeline-ndvi-guadalquivir",
        "",
    ]
    if faltan:
        lineas += ["AVISO: falta por incluir " + ", ".join(faltan) + ".", ""]
    (dest / "LEEME.txt").write_text("\n".join(lineas), encoding="utf-8")


def build(root: Path, output_dir: Path, *, allow_dirty: bool) -> Path:
    if not git_is_clean(root) and not allow_dirty:
        raise SystemExit(
            "Hay cambios sin confirmar. El entregable sale de git, asi que lo "
            "que se empaquetaria no es lo que tienes delante. Confirma los "
            "cambios, o pasa --allow-dirty si sabes lo que haces."
        )

    montaje = output_dir / DELIVERABLE_NAME
    if montaje.exists():
        shutil.rmtree(montaje)
    montaje.mkdir(parents=True)

    print("Exportando el codigo versionado...")
    export_source(root, montaje)

    print("Copiando los datos del panel...")
    faltan_datos = copy_panel_data(root, montaje / "codigo")

    print("Construyendo el paquete...")
    paquetes = build_package(root, montaje / "paquete")

    print("Buscando memoria y video...")
    faltan = collect_documents(root, montaje / "docs")
    faltan += ["datos del panel (" + ", ".join(faltan_datos) + ")"] if faltan_datos else []

    write_manifest(montaje, paquetes, faltan)

    destino = output_dir / f"{DELIVERABLE_NAME}.zip"
    if destino.exists():
        destino.unlink()
    print(f"Comprimiendo en {destino}...")
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as zf:
        for fichero in sorted(montaje.rglob("*")):
            if fichero.is_file():
                zf.write(fichero, fichero.relative_to(output_dir))
    shutil.rmtree(montaje)

    tamano = destino.stat().st_size / 1e6
    print(f"\nListo: {destino} ({tamano:.1f} MB)")
    if faltan:
        print("\nOJO, el entregable aun NO esta completo. Falta:")
        for etiqueta in faltan:
            print(f"  - {etiqueta}")
        print("Vuelve a lanzarlo cuando lo tengas.")
    return destino


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path, default=Path("build"),
        help="Donde dejar el zip (por defecto: build/).",
    )
    parser.add_argument(
        "--allow-dirty", action="store_true",
        help="Empaquetar aunque haya cambios sin confirmar en git.",
    )
    args = parser.parse_args(argv)

    root = repo_root()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    build(root, args.output_dir.resolve(), allow_dirty=args.allow_dirty)
    return 0


if __name__ == "__main__":
    sys.exit(main())

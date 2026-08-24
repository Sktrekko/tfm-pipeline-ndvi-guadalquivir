"""Tests de la construccion de la capa de zonas.

Las fuentes reales pesan mas de 100 MB y viven en servidores ajenos, asi que
aqui se fabrican versiones minusculas con la misma forma: un shapefile de
cuencas con dos poligonos y otro de municipios con los campos que publica el
IGN. Con eso se puede probar todo el razonamiento sin red y en milisegundos.
"""

from __future__ import annotations

import zipfile

import geopandas as gpd
import pytest
from shapely.geometry import box

from ndvi_guadalquivir.zones import (
    EQUAL_AREA_CRS,
    _download,
    _extract_shapefile,
    build_zone_layer,
    load_basin,
    load_municipalities,
    select_basin_municipalities,
)

#: Cuenca de juguete: un cuadrado de un grado sobre el valle del Guadalquivir.
BASIN_BOX = box(-5.0, 37.0, -4.0, 38.0)
FAKE_HYBAS_ID = 2_040_018_360


@pytest.fixture
def basin() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"HYBAS_ID": [FAKE_HYBAS_ID]}, geometry=[BASIN_BOX], crs="EPSG:4326"
    )


@pytest.fixture
def municipalities() -> gpd.GeoDataFrame:
    """Cuatro municipios en distinta relacion con la cuenca.

    Uno entero dentro, uno entero fuera, uno partido justo por la mitad y uno
    que solo asoma una esquina.
    """
    return gpd.GeoDataFrame(
        {
            "zone_id": ["14001", "28079", "41002", "23003"],
            "zone_name": ["Dentro", "Fuera", "Mitad", "Esquina"],
            "province_code": ["14", "28", "41", "23"],
        },
        geometry=[
            box(-4.8, 37.2, -4.6, 37.4),     # dentro por completo
            box(-3.0, 40.0, -2.8, 40.2),     # lejos, fuera
            box(-4.1, 37.4, -3.9, 37.6),     # mitad este dentro, mitad fuera
            box(-4.1, 37.9, -3.9, 38.1),      # una cuarta parte dentro
        ],
        crs="EPSG:4326",
    )


class TestSelectBasinMunicipalities:
    """Criterio de pertenencia a la cuenca."""

    def test_el_de_dentro_entra_y_el_de_fuera_no(self, municipalities, basin):
        selected = select_basin_municipalities(municipalities, basin)
        ids = selected["zone_id"].tolist()
        assert "14001" in ids
        assert "28079" not in ids

    def test_el_solape_de_uno_interior_es_total(self, municipalities, basin):
        selected = select_basin_municipalities(municipalities, basin)
        dentro = selected[selected["zone_id"] == "14001"].iloc[0]
        assert dentro["basin_overlap_fraction"] == pytest.approx(1.0, abs=1e-6)

    def test_el_partido_por_la_mitad_entra_por_los_pelos(self, municipalities, basin):
        """Con el umbral por defecto del 50 %, la mitad justa se acepta."""
        selected = select_basin_municipalities(municipalities, basin)
        mitad = selected[selected["zone_id"] == "41002"]
        assert not mitad.empty
        assert mitad.iloc[0]["basin_overlap_fraction"] == pytest.approx(0.5, abs=0.02)

    def test_el_que_solo_asoma_una_esquina_se_descarta(self, municipalities, basin):
        selected = select_basin_municipalities(municipalities, basin)
        assert "23003" not in selected["zone_id"].tolist()

    def test_el_umbral_es_configurable(self, municipalities, basin):
        """Bajandolo entra tambien el de la esquina; subiendolo se cae el de la mitad."""
        laxo = select_basin_municipalities(municipalities, basin, min_overlap_fraction=0.2)
        estricto = select_basin_municipalities(municipalities, basin, min_overlap_fraction=0.9)
        assert "23003" in laxo["zone_id"].tolist()
        assert estricto["zone_id"].tolist() == ["14001"]

    def test_la_geometria_no_se_recorta(self, municipalities, basin):
        """El municipio partido conserva su superficie completa.

        Es la decision de diseno mas discutible del modulo, asi que conviene
        que este fijada por un test: si alguien la cambia, que sea a sabiendas.
        """
        selected = select_basin_municipalities(municipalities, basin)
        mitad = selected[selected["zone_id"] == "41002"].geometry.iloc[0]
        original = municipalities[municipalities["zone_id"] == "41002"].geometry.iloc[0]
        assert mitad.equals(original)

    def test_la_superficie_se_calcula_en_kilometros_cuadrados(self, municipalities, basin):
        """Un cuadrado de 0,2 grados en esta latitud ronda los 400 km2."""
        selected = select_basin_municipalities(municipalities, basin)
        area = float(selected[selected["zone_id"] == "14001"]["area_km2"].iloc[0])
        esperada = float(
            municipalities.iloc[[0]].to_crs(EQUAL_AREA_CRS).geometry.area.iloc[0] / 1e6
        )
        assert area == pytest.approx(esperada, rel=1e-6)
        assert 300 < area < 500

    def test_el_resultado_sale_ordenado_por_identificador(self, municipalities, basin):
        selected = select_basin_municipalities(municipalities, basin, min_overlap_fraction=0.2)
        assert selected["zone_id"].tolist() == sorted(selected["zone_id"].tolist())

    def test_conserva_las_columnas_de_origen(self, municipalities, basin):
        selected = select_basin_municipalities(municipalities, basin)
        for column in ("zone_name", "province_code", "area_km2", "basin_overlap_fraction"):
            assert column in selected.columns

    def test_si_no_queda_ninguno_avisa(self, municipalities, basin):
        lejos = municipalities[municipalities["zone_id"] == "28079"]
        with pytest.raises(ValueError, match="Ningun municipio"):
            select_basin_municipalities(lejos, basin)


class TestLoadBasin:
    """Extraccion de la cuenca del fichero de HydroBASINS."""

    @pytest.fixture
    def basin_shapefile(self, tmp_path, basin):
        otra = gpd.GeoDataFrame(
            {"HYBAS_ID": [2_040_018_240]},
            geometry=[box(-7.0, 36.0, -6.5, 36.5)],
            crs="EPSG:4326",
        )
        import pandas as pd
        capa = gpd.GeoDataFrame(pd.concat([basin, otra], ignore_index=True), crs="EPSG:4326")
        path = tmp_path / "hybas.shp"
        capa.to_file(path)
        return path

    def test_se_queda_solo_con_la_cuenca_pedida(self, basin_shapefile):
        cuenca = load_basin(basin_shapefile)
        assert len(cuenca) == 1
        assert int(cuenca["HYBAS_ID"].iloc[0]) == FAKE_HYBAS_ID

    def test_devuelve_coordenadas_geodesicas(self, basin_shapefile):
        assert load_basin(basin_shapefile).crs.to_string() == "EPSG:4326"

    def test_identificador_inexistente_da_mensaje_util(self, basin_shapefile):
        with pytest.raises(ValueError, match="no esta en"):
            load_basin(basin_shapefile, hybas_id=1)


class TestLoadMunicipalities:
    """Normalizacion de los recintos del IGN."""

    @pytest.fixture
    def ign_shapefile(self, tmp_path):
        """Reproduce los campos del IGN, con un condominio incluido.

        `34014141091` es Sevilla capital: provincia 41, codigo INE 41091.
        `34012353050` es un condominio de la provincia de Jaen (23) cuyo
        codigo del INE, 53050, no empieza por su provincia.
        """
        capa = gpd.GeoDataFrame(
            {
                "NATCODE": ["34014141091", "34012353050"],
                "NAMEUNIT": ["Sevilla", "Cuarto del Madrono"],
                "CODNUT3": ["ES618", "ES616"],
            },
            geometry=[box(-6.0, 37.3, -5.9, 37.4), box(-3.0, 38.0, -2.9, 38.1)],
            crs="EPSG:4326",
        ).to_crs("EPSG:25830")
        path = tmp_path / "recintos.shp"
        capa.to_file(path)
        return path

    def test_el_identificador_es_el_codigo_del_ine(self, ign_shapefile):
        zonas = load_municipalities(ign_shapefile, bbox=None)
        assert "41091" in zonas["zone_id"].tolist()

    def test_la_provincia_de_un_condominio_no_se_inventa(self, ign_shapefile):
        """Regresion del fallo de la provincia 53.

        Deducir la provincia de los dos primeros digitos del codigo del INE
        funciona para casi todos los municipios y falla para los condominios,
        que el INE numera en el rango 53xxx. La provincia se lee de su propio
        campo dentro del NATCODE, que si es fiable.
        """
        zonas = load_municipalities(ign_shapefile, bbox=None)
        condominio = zonas[zonas["zone_id"] == "53050"].iloc[0]
        assert condominio["province_code"] == "23"
        assert "53" not in zonas["province_code"].tolist()

    def test_la_provincia_de_un_municipio_normal_es_la_esperada(self, ign_shapefile):
        zonas = load_municipalities(ign_shapefile, bbox=None)
        sevilla = zonas[zonas["zone_id"] == "41091"].iloc[0]
        assert sevilla["province_code"] == "41"

    def test_reproyecta_a_coordenadas_geodesicas(self, ign_shapefile):
        assert load_municipalities(ign_shapefile, bbox=None).crs.to_string() == "EPSG:4326"

    def test_la_caja_filtra_lo_que_queda_lejos(self, ign_shapefile):
        zonas = load_municipalities(ign_shapefile, bbox=(-6.5, 37.0, -5.5, 37.8))
        assert zonas["zone_id"].tolist() == ["41091"]


class TestExtractShapefile:
    """Extraccion del shapefile de dentro del ZIP descargado."""

    @pytest.fixture
    def archive(self, tmp_path, basin):
        origen = tmp_path / "origen"
        origen.mkdir()
        basin.to_file(origen / "capa.shp")
        # Un indice espacial del formato antiguo, como el que trae HydroBASINS.
        (origen / "capa.sbn").write_bytes(b"indice corrupto")
        (origen / "capa.sbx").write_bytes(b"indice corrupto")

        path = tmp_path / "fuente.zip"
        with zipfile.ZipFile(path, "w") as zf:
            for fichero in sorted(origen.iterdir()):
                zf.write(fichero, f"dentro/carpeta/{fichero.name}")
        return path

    def test_saca_el_shapefile_y_lo_deja_legible(self, archive, tmp_path):
        shp = _extract_shapefile(archive, "dentro/carpeta/capa.shp", tmp_path / "salida")
        assert shp.exists()
        assert len(gpd.read_file(shp)) == 1

    def test_descarta_los_indices_antiguos(self, archive, tmp_path):
        """Vienen corruptos y hacen abortar la lectura, asi que no se extraen."""
        destino = tmp_path / "salida"
        _extract_shapefile(archive, "dentro/carpeta/capa.shp", destino)
        assert not (destino / "capa.sbn").exists()
        assert not (destino / "capa.sbx").exists()

    def test_trae_los_ficheros_acompanantes(self, archive, tmp_path):
        """Un shapefile no es un fichero sino un conjunto que va junto."""
        destino = tmp_path / "salida"
        _extract_shapefile(archive, "dentro/carpeta/capa.shp", destino)
        extensiones = {p.suffix for p in destino.iterdir()}
        assert {".shp", ".shx", ".dbf"} <= extensiones

    def test_si_el_zip_no_lo_contiene_avisa(self, archive, tmp_path):
        with pytest.raises(FileNotFoundError, match="no contiene el shapefile"):
            _extract_shapefile(archive, "otra/ruta/inexistente.shp", tmp_path / "salida")


class TestDownload:
    """Cache de las descargas."""

    def test_no_vuelve_a_bajar_lo_que_ya_esta(self, tmp_path):
        """La URL apunta a un host inexistente: si intentara bajarlo, fallaria."""
        destino = tmp_path / "ya_descargado.zip"
        destino.write_bytes(b"contenido previo")
        assert _download("https://no.existe.invalid/x.zip", destino) == destino
        assert destino.read_bytes() == b"contenido previo"


class TestBuildZoneLayer:
    """Orquestacion completa, con las descargas ya en cache."""

    def test_escribe_la_capa_y_la_cuenca(self, tmp_path, basin, monkeypatch):
        import ndvi_guadalquivir.zones as modulo

        municipios = gpd.GeoDataFrame(
            {
                "NATCODE": ["34011414021", "34012828079"],
                "NAMEUNIT": ["Dentro", "Fuera"],
            },
            geometry=[box(-4.8, 37.2, -4.6, 37.4), box(-3.0, 40.0, -2.8, 40.2)],
            crs="EPSG:4326",
        )

        cache = tmp_path / "cache"
        (cache / "work" / "basin").mkdir(parents=True)
        (cache / "work" / "municipios").mkdir(parents=True)
        basin.to_file(cache / "work" / "basin" / "hybas_eu_lev04_v1c.shp")
        municipios.to_file(
            cache / "work" / "municipios"
            / "recintos_municipales_inspire_peninbal_etrs89.shp"
        )

        # Las fuentes ya estan en disco: se anulan descarga y extraccion.
        monkeypatch.setattr(modulo, "_download", lambda url, dest: dest)
        monkeypatch.setattr(
            modulo, "_extract_shapefile",
            lambda archive, member, dest_dir: dest_dir / f"{member.rsplit('/', 1)[-1]}",
        )

        dest = build_zone_layer(cache_dir=cache, dest=tmp_path / "salida" / "zonas.gpkg")

        assert dest.exists()
        assert dest.with_name("guadalquivir_basin.gpkg").exists()
        zonas = gpd.read_file(dest)
        assert zonas["zone_id"].tolist() == ["14021"]

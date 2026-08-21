"""Pipeline geoespacial de monitorizacion de vegetacion (NDVI) sobre la
cuenca del Guadalquivir a partir de imagenes Sentinel-2 de Copernicus.

El paquete se organiza en capas independientes para que cada una sea
testeable por separado:

    catalog   descubrimiento de escenas en el catalogo STAC
    raster    lectura de bandas por ventana espacial (sin descargar la escena)
    indices   algebra de bandas y enmascarado de calidad (funciones puras)
    zonal     agregacion de raster a poligonos administrativos
    schemas   contratos de datos validados con Pandera
"""

__version__ = "0.1.0"

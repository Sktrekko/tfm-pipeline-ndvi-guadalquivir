# Metodología y medidas

Material para la memoria del TFM. Todo número de este documento está medido
contra datos reales, no estimado. Cuando una cifra procede de una muestra y no
del conjunto completo, se dice de dónde sale.

La sección entre corchetes al final de cada apartado indica dónde encaja en la
plantilla de la memoria.

## 1. El planteamiento: no descargar las imágenes

Una escena de Sentinel-2 pesa entre 500 MB y 1 GB. La serie completa de este
trabajo son 14.029 escenas útiles sobre la cuenca del Guadalquivir entre 2018 y
2026, así que descargarlas supondría del orden de diez terabytes y varias
semanas de transferencia.

El planteamiento del pipeline es el contrario: las imágenes se quedan donde
están, en el bucket público de Element84 en AWS, y de cada una se lee solo la
ventana que cae dentro de la zona de estudio y solo al nivel de detalle que
hace falta. Esto es posible porque las escenas se publican en formato COG
(*Cloud Optimized GeoTIFF*), que permite pedir por HTTP un trozo concreto del
fichero en lugar del fichero entero.

La consecuencia medible: **el almacén final ocupa unos 200 MB**, frente a los
terabytes que ocuparía el archivo original. Cabe en un pendrive.

[5.2 Arquitectura, 6.1 Fuentes]

## 2. Los ahorros, medidos uno a uno

### 2.1 Leer por pirámide en vez de a resolución nativa

Un COG guarda, además de la imagen a resolución completa, versiones reducidas
previamente calculadas (*overviews*). Como el trabajo agrega a municipio, no
hace falta el detalle de 10 metros.

| Medida | Resolución nativa | Overview 2 (80 m) |
|---|---|---|
| Banda roja, ventana de 35×33 km | 48,8 MB / 16,07 s | 0,8 MB / 0,82 s |
| Escena completa real | 30,2 s | 6,0 s |

Son **cinco veces menos tiempo y sesenta veces menos datos transferidos**.

El coste de este atajo hay que declararlo: calcular el NDVI sobre píxeles ya
promediados no es idéntico a promediar los NDVI de los píxeles originales. Se
midió esa diferencia y resultó ser de **0,0027**, unas cinco veces menor que el
propio ruido del sensor. Es decir, el atajo introduce un error un orden de
magnitud inferior a la incertidumbre que ya tiene el dato de partida.

### 2.2 Deduplicar las escenas reprocesadas

La ESA reprocesa periódicamente su archivo, y el catálogo conserva las dos
versiones del mismo gránulo sin retirar la antigua. Leerlas ambas cuesta el
doble y además mezclaría calibraciones distintas.

De las **19.943 escenas** que devuelve el catálogo para la zona y el periodo,
**5.914 son duplicados**, un **30%**. En los años 2019 a 2021 la proporción
llega a casi la mitad. Descartarlos deja **14.029 escenas útiles**.

### 2.3 Paralelismo, hasta donde el servidor deja

Leer una fecha de 18 gránulos en serie tarda 81 segundos. Con hilos:

| Hilos | Tiempo |
|---|---|
| 1 | 81 s |
| **4** | **42 s** |
| 6 | 92 s |
| 10 | 84 s |

Por encima de cuatro hilos el servidor estrangula las peticiones y el
rendimiento empeora hasta ser peor que en serie. Es un resultado interesante
porque contradice la intuición de que más paralelismo siempre ayuda: el cuello
de botella no está en la máquina local sino en el servicio remoto.

El reparto del tiempo dentro de una fecha es **91,7% lectura, 5,9% estadística
zonal y 2,4% mosaico**, lo que confirma que optimizar la lectura es lo único
que mueve la aguja.

### 2.4 Memoria: acumular en vez de apilar

Construir el mosaico de la cuenca completa apilando todos los gránulos en
memoria antes de combinarlos daba un pico de **1.235 MB**. Acumulando sobre un
lienzo único conforme llegan, el pico baja a **505 MB**, un 59% menos.

### 2.5 Caché de GDAL

Abrir por segunda vez el mismo COG pasa de **1,050 s a 0,006 s**, porque GDAL
guarda la cabecera. Es la razón de que convenga agrupar las lecturas por fecha
y no por zona.

[7.2 Métricas, 6.2 Preparación]

## 3. Cuánto se tarda de verdad

La carga histórica completa se lanzó el 25 de agosto de 2026 a las 11:23.

| Concepto | Medida |
|---|---|
| Consulta al catálogo STAC | **19 min 37 s** |
| Fechas a procesar | 1.852 |
| Escenas a leer | 14.029 |
| Ritmo con 4 hilos | **3,74 s de reloj por escena** |
| Trabajo por escena | 14,2 a 14,6 s |
| Duración total estimada | unas 14 h |

Dos observaciones metodológicas que merecen aparecer en la memoria:

**El buen predictor es la escena, no la fecha.** Estimar por fechas engaña,
porque una fecha de invierno con una tesela y otra de verano con dieciocho no
se parecen en nada: el coste por fecha varía entre 51,6 y 130,4 segundos según
el tramo del año. El coste por escena, en cambio, se mantuvo entre 14,2 y 14,6
segundos durante toda la carga. Como el 92% del tiempo es lectura, el número de
lecturas es lo que gobierna la duración.

**La primera estimación se quedó corta a la mitad.** Se había estimado en 5 o 6
horas y la medición sobre las primeras 50 fechas reales dio 13 a 15. El error
venía de extrapolar desde fechas de principios de 2018, que traen 4,9 escenas de
media, cuando el conjunto promedia 7,5.

[7.2 Métricas]

## 4. Lo que se rompió por el camino

Estas incidencias son material valioso para la memoria porque documentan el
proceso real de ingeniería, no la versión idealizada.

### 4.1 El offset de reflectancia y quién lo aplica

Desde la línea base de procesado 04.00, el producto de Sentinel-2 lleva un
desplazamiento de -1000 que hay que restar. Ignorarlo sesga todo el NDVI hacia
arriba sin salirse del rango válido, lo que produciría un escalón artificial a
finales de 2021 que parecería un cambio real en la vegetación.

La trampa fina es que **quién aplica ese offset depende de la colección**. En
`sentinel-2-c1-l2a` los ficheros traen los enteros crudos y lo aplica el
pipeline. En `sentinel-2-l2a`, el proveedor ya lo restó dentro del fichero en
casi todas las escenas modernas y lo declara en un campo del catálogo, salvo en
unas pocas escenas de 2022 donde no. Aplicarlo dos veces hunde la reflectancia
tanto como ignorarlo. Se resolvió consultándolo escena a escena.

Comprobación de que quedó bien, hecha de punta a punta sobre datos escritos:
una fecha de primavera con línea base antigua (2 de mayo de 2019) da NDVI medio
**0,4247**, y otra con línea base moderna (2 de mayo de 2024) da **0,4465**, con
rangos casi idénticos. Si el offset se aplicase dos veces, la fecha moderna
aparecería visiblemente hundida. No hay escalón entre épocas.

### 4.2 La deduplicación descartaba la única versión legible

Al quedarse con la línea base más alta, el criterio elegía a veces una versión
cuyo enlace apunta al archivo antiguo en JP2, alojado en un bucket de pago por
petición inaccesible, en lugar del COG público. Como esas versiones suelen ser
las más modernas, se llevaban por delante a la única que se podía abrir.

Medido durante la carga: **20 escenas de unas 6.000, un 0,3%**, con varias
fechas perdidas enteras por tener una sola escena disponible. Se corrigió
haciendo que la legibilidad del enlace pese más que la línea base.

### 4.3 Otras trampas documentadas

- **NDVI de 1.476.395.** En agua profunda el denominador tiende a cero. Se
  protege con umbral y recorte al rango teórico.
- **Zonas partidas entre teselas.** Al principio solo una de cada tres zonas
  obtenía dato. La malla de referencia debe derivarse de la zona de estudio y
  nunca del primer gránulo que llegue, o las fechas dejan de ser comparables.
- **Fracción de solape mayor que uno.** El área del municipio recortado y la
  del municipio entero se calculan por caminos distintos, así que para los que
  caen enteros dentro el cociente daba 1,0000000000000044. Rompía la validación
  de rango en 181 municipios.
- **Filtro por caja de GDAL.** Trabaja en el sistema de coordenadas del fichero
  y devuelve vacío sin avisar si no coincide.
- **Catálogo Iceberg en memoria.** La imagen de referencia guarda por defecto
  sus metadatos en SQLite en memoria: al reiniciar el contenedor las tablas
  desaparecen aunque sus ficheros sigan en el almacenamiento de objetos.

[6.2 Preparación, 7.4 Limitaciones]

## 5. El almacén

Los datos se escriben en formato Apache Iceberg sobre almacenamiento de objetos
compatible con S3, particionados por mes de adquisición.

Tamaños medidos con los metadatos del propio Iceberg:

| Componente | Tamaño |
|---|---|
| Dato útil por fila | **79,6 bytes** (Parquet comprimido) |
| Capa de zonas (445 municipios con geometría) | 6,2 MB, fijo |
| Almacén completo estimado | por debajo de 200 MB |

Un detalle operativo que conviene mencionar: el pipeline escribe cada 10
fechas, lo que genera del orden de 186 ficheros Parquet pequeños. Para
consultas intensivas conviene compactarlos, algo que Iceberg ofrece como
operación de mantenimiento.

[6.6 Almacenamiento, 5.4 Costes]

## 6. Verificación de que el dato es correcto

Además de las validaciones automáticas (contratos de esquema antes de escribir
y 31 comprobaciones en la capa de transformación), se hicieron comprobaciones
de sentido físico sobre los datos ya cargados.

**Integridad.** Sobre 15.029 filas: cero nulos, cero valores fuera del rango
teórico del NDVI, cero percentiles invertidos, cero casos de píxeles válidos por
encima del total y cero duplicados de municipio y fecha.

**Estacionalidad.** El NDVI medio por mes reproduce el ciclo del secano
mediterráneo sin que se le haya impuesto nada:

| Mes | NDVI medio |
|---|---|
| Febrero a marzo | 0,39 a 0,45 |
| Abril a mayo | 0,42 |
| Junio | 0,32 |
| Julio a agosto | 0,30 a 0,28 |
| Noviembre a diciembre | 0,44 a 0,43 |

La vegetación crece con las lluvias de invierno, hace máximo en primavera, se
seca en verano y repunta en otoño. **La caída de mayo a junio, de 0,422 a
0,323, es la senescencia del cereal**, es decir el cultivo secándose antes de la
siega.

Esta comprobación es más exigente de lo que parece: ningún error de offset ni de
máscara de nubes sobrevive a ella. Un offset mal aplicado desplazaría o
aplanaría la curva, y una máscara defectuosa daría valores absurdos en invierno.

[7.1 Logros, 7.2 Métricas]

## 7. Resultado: la sequía se ve, pero un año después

Media de NDVI de primavera (abril y mayo) por año, sobre el conjunto de la
cuenca:

| 2018 | 2019 | 2020 | 2021 | 2022 | 2023 |
|---|---|---|---|---|---|
| 0,421 | 0,378 | 0,404 | 0,377 | 0,413 | **0,345** |

El hallazgo tiene dos partes y conviene contarlas juntas.

**2022 no destaca como año seco**, pese a ser el año en que se declaró la
sequía. Aparece como el segundo más verde de la serie.

**2023 se descuelga del resto de forma inequívoca.** Está 0,032 por debajo del
año más flojo anterior y 0,076 por debajo de 2018. Como referencia de escala,
entre los cinco años anteriores toda la variación cabía en 0,044: 2023 no es un
matiz dentro del rango normal, es otro régimen.

La lectura agronómica es que **la sequía se declara cuando fallan las lluvias,
pero el daño a la vegetación se mide al ciclo siguiente**. La primavera de 2023
fue excepcionalmente seca y cálida y arrasó el cereal de secano andaluz. El
NDVI mide el estado de la planta, no la precipitación, así que retrata el
segundo momento.

Dos cautelas antes de dar esto por cerrado. Es una media sin ponderar, donde
una fecha medio nublada con diez municipios pesa igual que una despejada con
cuatrocientos; el indicador fino del trabajo es la anomalía de cada municipio
frente a su propia normal histórica. Y promediar los 445 municipios mezcla
secano con regadío, que es precisamente donde debería estar la diferencia: el
regadío mantiene su verdor a costa del embalse mientras el secano se seca.

Que la señal aparezca ya en la medición más tosca posible es, en sí mismo, un
indicio de que el método funciona.

Queda pendiente contrastarlo con datos de precipitación de AEMET. Ese cruce
convertiría el resultado de "el pipeline detecta una anomalía" en "el pipeline
detecta la sequía que registró la agencia meteorológica", que es
considerablemente más sólido.

[7.1 Logros, 7.4 Limitaciones]

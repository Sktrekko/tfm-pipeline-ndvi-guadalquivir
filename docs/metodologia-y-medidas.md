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

### 2.6 El cuello de botella es la latencia, no el ancho de banda

Conviene medirlo porque la intuición falla: con una carga que pasa el 92% del
tiempo leyendo por red, parece que una conexión más rápida la aceleraría.

Medido durante la carga, el proceso consume **648 KB/s, unos 5 Mbps**. Es una
fracción minúscula de cualquier conexión doméstica actual, así que el ancho de
banda no está ni cerca de saturarse.

Lo que sí pesa es el tiempo de ida y vuelta hasta el servidor. Pidiendo un
kilobyte a uno de los ficheros reales:

| Fase | Tiempo |
|---|---|
| Establecer la conexión | 0,18 s |
| Negociación TLS | 0,35 s acumulado |
| Primer byte recibido | **0,54 a 0,65 s** |
| Total | prácticamente igual al primer byte |

Es decir, **más de medio segundo antes de recibir el primer dato, y transferir
el contenido no cuesta nada apreciable**. La causa es geográfica: el archivo
está alojado en la región de AWS en Oregón y el proceso corre en España, de modo
que cada petición cruza el Atlántico. Leer una escena implica varias peticiones
por banda (cabecera, índice de bloques y los bloques en sí), y ahí se van los
14 segundos de trabajo por escena, casi todos esperando.

Esto explica también por qué subir de cuatro hilos empeora el rendimiento
(apartado 2.3): el problema nunca fue la capacidad del enlace.

La consecuencia práctica es que **mejorar la conexión contratada no aceleraría
la carga**. Lo que la aceleraría de forma drástica es ejecutar el pipeline en la
misma región donde viven los datos, donde la ida y vuelta baja de unos 180
milisegundos a menos de uno. Es un argumento fuerte a favor de la portabilidad
del diseño: el mismo código, sin cambios, correría en AWS contra los mismos
ficheros y tardaría una fracción.

[7.2 Métricas, 5.4 Costes, 7.5 Futuras líneas]

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

**Cuánto daño hizo de verdad.** La carga del 25 de agosto corrió con el código
anterior al arreglo, así que hubo que reprocesar las 40 fechas afectadas. La
lista no sale del registro de ingesta sino del log, y esa es la parte sutil: una
fecha que perdió una escena de diez se mosaicó igual con las nueve restantes y
quedó anotada como correcta, indistinguible de las buenas. Solo las fechas de
una única escena fallaron de forma visible.

El reproceso permite medir el sesgo exacto, porque Iceberg conserva la versión
anterior de la tabla y se pueden comparar las dos instantáneas fila a fila:

| | |
|---|---|
| Filas comparadas | 974 |
| Con NDVI idéntico | 962 |
| Con NDVI distinto | 12, todas del 10 de junio de 2021 |
| Diferencia máxima | **0,0031** |
| Diferencia media | 0,000013 |

El caso extremo es Almonte, cuya cobertura útil pasó del 90,8% al 99,7% del
término al recuperar el granulo que le faltaba, y su NDVI medio de 0,3206 a
0,3175. La diferencia máxima está en el mismo orden que el sesgo de 0,0027 por
leer overviews, es decir, por debajo del ruido del sensor. El fallo era real
pero su efecto sobre el resultado es despreciable.

**Once fechas no se pueden recuperar.** Son días en que el satélite hizo una
sola pasada sobre la cuenca y esa única escena solo existe en el bucket antiguo.
No hay copia legible que elegir, así que fallan en el momento de abrir el
fichero. Es un límite del archivo público y no del método: sobre 1.860 fechas
del intervalo son un 0,6%, repartidas y sin agrupar en ninguna estación, de modo
que no sesgan la serie temporal.

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

## 5-bis. La orquestación: qué corre solo y cuándo

La carga histórica es una operación de un solo uso. Lo que mantiene la serie
viva después es otra cosa, y ahí es donde entra Airflow 3.

Son dos DAG con una división deliberada. `ndvi_ingest` se despierta cada
madrugada, pregunta al catálogo qué días hay en los últimos veinte, descarta
contra el registro de ingesta los que ya están resueltos y procesa el resto.
`ndvi_transform` no tiene hora: se programa sobre el asset que produce la
ingesta, así que dbt corre cuando entra dato nuevo y no corre cuando no entra.

### Por qué una tarea por fecha y no un bucle

Las fechas son independientes entre sí. Con un bucle dentro de una sola tarea,
un corte de red en el día 12 obliga a repetir también el 10 y el 11, que ya
habían salido bien. Airflow genera las tareas en el momento de ejecutar, una
por fecha pendiente, y reintenta solo la que falló. De paso, la rejilla de la
ejecución pasa a leerse como el calendario del satélite, con el día de la
adquisición por nombre en lugar de un índice.

### Por qué el disparo va por dato y no por reloj

Un cron para dbt obliga a elegir una hora y aceptar sus dos fallos, que son
simétricos: si corre antes de que termine la ingesta transforma dato viejo, y
si corre después gasta el trabajo de recalcular nueve años de climatología en
días en los que no ha entrado ni una fila. Sobre esta cuenca el satélite pasa
cada cinco días, de modo que la mayoría de las madrugadas no hay nada que
recalcular. La tarea que publica el asset se marca como saltada si la ejecución
no escribió filas, y una tarea saltada no emite el evento.

Esta es la razón concreta por la que el proyecto usa Airflow 3 y no la 2: la
programación por assets es la novedad de la 3 que resuelve exactamente este
problema.

### Números medidos, 27 de agosto de 2026

Contra el almacén real, no contra datos de prueba:

| Paso | Tiempo |
|---|---|
| Consulta acotada a la ventana de 20 días | **12 s** (17 fechas, 16 ya resueltas) |
| Proceso de una fecha (14 escenas, 336 filas) | **43,4 s** |
| `ndvi_ingest` completo | **61,8 s** |
| `ndvi_transform` completo: 6 modelos y sus 31 comprobaciones | **7,4 s** |

La cadena entre los dos DAG se comprobó con el planificador en marcha, no
leyendo la base de metadatos: la ingesta programada terminó a las 07:06:13 y
`ndvi_transform` arrancó a las 07:06:14 con tipo de ejecución
`asset_triggered`.

La misma prueba sirvió de comprobación de reproducibilidad. Para forzar que
hubiera trabajo pendiente se borró el 25 de agosto de 2026, filas y entrada del
registro, y se dejó que el DAG lo rehiciera por su cuenta. Volvieron las mismas
**336 filas** con la misma media de NDVI (**0,3009**), y la capa gold regresó a
sus 128.831 filas y sus 553 municipios-semana por debajo de dos desviaciones
típicas. El reproceso de una fecha es determinista hasta la cifra.

### Un detalle de despliegue que ahorró trabajo

Airflow 3.3.1 resuelve sus dependencias sin un solo conflicto contra pandas
3.0.5, rasterio y geopandas, que son las del cálculo. Eso permite instalarlo en
el mismo entorno virtual, en un grupo aparte que no se sincroniza por defecto, y
evita tener que mantener una imagen de Docker propia con GDAL dentro. Era el
riesgo esperado de este bloque y no se materializó.

[5.3 Tecnologías, 7.2 Métricas]

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

## 7. Resultado: la sequía se encadena de un año al siguiente

Este apartado se apoya en `ndvi_anomaly`, que compara cada municipio con su
propia normal de esa misma semana ISO. Es el indicador fino del trabajo: la
media de la cuenca mezcla el olivar de Jaén con la marisma de Doñana, mientras
que la anomalía pregunta a cada sitio si está peor de lo que suele estar él.

### 7.1 Los nueve años ordenados

Diferencia de cada primavera (semanas 14 a 22, abril y mayo) respecto a la media
que ese mismo municipio da en las demás primaveras, y porcentaje de municipios
que quedaron por debajo de su propia media:

| Año | Diferencia | Municipios por debajo |
|---|---|---|
| 2025 | +0,0280 | 19% |
| 2026 | +0,0237 | 19% |
| 2020 | +0,0204 | 18% |
| 2018 | +0,0203 | 27% |
| 2024 | +0,0203 | 17% |
| 2022 | +0,0112 | 41% |
| 2019 | -0,0226 | 88% |
| 2021 | -0,0330 | 89% |
| **2023** | **-0,0683** | **98%** |

La última columna es la que más dice. En un año corriente queda por debajo de su
media uno de cada cinco municipios, que es lo que cabe esperar del ruido. En
2023 quedaron 437 de 445, de modo que la caída no sale de unos pocos sitios muy
malos tirando de la media: es la cuenca entera a la vez.

Contrastado con una prueba t pareada sobre los 445 municipios, la caída de 2023
da t = -32,2 y una d de Cohen de -1,53. El umbral habitual para hablar de efecto
grande es 0,8.

### 7.2 El fallo tiene fecha

Semanas de 2023 en las que más de la mitad de la cuenca estuvo por debajo de lo
normal, con el porcentaje que cayó por debajo de una desviación:

| Semana | Fechas | Anomalía en sigmas | Cuenca afectada |
|---|---|---|---|
| 16 | mediados de abril | -1,35 | 89% |
| 17 | finales de abril | -1,57 | 92% |
| **18** | **principios de mayo** | **-1,73** | **95%** |
| 19 | mediados de mayo | -1,49 | 87% |
| 22 | finales de mayo | -1,44 | 96% |

El pico cae en la primera semana de mayo, que es cuando el cereal de secano
llena el grano. Si le falta agua en esa ventana la espiga no cuaja y la planta
seca antes de tiempo, y el NDVI lo retrata porque mide cuánta hoja verde queda
en pie.

### 7.3 2022 sí aparece, pero en otra estación

Esta es la corrección a la primera lectura de estos datos, que daba 2022 por
año sin señal. Anomalía media en sigmas por año y estación:

| Año | Primavera | Verano | Otoño | Invierno |
|---|---|---|---|---|
| 2019 | -0,57 | -0,70 | -0,19 | -0,18 |
| 2021 | -0,56 | -0,30 | +0,04 | +0,19 |
| 2022 | +0,12 | -0,31 | **-0,46** | **-0,67** |
| 2023 | **-1,37** | -0,74 | -0,11 | -0,04 |
| 2024 | +0,33 | -0,07 | +0,92 | +0,27 |

El otoño y el invierno de 2022 son los peores de la serie. La primavera de 2023
es la peor de la serie. Y en otoño de 2023 la cuenca ya está en -0,11, es decir,
recuperada.

Leídos juntos, los dos años cuentan un mecanismo en vez de señalar una rareza:
fallan las lluvias de otoño e invierno de 2022, la vegetación lo aguanta porque
en invierno consume poco, y el daño estalla en la primavera siguiente, cuando el
cultivo pide el agua que no está. La sequía se declara cuando deja de llover; el
NDVI mide la planta, así que retrata el segundo momento, no el primero.

### 7.4 Dónde dolió más

Los diez municipios con peor anomalía en la primavera de 2023 forman un grupo
geográfico, no una lista dispersa. Añora, Villanueva de Córdoba, Pozoblanco,
Peñarroya-Pueblonuevo y La Granjuela son de Los Pedroches y el norte de Córdoba;
Hinojosas de Calatrava y Llerena caen al otro lado de Sierra Morena. Todo dehesa
y cereal de secano, sin una gota de riego. Añora, el peor, pasó de una normal de
0,552 a un observado de 0,320.

Por provincias, ordenadas de más a menos castigo en sigmas: Badajoz -1,68,
Huelva -1,64, Sevilla -1,49, Córdoba -1,49, Ciudad Real -1,47, Granada -1,25,
Jaén -1,02.

Que Jaén sea el que menos sufre encaja con la agronomía: es olivar, y el olivo
tiene raíz profunda y aguanta una campaña seca mucho mejor que un cereal anual.
La lectura de secano contra regadío que faltaba en la versión anterior de este
apartado aparece aquí por la vía del cultivo dominante de cada provincia.

Cautela sobre esos números: Badajoz aporta 11 municipios y Ciudad Real 18, que
son bordes de la cuenca y muestras pequeñas. Sevilla (103), Granada (121), Jaén
(89) y Córdoba (63) sostienen mucho mejor la comparación.

### 7.5 Tres cautelas y una pregunta abierta

**La cobertura es comparable entre años**, y conviene decirlo porque es la
objeción evidente. Entre 1,3 y 1,6 observaciones por semana y municipio, y entre
el 92% y el 95% de píxeles útiles, en los nueve años. La anomalía de 2023 no
sale de tener menos imágenes ese año.

**La normal se calcula con los mismos ocho años, 2023 incluido.** Eso significa
que 2023 aporta una octava parte a la referencia contra la que se compara, lo
cual reduce su anomalía en lugar de inflarla. El efecto real es algo mayor que
el de las tablas.

**2026 llega solo hasta el 23 de agosto**, así que su otoño está vacío y su
media anual no es comparable con la de un año cerrado.

Y una pregunta que este apartado dejó abierta: 2024, 2025 y 2026 salen los tres
por encima, con inviernos que suben de +0,27 a +0,44 y a +0,64. Podía ser
recuperación real tras la sequía o podía ser un efecto del sensor o del
reprocesado del archivo. Tres años sobre una serie de nueve no bastan para
llamarlo tendencia, y afirmarlo sin contrastar sería justo el tipo de conclusión
que el resto del trabajo se ha cuidado de no sacar. **El apartado 8 la
responde**, y con una fuente que no tiene nada que ver con el satélite.

[7.1 Logros, 7.2 Métricas, 7.4 Limitaciones]

## 8. El cruce con la lluvia: de una anomalía a un mecanismo

Todo lo anterior sale de un solo instrumento. Un sensor óptico en órbita dice
que la vegetación de la cuenca se apartó de lo normal en la primavera de 2023,
y eso, por sí solo, admite varias explicaciones que no son la sequía: un cambio
de cultivo, un incendio, un reprocesado del archivo o un error del propio
pipeline. La forma de descartarlas no es afinar el mismo instrumento sino
traer otro que no se hable con él.

Ese otro instrumento es la red de pluviómetros de AEMET: 109 estaciones dentro
de la cuenca, 298.257 observaciones diarias del 1 de enero de 2018 al 23 de
agosto de 2026. No comparte con Sentinel-2 ni el sensor, ni el operador, ni la
física de la medida, ni el grano espacial. Si las dos series coinciden, la
coincidencia no puede venir de un fallo compartido.

### 8.1 Por qué el cruce va sobre la cuenca y no sobre el municipio

Es la decisión de diseño de esta parte y va en contra de lo que apetece hacer.
Hay 445 municipios con NDVI y 109 estaciones con lluvia, así que para poner
lluvia en cada municipio habría que interpolar: elegir un método (vecino más
próximo, distancia inversa, kriging), justificarlo y cargar con el error que
introduzca. Ese error tiene una propiedad desagradable: no se ve en el
resultado, se disfraza de señal.

Agregando a la cuenca la pregunta se responde sin interpolar nada. Los 445
municipios dan una media por su lado, las 109 estaciones dan otra por el suyo, y
se comparan las dos series. Si la señal se ve así, no hace falta asumir el
riesgo. La versión por municipio queda como línea futura, para cuando la
pregunta sea *dónde* y no *cuándo*.

Hay además una trampa propia de esta fuente que obliga a trabajar con anomalías
y no con lluvia bruta: **la red no es la misma todos los años**. Las estaciones
activas pasan de 92 en 2018 a 108 en 2026. Promediar los milímetros de cada año
compararía redes distintas y confundiría un cambio de instrumental con un cambio
de clima. Por eso cada estación se compara con su propia normal para ese mes
concreto, igual que cada municipio se compara con la suya, y solo entra al
promedio de la cuenca si tiene cuatro años o más de historia y el mes medido
entero (al menos el 90% de los días). El 90,2% de los pares estación-mes cumplen
ese segundo requisito.

El grano temporal del cruce es el **mes** y no la semana, que es el de la serie
de NDVI. La razón es que la lluvia es discontinua: una semana sin lluvia es lo
normal incluso en un invierno húmedo, de modo que la serie semanal está llena
de ceros que no significan nada. El mes es la ventana más corta en la que un
cero sí es una noticia.

### 8.2 Los nueve años, vistos por los dos instrumentos a la vez

| Año | Lluvia (mm) | Sobre lo normal | Anomalía NDVI | Municipios por debajo |
|---|---|---|---|---|
| 2018 | 680,5 | 1,40 | +0,0070 | 37% |
| 2019 | 372,3 | 0,77 | -0,0172 | 76% |
| 2020 | 412,6 | 0,86 | +0,0066 | 39% |
| 2021 | 397,6 | 0,83 | -0,0046 | 59% |
| 2022 | 422,5 | 0,87 | -0,0160 | 65% |
| **2023** | **290,6** | **0,61** | **-0,0243** | **74%** |
| 2024 | 543,6 | 1,12 | +0,0269 | 27% |
| 2025 | 659,3 | 1,36 | +0,0123 | 40% |
| 2026 (8 meses) | 403,5 | 1,43 | +0,0133 | 27% |

Las dos columnas suben y bajan juntas. **2023 es el año más seco de la serie con
diferencia**, 0,61 de lo que le tocaría, y es también el peor año de vegetación.
Los tres años húmedos, 2024 a 2026, son los tres mejores. La correlación entre
las dos columnas sobre los nueve años es **r = +0,756** (t = 3,06 con 7 grados
de libertad). Sobre los ocho años cerrados, dejando fuera 2026 porque solo llega
a agosto, sale **r = +0,738**.

Esto es lo que cierra la pregunta abierta del apartado 7.5. **El repunte de
2024-2026 no es un efecto del sensor: es lluvia.** Los tres años que el satélite
ve más verdes son exactamente los tres en que los pluviómetros recogen más agua,
y los pluviómetros no saben nada de líneas base de procesado ni de reprocesados
del archivo. Sigue sin ser demostración, porque una deriva del sensor podría
coincidir en el tiempo con un periodo húmedo, pero ya no hace falta invocarla
para explicar lo que se ve.

### 8.3 El desfase, medido

La afirmación que sostiene todo el relato es que la lluvia y la hoja no van a la
vez: fallan las lluvias de otoño, la vegetación aguanta el invierno porque
consume poco, y el daño estalla en la primavera siguiente. Esa frase es fácil de
escribir y hasta ahora estaba sin medir. Con las dos series mensuales en la misma
tabla se mide correlacionándolas a distintos retardos, sobre los 104 meses de la
serie:

| Qué se correlaciona con la anomalía de NDVI del mes | n | r | t |
|---|---|---|---|
| Lluvia de **ese mismo** mes | 102 | +0,159 | 1,61 |
| Lluvia del **mes anterior** | 103 | **+0,473** | **5,39** |
| Lluvia acumulada de los **3 meses** que terminan ahí | 103 | **+0,499** | **5,79** |
| Lluvia acumulada de los **6 meses** que terminan ahí | 103 | +0,493 | 5,69 |

La primera fila es el resultado y conviene leerla despacio: **la lluvia de un mes
no explica la vegetación de ese mes**. Con r = 0,159 no hay relación que
defender. La misma lluvia, mirada un mes después, explica tres veces más. Ese
salto de una fila a la siguiente es el desfase, y sale del dato en vez de la
intuición.

Lo que mejor funciona es el acumulado del trimestre (r = 0,499), y tiene sentido
agronómico: la planta no responde al chaparrón de anteayer sino al agua que hay
en el suelo, que es la que ha ido entrando durante meses. Estirar la ventana a
seis meses no mejora nada (0,493), lo cual también es información: la memoria
hídrica útil de la cuenca es de un trimestre, no de medio año.

### 8.4 El episodio 2022-2023, mes a mes

Con las dos series juntas, la secuencia se lee de corrido. Cada fila es un mes;
el ratio es lo que recogió la estación típica respecto a su propia normal.

| Mes | Lluvia / normal | Anomalía NDVI | Municipios por debajo |
|---|---|---|---|
| oct 2022 | 0,25 | -0,0250 | 81% |
| nov 2022 | 0,43 | -0,0558 | 98% |
| dic 2022 | 2,39 | -0,0050 | 57% |
| ene 2023 | 0,34 | -0,0037 | 52% |
| feb 2023 | 0,18 | -0,0414 | 93% |
| mar 2023 | 0,12 | -0,0294 | 84% |
| **abr 2023** | **0,02** | **-0,0811** | **99%** |
| **may 2023** | 2,37 | **-0,0880** | **99%** |
| jun 2023 | 3,48 | -0,0164 | 67% |

El otoño de 2022 falla: octubre recoge 17,5 mm donde le tocan 62,0 y noviembre
30,9 donde le tocan 58,4. Diciembre da un respiro (más del doble de lo normal) y
la vegetación lo acusa al instante, con la anomalía casi en cero en enero. Y
entonces se hunde el invierno entero: febrero al 18%, marzo al 12% (14,7 mm
frente a 100,4) y **abril de 2023 con 2,2 mm frente a una normal de 47,5**, o
sea que la estación típica recogió el 2% de lo suyo. Es un mes prácticamente sin
lluvia en el mes en que el cereal de secano llena el grano.

El NDVI toca fondo justo detrás, en abril y mayo, con **el 99% de los 445
municipios por debajo de su normal**. Y el detalle que mejor ilustra el desfase
está en mayo y junio: llueve más del doble y el triple de lo normal, y la
vegetación sigue en su peor momento en mayo y todavía por debajo en junio. **La
lluvia que llega tarde no deshace el daño**, porque el cereal ya no está en
condiciones de aprovecharla. Un análisis sin retardo, correlacionando cada mes
consigo mismo, habría leído esos dos meses como una contradicción.

### 8.5 Lo que este cruce no demuestra

**La campaña agrícola completa no alcanza significación por sí sola.** Agregando
a campaña, de octubre a marzo para la lluvia y abril-mayo para el NDVI, quedan
ocho puntos y la correlación es r = 0,530 con t = 1,53 y 6 grados de libertad:
apunta en la dirección esperada pero ocho campañas son demasiadas pocas para
sostener nada. La serie mensual (n = 103) sí llega holgadamente, y es la que se
usa. Decir lo contrario sería elegir el corte que da el número bonito.

**Correlación no es causalidad, y aquí menos que en otros sitios.** Que la lluvia
y el NDVI se muevan juntos con un mes de retardo es compatible con la
explicación agronómica, que es la razonable, pero también con que ambos
respondan a un tercer factor (la temperatura, por ejemplo, que en la primavera
de 2023 también fue anómala). El cruce descarta que la anomalía de NDVI sea un
artefacto del pipeline; no aísla el mecanismo físico.

**La media de la cuenca esconde el regadío.** Se promedian 445 municipios de
secano y de regadío juntos, y el regadío no responde a la lluvia del mes sino a
la asignación de la confederación. Eso amortigua la señal en lugar de inflarla,
así que el efecto real en secano es mayor que el que muestran estas tablas, pero
separar los dos regímenes exige un dato de usos del suelo que este trabajo no
carga.

**Tres meses de la serie tienen menos municipios de lo normal** (noviembre de
2022 con 392, mayo de 2023 con 285, octubre de 2023 con 371), porque las nubes
tiraron pasadas. Son justo meses del episodio, así que conviene decirlo: el
valor de mayo de 2023 sale de menos de dos tercios de la cuenca. La fracción de
municipios por debajo, en cambio, es robusta a eso, porque es una proporción
sobre los que sí tienen dato.

## Apéndice: cierre de la carga

Generado automáticamente al terminar la carga histórica, el
26 de agosto de 2026 a las 02:29.

```
2026-08-26 02:29:12,428 INFO ndvi_guadalquivir.pipeline: Carga terminada. 1860 fechas en el intervalo, 8 ya cargadas, 1852 procesadas (1292 con dato, 546 sin dato util, 14 con error). 184872 filas escritas en 905.3 min, 114.8 s por fecha.
```

Fechas marcadas con error: 14.
Escenas ilegibles por la trampa del apartado 4.2:
42.

Fechas a recuperar:

```
2018-06-14 2018-08-13 2018-09-22 2018-11-30 2018-12-05 2019-03-26 2019-09-12 2019-10-07 2019-11-26 2019-12-06 2019-12-16 2019-12-26 2020-01-15 2020-03-05 2020-03-15 2020-04-04 2020-04-14 2020-07-03 2020-07-23 2020-08-02 2020-12-15 2021-04-19 2021-06-10 2021-09-06 2021-09-11 2021-12-15 2022-05-04 2022-05-09 2022-08-22 2022-09-01 2023-01-29 2023-02-18 2023-06-25 2023-10-06 2023-12-05 2024-01-23 2024-02-03 2024-03-04 2024-08-01 2025-01-18 
```

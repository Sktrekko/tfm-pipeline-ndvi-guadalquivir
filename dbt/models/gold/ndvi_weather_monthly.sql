-- El cruce: la vegetacion y la lluvia de la cuenca, mes a mes, en la misma fila.
--
-- Este modelo es el que convierte el resultado del proyecto de "el pipeline
-- detecta una anomalia" en "el pipeline detecta la sequia que registro la
-- agencia meteorologica". Son dos instrumentos que no se hablan entre si, un
-- sensor optico en orbita y una red de pluviometros en el suelo, y la unica
-- forma de que coincidan es que los dos esten midiendo lo mismo.
--
-- Por que sobre la cuenca entera y no por municipio
-- -------------------------------------------------
-- Es la decision principal de este modelo y va en contra de lo que apetece
-- hacer. Hay 445 municipios con NDVI y 109 estaciones con lluvia, asi que
-- asignar lluvia a cada municipio exige interpolar: elegir un metodo (vecino mas
-- proximo, distancia inversa, kriging), justificarlo y cargar con el error que
-- introduzca. Ese error no se ve en el resultado, se disfraza de senal.
--
-- Agregando a la cuenca la pregunta se puede responder sin interpolar nada: los
-- 445 municipios dan una media y las 109 estaciones dan otra, cada una por su
-- lado, y se comparan las dos series. Si la senal se ve asi, no hace falta
-- asumir el riesgo. La version por municipio queda como linea futura, para
-- cuando la pregunta sea donde y no cuando.
--
-- El desfase, que es lo que de verdad se busca
-- --------------------------------------------
-- La lluvia y la hoja no van a la vez. Lo que se cuenta en el apartado 7.3 es
-- que fallan las lluvias de otono, la vegetacion aguanta el invierno porque
-- consume poco, y el dano estalla en la primavera siguiente. Para poder medir
-- eso no basta con la lluvia del mes: hace falta la que se ha acumulado por
-- detras. Por eso las columnas de ventana movil, que son las que permiten
-- preguntar con que retardo se correlacionan las dos series en lugar de
-- afirmarlo.
--
-- La ventana de seis meses no es arbitraria: cubre el otono y el invierno
-- completos vistos desde la primavera, que es justo el periodo cuyo fallo se
-- quiere fechar. La de tres meses se guarda al lado para poder comprobar que la
-- conclusion no depende de haber elegido esa longitud.

with ndvi as (
    select * from {{ ref('ndvi_monthly_anomaly') }}
),

zonas as (
    select * from {{ ref('stg_zones') }}
),

lluvia as (
    select * from {{ ref('weather_monthly_anomaly') }}
),

-- La cuenca vista por el satelite. El promedio va ponderado por superficie por
-- la misma razon que en el resumen provincial: sin ponderar, un municipio de dos
-- kilometros cuadrados pesaria lo mismo que uno de mil doscientos y la media
-- hablaria de cuantos municipios pequenos hay.
ndvi_cuenca as (
    select
        n.anio,
        n.mes,

        sum(n.anomalia_ndvi * z.area_km2) / nullif(sum(z.area_km2), 0)
            as anomalia_ndvi,
        avg(n.anomalia_ndvi)   as anomalia_ndvi_simple,
        avg(n.anomalia_sigmas) as anomalia_ndvi_sigmas,
        sum(n.ndvi_observado * z.area_km2) / nullif(sum(z.area_km2), 0)
            as ndvi_observado,
        sum(n.ndvi_normal * z.area_km2) / nullif(sum(z.area_km2), 0)
            as ndvi_normal,

        count(*) as municipios,

        -- Cuanta cuenca esta por debajo de lo normal. Es una medida distinta de
        -- la media y responde a otra pregunta: la media dice cuanto, esta dice
        -- si el episodio es general o son cuatro municipios tirando del resto.
        avg(case when n.anomalia_ndvi < 0 then 1.0 else 0.0 end)
            as fraccion_municipios_por_debajo
    from ndvi n
    inner join zonas z using (zone_id)
    where n.normal_fiable
    group by n.anio, n.mes
),

-- La cuenca vista por los pluviometros. Aqui no se pondera por nada: una
-- estacion es un punto, no una superficie, y repartir area entre puntos seria
-- interpolar por la puerta de atras, que es justo lo que este modelo evita.
lluvia_cuenca as (
    select
        anio,
        mes,

        avg(anomalia_lluvia_mm) as anomalia_lluvia_mm,
        avg(lluvia_mm)          as lluvia_mm,
        avg(lluvia_normal_mm)   as lluvia_normal_mm,

        -- La mediana del cociente y no el cociente de las medias. Con lluvia el
        -- promedio lo domina la estacion de sierra que recogio trescientos
        -- milimetros en un temporal, y la mediana dice como fue el mes en la
        -- estacion tipica.
        median(ratio_lluvia)    as ratio_lluvia,

        avg(temp_media_c)       as temp_media_c,
        avg(anomalia_temp_c)    as anomalia_temp_c,
        count(*)                as estaciones
    from lluvia
    where mes_completo
      and normal_fiable
    group by anio, mes
),

unido as (
    select
        coalesce(n.anio, l.anio) as anio,
        coalesce(n.mes, l.mes)   as mes,
        make_date(coalesce(n.anio, l.anio), coalesce(n.mes, l.mes), 1) as mes_fecha,

        n.anomalia_ndvi,
        n.anomalia_ndvi_simple,
        n.anomalia_ndvi_sigmas,
        n.ndvi_observado,
        n.ndvi_normal,
        n.municipios,
        n.fraccion_municipios_por_debajo,

        l.anomalia_lluvia_mm,
        l.lluvia_mm,
        l.lluvia_normal_mm,
        l.ratio_lluvia,
        l.temp_media_c,
        l.anomalia_temp_c,
        l.estaciones
    from ndvi_cuenca n
    full outer join lluvia_cuenca l
        on n.anio = l.anio
       and n.mes = l.mes
)

select
    *,

    -- Lluvia acumulada en la ventana que termina en este mes, y lo que se aparta
    -- de lo normal en esa misma ventana. La suma corre sobre la serie ordenada
    -- por fecha, de modo que en abril de 2023 estas columnas contienen el otono
    -- y el invierno de 2022-2023, que es lo que la hoja de abril lleva bebido.
    sum(lluvia_mm) over ventana_3 as lluvia_3m_mm,
    sum(lluvia_mm) over ventana_6 as lluvia_6m_mm,
    sum(anomalia_lluvia_mm) over ventana_3 as anomalia_lluvia_3m_mm,
    sum(anomalia_lluvia_mm) over ventana_6 as anomalia_lluvia_6m_mm,

    sum(lluvia_mm) over ventana_6
        / nullif(sum(lluvia_normal_mm) over ventana_6, 0) as ratio_lluvia_6m,

    -- Cuantos meses de los que entran en la ventana traen dato. Sin esta
    -- columna, los primeros meses de 2018 pareceria que tuvieron una sequia
    -- historica cuando lo unico que pasa es que la serie empieza ahi.
    count(lluvia_mm) over ventana_6 as meses_en_ventana_6
from unido
window
    ventana_3 as (order by mes_fecha rows between 2 preceding and current row),
    ventana_6 as (order by mes_fecha rows between 5 preceding and current row)
order by mes_fecha

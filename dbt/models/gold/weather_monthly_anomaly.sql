-- Lluvia mensual de cada estacion frente a su propia normal.
--
-- Es el equivalente meteorologico de `ndvi_anomaly`, y esta escrito con la
-- misma logica a proposito: comparar cada unidad consigo misma. Una estacion de
-- la sierra de Cazorla recoge en un ano normal el triple que una de la campina
-- de Sevilla, asi que restar una media general diria sobre todo donde esta cada
-- estacion y no que tal fue el ano. Comparando cada estacion con lo que ella
-- misma suele recoger en ese mes, la geografia se cancela y queda la anomalia.
--
-- Hay una razon adicional para hacerlo asi, que es la trampa de esta fuente. La
-- red no es la misma todos los anos: crece de 92 estaciones activas en 2018 a
-- 108 en 2026. Promediar la lluvia bruta de cada ano compararia redes distintas
-- y confundiria un cambio de instrumental con un cambio de clima. La anomalia
-- por estacion no tiene ese problema: cada estacion aporta su propia desviacion
-- y las que no llevan suficientes anos se descartan.
--
-- El grano es mensual y no semanal, al contrario que en el NDVI, porque la
-- lluvia es un fenomeno discontinuo. Una semana sin lluvia es lo normal incluso
-- en un invierno humedo, de modo que la serie semanal esta llena de ceros que no
-- significan nada. El mes es la ventana mas corta en la que un cero si es una
-- noticia.

-- Anos minimos para que la normal de un mes sea creible. Es el mismo criterio
-- que en `ndvi_climatology`, y por el mismo motivo: con dos o tres anos no se
-- calcula una normal sino una casualidad.
{% set anios_minimos = 4 %}

-- Fraccion de dias del mes que hace falta haber medido para que el total mensual
-- valga. Un mes con veinte dias de datos no da una lluvia mensual mas baja, da
-- una lluvia mensual desconocida, y meterla en la serie como si fuera un total
-- inventaria una sequia donde solo hubo una averia. Medido sobre la carga: el
-- 90,2% de los pares estacion-mes llegan al 90% de dias, asi que el corte
-- descarta poco y protege mucho.
{% set cobertura_minima = 0.9 %}

with diario as (
    select * from {{ ref('stg_weather_daily') }}
),

mensual as (
    select
        station_id,
        station_name,
        province,
        anio,
        mes,

        sum(precipitation_mm)                  as lluvia_mm,
        count(precipitation_mm)                as dias_con_dato,
        day(last_day(make_date(anio, mes, 1))) as dias_del_mes,

        avg(temp_mean_c) as temp_media_c,
        avg(temp_max_c)  as temp_maxima_media_c,
        count(temp_mean_c) as dias_con_temperatura
    from diario
    group by station_id, station_name, province, anio, mes
),

completos as (
    select
        *,
        dias_con_dato >= {{ cobertura_minima }} * dias_del_mes as mes_completo
    from mensual
),

normal as (
    select
        station_id,
        mes,
        count(*)                as anios_observados,
        avg(lluvia_mm)          as lluvia_normal_mm,
        stddev_samp(lluvia_mm)  as lluvia_desviacion_mm,
        median(lluvia_mm)       as lluvia_mediana_mm,
        avg(temp_media_c)       as temp_normal_c
    from completos
    where mes_completo
    group by station_id, mes
)

select
    c.station_id,
    c.station_name,
    c.province,
    c.anio,
    c.mes,

    c.lluvia_mm,
    c.dias_con_dato,
    c.dias_del_mes,
    c.mes_completo,

    n.lluvia_normal_mm,
    n.lluvia_desviacion_mm,
    n.anios_observados,

    c.lluvia_mm - n.lluvia_normal_mm as anomalia_lluvia_mm,

    -- La misma anomalia en tanto por uno de lo normal. Hace falta porque la
    -- lluvia no se reparte por igual en el ano: faltar treinta milimetros en
    -- diciembre es un mes algo flojo y faltarlos en julio, cuando lo normal son
    -- cinco, es imposible. La resta sola trataria los dos casos igual y daria
    -- todo el peso del resultado a los meses de invierno.
    c.lluvia_mm / nullif(n.lluvia_normal_mm, 0) as ratio_lluvia,

    c.temp_media_c,
    c.temp_maxima_media_c,
    c.temp_media_c - n.temp_normal_c as anomalia_temp_c,

    -- Se marca en lugar de ocultarse, igual que en la climatologia de NDVI, para
    -- que quien consuma la tabla pueda decidir si le vale.
    n.anios_observados >= {{ anios_minimos }} as normal_fiable
from completos c
left join normal n
    on c.station_id = n.station_id
   and c.mes = n.mes

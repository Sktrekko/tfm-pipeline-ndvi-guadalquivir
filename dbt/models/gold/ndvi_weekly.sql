-- Serie semanal por municipio.
--
-- El satelite no pasa en dias fijos y las nubes tiran fechas sueltas, asi que la
-- serie diaria tiene huecos irregulares y no se puede comparar un ano con otro
-- directamente. Agrupar por semana la vuelve regular.
--
-- Cuando en una semana hay varias observaciones se promedian **ponderando por
-- pixeles utiles**: una pasada con el municipio despejado entero pesa mas que
-- otra en la que solo asomaba un tercio. Promediar a secas trataria las dos
-- igual y dejaria que la observacion peor arrastrase el valor.
--
-- Se guarda ademas el maximo de la semana. En teledeteccion es habitual quedarse
-- con el, porque las nubes que la mascara no llega a detectar siempre bajan el
-- NDVI y nunca lo suben, de modo que el mayor valor es el menos contaminado.
-- Tener las dos columnas permite comprobar en la memoria que las conclusiones no
-- dependen de haber elegido una u otra.

with diario as (
    select * from {{ ref('stg_ndvi_daily') }}
)

select
    zone_id,
    zone_name,
    anio,
    semana,

    sum(ndvi_mean * valid_pixel_count) / nullif(sum(valid_pixel_count), 0)
        as ndvi_ponderado,
    avg(ndvi_mean)              as ndvi_simple,
    max(ndvi_mean)              as ndvi_maximo,

    count(*)                    as observaciones,
    sum(valid_pixel_count)      as pixeles_utiles,
    avg(valid_pixel_fraction)   as fraccion_util_media,
    min(acquisition_date)       as primera_fecha,
    max(acquisition_date)       as ultima_fecha
from diario
group by zone_id, zone_name, anio, semana

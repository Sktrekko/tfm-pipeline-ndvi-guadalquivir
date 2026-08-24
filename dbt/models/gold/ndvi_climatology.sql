-- La normal historica: que es lo habitual en cada municipio y cada semana.
--
-- Es la pieza que convierte el trabajo de "mido vegetacion" en "detecto
-- anomalias", y la razon de cargar ocho anos en lugar de tres. Con tres anos
-- solo se ve la estacionalidad: que el cereal crece en primavera y se siega en
-- junio, cosa que ya se sabia. Para poder decir que un ano fue raro hace falta
-- antes una definicion de normal, y eso son muchos anos de la misma semana.
--
-- El grano es municipio por semana del ano. Se agrupan todas las semanas 15 de
-- todos los anos y se calcula que valor y que dispersion tuvieron. La
-- dispersion importa tanto como la media: una desviacion de 0,05 significa cosas
-- muy distintas en un olivar estable y en una vega de regadio.

{% set anios_minimos = 4 %}

with semanal as (
    select * from {{ ref('ndvi_weekly') }}
),

normal as (
    select
        zone_id,
        zone_name,
        semana,

        count(distinct anio)        as anios_observados,
        avg(ndvi_ponderado)         as ndvi_normal,
        stddev_samp(ndvi_ponderado) as ndvi_desviacion,
        median(ndvi_ponderado)      as ndvi_mediana,
        min(ndvi_ponderado)         as ndvi_minimo,
        max(ndvi_ponderado)         as ndvi_maximo,
        quantile_cont(ndvi_ponderado, 0.10) as ndvi_p10,
        quantile_cont(ndvi_ponderado, 0.90) as ndvi_p90
    from semanal
    where ndvi_ponderado is not null
    group by zone_id, zone_name, semana
)

select
    *,
    -- Una normal calculada con dos o tres anos no es una normal, es una
    -- casualidad. Se marca en lugar de borrarse para que el panel pueda
    -- avisar de que ahi la anomalia es menos de fiar, en vez de dejar un
    -- hueco sin explicacion.
    anios_observados >= {{ anios_minimos }} as normal_fiable
from normal

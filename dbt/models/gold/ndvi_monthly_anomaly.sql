-- Anomalia mensual de NDVI por municipio.
--
-- Existe para poder cruzar el NDVI con la lluvia, y solo para eso. El resultado
-- del proyecto se cuenta con `ndvi_anomaly`, que va por semana ISO porque es el
-- grano en que se ve el detalle de una campana agricola. La lluvia, en cambio,
-- no admite ese grano (ver `weather_monthly_anomaly`), asi que para juntarlas
-- hay que bajar el NDVI al mes.
--
-- Se calcula de nuevo desde el dato diario en vez de reagregar la anomalia
-- semanal, y esa es la decision de diseno de este modelo. Reagregar obligaria a
-- repartir en meses las semanas que caen a caballo de dos, que son una de cada
-- cuatro, y ese reparto seria una convencion inventada aqui que luego habria que
-- defender. Recalcular no inventa nada: cada observacion cuenta en el mes en que
-- el satelite la tomo.
--
-- El precio es tener dos definiciones de anomalia en el proyecto, y conviene ver
-- lo que se gana con ello. Al ser independientes, que las dos senalen el mismo
-- ano es una comprobacion de que el resultado no depende de haber elegido la
-- semana o el mes como caja.

{% set anios_minimos = 4 %}

with diario as (
    select * from {{ ref('stg_ndvi_daily') }}
),

mensual as (
    select
        zone_id,
        zone_name,
        anio,
        mes,

        -- Ponderado por pixeles utiles, igual que en la serie semanal: una
        -- pasada con el municipio despejado entero dice mas que otra en la que
        -- solo asomaba un tercio.
        sum(ndvi_mean * valid_pixel_count) / nullif(sum(valid_pixel_count), 0)
            as ndvi_ponderado,
        avg(ndvi_mean)            as ndvi_simple,
        count(*)                  as observaciones,
        sum(valid_pixel_count)    as pixeles_utiles,
        avg(valid_pixel_fraction) as fraccion_util_media
    from diario
    group by zone_id, zone_name, anio, mes
),

normal as (
    select
        zone_id,
        mes,
        count(distinct anio)        as anios_observados,
        avg(ndvi_ponderado)         as ndvi_normal,
        stddev_samp(ndvi_ponderado) as ndvi_desviacion
    from mensual
    where ndvi_ponderado is not null
    group by zone_id, mes
)

select
    m.zone_id,
    m.zone_name,
    m.anio,
    m.mes,

    m.ndvi_ponderado as ndvi_observado,
    m.ndvi_simple,
    m.observaciones,
    m.pixeles_utiles,
    m.fraccion_util_media,

    n.ndvi_normal,
    n.ndvi_desviacion,
    n.anios_observados,

    m.ndvi_ponderado - n.ndvi_normal as anomalia_ndvi,

    case
        when n.ndvi_desviacion > 0.001
        then (m.ndvi_ponderado - n.ndvi_normal) / n.ndvi_desviacion
    end as anomalia_sigmas,

    n.anios_observados >= {{ anios_minimos }} as normal_fiable
from mensual m
inner join normal n
    on m.zone_id = n.zone_id
   and m.mes = n.mes
where m.ndvi_ponderado is not null

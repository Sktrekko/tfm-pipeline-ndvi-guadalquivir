-- Cuanto se aparta cada semana de lo normal en ese municipio y esa epoca.
--
-- Este es el resultado final del proyecto. Cada observacion semanal se compara
-- con la normal de su propio municipio para su propia semana del ano, no con una
-- media general: comparar el olivar de Jaen en marzo con la vega de Sevilla en
-- agosto no dice nada.
--
-- La anomalia se da de dos formas porque responden a preguntas distintas.
--
-- En unidades de NDVI dice cuanto verde falta o sobra. Es la magnitud fisica,
-- comparable entre municipios en terminos absolutos.
--
-- En numero de desviaciones tipicas dice como de raro es. Un descenso de 0,10
-- es rutina en una zona que oscila mucho y una alarma en una que apenas se
-- mueve. Es lo que permite ordenar municipios por gravedad y no por tamano del
-- salto, y por eso es la columna que manda en el mapa del panel.

with semanal as (
    select * from {{ ref('ndvi_weekly') }}
),

normal as (
    select * from {{ ref('ndvi_climatology') }}
),

comparado as (
    select
        s.zone_id,
        s.zone_name,
        s.anio,
        s.semana,
        s.primera_fecha,
        s.ultima_fecha,
        s.observaciones,
        s.fraccion_util_media,

        s.ndvi_ponderado as ndvi_observado,
        n.ndvi_normal,
        n.ndvi_desviacion,
        n.anios_observados,
        n.normal_fiable,

        s.ndvi_ponderado - n.ndvi_normal as anomalia_ndvi,

        -- Con desviacion practicamente nula la division se dispara, asi que se
        -- exige un minimo. Por debajo de 0,001 la serie es plana y hablar de
        -- desviaciones tipicas no tiene sentido.
        case
            when n.ndvi_desviacion > 0.001
            then (s.ndvi_ponderado - n.ndvi_normal) / n.ndvi_desviacion
        end as anomalia_sigmas
    from semanal s
    inner join normal n
        on s.zone_id = n.zone_id
       and s.semana = n.semana
    where s.ndvi_ponderado is not null
)

select
    *,
    -- Etiqueta legible para el panel. Los cortes en una y dos desviaciones son
    -- la convencion habitual: fuera de dos sigmas queda alrededor del 5 % de
    -- las observaciones si la serie se comporta con normalidad.
    case
        when anomalia_sigmas is null      then 'sin referencia'
        when anomalia_sigmas <= -2        then 'muy por debajo'
        when anomalia_sigmas <= -1        then 'por debajo'
        when anomalia_sigmas <   1        then 'normal'
        when anomalia_sigmas <   2        then 'por encima'
        else                                   'muy por encima'
    end as categoria
from comparado

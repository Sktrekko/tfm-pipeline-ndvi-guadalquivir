-- Resumen mensual por provincia.
--
-- Existe para demostrar algo concreto de la decision de ingerir por municipio:
-- subir de grano cuesta esta consulta y nada mas. Ninguna imagen se vuelve a
-- leer. Al reves seria imposible, y ese es todo el argumento de haber elegido la
-- unidad mas pequena.
--
-- El promedio va ponderado por superficie. Sin ponderar, Cajar con sus 1,6 km2
-- pesaria lo mismo que Cordoba con 1.254, y la media provincial hablaria mas de
-- cuantos municipios pequenos hay que de como esta el campo.

with diario as (
    select * from {{ ref('stg_ndvi_daily') }}
),

zonas as (
    select * from {{ ref('stg_zones') }}
)

select
    z.province_code,
    z.province_name,
    d.anio,
    d.mes,

    sum(d.ndvi_mean * z.area_km2) / nullif(sum(z.area_km2), 0) as ndvi_ponderado_area,
    avg(d.ndvi_mean)            as ndvi_simple,
    count(distinct d.zone_id)   as municipios_con_dato,
    count(distinct d.acquisition_date) as fechas,
    sum(z.area_km2)             as km2_observados
from diario d
inner join zonas z using (zone_id)
group by z.province_code, z.province_name, d.anio, d.mes

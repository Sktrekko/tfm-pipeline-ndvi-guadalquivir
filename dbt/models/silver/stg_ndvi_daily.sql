-- Observaciones diarias, limpias y con las columnas de tiempo derivadas.
--
-- Aqui no se agrega nada: se toma lo que escribio el pipeline y se le anaden
-- las columnas de calendario que necesitan todos los modelos de arriba. Tenerlas
-- calculadas una sola vez evita que cada consulta repita la misma extraccion de
-- fecha y que dos modelos usen definiciones de semana distintas sin darse cuenta.

with fuente as (
    select * from {{ source('bronze', 'ndvi_zonal_daily') }}
),

fechado as (
    select
        zone_id,
        zone_name,
        acquisition_date,

        year(acquisition_date)      as anio,
        month(acquisition_date)     as mes,
        dayofyear(acquisition_date) as dia_del_anio,

        -- La semana ISO reparte el ano en 52 cajas comparables entre anos.
        -- La 53 solo aparece en algunos y con muy pocas observaciones, asi que
        -- se une a la 52: una caja con dos anos de datos no permite calcular
        -- ninguna normal fiable.
        least(week(acquisition_date), 52) as semana,

        ndvi_mean,
        ndvi_median,
        ndvi_std,
        ndvi_p10,
        ndvi_p90,
        pixel_count,
        valid_pixel_count,
        valid_pixel_fraction,
        scene_count,
        mean_cloud_cover,
        tile_ids,
        scene_ids
    from fuente
)

select * from fechado

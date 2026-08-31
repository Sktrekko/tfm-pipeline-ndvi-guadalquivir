-- Observaciones diarias de las estaciones, con las columnas de calendario.
--
-- Es el gemelo de `stg_ndvi_daily` para la otra fuente, y hace exactamente lo
-- mismo: no agrega nada, solo deriva de una vez las columnas de tiempo que van
-- a usar todos los modelos de arriba. Que las dos fuentes deriven el ano y el
-- mes con la misma expresion no es cosmetico: si una usara el ano natural y la
-- otra el ano ISO, el cruce compararia meses que no son el mismo mes y nada en
-- el resultado lo delataria.
--
-- La unica limpieza real que ocurre aqui es marcar la estacion como con lluvia
-- medida o sin ella. En el almacen la precipitacion es opcional a proposito
-- (ver `weather_schema`), porque un pluviometro averiado y un dia sin lluvia
-- cuentan cosas opuestas. Convertir ese nulo en cero es la mentira que mas dano
-- haria en una serie sobre sequia, asi que aqui se conserva el nulo y lo que se
-- anade es la cuenta de dias en que si hubo medida.

with fuente as (
    select * from {{ source('bronze', 'weather_station_daily') }}
),

fechado as (
    select
        station_id,
        station_name,
        province,
        longitude,
        latitude,
        altitude_m,

        observed_on,
        year(observed_on)  as anio,
        month(observed_on) as mes,

        precipitation_mm,
        temp_mean_c,
        temp_max_c,
        temp_min_c,

        precipitation_mm is not null as lluvia_medida
    from fuente
)

select * from fechado

-- Municipios de la cuenca, con el nombre de la provincia resuelto.
--
-- El almacen guarda el codigo del INE porque es lo que cruza con cualquier
-- estadistica oficial, pero un panel que muestre "41" en lugar de "Sevilla" no
-- lo lee nadie. La correspondencia se resuelve aqui, una vez, y no en cada
-- consulta ni en el codigo del panel.

with zonas as (
    select * from {{ source('bronze', 'zones') }}
),

provincias as (
    select * from (values
        ('02', 'Albacete'), ('06', 'Badajoz'),   ('11', 'Cadiz'),
        ('13', 'Ciudad Real'), ('14', 'Cordoba'), ('18', 'Granada'),
        ('21', 'Huelva'),   ('23', 'Jaen'),      ('29', 'Malaga'),
        ('41', 'Sevilla')
    ) as t(province_code, province_name)
)

select
    z.zone_id,
    z.zone_name,
    z.province_code,
    coalesce(p.province_name, 'Desconocida') as province_name,
    z.area_km2,
    z.basin_overlap_fraction,
    z.geometry_wkt
from zonas z
left join provincias p using (province_code)

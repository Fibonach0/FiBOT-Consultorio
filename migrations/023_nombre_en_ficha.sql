-- La ficha guarda de quién es. Sin esto, cuando alguien se quedaba sin sesiones
-- su ficha no tenía nombre y el próximo de ese teléfono heredaba sus datos.
alter table pacientes add column nombre text;

insert into pacientes (tenant_id, paciente_telefono, ficha, nombre)
select distinct on (tenant_id, paciente_telefono, ficha)
       tenant_id, paciente_telefono, ficha, paciente_nombre
from turnos
where estado = 'reservado' and paciente_nombre is not null
order by tenant_id, paciente_telefono, ficha, inicio desc
on conflict (tenant_id, paciente_telefono, ficha) do update set nombre = excluded.nombre;

-- Cerrar un tratamiento sin borrar la ficha. Sin fila en pacientes = activo.
alter table pacientes add column estado text not null default 'activo'
  check (estado in ('activo', 'pausa', 'alta'));
alter table pacientes add column estado_fecha date;
alter table pacientes add column estado_motivo text;

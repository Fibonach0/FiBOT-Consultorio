-- La ficha de cada paciente. Casi todo lo que muestra sale de sus sesiones en
-- `turnos`; acá va sólo lo que no se puede deducir de ahí. Se identifica por
-- teléfono, igual que en `turnos`, y sobrevive a que se cancelen sus sesiones.
--
-- Se guarda la fecha de nacimiento y no la edad, que quedaría vieja sola.
--
-- SIN datos clínicos: diagnóstico y notas siguen fuera de alcance (CLAUDE.md).
create table pacientes (
  tenant_id uuid not null references tenants (id) on delete cascade,
  paciente_telefono text not null,
  fecha_nacimiento date,
  created_at timestamptz not null default now(),
  primary key (tenant_id, paciente_telefono)
);

-- Igual que tenants y turnos: sólo la toca el backend con la service_role key.
alter table pacientes enable row level security;

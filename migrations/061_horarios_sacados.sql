-- Horarios libres que el profesional sacó con «Sacar». El scheduler los vuelve
-- a generar desde el horario de atención cada día; esta lista es lo que
-- impide que reaparezcan. Generarlos a mano los vuelve a habilitar.
create table horarios_sacados (
  tenant_id uuid not null references tenants(id) on delete cascade,
  inicio timestamptz not null,
  primary key (tenant_id, inicio)
);
alter table horarios_sacados enable row level security;

-- Los generados con «Generar horarios a mano» (fechas sueltas fuera del
-- horario): al guardar un horario de atención nuevo no se limpian.
alter table turnos add column a_mano boolean not null default false;

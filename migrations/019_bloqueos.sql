-- Vacaciones, congresos: rango en el que no se ofrecen ni se toman turnos.
-- `hasta` es exclusivo: la medianoche local del día siguiente al último.
create table bloqueos (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants (id) on delete cascade,
  desde timestamptz not null,
  hasta timestamptz not null,
  motivo text,
  check (hasta > desde)
);

create index bloqueos_idx on bloqueos (tenant_id, hasta);

-- Igual que el resto: sólo la toca el backend con la service_role key.
alter table bloqueos enable row level security;

create or replace view turnos_disponibles with (security_invoker = true) as
select t.*
from turnos t
where t.estado = 'libre'
  and not exists (
    select 1
    from turnos r
    where r.tenant_id = t.tenant_id
      and r.estado = 'reservado'
      and r.inicio < t.fin
      and r.fin > t.inicio
  )
  and not exists (
    select 1
    from bloqueos b
    where b.tenant_id = t.tenant_id
      and b.desde < t.fin
      and b.hasta > t.inicio
  );

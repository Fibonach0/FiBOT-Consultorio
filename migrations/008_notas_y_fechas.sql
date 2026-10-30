-- Lo que el profesional anota de cada paciente en su ficha: notas libres, la
-- gente y las mascotas de su vida, y las fechas que le importan.
--
-- OJO: esto ya es información sensible y bajo secreto profesional. Ver en
-- CLAUDE.md qué falta resolver antes de cargar pacientes reales.

alter table pacientes add column notas text;

-- Personas cercanas y mascotas: `vinculo` es texto libre ("madre", "perro").
create table paciente_personas (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants (id) on delete cascade,
  paciente_telefono text not null,
  nombre text not null,
  vinculo text,
  created_at timestamptz not null default now()
);

-- Se repiten todos los años, por eso día y mes van aparte: Navidad no tiene
-- año, un fallecimiento puede tenerlo o no.
create table paciente_fechas (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants (id) on delete cascade,
  paciente_telefono text not null,
  titulo text not null,
  dia smallint not null check (dia between 1 and 31),
  mes smallint not null check (mes between 1 and 12),
  anio smallint,
  -- NULL = no se sabe cómo le pega.
  animo text check (animo in ('bien', 'mal')),
  created_at timestamptz not null default now()
);

create index paciente_personas_idx on paciente_personas (tenant_id, paciente_telefono);
create index paciente_fechas_idx on paciente_fechas (tenant_id, paciente_telefono);

-- Igual que el resto: sólo las toca el backend con la service_role key.
alter table paciente_personas enable row level security;
alter table paciente_fechas enable row level security;

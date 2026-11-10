-- Bitácora de la ficha: lo que el profesional anota día a día, con fecha.
-- Varias entradas por paciente, nunca se pisan. Es tan sensible como las
-- notas de sesión: no sale por WhatsApp, constancias ni exportaciones.
create table paciente_bitacora (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants (id) on delete cascade,
  paciente_telefono text not null,
  ficha text not null default '',
  fecha date not null,
  texto text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz
);

create index paciente_bitacora_idx on paciente_bitacora (tenant_id, paciente_telefono, ficha, fecha desc);

-- Igual que el resto: sólo la toca el backend con la service_role key.
alter table paciente_bitacora enable row level security;

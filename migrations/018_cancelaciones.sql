-- Al cancelar, el turno vuelve a 'libre' y pierde al paciente: esto es lo único
-- que recuerda que hubo una cancelación. Reprogramar no cuenta.
create table cancelaciones (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants (id) on delete cascade,
  paciente_telefono text not null,
  inicio timestamptz not null,
  origen text not null check (origen in ('paciente', 'profesional')),
  created_at timestamptz not null default now()
);

create index cancelaciones_idx on cancelaciones (tenant_id, created_at);

-- Igual que el resto: sólo la toca el backend con la service_role key.
alter table cancelaciones enable row level security;

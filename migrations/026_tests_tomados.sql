-- Tests que ya se tomaron: dejan de sugerirse por un año.
create table paciente_tests (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants (id) on delete cascade,
  paciente_telefono text not null,
  ficha text not null default '',
  test text not null,
  fecha date not null,
  created_at timestamptz not null default now()
);

create index paciente_tests_idx on paciente_tests (tenant_id, paciente_telefono, ficha);

-- Igual que el resto: sólo la toca el backend con la service_role key.
alter table paciente_tests enable row level security;

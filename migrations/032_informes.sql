-- Informes guardados: cada guardado es una versión nueva, nunca se pisa.
-- contenido = {"titulo", "datos": [[k, v]], "secciones": [{"titulo", "texto"}]}
create table informes (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants (id) on delete cascade,
  paciente_telefono text not null,
  ficha text not null default '',
  contenido jsonb not null,
  created_at timestamptz not null default now()
);

create index informes_idx on informes (tenant_id, paciente_telefono, ficha);

-- Igual que el resto: sólo la toca el backend con la service_role key.
alter table informes enable row level security;

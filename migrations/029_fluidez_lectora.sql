-- Fluidez lectora: textos propios de la profesional y lecturas tomadas en sesión.
create table textos_lectura (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants (id) on delete cascade,
  titulo text not null,
  texto text not null,
  created_at timestamptz not null default now()
);

create index textos_lectura_idx on textos_lectura (tenant_id);

create table lecturas (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants (id) on delete cascade,
  paciente_telefono text not null,
  ficha text not null default '',
  fecha date not null,
  texto_titulo text not null,
  palabras_texto int not null,
  palabras_leidas int not null,
  errores int not null,
  errores_palabras jsonb not null default '[]'::jsonb,
  segundos numeric(6, 1) not null,
  created_at timestamptz not null default now()
);

create index lecturas_idx on lecturas (tenant_id, paciente_telefono, ficha);

-- Igual que el resto: sólo las toca el backend con la service_role key.
alter table textos_lectura enable row level security;
alter table lecturas enable row level security;

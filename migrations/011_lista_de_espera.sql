-- Gente que quiere un horario y no lo consiguió. El ofrecimiento lo dispara el
-- profesional a mano; `ofrecido_en` evita mandarlo dos veces el mismo día.
create table lista_espera (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants (id) on delete cascade,
  nombre text not null,
  telefono text not null,
  preferencia text,
  ofrecido_en date,
  created_at timestamptz not null default now()
);

create index lista_espera_idx on lista_espera (tenant_id, created_at);

-- Igual que el resto: sólo la toca el backend con la service_role key.
alter table lista_espera enable row level security;

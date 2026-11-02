-- Entrega de la ficha completa por link de un solo uso. Se guarda el HTML
-- tal como lo revisó la profesional; abrirlo marca abierta_at y no vuelve a abrir.
create table entregas (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants (id) on delete cascade,
  paciente_telefono text not null,
  ficha text not null default '',
  token text not null unique,
  vence timestamptz not null,
  abierta_at timestamptz,
  html text not null,
  created_at timestamptz not null default now()
);

create index entregas_idx on entregas (tenant_id, paciente_telefono, ficha);
alter table entregas enable row level security;

-- Moderación de la biblioteca compartida.
alter table tenants add column moderador boolean not null default false;
alter table biblioteca_cuestionarios add column oculto boolean not null default false;
alter table biblioteca_cuestionarios add column oculto_por text;

-- Plantillas de informe: títulos de secciones que la profesional reutiliza.
-- contenido = {"nombre", "secciones": [str]}
create table plantillas_informe (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants (id) on delete cascade,
  contenido jsonb not null,
  created_at timestamptz not null default now()
);

create index plantillas_informe_idx on plantillas_informe (tenant_id);
alter table plantillas_informe enable row level security;

-- Firma y sello como data URL (PNG o JPG, hasta 300 KB, validado por bytes).
alter table tenants add column firma_imagen text;

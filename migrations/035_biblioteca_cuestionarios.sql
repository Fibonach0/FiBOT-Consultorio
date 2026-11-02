-- Biblioteca compartida de cuestionarios propios: la profesional decide
-- compartir; por defecto viaja sin el texto de las preguntas (sólo cómo se
-- corrige). Es la única tabla que se lee entre profesionales.
create table biblioteca_cuestionarios (
  id uuid primary key default gen_random_uuid(),
  autor_tenant_id uuid not null references tenants (id) on delete cascade,
  autor text not null,
  contenido jsonb not null,
  con_preguntas boolean not null default false,
  created_at timestamptz not null default now()
);

alter table biblioteca_cuestionarios enable row level security;

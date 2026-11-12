-- Sitio propio de cada profesional (consultorio.fibot.ar/<slug>).
-- El profesional elige su link corto; el anterior sigue llevando a su página
-- (redirige), así no se rompen los links que ya compartió.
create table if not exists slugs_anteriores (
  slug text primary key,
  tenant_id uuid not null references tenants(id) on delete cascade,
  creado_at timestamptz not null default now()
);
alter table slugs_anteriores enable row level security;
create index if not exists slugs_anteriores_tenant on slugs_anteriores (tenant_id);
-- Preguntas frecuentes propias de su página: [{"p": pregunta, "r": respuesta}].
alter table tenants add column if not exists preguntas jsonb;
-- `en_directorio` pasa a querer decir «que Google pueda mostrar mi página»
-- (la portada ya no lista profesionales). Mismo default: sí.
comment on column tenants.en_directorio is 'false = la página del profesional va noindex y fuera del sitemap';

-- Profesionales que pidieron sumarse desde /sumate. Es de la plataforma (como
-- `admins`), no de un tenant: sólo la ve administración.
create table interesados (
  id uuid primary key default gen_random_uuid(),
  creado_at timestamptz not null default now(),
  nombre text not null,
  profesion text,
  whatsapp text,
  email text,
  zona text,
  mensaje text,
  visto boolean not null default false
);
alter table interesados enable row level security;
create index interesados_creado on interesados (creado_at desc);

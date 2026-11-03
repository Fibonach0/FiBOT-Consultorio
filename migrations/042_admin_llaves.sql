-- Llaves de acceso de la administración (mismo esquema que llaves_acceso).
create table admin_llaves (
  id text primary key,
  admin_id uuid not null references admins (id) on delete cascade,
  clave_publica text not null,
  contador bigint not null default 0,
  nombre text,
  creada timestamptz not null default now(),
  ultima timestamptz
);
alter table admin_llaves enable row level security;

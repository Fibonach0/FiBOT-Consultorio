-- Mail de la profesional, para recuperar la clave.
alter table tenants add column email text;

-- Llaves de acceso (passkeys / WebAuthn): sólo la clave pública.
create table llaves_acceso (
  id text primary key,                 -- credential id en base64url
  tenant_id uuid not null references tenants (id) on delete cascade,
  clave_publica text not null,         -- COSE en base64url
  contador bigint not null default 0,
  nombre text,
  creada timestamptz not null default now(),
  ultima timestamptz
);
create index llaves_acceso_tenant_idx on llaves_acceso (tenant_id);
alter table llaves_acceso enable row level security;

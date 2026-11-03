-- Sesiones abiertas de cada profesional: la cookie lleva sólo el id; la fila
-- dice que sigue viva. Borrarla cierra esa sesión.
create table sesiones (
  id text primary key,
  tenant_id uuid not null references tenants (id) on delete cascade,
  dispositivo text,
  origen text not null default 'clave',
  creada timestamptz not null default now(),
  ultima timestamptz not null default now()
);
create index sesiones_tenant_idx on sesiones (tenant_id);
alter table sesiones enable row level security;

-- Registro de actividad de seguridad (sin datos de pacientes).
create table actividad (
  id bigint generated always as identity primary key,
  cuando timestamptz not null default now(),
  tenant_id uuid references tenants (id) on delete set null,
  actor text not null,
  accion text not null
);
create index actividad_cuando_idx on actividad (cuando desc);
alter table actividad enable row level security;

-- Link de un solo uso para poner clave nueva (se guarda sólo el hash del token).
create table enlaces_clave (
  token_hash text primary key,
  tenant_id uuid not null references tenants (id) on delete cascade,
  vence timestamptz not null,
  usado_at timestamptz,
  creado timestamptz not null default now()
);
alter table enlaces_clave enable row level security;

-- Entrar leyendo un QR con el celular ya logueado.
create table qr_logins (
  token_hash text primary key,
  nonce_hash text not null,
  dispositivo text,
  vence timestamptz not null,
  tenant_id uuid references tenants (id) on delete cascade,
  aprobado_at timestamptz,
  usado_at timestamptz
);
alter table qr_logins enable row level security;

-- Doble factor opcional para profesionales.
alter table tenants add column totp_secreto text;
alter table tenants add column totp_ultimo_paso bigint;

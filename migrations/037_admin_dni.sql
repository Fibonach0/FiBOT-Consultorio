-- Cuenta de administración de la plataforma (no es un profesional: no ve
-- datos de pacientes, sólo cuentas, uso y la biblioteca).
create table admins (
  id uuid primary key default gen_random_uuid(),
  usuario text not null unique,
  pass_hash text not null,
  ultimo_ingreso timestamptz,
  created_at timestamptz not null default now()
);
alter table admins enable row level security;

alter table tenants add column ultimo_ingreso timestamptz;

-- Segunda llave opcional de la entrega: DNI del destinatario (sólo el hash,
-- con el token como sal) y conteo de intentos fallidos.
alter table entregas add column dni_hash text;
alter table entregas add column intentos int not null default 0;

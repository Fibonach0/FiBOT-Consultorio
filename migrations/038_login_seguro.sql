-- Límite de intentos de login: una fila por usuario y otra por IP
-- (clave = 'panel:<usuario>', 'admin:<usuario>', 'ip:<ip>', 'totp:<admin id>').
create table login_intentos (
  clave text primary key,
  fallos int not null default 0,
  bloqueado_hasta timestamptz,
  ultimo timestamptz
);
alter table login_intentos enable row level security;

-- Doble factor obligatorio para administración.
alter table admins add column totp_secreto text;
alter table admins add column totp_ultimo_paso bigint;

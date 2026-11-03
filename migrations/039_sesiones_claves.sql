-- Versión de sesión: subirla cierra todas las sesiones abiertas de la cuenta
-- (cambio de clave, o «Cerrar sesiones» desde /admin).
alter table tenants add column sesion_version int not null default 0;
alter table admins add column sesion_version int not null default 0;
-- Cuándo se bloqueó la cuenta por claves equivocadas (aviso en panel y resumen).
alter table tenants add column ultimo_bloqueo timestamptz;

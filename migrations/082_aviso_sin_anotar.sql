-- Aviso de las 21 por WhatsApp si quedaron sesiones de hoy sin anotar (sólo
-- la cantidad). Se apaga desde ajustes; la marca evita mandarlo dos veces.
alter table tenants add column if not exists aviso_sin_anotar boolean;
alter table tenants add column if not exists ultimo_aviso_sin_anotar text;

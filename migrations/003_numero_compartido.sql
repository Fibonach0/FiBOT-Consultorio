-- Modo "sin número propio": el profesional arranca usando el número de
-- WhatsApp de la plataforma en vez de dar de alta el suyo en el Business
-- Portfolio (un trámite de días con verificación de Meta).
--
-- Dos cambios, y el segundo es el que importa:
--   1. whatsapp_phone_number_id pasa a ser nullable: NULL = usa el de la
--      plataforma (config.PLATAFORMA_PHONE_NUMBER_ID).
--   2. El UNIQUE deja de aplicar a los NULL. En Postgres un UNIQUE ya ignora
--      los NULL, así que varios tenants pueden compartir el número de la
--      plataforma sin chocar, y los que SÍ tienen número propio siguen sin
--      poder repetirlo entre ellos — que es exactamente lo que hace falta.

alter table tenants alter column whatsapp_phone_number_id drop not null;

-- Los mensajes que entran al número compartido no se pueden resolver por
-- número (lo comparten todos): se resuelven por el teléfono del paciente
-- contra sus turnos. Este índice es el que hace barata esa búsqueda.
create index if not exists turnos_paciente_telefono_idx
  on turnos (paciente_telefono, inicio desc)
  where paciente_telefono is not null;

-- Perfil público del profesional (/turnos y su calendario) y confirmación por
-- WhatsApp de las reservas hechas desde la web.
alter table tenants
  add column en_directorio boolean not null default true,      -- aparece en /turnos
  add column foto text,                                         -- data URL PNG/JPG (como firma_imagen)
  add column descripcion text,                                  -- una línea bajo el nombre
  add column modalidades_publicas text not null default 'presencial'
    check (modalidades_publicas in ('presencial', 'virtual', 'ambas'));

-- Describen a la RESERVA: van en db._LIBERADO y no viajan al reprogramar
-- (otro horario = hay que confirmar de nuevo).
alter table turnos
  add column confirmacion_enviada_at timestamptz,
  add column confirmado_at timestamptz,
  add column token_calendario text;
create unique index turnos_token_calendario on turnos (token_calendario) where token_calendario is not null;

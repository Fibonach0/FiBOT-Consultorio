-- Pausa con fecha de vuelta: ese día la página se reactiva sola.
alter table tenants add column pausado_hasta date;

-- Horario de atención semanal: [{"dia": 0-6 (lunes=0), "desde": "09:00", "hasta": "13:00"}, ...].
-- El scheduler genera solo los horarios libres de las próximas `horario_semanas`.
alter table tenants
  add column horario_semanal jsonb,
  add column horario_semanas integer not null default 4 check (horario_semanas between 1 and 12);

-- Tipos de turno con su duración: [{"nombre": "Primera entrevista", "minutos": 60}, ...].
-- Vacío = un solo tipo, la duración de siempre.
alter table tenants add column tipos_turno jsonb;
-- Qué tipo eligió el paciente (describe a la reserva: va en db._LIBERADO).
alter table turnos add column tipo text;

-- Seguimiento de cada sesión ya reservada: si el paciente vino, si pagó, y
-- si la sesión se pasó de otro horario. Es lo que pide un profesional con
-- pacientes fijos (psicología, sobre todo) apenas tiene la agenda andando:
-- "algo donde organizar si me pagó o no, si vino o no, si la sesión se pasó
-- y a cuándo".
--
-- Las tres columnas describen a la RESERVA, no al horario: cuando un turno
-- vuelve a 'libre' (cancelar, o porque se reprogramó a otro horario) se
-- limpian junto con paciente_* — ver db._LIBERADO. Si no, el próximo que
-- reserve ese horario heredaría el "pagó" del anterior.

-- NULL = todavía sin marcar (lo normal para una sesión que no pasó).
alter table turnos add column asistencia text
  check (asistencia in ('vino', 'no_vino'));

-- 'no_cobra' existe para el que no vino y no se le cobra la sesión: sin eso
-- quedaría para siempre en "pendiente de pago".
alter table turnos add column pago text not null default 'pendiente'
  check (pago in ('pendiente', 'pagado', 'no_cobra'));

-- Cuándo era la sesión antes de pasarla. Se guarda el horario y no un id al
-- turno viejo a propósito: ese horario queda libre y puede tomarlo otro
-- paciente, así que un id apuntaría a la reserva de otra persona.
alter table turnos add column reprogramado_desde timestamptz;

-- "Pendientes de cobro" del panel: reservados, pasados, sin pagar.
create index if not exists turnos_pago_idx on turnos (tenant_id, estado, pago, inicio);

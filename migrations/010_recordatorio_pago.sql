-- Último recordatorio de pago mandado a este paciente: lo pide el profesional a
-- mano, y esto evita que salga dos veces el mismo día por un doble clic.
alter table pacientes add column ultimo_recordatorio_pago date;

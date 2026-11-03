-- Segundo aviso a quien reservó en la web y no confirmó por WhatsApp. Describe
-- a la reserva: va en db._LIBERADO.
alter table turnos add column confirmacion_recordada_at timestamptz;

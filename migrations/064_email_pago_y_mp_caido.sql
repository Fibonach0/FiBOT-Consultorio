-- Email opcional del paciente en las reservas que se pagan (Mercado Pago lo
-- usa para aprobar y se le manda el recibo). Sólo vive en el pago.
alter table pagos add column if not exists paciente_email text;
-- Cuándo se cortó la conexión con Mercado Pago (permiso revocado o vencido).
-- Mientras esté cargado no se cobra por Mercado Pago y el panel pide reconectar.
alter table tenants add column if not exists mp_caido_at timestamptz;

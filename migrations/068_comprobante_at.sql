-- Cuándo subió el paciente el comprobante de transferencia: /admin avisa
-- de los que llevan más de 24 h sin revisar.
alter table pagos add column if not exists comprobante_at timestamptz;

-- Numeración correlativa de recibos por profesional (0001, 0002…), asignada
-- al aprobarse el pago, y la opción de no recibir el resumen mensual por mail.
alter table pagos add column if not exists recibo_numero integer;
create unique index if not exists pagos_recibo_numero_unico on pagos (tenant_id, recibo_numero) where recibo_numero is not null;
alter table tenants add column if not exists resumen_cobros_mail boolean not null default true;

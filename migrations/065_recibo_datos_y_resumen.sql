-- Datos opcionales del profesional para el recibo y marca del resumen mensual
-- de cobros por mail (YYYY-MM del último mes mandado: se manda una sola vez).
alter table tenants add column if not exists recibo_cuit text;
alter table tenants add column if not exists recibo_domicilio text;
alter table tenants add column if not exists resumen_cobros_mes text;

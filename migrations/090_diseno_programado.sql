-- Programar un diseño guardado por fechas («Navidad del 20/12 al 7/1»).
-- {id, nombre, desde, hasta, estado: esperando | puesto, volver_id}: chico a
-- propósito (tenants se lee en cada pedido); el CSS queda en disenos_publicados.
alter table tenants add column if not exists diseno_programado jsonb;

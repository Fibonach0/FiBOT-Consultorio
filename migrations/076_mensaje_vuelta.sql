-- Mensaje propio que le llega a la lista de espera 2 días antes de que termine
-- una pausa con fecha (vacío = el de fábrica, con la fecha).
alter table tenants add column if not exists mensaje_vuelta text;

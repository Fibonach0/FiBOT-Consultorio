-- Resumen diario por WhatsApp al PROFESIONAL (nunca al paciente).
-- aviso_telefono NULL = no quiere avisos. ultimo_aviso evita mandarlo dos veces
-- el mismo día: el scheduler corre cada pocos minutos.
alter table tenants add column aviso_telefono text;
alter table tenants add column ultimo_aviso date;

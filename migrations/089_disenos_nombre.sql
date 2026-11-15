-- Diseños guardados con nombre («Navidad», «el de siempre») y fijados: los
-- fijados no cuentan para el límite de app.HISTORIAL_DISENOS ni se borran solos.
alter table disenos_publicados add column if not exists nombre text;
alter table disenos_publicados add column if not exists fijado boolean not null default false;

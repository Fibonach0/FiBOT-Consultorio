-- Diseño propio de /<slug>: estilo (colores, letras, bordes, módulos) y CSS
-- revisado por diseno.limpiar_css. Lo publicado y el borrador que la
-- profesional mira antes de publicar. Nunca HTML ni JavaScript.
alter table tenants add column if not exists diseno jsonb;
alter table tenants add column if not exists diseno_borrador jsonb;
alter table tenants add column if not exists css_propio text;
alter table tenants add column if not exists css_borrador text;

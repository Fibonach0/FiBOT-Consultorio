-- Slug público por tenant, para la página de reserva sin login:
-- consultorios.fibot.ar/<slug>. Nullable a propósito: un tenant sin slug
-- simplemente no tiene página pública todavía (sigue funcionando por
-- WhatsApp y panel como antes).

alter table tenants add column slug text unique;

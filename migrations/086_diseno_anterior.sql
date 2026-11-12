-- El diseño que estaba publicado antes del último cambio (publicar o volver al
-- original), para «Volver al diseño anterior». Mismo formato que diseno /
-- css_propio; los dos en null = no hay anterior para ofrecer.
alter table tenants add column if not exists diseno_anterior jsonb;
alter table tenants add column if not exists css_anterior text;

-- diseno_anterior / css_anterior quedaron reemplazadas por disenos_publicados
-- (migración 087). Se aplica después del deploy que deja de usarlas.
alter table tenants drop column if exists diseno_anterior;
alter table tenants drop column if exists css_anterior;

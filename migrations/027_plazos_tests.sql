-- Meses para volver a sugerir cada test, si el profesional cambia el de fábrica.
-- {"WISC-V": 24}. Vacío = usa los de tests_sugeridos.CATALOGO.
alter table tenants add column plazos_tests jsonb not null default '{}'::jsonb;

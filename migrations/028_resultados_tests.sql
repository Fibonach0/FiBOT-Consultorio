-- Puntajes ya corregidos de cada test tomado (los carga la profesional).
-- [{"escala": "CI Total", "tipo": "compuesto", "puntaje": 98, "nivel": "Promedio"}]
alter table paciente_tests add column resultados jsonb not null default '[]'::jsonb;

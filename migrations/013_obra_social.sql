-- Datos para que el paciente pida el reintegro: salen en la constancia.
alter table pacientes add column obra_social text;
alter table pacientes add column numero_afiliado text;

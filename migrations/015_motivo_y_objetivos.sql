-- Encuadre del tratamiento: arriba de todo en la ficha.
alter table pacientes add column motivo_consulta text;
alter table pacientes add column objetivos text;
alter table pacientes add column como_viene text;

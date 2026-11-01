-- Datos de la escuela: los usa la ficha de psicopedagogía.
alter table pacientes add column escuela text;
alter table pacientes add column grado text;
alter table pacientes add column docente text;
alter table pacientes add column equipo_orientador text;

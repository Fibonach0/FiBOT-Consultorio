-- A quién llamar y qué hay que saber si el paciente entra en crisis.
alter table pacientes add column psiquiatra text;
alter table pacientes add column medicacion text;
alter table pacientes add column plan_crisis text;

alter table paciente_personas add column telefono text;
alter table paciente_personas add column emergencia boolean not null default false;

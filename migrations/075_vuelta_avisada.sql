-- Aviso a la lista de espera dos días antes de que termine una pausa: guarda
-- la fecha de vuelta ya avisada (si la cambia, se avisa la nueva).
alter table tenants add column if not exists vuelta_avisada text;

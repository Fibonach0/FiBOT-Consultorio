-- Hasta cuántas horas antes el paciente puede cancelar o cambiar el turno
-- desde el link de agendar. Después, «escribile directamente».
alter table tenants add column cancelar_hasta_horas integer not null default 24
  check (cancelar_hasta_horas between 0 and 168);

-- De dónde vino cada persona de la lista de espera.
alter table lista_espera add column origen text not null default 'panel'
  check (origen in ('panel', 'web'));

-- Cuándo llamó Meta al webhook por última vez y cuándo llegó el último mensaje
-- de un paciente. Una fila por clave ('webhook', 'mensaje'): sólo la hora y el
-- phone_number_id, nunca el teléfono ni el texto del paciente.
create table eventos_plataforma (
  clave text primary key,
  cuando timestamptz not null default now(),
  detalle text
);
alter table eventos_plataforma enable row level security;

-- Mensajes al número compartido de alguien sin turno: no se pueden rutear a
-- ningún profesional. Sólo los últimos 4 dígitos, un hash para no contestarle
-- dos veces el mismo día, y la hora. Nunca el número entero ni el texto.
create table mensajes_sin_destino (
  id uuid primary key default gen_random_uuid(),
  final text not null,
  telefono_hash text not null,
  respondido boolean not null default false,
  cuando timestamptz not null default now()
);
create index mensajes_sin_destino_hash on mensajes_sin_destino (telefono_hash, cuando desc);
alter table mensajes_sin_destino enable row level security;

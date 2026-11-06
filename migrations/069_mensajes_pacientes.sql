-- Bandeja del profesional: lo que un paciente le escribe al bot y el bot no
-- sabe resolver («llego 10 minutos tarde»), más los audios y archivos que no
-- puede leer (sólo la marca, nunca el archivo). Desde el panel se marca leído
-- o se contesta (dentro de las 24 h de WhatsApp).
create table mensajes_pacientes (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants (id) on delete cascade,
  telefono text not null,
  tipo text not null default 'texto' check (tipo in ('texto', 'audio', 'imagen', 'documento', 'video', 'otro')),
  texto text,
  creado timestamptz not null default now(),
  leido boolean not null default false,
  respuesta text,
  respondido timestamptz
);
create index mensajes_pacientes_tenant on mensajes_pacientes (tenant_id, leido, creado desc);
alter table mensajes_pacientes enable row level security;

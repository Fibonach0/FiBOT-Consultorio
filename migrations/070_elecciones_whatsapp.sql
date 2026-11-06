-- Número compartido y un paciente con turnos con dos profesionales: el bot le
-- pregunta «¿con quién es?» y recuerda la respuesta un rato. `pendiente` es el
-- mensaje que mandó antes de elegir, para contestarlo después de que elige.
create table elecciones_whatsapp (
  telefono text primary key,
  tenant_id uuid references tenants (id) on delete cascade,
  pendiente text,
  vence timestamptz not null
);
alter table elecciones_whatsapp enable row level security;

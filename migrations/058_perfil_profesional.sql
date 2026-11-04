-- Perfil público del profesional: portada, título, dónde atiende, «sobre mí»,
-- enfoque, formación, algo más personal y obras sociales.
alter table tenants
  add column titulo text,                                  -- «Lic. en Psicología (UBA)»
  add column ubicacion text,                               -- «Palermo, CABA»
  add column sobre_mi text,
  add column enfoques jsonb not null default '[]'::jsonb,  -- lista de textos cortos
  add column formacion jsonb not null default '[]'::jsonb, -- lista de renglones
  add column mas_de_mi text,                               -- lo humano: hobbies, mascotas…
  add column obras_sociales text,
  add column portada_version int not null default 0;       -- para que el navegador no use la vieja

-- La portada va aparte: pesa más que la foto y `tenants` se lee en cada pedido.
create table portadas (
  tenant_id uuid primary key references tenants(id) on delete cascade,
  tipo text not null check (tipo in ('image/png', 'image/jpeg')),
  datos text not null,                                     -- base64
  actualizada timestamptz not null default now()
);
alter table portadas enable row level security;

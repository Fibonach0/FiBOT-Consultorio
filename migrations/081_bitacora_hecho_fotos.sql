-- «Para la próxima» se tilda como hecho (desde Hoy o la página rápida).
alter table paciente_bitacora add column if not exists proxima_hecha_at timestamptz;

-- Fotos de una anotación (una lámina, un dibujo, un dictado). Cuelgan de la
-- anotación: al borrarla se borran, y al pasar la ficha a otro número viajan
-- solas (no llevan teléfono). Igual de sensibles que la bitácora: sólo se
-- sirven con sesión del profesional y sin caché.
create table if not exists bitacora_fotos (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants (id) on delete cascade,
  entrada_id uuid not null references paciente_bitacora (id) on delete cascade,
  imagen text not null,
  created_at timestamptz not null default now()
);

create index if not exists bitacora_fotos_idx on bitacora_fotos (tenant_id, entrada_id);

alter table bitacora_fotos enable row level security;

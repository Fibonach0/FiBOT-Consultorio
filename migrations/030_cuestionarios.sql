-- Cuestionarios por link (PHQ-9, GAD-7): el paciente los completa sin login
-- con un token de un solo uso y el sistema los corrige.
create table cuestionarios (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants (id) on delete cascade,
  paciente_telefono text not null,
  ficha text not null default '',
  instrumento text not null,
  token text not null unique,
  vence timestamptz not null,
  respondido_at timestamptz,
  respuestas jsonb,
  puntaje int,
  severidad text,
  alerta boolean not null default false,
  dificultad text,
  visto boolean not null default false,
  created_at timestamptz not null default now()
);

create index cuestionarios_idx on cuestionarios (tenant_id, paciente_telefono, ficha);

-- Igual que el resto: sólo la toca el backend con la service_role key.
alter table cuestionarios enable row level security;

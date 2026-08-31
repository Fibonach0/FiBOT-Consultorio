-- FiBOT Consultorio — esquema inicial. Multi-tenant: TODOS los profesionales
-- viven en este mismo proyecto de Supabase, distinguidos por tenant_id.
-- Aplicar en orden numérico, nunca editar una migración ya aplicada.

create extension if not exists pgcrypto;

create table tenants (
  id uuid primary key default gen_random_uuid(),
  nombre text not null,                          -- cómo se presenta el bot: "Lic. Fulana de Tal"
  rubro text,                                     -- informativo: psicología, odontología, nutrición...
  whatsapp_phone_number_id text unique not null,  -- id de Meta del número de ESTE profesional
  whatsapp_display_phone text,                    -- el número que ve el paciente (solo informativo)
  timezone text not null default 'America/Argentina/Buenos_Aires',
  duracion_turno_minutos int not null default 50,
  recordatorio_horas_antes int not null default 24,
  panel_user text unique not null,
  panel_pass_hash text not null,                  -- hash de bcrypt, nunca la clave en texto plano
  activo boolean not null default true,
  created_at timestamptz not null default now()
);

create table turnos (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete cascade,
  inicio timestamptz not null,
  fin timestamptz not null,
  estado text not null default 'libre' check (estado in ('libre', 'reservado')),
  paciente_telefono text,
  paciente_nombre text,
  recordatorio_enviado boolean not null default false,
  created_at timestamptz not null default now()
);

-- Un mismo tenant no puede tener dos turnos que arrancan a la misma hora
-- (esto es lo que hace que crear_turnos_libres() se pueda correr dos veces
-- sobre el mismo rango sin duplicar filas — usa upsert sobre este índice).
create unique index turnos_tenant_inicio_idx on turnos (tenant_id, inicio);

create index turnos_recordatorio_idx on turnos (tenant_id, estado, recordatorio_enviado, inicio);
create index turnos_paciente_idx on turnos (tenant_id, paciente_telefono, estado, inicio);

-- RLS: esta tabla solo la toca el backend con la service_role key (nunca el
-- browser directo), pero se deja prendida por si en el futuro se agrega
-- acceso desde el cliente — hoy no hay policies, así que anon queda afuera
-- por default.
alter table tenants enable row level security;
alter table turnos enable row level security;

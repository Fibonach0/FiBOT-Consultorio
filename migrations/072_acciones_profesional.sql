-- Lo que el profesional le pidió a su asistente por WhatsApp y espera su
-- «Sí» (aceptar un comprobante, bloquear un día, pausar, avisarle a un
-- paciente). Una por WhatsApp de avisos (últimos 10 dígitos); vence a los
-- 10 minutos (asistente_profesional).
create table if not exists acciones_profesional (
  telefono text primary key,
  tenant_id uuid not null references tenants (id) on delete cascade,
  accion jsonb not null,
  creado timestamptz not null default now()
);
alter table acciones_profesional enable row level security;

-- Resumen de la noche: las sesiones de mañana, con nombres (sólo con el
-- WhatsApp vinculado). Se puede apagar desde avisos.
alter table tenants add column if not exists resumen_noche boolean not null default true;
alter table tenants add column if not exists ultimo_resumen_noche text;

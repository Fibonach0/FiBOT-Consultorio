-- Cobro de las reservas hechas en la web: Mercado Pago (cada profesional
-- conecta su cuenta por OAuth) y/o transferencia con comprobante que valida el
-- profesional. La reserva queda apartada hasta que se paga.
alter table tenants
  add column cobro_web text not null default 'no' check (cobro_web in ('no', 'mercadopago', 'transferencia', 'ambos')),
  add column cobro_modo text not null default 'total' check (cobro_modo in ('total', 'sena')),
  add column sena_monto integer check (sena_monto > 0),
  add column precio_sesion integer check (precio_sesion > 0),     -- si no hay tipos con precio
  add column transferencia_datos text,                             -- alias / CBU / titular
  add column mp_user_id text,
  add column mp_token_cifrado text,                                -- access_token cifrado (Fernet)
  add column mp_refresh_cifrado text,
  add column mp_vence timestamptz;

-- Estado del pago web de la reserva. Describe a la reserva: va en db._LIBERADO.
alter table turnos
  add column pago_web text check (pago_web in ('esperando', 'comprobante', 'aprobado')),
  add column pago_vence timestamptz;

-- Un pago por reserva web (una o varias sesiones). El token es la credencial
-- de la página del paciente (/pago/<token>). Queda aunque el turno se libere:
-- si el pago llega tarde, el profesional se entera para devolverlo.
create table pagos (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants (id) on delete cascade,
  token text not null unique,
  turnos uuid[] not null,
  monto integer not null check (monto > 0),
  metodo text check (metodo in ('mercadopago', 'transferencia')),
  estado text not null default 'esperando'
    check (estado in ('esperando', 'comprobante', 'aprobado', 'rechazado', 'vencido', 'aprobado_tarde')),
  mp_pago_id text,
  comprobante text,            -- data URL (PNG/JPG/PDF), se borra al resolver
  vence timestamptz not null,
  creado timestamptz not null default now(),
  resuelto timestamptz
);
create index pagos_tenant on pagos (tenant_id, estado);
alter table pagos enable row level security;

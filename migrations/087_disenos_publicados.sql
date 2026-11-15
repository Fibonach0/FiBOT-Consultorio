-- Historial de diseños publicados de /<slug>: los últimos app.HISTORIAL_DISENOS
-- que estuvieron publicados antes del actual, para «Volver a este». Aparte de
-- tenants (que se lee en cada pedido): cada fila puede traer hasta 20 KB de CSS.
create table if not exists disenos_publicados (
    id uuid primary key default gen_random_uuid(),
    tenant_id uuid not null references tenants(id) on delete cascade,
    created_at timestamptz not null default now(),
    diseno jsonb,
    css text
);
create index if not exists disenos_publicados_tenant on disenos_publicados (tenant_id, created_at desc);
alter table disenos_publicados enable row level security;

-- Lo que había en «el anterior» (migración 086) pasa a ser la primera entrada.
insert into disenos_publicados (tenant_id, diseno, css)
select id, diseno_anterior, css_anterior from tenants
where diseno_anterior is not null or css_anterior is not null;

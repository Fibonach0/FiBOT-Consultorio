-- Qué pasa cuando un paciente cancela: 'liberar' (el horario vuelve a estar
-- disponible enseguida) o 'preguntar' (queda retenido hasta que el
-- profesional decide si lo libera o se lo toma).
alter table tenants
  add column al_cancelar text not null default 'liberar' check (al_cancelar in ('liberar', 'preguntar')),
  add column ofrecer_espera_auto boolean not null default false;

-- Horario libre que NO se ofrece: 'pendiente' (esperando que el profesional
-- decida) o 'propio' (se lo tomó). Null = normal.
alter table turnos add column hueco text check (hueco in ('pendiente', 'propio'));

create or replace view turnos_disponibles as
 SELECT id, tenant_id, inicio, fin, estado, paciente_telefono, paciente_nombre, recordatorio_enviado, created_at,
        asistencia, pago, reprogramado_desde, valor, frecuencia, modalidad, nota
   FROM turnos t
  WHERE estado = 'libre'::text AND t.hueco IS NULL
    AND NOT (EXISTS (SELECT 1 FROM turnos r WHERE r.tenant_id = t.tenant_id AND r.estado = 'reservado'::text
                     AND r.inicio < t.fin AND r.fin > t.inicio))
    AND NOT (EXISTS (SELECT 1 FROM bloqueos b WHERE b.tenant_id = t.tenant_id AND b.desde < t.fin AND b.hasta > t.inicio));

-- Estadísticas de la página de turnos: un evento por cosa que pasa. Sin
-- datos de pacientes.
create table eventos_pagina (
  id bigserial primary key,
  tenant_id uuid not null references tenants (id) on delete cascade,
  tipo text not null check (tipo in ('visita', 'reserva', 'cancelacion', 'cambio', 'espera')),
  cuando timestamptz not null default now()
);
create index eventos_pagina_idx on eventos_pagina (tenant_id, cuando);
alter table eventos_pagina enable row level security;

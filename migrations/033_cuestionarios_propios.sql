-- Cuestionarios propios: la profesional carga preguntas, opciones y corrección.
-- contenido = {"nombre", "que_mide", "consigna", "opciones": [{"texto", "valor"}],
--              "items": [str], "subescalas": [{"nombre", "items": [int], "corte"}],
--              "corte_total", "fuente"}
create table plantillas_cuestionario (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants (id) on delete cascade,
  contenido jsonb not null,
  created_at timestamptz not null default now()
);

create index plantillas_cuestionario_idx on plantillas_cuestionario (tenant_id);
alter table plantillas_cuestionario enable row level security;

-- Copia de la plantilla al mandar el link (editarla no cambia lo respondido)
-- y el puntaje de cada subescala.
alter table cuestionarios add column plantilla jsonb;
alter table cuestionarios add column detalle jsonb;

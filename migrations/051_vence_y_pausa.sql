-- Si el profesional no decide sobre un horario retenido en `hueco_vence_horas`
-- (0 = nunca), se aplica `hueco_por_defecto`.
alter table tenants
  add column hueco_vence_horas integer not null default 0 check (hueco_vence_horas between 0 and 168),
  add column hueco_por_defecto text not null default 'liberar' check (hueco_por_defecto in ('liberar', 'tomar')),
  -- Pausa de la página de turnos (vacaciones): no se ofrece ni se reserva nada,
  -- la lista de espera sigue abierta.
  add column pausado boolean not null default false,
  add column mensaje_pausa text;

-- Desde cuándo está retenido el horario (para el vencimiento).
alter table turnos add column hueco_desde timestamptz;

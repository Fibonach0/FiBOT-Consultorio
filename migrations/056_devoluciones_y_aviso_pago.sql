-- Devoluciones de los pagos web, política de devolución al cancelar, recibo
-- para el paciente y aviso antes de que venza el plazo para pagar.

-- Política: si el paciente cancela con al menos `devolucion_horas` de
-- anticipación se le devuelve todo, todo menos la seña, o nada.
alter table tenants
  add column devolucion_modo text not null default 'todo' check (devolucion_modo in ('todo', 'menos_sena', 'nada')),
  add column devolucion_horas integer not null default 24 check (devolucion_horas between 0 and 168);

-- El pago que cubre la reserva (describe a la reserva: va en db._LIBERADO y
-- viaja al reprogramar). Sirve para devolver la parte de una sesión cancelada.
alter table turnos add column pago_id uuid;
update turnos t set pago_id = p.id from pagos p where t.id = any (p.turnos) and t.estado = 'reservado';

alter table pagos
  add column devuelto integer not null default 0 check (devuelto >= 0),
  add column a_devolver integer not null default 0 check (a_devolver >= 0),  -- lo que el profesional tiene que devolver a mano
  add column devuelto_at timestamptz,
  add column avisado_at timestamptz,          -- aviso de «se vence el plazo para pagar»
  add column paciente_nombre text,            -- para el recibo, aunque el turno se libere
  add column sesiones jsonb;                  -- inicios de las sesiones pagadas (para el recibo)
update pagos p set sesiones = (select jsonb_agg(t.inicio order by t.inicio) from turnos t where t.id = any (p.turnos)),
                   paciente_nombre = (select t.paciente_nombre from turnos t where t.id = any (p.turnos) and t.paciente_nombre is not null limit 1);
alter table pagos drop constraint pagos_estado_check;
alter table pagos add constraint pagos_estado_check
  check (estado in ('esperando', 'comprobante', 'aprobado', 'rechazado', 'vencido', 'aprobado_tarde', 'devuelto'));
create index pagos_resuelto on pagos (tenant_id, resuelto);

-- Horarios que realmente se pueden ofrecer. Una sesión pasada a mano a una hora
-- cualquiera (15:30) puede pisarse con un horario libre que no arranca a la
-- misma hora (15:00): ese horario sigue en estado 'libre', pero ya no se puede
-- ofrecer a nadie. Se resuelve al leer y no borrando la fila: si esa sesión
-- después se mueve o se cancela, el horario vuelve a aparecer solo.
--
-- security_invoker: sin esto la vista correría con los permisos de su dueño y
-- saltearía el RLS de turnos.
create view turnos_disponibles with (security_invoker = true) as
select t.*
from turnos t
where t.estado = 'libre'
  and not exists (
    select 1
    from turnos r
    where r.tenant_id = t.tenant_id
      and r.estado = 'reservado'
      and r.inicio < t.fin
      and r.fin > t.inicio
  );

create index if not exists turnos_solape_idx on turnos (tenant_id, estado, inicio, fin);

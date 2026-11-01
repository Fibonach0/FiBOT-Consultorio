-- Qué se habló en cada sesión. Describe a la reserva: viaja al reprogramar y se
-- limpia al liberar (db._LIBERADO). Información sensible: ver CLAUDE.md.
alter table turnos add column nota text;

-- Postgres expande el t.* al crear la vista: sin esto no ve la columna nueva.
create or replace view turnos_disponibles with (security_invoker = true) as
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

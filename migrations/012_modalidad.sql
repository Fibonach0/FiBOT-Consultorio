-- Sesión presencial o por videollamada. NULL = no se indicó, se trata como
-- presencial. Describe a la reserva: se limpia al liberar (db._LIBERADO).
alter table turnos add column modalidad text check (modalidad in ('presencial', 'virtual'));

-- Link fijo de la sala del profesional: va en el recordatorio de las virtuales.
alter table tenants add column link_videollamada text;

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

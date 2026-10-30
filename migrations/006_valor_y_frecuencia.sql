-- Lo que pidió Marcos después de probar el panel: anotar cuánto vale cada
-- sesión y tener pacientes semanales o quincenales.
--
-- Igual que asistencia y pago, las dos columnas describen a la RESERVA: se
-- limpian cuando el turno vuelve a 'libre' (db._LIBERADO).

-- En pesos enteros. NULL = no cargado (las reservas de la página pública
-- llegan sin valor, y el profesional lo agrega después).
alter table turnos add column valor integer check (valor >= 0);

-- NULL = sesión suelta. No hay una "serie": cada sesión es su propia fila y se
-- mueve o cancela sola, esto sólo dice cómo se la agendó.
alter table turnos add column frecuencia text
  check (frecuencia in ('semanal', 'quincenal'));

-- Postgres expande el t.* al crear la vista: sin esto no ve las columnas nuevas.
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

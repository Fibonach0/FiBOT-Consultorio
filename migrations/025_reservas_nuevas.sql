-- Reservas que llegaron solas (página pública o bot). `reserva_nueva` queda en el
-- panel hasta que el profesional la marca vista; `reserva_avisada` evita repetir
-- el WhatsApp. Lo existente no cuenta como nuevo.
alter table turnos add column reserva_nueva boolean not null default false;
alter table turnos add column reserva_avisada boolean not null default false;

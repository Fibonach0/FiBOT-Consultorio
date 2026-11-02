-- Reserva que no se pudo asignar con certeza a una ficha (mismo nombre y mismo
-- teléfono en más de una). Queda en el panel hasta que el profesional la revise.
alter table turnos add column revisar_ficha boolean not null default false;

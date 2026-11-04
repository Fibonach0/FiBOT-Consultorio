-- Una reserva cuyo pago se devolvió entero sigue reservada (el profesional
-- decide si la mantiene), pero ya no figura como pagada.
alter table turnos drop constraint if exists turnos_pago_web_check;
alter table turnos add constraint turnos_pago_web_check
  check (pago_web in ('esperando', 'comprobante', 'aprobado', 'devuelto'));

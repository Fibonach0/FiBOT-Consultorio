-- Descanso entre turnos: con sesiones de 45 y 15 de descanso, los horarios
-- arrancan cada 60 (9:15, 10:15…), cada uno termina en punto.
alter table tenants add column descanso_minutos integer not null default 0 check (descanso_minutos between 0 and 60);

-- Modalidad del HORARIO (no de la reserva): sale de la franja del horario de
-- atención. Null = el paciente elige según lo que ofrezca el profesional. No va
-- en db._LIBERADO: es del horario, sobrevive a las cancelaciones.
alter table turnos add column solo_modalidad text check (solo_modalidad in ('presencial', 'virtual'));

-- La vista expone la modalidad del horario (columnas nuevas, al final).
create or replace view turnos_disponibles as
 SELECT id, tenant_id, inicio, fin, estado, paciente_telefono, paciente_nombre, recordatorio_enviado, created_at,
        asistencia, pago, reprogramado_desde, valor, frecuencia, modalidad, nota, solo_modalidad
   FROM turnos t
  WHERE estado = 'libre'::text AND t.hueco IS NULL
    AND NOT (EXISTS (SELECT 1 FROM turnos r WHERE r.tenant_id = t.tenant_id AND r.estado = 'reservado'::text
                     AND r.inicio < t.fin AND r.fin > t.inicio))
    AND NOT (EXISTS (SELECT 1 FROM bloqueos b WHERE b.tenant_id = t.tenant_id AND b.desde < t.fin AND b.hasta > t.inicio));

-- Un horario libre de un día bloqueado (feriado, vacaciones) que el
-- profesional decidió ofrecer igual a sus pacientes. El bloqueo sigue (y el
-- feriado se vuelve a cargar solo cada año); la excepción es del horario.
alter table turnos add column abierto boolean not null default false;

create or replace view turnos_disponibles as
 SELECT id, tenant_id, inicio, fin, estado, paciente_telefono, paciente_nombre, recordatorio_enviado, created_at,
        asistencia, pago, reprogramado_desde, valor, frecuencia, modalidad, nota, solo_modalidad, sede, abierto
   FROM turnos t
  WHERE estado = 'libre'::text AND t.hueco IS NULL
    AND NOT (EXISTS (SELECT 1 FROM turnos r WHERE r.tenant_id = t.tenant_id AND r.estado = 'reservado'::text
                     AND r.inicio < t.fin AND r.fin > t.inicio))
    AND (t.abierto OR NOT (EXISTS (SELECT 1 FROM bloqueos b WHERE b.tenant_id = t.tenant_id AND b.desde < t.fin AND b.hasta > t.inicio)));

-- Al terminar una pausa, a cada persona de la lista de espera se le ofrece un
-- horario distinto y se le GUARDA unas horas: nadie más lo ve ni lo puede
-- reservar hasta `guardado_hasta`, salvo ese teléfono (o el profesional desde
-- el panel). Vencido el plazo vuelve a estar libre para todos, sin tocar nada.
alter table turnos add column guardado_tel text;
alter table turnos add column guardado_hasta timestamptz;

create or replace view turnos_disponibles as
 SELECT id, tenant_id, inicio, fin, estado, paciente_telefono, paciente_nombre, recordatorio_enviado, created_at,
        asistencia, pago, reprogramado_desde, valor, frecuencia, modalidad, nota, solo_modalidad, sede, abierto
   FROM turnos t
  WHERE estado = 'libre'::text AND t.hueco IS NULL
    AND (t.guardado_hasta IS NULL OR t.guardado_hasta <= now())
    AND NOT (EXISTS (SELECT 1 FROM turnos r WHERE r.tenant_id = t.tenant_id AND r.estado = 'reservado'::text
                     AND r.inicio < t.fin AND r.fin > t.inicio))
    AND (t.abierto OR NOT (EXISTS (SELECT 1 FROM bloqueos b WHERE b.tenant_id = t.tenant_id AND b.desde < t.fin AND b.hasta > t.inicio)));

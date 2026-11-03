-- Feriados nacionales: 'bloquear' (se cargan solos como bloqueos) o 'atender'.
alter table tenants add column feriados text not null default 'bloquear' check (feriados in ('bloquear', 'atender'));

-- Consultorios: [{"nombre": "Palermo", "direccion": "Av. Santa Fe 1234, 3° B"}, ...]. Cada franja del
-- horario puede decir en cuál atiende; el horario guarda el nombre (del HORARIO,
-- no de la reserva: no va en db._LIBERADO).
alter table tenants add column sedes jsonb;
alter table turnos add column sede text;

create or replace view turnos_disponibles as
 SELECT id, tenant_id, inicio, fin, estado, paciente_telefono, paciente_nombre, recordatorio_enviado, created_at,
        asistencia, pago, reprogramado_desde, valor, frecuencia, modalidad, nota, solo_modalidad, sede
   FROM turnos t
  WHERE estado = 'libre'::text AND t.hueco IS NULL
    AND NOT (EXISTS (SELECT 1 FROM turnos r WHERE r.tenant_id = t.tenant_id AND r.estado = 'reservado'::text
                     AND r.inicio < t.fin AND r.fin > t.inicio))
    AND NOT (EXISTS (SELECT 1 FROM bloqueos b WHERE b.tenant_id = t.tenant_id AND b.desde < t.fin AND b.hasta > t.inicio));

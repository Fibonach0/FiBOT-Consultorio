-- Dos personas pueden compartir WhatsApp (hermanos con el teléfono de un adulto).
-- La ficha se identifica por teléfono + `ficha`. '' es la de siempre, así que
-- todo lo existente queda como estaba; el teléfono sigue siendo sólo el contacto.
alter table turnos add column ficha text not null default '';
alter table pacientes add column ficha text not null default '';
alter table paciente_personas add column ficha text not null default '';
alter table paciente_fechas add column ficha text not null default '';
alter table cancelaciones add column ficha text not null default '';

alter table pacientes drop constraint pacientes_pkey;
alter table pacientes add primary key (tenant_id, paciente_telefono, ficha);

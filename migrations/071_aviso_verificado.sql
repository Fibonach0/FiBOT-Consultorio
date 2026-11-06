-- El WhatsApp de avisos que el profesional vinculó mandando desde ahí el
-- código de su panel (asistente_profesional.vincular). El asistente le pasa
-- datos de pacientes sólo si coincide con `aviso_telefono`: si cambia el
-- número en el panel, queda desvinculado solo.
alter table tenants add column if not exists aviso_verificado text;

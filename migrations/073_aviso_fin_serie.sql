-- Fin de una serie de sesiones fijas: el asistente le avisa al profesional
-- una sola vez por serie. Guarda el inicio de la última sesión avisada.
alter table pacientes add column if not exists aviso_fin_serie text;

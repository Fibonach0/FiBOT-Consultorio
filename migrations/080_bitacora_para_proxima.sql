-- «Para la próxima»: un renglón opcional en cada anotación de la bitácora con
-- lo que quedó pendiente. La tarjeta de Hoy y la página rápida lo muestran el
-- día de la sesión siguiente. Igual de sensible que el texto.
alter table paciente_bitacora add column if not exists para_proxima text;

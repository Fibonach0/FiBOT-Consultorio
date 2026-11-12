-- De dónde llegó cada visita a la página de turnos: solo el sitio (Google,
-- Instagram, Facebook, el QR impreso u otro). Nada de la persona: ni IP, ni
-- navegador, ni la dirección exacta de la que vino.
alter table eventos_pagina add column if not exists origen text
  check (origen in ('google', 'instagram', 'facebook', 'qr', 'otro'));

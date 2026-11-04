-- Preferencias de vista del profesional (tema y tamaño de letra), para que lo
-- siga a cualquier dispositivo, y color de su página pública.
alter table tenants
  add column preferencias_vista jsonb not null default '{}'::jsonb,
  add column color_pagina text check (color_pagina in ('verde', 'azul', 'terracota', 'violeta', 'pizarra'));

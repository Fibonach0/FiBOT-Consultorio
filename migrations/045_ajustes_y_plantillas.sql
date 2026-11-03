-- Ajustes de WhatsApp que se cargan desde administración y pisan a los de
-- Railway: número de la plataforma, cuenta, freno y teléfono de avisos.
create table ajustes_plataforma (
  clave text primary key,
  valor text not null default '',
  cuando timestamptz not null default now()
);
alter table ajustes_plataforma enable row level security;

-- Último aviso de Meta sobre cada plantilla (aprobada, rechazada, cambio de
-- categoría). Lo llena el webhook.
create table plantillas_meta (
  nombre text not null,
  idioma text not null default '',
  estado text not null,
  motivo text,
  cuando timestamptz not null default now(),
  primary key (nombre, idioma)
);
alter table plantillas_meta enable row level security;

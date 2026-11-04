-- Días puente (no laborables) aparte de los feriados: se pueden atender
-- aunque los feriados se bloqueen. 'bloquear' (por defecto) / 'atender'.
alter table tenants add column if not exists puentes text not null default 'bloquear'
  check (puentes in ('bloquear', 'atender'));

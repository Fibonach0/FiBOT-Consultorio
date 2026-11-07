-- Desde cuándo está pausada la página de turnos: el panel avisa si lleva días
-- pausada sin fecha de vuelta. Lo mantiene un trigger, así vale para todas las
-- formas de pausar (panel, asistente por WhatsApp, reactivación automática).
alter table tenants add column if not exists pausado_desde timestamptz;

create or replace function tenants_pausado_desde() returns trigger
language plpgsql set search_path = public as $$
begin
  if new.pausado and (tg_op = 'INSERT' or not coalesce(old.pausado, false)) then
    new.pausado_desde := now();
  elsif not coalesce(new.pausado, false) then
    new.pausado_desde := null;
  end if;
  return new;
end $$;

drop trigger if exists tenants_pausado_desde on tenants;
create trigger tenants_pausado_desde before insert or update of pausado on tenants
  for each row execute function tenants_pausado_desde();

-- El QR también sirve para entrar a administración: «tipo» separa los dos
-- usos para que un QR de un lado nunca abra el otro.
alter table qr_logins add column tipo text not null default 'panel';
alter table qr_logins add column admin_id uuid references admins (id) on delete cascade;

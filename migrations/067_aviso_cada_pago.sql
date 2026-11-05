-- El profesional elige si quiere un WhatsApp por cada pago de Mercado Pago
-- o sólo el resumen del mes por mail.
alter table tenants add column if not exists aviso_cada_pago boolean not null default true;

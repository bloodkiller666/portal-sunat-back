-- Deshace 0011.

begin;

alter table public.comprobantes
    drop column if exists tipo_cambio,
    drop column if exists monto_pen;

commit;

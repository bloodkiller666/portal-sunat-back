-- 0011_tipo_cambio_monto_pen.sql
--
-- SIRE entrega `Total CP` en SOLES (convertido con `Tipo Cambio`) aunque `Moneda` diga USD.
-- Para que `monto_total` sea el importe en la moneda del comprobante (el que Bizlinks exige
-- y el que ve el usuario) se guarda aparte lo que informó SIRE:
--   tipo_cambio : tipo de cambio de SIRE (NULL si no lo informó o el comprobante vino de un XML)
--   monto_pen   : importe en soles tal como lo informó SIRE
-- Ambas columnas son NULL en lo ya cargado: no se inventa ningún valor.
-- Deshacer: supabase/rollback/0011_tipo_cambio_monto_pen.down.sql

begin;

alter table public.comprobantes
    add column tipo_cambio numeric(12, 6) check (tipo_cambio is null or tipo_cambio > 0),
    add column monto_pen   numeric(16, 2);

comment on column public.comprobantes.tipo_cambio is
    'Tipo de cambio informado por SIRE para el comprobante (NULL si no se conoce)';
comment on column public.comprobantes.monto_pen is
    'Importe total en soles tal como lo informa SIRE (monto_total está en la moneda del comprobante)';

commit;

-- 0006_comprobante_pago.sql
-- Condiciones de pago del comprobante, tal como las declara el XML UBL:
--   * fecha_vencimiento — fecha límite de pago (DueDate o última cuota).
--   * forma_pago        — "Contado", "Crédito" o "Crédito N días".
-- Ambas son opcionales: los XML antiguos y las boletas no siempre las traen, y
-- los comprobantes ya importados quedan en NULL (el portal los muestra vacíos).

begin;

alter table public.comprobantes
    add column fecha_vencimiento date,
    add column forma_pago text;

comment on column public.comprobantes.fecha_vencimiento is
    'Fecha de vencimiento del pago según el XML (DueDate o última cuota). NULL si no la declara';
comment on column public.comprobantes.forma_pago is
    'Forma de pago según el XML: Contado, Crédito o Crédito N días. NULL si no la declara';

commit;

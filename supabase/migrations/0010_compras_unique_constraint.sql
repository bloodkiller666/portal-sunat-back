-- 0010_compras_unique_constraint.sql
--
-- En compras (`origen = 'recibido'`) el emisor es el proveedor, y dos proveedores distintos
-- pueden emitir la misma F001-00000123. La restricción de 0005 no incluía `ruc_emisor`, así
-- que el segundo se habría tomado por duplicado. En ventas el emisor es siempre la empresa,
-- de modo que las filas existentes no cambian de significado.
--
-- Sigue siendo una *restricción* (no solo un índice) porque el código inserta con
-- `on conflict on constraint comprobantes_unicos_por_ambiente`.
-- Deshacer: supabase/rollback/0010_compras_unique_constraint.down.sql

begin;

alter table public.comprobantes drop constraint comprobantes_unicos_por_ambiente;
alter table public.comprobantes
    add constraint comprobantes_unicos_por_ambiente
    unique (company_id, origen, ruc_emisor, tipo_documento, serie, correlativo, sunat_ambiente);

commit;

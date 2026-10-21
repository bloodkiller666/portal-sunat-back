-- Deshace 0010: vuelve a la llave sin `ruc_emisor`.
-- Falla si ya hay dos compras con la misma serie-número de proveedores distintos.

begin;

alter table public.comprobantes drop constraint comprobantes_unicos_por_ambiente;
alter table public.comprobantes
    add constraint comprobantes_unicos_por_ambiente
    unique (company_id, origen, tipo_documento, serie, correlativo, sunat_ambiente);

commit;

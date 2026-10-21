-- 0012_emisor_tipo_doc.sql
--
-- Tipo de documento de identidad de cada parte, tal como lo informa SIRE (catálogo 06 de SUNAT:
-- 6 = RUC, 1 = DNI, 0 = documento tributario de no domiciliado sin RUC, 4 = carné de
-- extranjería, 7 = pasaporte...). Permite saber por qué un comprobante no tiene RUC (por
-- ejemplo una exportación a un cliente del exterior) y mostrar el documento que sí tiene.
-- NULL en lo ya cargado y en lo importado desde XML.
-- Deshacer: supabase/rollback/0012_emisor_tipo_doc.down.sql

begin;

alter table public.comprobantes
    add column emisor_tipo_doc   text,
    add column receptor_tipo_doc text;

comment on column public.comprobantes.emisor_tipo_doc is
    'Tipo de documento del emisor según SIRE (6 = RUC, 0 = no domiciliado sin RUC...)';
comment on column public.comprobantes.receptor_tipo_doc is
    'Tipo de documento del adquirente según SIRE (6 = RUC, 1 = DNI, 0 = no domiciliado...)';

commit;

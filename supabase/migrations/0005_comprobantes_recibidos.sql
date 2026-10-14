-- 0005_comprobantes_recibidos.sql
-- El portal guarda dos clases de comprobantes:
--   * emitido  — lo emitió la empresa (ruc_emisor = RUC de la empresa).
--   * recibido — compra a un tercero: el emisor es otro RUC y la empresa es el
--                adquirente. El XML lo entrega el proveedor o se descarga del
--                portal de SUNAT; el portal lo archiva y lo lista.
-- Los datos del adquirente solo se conocen al importar el XML, así que se guardan
-- junto al comprobante (no hay tabla aparte: es 1:1 y no se consulta por él).

begin;

create type public.origen_comprobante as enum ('emitido', 'recibido');

alter table public.comprobantes
    add column origen public.origen_comprobante not null default 'emitido',
    add column ruc_receptor char(11)
        check (ruc_receptor is null or ruc_receptor ~ '^[0-9]{11}$'),
    add column receptor_doc text,
    add column razon_social_emisor text,
    add column razon_social_receptor text;

comment on column public.comprobantes.origen is
    'emitido: lo emitió la empresa. recibido: compra a un tercero (el emisor es otro RUC)';
comment on column public.comprobantes.ruc_receptor is
    'RUC del adquirente. NULL en boletas emitidas a DNI o consumidor final';
comment on column public.comprobantes.receptor_doc is
    'Documento del adquirente tal como figura en el XML (RUC, DNI o -)';
comment on column public.comprobantes.razon_social_emisor is
    'Nombre del emisor según el XML: el proveedor en las compras';

-- La unicidad debe separar emitidas de recibidas: una empresa puede tener la
-- F001-00000001 que emitió y, a la vez, la F001-00000001 que le emitió otro
-- contribuyente.
alter table public.comprobantes drop constraint comprobantes_unicos_por_ambiente;
alter table public.comprobantes
    add constraint comprobantes_unicos_por_ambiente
    unique (company_id, origen, tipo_documento, serie, correlativo, sunat_ambiente);

create index comprobantes_empresa_origen_idx
    on public.comprobantes (company_id, origen, fecha_emision desc);

commit;

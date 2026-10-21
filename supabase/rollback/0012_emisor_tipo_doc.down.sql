-- Deshace 0012.

begin;

alter table public.comprobantes
    drop column if exists emisor_tipo_doc,
    drop column if exists receptor_tipo_doc;

commit;

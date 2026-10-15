-- 0007_comprobante_fuente.sql
-- De dónde salió el registro del comprobante:
--   * xml  — importado desde su XML UBL (trae ítems y el XML se archiva).
--   * sire — sincronizado desde la propuesta RVIE de SIRE (solo cabecera: SUNAT
--            no entrega las líneas del detalle).
-- Permite que un XML importado después complete (en vez de chocar con) un
-- registro que vino de SIRE.

begin;

alter table public.comprobantes
    add column fuente text not null default 'xml'
        check (fuente in ('xml', 'sire'));

comment on column public.comprobantes.fuente is
    'xml: importado desde su XML UBL. sire: sincronizado desde la propuesta RVIE (sin ítems)';

commit;

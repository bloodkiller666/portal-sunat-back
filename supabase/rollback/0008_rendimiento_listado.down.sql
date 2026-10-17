-- Deshace 0008_rendimiento_listado.sql (solo si hiciera falta volver atrás).
-- Restaura las políticas RLS originales (idénticas a 0003_rls.sql) y quita lo agregado.
-- El código anterior a 0008 funciona con la base en este estado.

begin;

drop trigger if exists archivos_sincroniza_respaldo on public.archivos;
drop function if exists app.sincronizar_respaldo_comprobante();

drop policy if exists comprobantes_select_members on public.comprobantes;
create policy comprobantes_select_members
    on public.comprobantes for select to authenticated
    using (app.is_company_member(company_id));

drop policy if exists archivos_select_members on public.archivos;
create policy archivos_select_members
    on public.archivos for select to authenticated
    using (app.is_company_member(company_id));

drop function if exists app.mis_company_ids();

drop index if exists public.comprobantes_empresa_fecha_orden_idx;
drop index if exists public.comprobantes_empresa_numero_idx;

alter table public.comprobantes
    drop column if exists tiene_xml,
    drop column if exists tiene_cdr,
    drop column if exists tiene_pdf;

commit;

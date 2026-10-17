-- 0008_rendimiento_listado.sql
-- Listado de comprobantes rápido a gran escala (cientos de miles de filas por empresa).
--
-- Medido con 150 000 comprobantes sintéticos (en una transacción revertida):
--   * la política RLS llamaba a `app.is_company_member()` fila por fila (~20 µs c/u),
--     así que una página tardaba ~4 s aunque solo devolviera 50 filas;
--   * el orden por defecto no tenía un índice que lo cubriera (ordenaba todo el conjunto);
--   * los filtros "sin XML / sin CDR" y la cobertura recorrían `archivos` por cada fila.
-- Este script no cambia ningún permiso: la pertenencia a la empresa se sigue exigiendo,
-- solo se resuelve una vez por consulta en lugar de una vez por fila.

begin;

-- --------------------------------------------------------------------------- --
-- 1. RLS: empresas del usuario resueltas una sola vez por consulta
--    (equivale a `app.is_company_member(company_id)` para cada fila)
-- --------------------------------------------------------------------------- --
create or replace function app.mis_company_ids()
returns setof uuid
language sql
stable
security definer
set search_path = public, auth, pg_temp
as $$
    select uc.company_id
    from public.user_companies uc
    where uc.user_id = auth.uid()
      and uc.activo;
$$;

comment on function app.mis_company_ids() is
    'Empresas activas de auth.uid(); las políticas la usan como subconsulta sin correlación';

revoke all on function app.mis_company_ids() from public;
grant execute on function app.mis_company_ids() to authenticated, service_role;

drop policy if exists comprobantes_select_members on public.comprobantes;
create policy comprobantes_select_members
    on public.comprobantes for select to authenticated
    using (company_id in (select app.mis_company_ids()));

drop policy if exists archivos_select_members on public.archivos;
create policy archivos_select_members
    on public.archivos for select to authenticated
    using (company_id in (select app.mis_company_ids()));

-- --------------------------------------------------------------------------- --
-- 2. Respaldo (XML / CDR / PDF) desnormalizado en el comprobante
--    Filtrar y contar la cobertura pasa a ser leer una columna, no una subconsulta.
-- --------------------------------------------------------------------------- --
alter table public.comprobantes
    add column if not exists tiene_xml boolean not null default false,
    add column if not exists tiene_cdr boolean not null default false,
    add column if not exists tiene_pdf boolean not null default false;

comment on column public.comprobantes.tiene_xml is
    'Hay un XML ya subido a R2 (lo mantiene el trigger de `archivos`)';
comment on column public.comprobantes.tiene_cdr is
    'Hay un CDR ya subido a R2 (lo mantiene el trigger de `archivos`)';
comment on column public.comprobantes.tiene_pdf is
    'Hay un PDF ya subido a R2 (lo mantiene el trigger de `archivos`)';

-- Datos existentes
update public.comprobantes c
set tiene_xml = exists (
        select 1 from public.archivos a
        where a.comprobante_id = c.id and a.estado = 'subido' and a.tipo = 'xml'),
    tiene_cdr = exists (
        select 1 from public.archivos a
        where a.comprobante_id = c.id and a.estado = 'subido' and a.tipo = 'cdr'),
    tiene_pdf = exists (
        select 1 from public.archivos a
        where a.comprobante_id = c.id and a.estado = 'subido' and a.tipo = 'pdf')
where exists (select 1 from public.archivos a where a.comprobante_id = c.id);

-- Mantenimiento automático. SECURITY DEFINER: lo dispara quien sube el archivo (admin o
-- emisor) y debe poder refrescar la fila aunque la política de `comprobantes` cambie.
create or replace function app.sincronizar_respaldo_comprobante()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_ids uuid[];
begin
    if tg_op = 'DELETE' then
        v_ids := array[old.comprobante_id];
    elsif tg_op = 'UPDATE' then
        v_ids := array[old.comprobante_id, new.comprobante_id];
    else
        v_ids := array[new.comprobante_id];
    end if;

    update public.comprobantes c
    set tiene_xml = exists (
            select 1 from public.archivos a
            where a.comprobante_id = c.id and a.estado = 'subido' and a.tipo = 'xml'),
        tiene_cdr = exists (
            select 1 from public.archivos a
            where a.comprobante_id = c.id and a.estado = 'subido' and a.tipo = 'cdr'),
        tiene_pdf = exists (
            select 1 from public.archivos a
            where a.comprobante_id = c.id and a.estado = 'subido' and a.tipo = 'pdf')
    where c.id = any (v_ids);

    return null;
end;
$$;

drop trigger if exists archivos_sincroniza_respaldo on public.archivos;
create trigger archivos_sincroniza_respaldo
    after insert or delete or update of comprobante_id, estado, tipo
    on public.archivos
    for each row execute function app.sincronizar_respaldo_comprobante();

-- --------------------------------------------------------------------------- --
-- 3. Índices que cubren los órdenes del listado (por defecto: fecha desc)
--    El desempate por `id` va en el mismo sentido para poder recorrer el índice al revés.
-- --------------------------------------------------------------------------- --
create index if not exists comprobantes_empresa_fecha_orden_idx
    on public.comprobantes (company_id, fecha_emision desc nulls last, correlativo desc, id desc);

create index if not exists comprobantes_empresa_numero_idx
    on public.comprobantes (company_id, serie desc, correlativo desc, id desc);

-- Nota: no se crean índices trigram para la búsqueda parcial (`ilike '%texto%'`): bajo RLS
-- Postgres no puede usarlos (el operador `ilike` no es "leakproof"), así que no servirían.
-- La búsqueda recorre las filas de la empresa (≈1 s con 150 000); si algún día hiciera falta
-- más, la salida es una función SECURITY DEFINER que compruebe la pertenencia a mano.

commit;

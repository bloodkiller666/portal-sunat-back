-- 0002_functions_triggers.sql
-- Funciones auxiliares, triggers de updated_at y helpers de membresía para RLS.

begin;

-- --------------------------------------------------------------------------- --
-- updated_at automático
-- --------------------------------------------------------------------------- --
create or replace function app.set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

create trigger companies_set_updated_at
    before update on public.companies
    for each row execute function app.set_updated_at();

create trigger user_companies_set_updated_at
    before update on public.user_companies
    for each row execute function app.set_updated_at();

create trigger comprobantes_set_updated_at
    before update on public.comprobantes
    for each row execute function app.set_updated_at();

create trigger comprobante_items_set_updated_at
    before update on public.comprobante_items
    for each row execute function app.set_updated_at();

create trigger archivos_set_updated_at
    before update on public.archivos
    for each row execute function app.set_updated_at();

-- --------------------------------------------------------------------------- --
-- Campos derivados del comprobante (nomenclatura exigida por SUNAT)
-- --------------------------------------------------------------------------- --
create or replace function app.comprobantes_before_save()
returns trigger
language plpgsql
security invoker
as $$
begin
    if new.ruc_emisor is null or btrim(new.ruc_emisor) = '' then
        select c.ruc into new.ruc_emisor
        from public.companies c
        where c.id = new.company_id;
    end if;

    new.numero := lpad(new.correlativo::text, 8, '0');
    new.nombre_archivo :=
        new.ruc_emisor || '-' || new.tipo_documento || '-' || new.serie || '-' || new.numero;
    return new;
end;
$$;

create trigger comprobantes_before_save
    before insert or update of company_id, ruc_emisor, tipo_documento, serie, correlativo
    on public.comprobantes
    for each row execute function app.comprobantes_before_save();

-- --------------------------------------------------------------------------- --
-- Helpers de RLS
--
-- SECURITY DEFINER a propósito: evitan la recursión infinita que ocurre cuando
-- una política de `user_companies` necesita consultar `user_companies`.
-- --------------------------------------------------------------------------- --
create or replace function app.is_company_member(p_company_id uuid)
returns boolean
language sql
stable
security definer
set search_path = public, auth, pg_temp
as $$
    select exists (
        select 1
        from public.user_companies uc
        where uc.company_id = p_company_id
          and uc.user_id = auth.uid()
          and uc.activo
    );
$$;

create or replace function app.has_company_role(p_company_id uuid, p_roles public.rol_empresa[])
returns boolean
language sql
stable
security definer
set search_path = public, auth, pg_temp
as $$
    select exists (
        select 1
        from public.user_companies uc
        where uc.company_id = p_company_id
          and uc.user_id = auth.uid()
          and uc.activo
          and uc.rol = any (p_roles)
    );
$$;

comment on function app.is_company_member(uuid) is
    'true si auth.uid() pertenece a la empresa indicada';
comment on function app.has_company_role(uuid, public.rol_empresa[]) is
    'true si auth.uid() pertenece a la empresa con alguno de los roles indicados';

revoke all on function app.is_company_member(uuid) from public;
revoke all on function app.has_company_role(uuid, public.rol_empresa[]) from public;
grant usage on schema app to authenticated, service_role;
grant execute on function app.is_company_member(uuid) to authenticated, service_role;
grant execute on function app.has_company_role(uuid, public.rol_empresa[]) to authenticated, service_role;

commit;

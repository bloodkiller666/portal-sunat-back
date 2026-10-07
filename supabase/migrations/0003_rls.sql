-- 0003_rls.sql
-- Row Level Security: toda consulta se limita a las empresas del usuario
-- autenticado y, para escritura, al rol dentro de esa empresa.

begin;

alter table public.companies enable row level security;
alter table public.user_companies enable row level security;
alter table public.comprobantes enable row level security;
alter table public.comprobante_items enable row level security;
alter table public.sunat_envios enable row level security;
alter table public.archivos enable row level security;

-- --------------------------------------------------------------------------- --
-- companies: ver las propias; administrar solo como admin
-- --------------------------------------------------------------------------- --
create policy companies_select_members
    on public.companies for select to authenticated
    using (app.is_company_member(id));

create policy companies_update_admins
    on public.companies for update to authenticated
    using (app.has_company_role(id, array['admin']::public.rol_empresa[]))
    with check (app.has_company_role(id, array['admin']::public.rol_empresa[]));

-- Sin política de INSERT/DELETE: crear o borrar empresas queda para
-- service_role o para las migraciones.

-- --------------------------------------------------------------------------- --
-- user_companies: cada usuario ve sus membresías; los admin gestionan la suya
-- --------------------------------------------------------------------------- --
create policy user_companies_select
    on public.user_companies for select to authenticated
    using (
        user_id = auth.uid()
        or app.has_company_role(company_id, array['admin']::public.rol_empresa[])
    );

create policy user_companies_insert_admins
    on public.user_companies for insert to authenticated
    with check (app.has_company_role(company_id, array['admin']::public.rol_empresa[]));

create policy user_companies_update_admins
    on public.user_companies for update to authenticated
    using (app.has_company_role(company_id, array['admin']::public.rol_empresa[]))
    with check (app.has_company_role(company_id, array['admin']::public.rol_empresa[]));

create policy user_companies_delete_admins
    on public.user_companies for delete to authenticated
    using (app.has_company_role(company_id, array['admin']::public.rol_empresa[]));

-- --------------------------------------------------------------------------- --
-- comprobantes: lectura para miembros; escritura para admin/emisor
-- --------------------------------------------------------------------------- --
create policy comprobantes_select_members
    on public.comprobantes for select to authenticated
    using (app.is_company_member(company_id));

create policy comprobantes_insert_emisores
    on public.comprobantes for insert to authenticated
    with check (app.has_company_role(company_id, array['admin', 'emisor']::public.rol_empresa[]));

create policy comprobantes_update_emisores
    on public.comprobantes for update to authenticated
    using (app.has_company_role(company_id, array['admin', 'emisor']::public.rol_empresa[]))
    with check (app.has_company_role(company_id, array['admin', 'emisor']::public.rol_empresa[]));

create policy comprobantes_delete_admins
    on public.comprobantes for delete to authenticated
    using (app.has_company_role(company_id, array['admin']::public.rol_empresa[]));

-- --------------------------------------------------------------------------- --
-- comprobante_items: heredan el permiso del comprobante padre
-- --------------------------------------------------------------------------- --
create policy comprobante_items_select_members
    on public.comprobante_items for select to authenticated
    using (
        exists (
            select 1 from public.comprobantes c
            where c.id = comprobante_id and app.is_company_member(c.company_id)
        )
    );

create policy comprobante_items_insert_emisores
    on public.comprobante_items for insert to authenticated
    with check (
        exists (
            select 1 from public.comprobantes c
            where c.id = comprobante_id
              and app.has_company_role(c.company_id, array['admin', 'emisor']::public.rol_empresa[])
        )
    );

create policy comprobante_items_update_emisores
    on public.comprobante_items for update to authenticated
    using (
        exists (
            select 1 from public.comprobantes c
            where c.id = comprobante_id
              and app.has_company_role(c.company_id, array['admin', 'emisor']::public.rol_empresa[])
        )
    )
    with check (
        exists (
            select 1 from public.comprobantes c
            where c.id = comprobante_id
              and app.has_company_role(c.company_id, array['admin', 'emisor']::public.rol_empresa[])
        )
    );

create policy comprobante_items_delete_admins
    on public.comprobante_items for delete to authenticated
    using (
        exists (
            select 1 from public.comprobantes c
            where c.id = comprobante_id
              and app.has_company_role(c.company_id, array['admin']::public.rol_empresa[])
        )
    );

-- --------------------------------------------------------------------------- --
-- sunat_envios: bitácora de solo agregado (sin update ni delete)
-- --------------------------------------------------------------------------- --
create policy sunat_envios_select_members
    on public.sunat_envios for select to authenticated
    using (app.is_company_member(company_id));

create policy sunat_envios_insert_emisores
    on public.sunat_envios for insert to authenticated
    with check (app.has_company_role(company_id, array['admin', 'emisor']::public.rol_empresa[]));

-- --------------------------------------------------------------------------- --
-- archivos: lectura para miembros; gestión para admin/emisor
-- --------------------------------------------------------------------------- --
create policy archivos_select_members
    on public.archivos for select to authenticated
    using (app.is_company_member(company_id));

create policy archivos_insert_emisores
    on public.archivos for insert to authenticated
    with check (app.has_company_role(company_id, array['admin', 'emisor']::public.rol_empresa[]));

create policy archivos_update_emisores
    on public.archivos for update to authenticated
    using (app.has_company_role(company_id, array['admin', 'emisor']::public.rol_empresa[]))
    with check (app.has_company_role(company_id, array['admin', 'emisor']::public.rol_empresa[]));

create policy archivos_delete_admins
    on public.archivos for delete to authenticated
    using (app.has_company_role(company_id, array['admin']::public.rol_empresa[]));

commit;

-- 0013_automatizacion_logs.sql
-- Registro de cada corrida de la automatización diaria (SIRE + correo + importación), para
-- mostrarlo en el apartado «Logs» del portal. La automatización corre en una PC y el portal en
-- Vercel, así que los archivos locales (bitácora, estado.json, pendientes_importar.json) no se
-- pueden leer desde la web: cada corrida guarda aquí un resumen y una foto de su estado y de lo
-- que quedó pendiente de importar.
-- Es de solo agregado y no guarda credenciales ni el contenido de los archivos.
-- Deshacer: supabase/rollback/0013_automatizacion_logs.down.sql

begin;

create table public.automatizacion_logs (
    id                uuid primary key default gen_random_uuid(),
    company_id        uuid not null references public.companies (id) on delete cascade,
    created_at        timestamptz not null default now(),
    modo              text not null default 'real' check (modo in ('real', 'simulacion')),
    -- Qué se hizo en la corrida: sire, correo.
    tareas            text[] not null default '{}',
    -- ok: sin novedades ni errores; con_novedades: se registró algo; con_errores: algo falló.
    resultado         text not null check (resultado in ('ok', 'con_novedades', 'con_errores')),
    duracion_segundos integer check (duracion_segundos is null or duracion_segundos >= 0),
    novedades         jsonb not null default '{}'::jsonb,
    errores           jsonb not null default '[]'::jsonb,
    advertencias      jsonb not null default '[]'::jsonb,
    -- Resumen de cada tarea (sire, correo, importacion).
    detalle           jsonb not null default '{}'::jsonb,
    -- Foto de estado.json (última sincronización de SIRE, último reintento de PDF sueltos).
    estado            jsonb not null default '{}'::jsonb,
    -- Foto de pendientes_importar.json (carpetas que aún faltan por importar).
    pendientes        jsonb not null default '{}'::jsonb,
    created_by        uuid references auth.users (id) on delete set null
);

create index automatizacion_logs_empresa_fecha_idx
    on public.automatizacion_logs (company_id, created_at desc);
create index automatizacion_logs_empresa_resultado_idx
    on public.automatizacion_logs (company_id, resultado, created_at desc);

comment on table public.automatizacion_logs is
    'Una fila por corrida de la automatización diaria: novedades, errores, estado y pendientes';

alter table public.automatizacion_logs enable row level security;

create policy automatizacion_logs_select_members
    on public.automatizacion_logs for select to authenticated
    using (app.is_company_member(company_id));

create policy automatizacion_logs_insert_emisores
    on public.automatizacion_logs for insert to authenticated
    with check (app.has_company_role(company_id, array['admin', 'emisor']::public.rol_empresa[]));

grant select, insert on public.automatizacion_logs to authenticated;

commit;

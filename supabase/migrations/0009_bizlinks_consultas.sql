-- 0009_bizlinks_consultas.sql
-- Bitácora de las consultas a Bizlinks (OSE) para recuperar el XML y el CDR de los
-- comprobantes emitidos. Sirve para tres cosas:
--   * topes de consumo: cuántos comprobantes se consultaron en las últimas 24 h;
--   * cortacircuito: si Bizlinks bloqueó o cambió su página, se pausa varias horas;
--   * no repetir una consulta que no encontró nada antes de 24 h.
-- Es de solo agregado (como sunat_envios) y no guarda credenciales ni contenido de
-- los archivos: solo el resultado y el código HTTP.

begin;

create table public.bizlinks_consultas (
    id              uuid primary key default gen_random_uuid(),
    company_id      uuid not null references public.companies (id) on delete cascade,
    comprobante_id  uuid references public.comprobantes (id) on delete set null,
    canal           text not null check (canal in ('api', 'portal')),
    resultado       text not null check (resultado in ('ok', 'sin_resultado', 'error', 'bloqueado')),
    http_status     integer,
    detalle         text,
    created_at      timestamptz not null default now()
);

create index bizlinks_consultas_empresa_fecha_idx
    on public.bizlinks_consultas (company_id, canal, created_at desc);
create index bizlinks_consultas_comprobante_idx
    on public.bizlinks_consultas (comprobante_id, created_at desc);

comment on table public.bizlinks_consultas is
    'Bitácora de consultas a Bizlinks (api o portal anónimo): topes diarios, cortacircuito y auditoría';

alter table public.bizlinks_consultas enable row level security;

create policy bizlinks_consultas_select_members
    on public.bizlinks_consultas for select to authenticated
    using (app.is_company_member(company_id));

create policy bizlinks_consultas_insert_emisores
    on public.bizlinks_consultas for insert to authenticated
    with check (app.has_company_role(company_id, array['admin', 'emisor']::public.rol_empresa[]));

grant select, insert on public.bizlinks_consultas to authenticated;

commit;

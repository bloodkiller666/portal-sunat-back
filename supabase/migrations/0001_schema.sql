-- 0001_schema.sql
-- Esquema base del Portal de Facturación Electrónica.
-- Ninguna credencial de empresa (clave SOL, archivo del certificado, claves de la
-- API REST) se guarda en la base: se resuelven por empresa con el SecretsProvider.

begin;

create extension if not exists pgcrypto;

create schema if not exists app;

-- --------------------------------------------------------------------------- --
-- Tipos
-- --------------------------------------------------------------------------- --
create type public.sunat_ambiente as enum ('beta', 'prod');
create type public.rol_empresa as enum ('admin', 'emisor', 'lector');
create type public.estado_comprobante as enum ('pendiente', 'aprobado', 'rechazado', 'anulado');
create type public.tipo_operacion_sunat as enum (
    'envio',
    'consulta_estado',
    'consulta_cdr',
    'prueba_conexion',
    'comunicacion_baja',
    'resumen_diario',
    'reversion'
);
create type public.tipo_archivo as enum ('pdf', 'xml', 'cdr', 'zip');
create type public.estado_archivo as enum ('pendiente', 'subido', 'error', 'eliminado');

comment on type public.estado_comprobante is
    'Estados que consume el frontend: Pendiente, Aprobado, Rechazado, Anulado';
comment on type public.rol_empresa is
    'admin (gestiona), emisor (emite/consulta), lector (solo consulta)';

-- --------------------------------------------------------------------------- --
-- companies
-- --------------------------------------------------------------------------- --
create table public.companies (
    id                    uuid primary key default gen_random_uuid(),
    ruc                   char(11) not null unique check (ruc ~ '^[0-9]{11}$'),
    razon_social          text not null,
    nombre_comercial      text,
    direccion             text,
    ubigeo                char(6) check (ubigeo is null or ubigeo ~ '^[0-9]{6}$'),
    activo                boolean not null default true,
    sunat_ambiente        public.sunat_ambiente,
    certificado_alias     text,
    certificado_expira_at date,
    created_at            timestamptz not null default now(),
    updated_at            timestamptz not null default now(),
    created_by            uuid references auth.users (id) on delete set null
);

comment on column public.companies.sunat_ambiente is
    'NULL = usar el ambiente global definido por SUNAT_ENV';
comment on column public.companies.certificado_alias is
    'Solo metadatos del certificado: el archivo .pfx y su clave viven en el SecretsProvider';

-- --------------------------------------------------------------------------- --
-- user_companies (usuarios de Supabase Auth <-> empresas)
-- --------------------------------------------------------------------------- --
create table public.user_companies (
    user_id    uuid not null references auth.users (id) on delete cascade,
    company_id uuid not null references public.companies (id) on delete cascade,
    rol        public.rol_empresa not null,
    activo     boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (user_id, company_id)
);

create index user_companies_company_idx on public.user_companies (company_id);

-- --------------------------------------------------------------------------- --
-- comprobantes
-- --------------------------------------------------------------------------- --
create table public.comprobantes (
    id                uuid primary key default gen_random_uuid(),
    company_id        uuid not null references public.companies (id) on delete restrict,
    ruc_emisor        char(11) not null check (ruc_emisor ~ '^[0-9]{11}$'),
    tipo_documento    text not null check (tipo_documento ~ '^[0-9]{2}$'),
    serie             text not null check (serie ~ '^[A-Z0-9]{1,4}$'),
    correlativo       integer not null check (correlativo > 0),
    numero            text not null default '',
    nombre_archivo    text not null default '',
    fecha_emision     date,
    moneda            char(3) not null default 'PEN',
    monto_total       numeric(14, 2),
    estado            public.estado_comprobante not null default 'pendiente',
    sunat_ambiente    public.sunat_ambiente not null default 'beta',
    n_sap             text,
    orden_compra      text,
    check_sap         boolean not null default false,
    cdr_codigo        text,
    cdr_descripcion   text,
    cdr_notas         jsonb not null default '[]'::jsonb,
    cdr_fecha         timestamptz,
    ultimo_envio_at   timestamptz,
    anulado_at        timestamptz,
    motivo_anulacion  text,
    importado_at      timestamptz,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now(),
    created_by        uuid references auth.users (id) on delete set null,
    constraint comprobantes_unicos_por_ambiente
        unique (company_id, tipo_documento, serie, correlativo, sunat_ambiente)
);

comment on column public.comprobantes.nombre_archivo is
    'Nombre exigido por SUNAT: {RUC}-{TIPO}-{SERIE}-{CORRELATIVO}. Lo completa un trigger';
comment on column public.comprobantes.n_sap is 'N° SAP, campo de gestión del grupo';
comment on column public.comprobantes.check_sap is 'Marca de conciliación con SAP (aún sin integración)';

create index comprobantes_empresa_fecha_idx on public.comprobantes (company_id, fecha_emision desc);
create index comprobantes_empresa_estado_idx on public.comprobantes (company_id, estado);
create index comprobantes_sap_idx on public.comprobantes (ruc_emisor, n_sap) where n_sap is not null;
create index comprobantes_orden_compra_idx
    on public.comprobantes (orden_compra) where orden_compra is not null;

-- --------------------------------------------------------------------------- --
-- comprobante_items
-- --------------------------------------------------------------------------- --
create table public.comprobante_items (
    id                  uuid primary key default gen_random_uuid(),
    comprobante_id      uuid not null references public.comprobantes (id) on delete cascade,
    item_nro            integer not null check (item_nro > 0),
    codigo              text,
    descripcion         text not null,
    unidad_medida       text,
    cantidad            numeric(12, 4) not null check (cantidad > 0),
    valor_unitario      numeric(14, 6),
    precio_unitario     numeric(14, 6),
    descuento           numeric(14, 2) not null default 0,
    igv                 numeric(14, 2) not null default 0,
    isc                 numeric(14, 2) not null default 0,
    tip_afectacion_igv  text,
    total               numeric(14, 2),
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),
    unique (comprobante_id, item_nro)
);

comment on column public.comprobante_items.tip_afectacion_igv is 'Catálogo No. 07 de SUNAT';

create index comprobante_items_comprobante_idx on public.comprobante_items (comprobante_id);

-- --------------------------------------------------------------------------- --
-- sunat_envios: bitácora de cada intento contra SUNAT (sin secretos)
-- --------------------------------------------------------------------------- --
create table public.sunat_envios (
    id                bigint generated always as identity primary key,
    company_id        uuid not null references public.companies (id) on delete cascade,
    comprobante_id    uuid references public.comprobantes (id) on delete set null,
    tipo_operacion    public.tipo_operacion_sunat not null,
    sunat_ambiente    public.sunat_ambiente not null,
    endpoint          text,
    metodo            text,
    request_xml       text,
    response_xml      text,
    http_status       integer,
    codigo_respuesta  text,
    mensaje_respuesta text,
    notas             jsonb not null default '[]'::jsonb,
    exito             boolean not null default false,
    error_tipo        text,
    duracion_ms       integer,
    created_at        timestamptz not null default now()
);

comment on column public.sunat_envios.request_xml is
    'Trama enviada con el header WS-Security eliminado: nunca contiene la clave SOL';
comment on column public.sunat_envios.response_xml is
    'Respuesta de SUNAT tal como llegó (CDR incluido); no contiene secretos';

create index sunat_envios_comprobante_idx on public.sunat_envios (comprobante_id, created_at desc);
create index sunat_envios_company_idx on public.sunat_envios (company_id, created_at desc);

-- --------------------------------------------------------------------------- --
-- archivos: referencia a los archivos alojados en Cloudflare R2
-- --------------------------------------------------------------------------- --
create table public.archivos (
    id                  uuid primary key default gen_random_uuid(),
    company_id          uuid not null references public.companies (id) on delete cascade,
    comprobante_id      uuid references public.comprobantes (id) on delete cascade,
    tipo                public.tipo_archivo not null,
    nombre              text not null,
    mime_type           text,
    size_bytes          bigint check (size_bytes is null or size_bytes >= 0),
    hash_sha256         text,
    estado              public.estado_archivo not null default 'pendiente',
    error_detalle       text,
    subido_at           timestamptz,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),
    created_by          uuid references auth.users (id) on delete set null
);

comment on table public.archivos is
    'Referencia a PDF/XML/CDR en Cloudflare R2 (bucket privado). La clave del objeto se agrega en la migración 0004';

create index archivos_comprobante_idx on public.archivos (comprobante_id, tipo);
create index archivos_empresa_estado_idx on public.archivos (company_id, estado);

-- --------------------------------------------------------------------------- --
-- Permisos para el rol authenticated (RLS sigue filtrando las filas)
-- --------------------------------------------------------------------------- --
grant usage on schema public to authenticated, service_role;
grant select, insert, update, delete on public.companies to authenticated;
grant select, insert, update, delete on public.user_companies to authenticated;
grant select, insert, update, delete on public.comprobantes to authenticated;
grant select, insert, update, delete on public.comprobante_items to authenticated;
grant select, insert on public.sunat_envios to authenticated;
grant select, insert, update, delete on public.archivos to authenticated;

commit;

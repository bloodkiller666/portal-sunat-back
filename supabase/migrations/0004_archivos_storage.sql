-- 0004_archivos_storage.sql
-- El almacenamiento de archivos es Cloudflare R2 (bucket privado con URLs
-- firmadas). La tabla `archivos` gana la referencia genérica al objeto en R2.

begin;

alter table public.archivos
    add column if not exists storage_provider text,
    add column if not exists storage_bucket   text,
    add column if not exists storage_key      text;

comment on column public.archivos.storage_provider is
    'Proveedor de almacenamiento: hoy siempre ''r2''';
comment on column public.archivos.storage_bucket is
    'Bucket privado de R2 donde vive el objeto';
comment on column public.archivos.storage_key is
    'Clave S3 del objeto (p. ej. comprobantes/{ruc}/{año}/{mes}/{archivo}). '
    'La descarga se hace con una URL firmada generada al momento, nunca con URL pública';

create index if not exists archivos_storage_key_idx
    on public.archivos (storage_key) where storage_key is not null;

commit;

-- 0014_comprobante_correo.sql
--
-- De qué correo salió cada compra: asunto, remitente y fecha de recepción del correo del buzón
-- de facturas de proveedores. Lo llena la automatización al importar la carpeta de la compra
-- (y `scripts/cargar_correos.py` para lo ya importado). Es solo para mostrar en la columna
-- «Correo» de Compras y como evidencia; NULL en las ventas y en lo que no llegó por correo.
-- Si la misma factura llegó en varios correos se conserva el primero.
-- Deshacer: supabase/rollback/0014_comprobante_correo.down.sql

begin;

alter table public.comprobantes
    add column correo_asunto      text,
    add column correo_remitente   text,
    add column correo_recibido_at timestamptz;

comment on column public.comprobantes.correo_asunto is
    'Asunto del correo en que llegó la compra (buzón de facturas de proveedores)';
comment on column public.comprobantes.correo_remitente is
    'Remitente del correo en que llegó la compra';
comment on column public.comprobantes.correo_recibido_at is
    'Fecha en que el buzón recibió el correo en que llegó la compra';

commit;

-- Deshace 0014.

begin;

alter table public.comprobantes
    drop column if exists correo_asunto,
    drop column if exists correo_remitente,
    drop column if exists correo_recibido_at;

commit;

-- Deshace 0013: elimina el registro de corridas de la automatización.

begin;

drop table if exists public.automatizacion_logs;

commit;

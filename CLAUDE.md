# Guía para Claude Code — cómo terminar el Portal de Facturación SUNAT

Documento de handoff: contexto, estado real del proyecto y trabajo pendiente
para llegar a producción. Léelo completo antes de escribir código.

## 1. Qué es este proyecto

Portal multiempresa de comprobantes electrónicos peruanos (SUNAT), dividido en
dos repositorios:

| Repositorio | Ruta local | Despliegue |
| --- | --- | --- |
| **Backend** (este repo) | `factura-sunat-main-back` | Docker (por desplegar) |
| **Frontend** | `C:\Users\majco\Desktop\factura-sunat-main` | Vercel (ya hosteado) |

- **Stack backend**: Python 3.12 + uv, FastAPI, Pydantic v2, asyncpg (Supabase
  Postgres con RLS), zeep (SOAP SUNAT), aioboto3 (Cloudflare R2), signxml,
  fpdf2. Tests: pytest + respx (SUNAT siempre mockeado).
- **Stack frontend**: Next.js 16 (App Router) + React 19 + Tailwind v4 + GSAP,
  `@supabase/supabase-js` para el login. Gestor: pnpm (nunca npm).
- **Empresa principal**: TDM ASFALTOS S.A.C., RUC `20514831620`. Sus
  credenciales (SOL, certificado, CLIENT_ID/CLIENT_SECRET de API SUNAT) están
  en el `.env` local — **nunca en el repo** (`.gitignore` ya lo cubre).

`README.md` de este repo documenta cada endpoint con ejemplos curl: es la
referencia de la API.

## 2. Estado actual (lo que YA funciona — no lo rehagas)

Backend (`uv run pytest -q` → 527 passed; `uv run ruff check` → limpio):

- **SUNAT real (SOAP)**: `GET /companies/{ruc}/sunat/status` (valida SOL +
  certificado), `POST /comprobantes/{id}/refresh-cdr` (estado + CDR real),
  `POST /companies/{ruc}/comprobantes/refresh` (lote de pendientes, se detiene
  si SUNAT cae).
- **Importación**: XML UBL 2.1 individual (`/import`, 5 MB) y ZIP masivo
  (`/import-paquete`, hasta 200 archivos, XML+PDF emparejados por nombre).
- **Ítems**: el parser (`app/sunat/ubl.py`) extrae las líneas del detalle
  (cantidad, unidad, precios con/sin IGV, IGV/ISC, catálogo 07) y se guardan en
  `comprobante_items` (migración 0001 ya existía).
- **Detalle**: `GET /comprobantes/{id}` (cabecera + ítems).
- **PDF**: `GET /comprobantes/{id}/pdf` genera la representación impresa con
  fpdf2 desde los datos de la base (`app/integrations/pdf.py`). No depende de
  R2 ni de SUNAT.
- **Archivos en R2**: XML importado y CDR recibido se archivan (bucket
  privado) y se descargan con URL firmada (`POST /archivos/{id}/download-url`).
- **Firma XML** (`SunatXmlSigner`, XMLDSig) implementada y probada.
- **Auth**: JWT de Supabase (HS256 o ES256/RS256 vía JWKS) + RLS por empresa +
  roles `admin/emisor/lector`.

Frontend (ya conectado al backend):

- Login Supabase, selector multiempresa con rol, listado real con filtros y
  KPIs, detalle con ítems reales, descargas reales (PDF del backend, XML/CDR
  del bucket con URL firmada), importación XML/ZIP, botón "Sincronizar SUNAT",
  exportación Excel/ZIP y correo real vía Resend (`/api/enviar-comprobantes`).
- Verificación: `pnpm lint` y `pnpm build` deben pasar sin errores.
- Configuración por `.env.local` (ver `.env.example` del frontend):
  `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY`.

## 3. Reglas del proyecto (no negociables)

1. **Nada de secretos en el repo**: `.env`, `secrets/`, `*.pfx` están
   ignorados. Los logs redactan credenciales (`app/core/redaction.py`); las
   tramas SOAP se guardan sin el header WS-Security.
2. **Solo servicios oficiales de SUNAT** (sin scraping): URLs en
   `app/sunat/endpoints.py`.
3. **Tests con SUNAT mockeado** (`tests/fakes.py` + respx): ninguna prueba toca
   la red. Si agregas funcionalidad, agrega pruebas siguiendo el patrón
   existente (repos en memoria, `FakeUnitOfWork`, fixtures en
   `tests/fixtures/ubl/`).
4. **Errores con un único formato** `{ error: { code, message, details,
   request_id } }` (clases en `app/core/errors.py`).
5. **Emisión bloqueada en prod** salvo `CONFIRM_PROD_EMISSION=true` (guard en
   `app/services/emission.py`).
6. Estilo: ruff (línea 100); nombres y comentarios en español; docstrings
   explicando el *por qué*.

## 4. Pendientes (en orden de prioridad para llegar a producción)

### P1 — Desplegar el backend (imagen y compose YA verificados en Docker local)

El `Dockerfile`, `docker-compose.yml` (API + Caddy con HTTPS) y CI ya existen y
se probaron en Docker local con `ENVIRONMENT=prod` (ver README, «VPS con Docker
Compose»). Falta:

1. Elegir destino (VPS con Docker Compose, o Railway/Render/Fly) y definir
   `PORT`/`WEB_CONCURRENCY` (los inyecta la plataforma).
2. Apuntar un subdominio fijo (ej. `api.tudominio.com`) — la URL debe ser
   estable para `NEXT_PUBLIC_API_URL` del frontend.
3. Variables de producción: `ENVIRONMENT=prod`, `SUNAT_ENV=prod`,
   `CONFIRM_PROD_EMISSION=false`, `DATABASE_URL` (pooler en modo sesión),
   `SUPABASE_JWT_ALG`/`SUPABASE_JWT_JWKS_URL` (signing keys asimétricas),
   `CORS_ORIGINS` (dominio exacto de Vercel, sin `*`), `LOG_JSON=true`,
   `DOCS_ENABLED=false`, `SUNAT_VERIFY_SSL=true`, credenciales
   `SUNAT_CRED_20514831620_*` y las de R2.
4. Migraciones `0001`–`0008` (las 0006, 0007 y 0008 ya están en el Supabase actual).
   `scripts.aplicar_migraciones` solo sirve para una base **vacía** (reaplica
   todo); en una base existente aplique únicamente las nuevas.
5. Rotar cualquier credencial que haya circulado por chat/correo antes del
   primer despliegue.

### P2 — Prueba end-to-end frontend ↔ backend ✅ HECHO (local)

Backend `uv run fastapi run --port 8000` con `CORS_ORIGINS=http://localhost:3000`;
frontend `pnpm dev` con `.env.local`. Validado con Edge headless (Playwright) y
por API: login → empresas → listado (249 comprobantes) → PDF → Check SAP
persistente → detalle → importar XML (archivado en R2) → sincronizar SIRE.
Falta repetirlo contra el dominio real tras desplegar (ver CORS de R2 abajo).

### P3 — Check SAP persistente ✅ HECHO

`PATCH /api/v1/comprobantes/{id}` (`check_sap` y/o `n_sap`, admin/emisor; el
`lector` recibe 403). El frontend lo llama con actualización optimista y revierte
con aviso si falla (`alternarCheckSap` en `app/page.tsx`). Verificado en
navegador: la marca sobrevive a recargar la página.

### P4 — Vencimiento y forma de pago ✅ HECHO

El parser UBL lee `DueDate`, `PaymentTerms/PaymentDueDate` (última cuota) y
`Payment/PaymentDueDate`; la forma de pago sale como `Contado`, `Crédito` o
`Crédito N días`. Migración `0006_comprobante_pago.sql` (aplicada). Los
comprobantes de SIRE traen vencimiento solo si SUNAT lo informa (hoy viene vacío)
y no traen forma de pago.

### P5 — Diagnóstico SIRE ✅ HECHO (la empresa SÍ está acogida)

`uv run python -m scripts.sire_probe --ruc 20514831620 --periodo 202609`.
**Correcciones al manual v31** respecto al texto original de esta guía:

- Token: `POST https://api-seguridad.sunat.gob.pe/v1/clientessol/{client_id}/oauth2/token/`
  (es `clientessol`, no `clientes_extranet`), `grant_type=password`,
  `scope=https://api-sire.sunat.gob.pe`, `client_id`, `client_secret`,
  `username` = RUC + usuario SOL, `password` = clave SOL. Dura 1 hora.
- El manual no define un código de «no incorporado»: el probe valida con los
  periodos habilitados (§5.2) y con «Descargar propuesta» (§5.18); un 401/403 o
  un error de acceso indica que no está acogida.
- URLs SIRE en `app/sunat/endpoints.py`; cliente en `app/sunat/sire.py`.

### P6 — Sincronización de ventas desde SIRE ✅ HECHO (solo lectura)

`POST /api/v1/companies/{ruc}/sire/ventas/sync` con `{ "periodo": "202609" }`
(admin/emisor). Flujo por tickets: pedir propuesta (§5.18) → consultar estado
(§5.16) → descargar ZIP (§5.17) → parsear el TXT (`app/sunat/sire_rvie.py`, 40
columnas separadas por `|`, UTF-8 con BOM) → insertar con
`create_if_absent` (idempotente). Si SUNAT tarda más de ~25 s responde
`estado=en_proceso` + `ticket`; se vuelve a llamar con ese `ticket`.

Decisiones de diseño (no las revierta sin motivo):

- Los registros SIRE se guardan con `sunat_ambiente=prod` y `fuente=sire`
  (migración `0007_comprobante_fuente.sql`); no traen ítems ni XML.
- `Est. Comp` 2 (o 0) = dado de baja (total 0) → `anulado`; los demás → `aprobado`.
  Los importes negativos de notas de crédito se guardan en positivo.
- Boletas resumidas en rango (`Nro Final`) y tipos distintos de 01/03/07/08 se
  omiten y se cuentan en `omitidos`.
- Importar después el XML de un comprobante `fuente=sire` lo **completa**
  (ítems, XML en R2) en vez de dar 409 (`completar_desde_xml`). Ojo: solo
  coincide si el ambiente de importación es `prod`; en local (`SUNAT_ENV=beta`)
  el XML crea una fila aparte.
- Cada sincronización archiva en R2 el ZIP original de SUNAT (`sire/{ruc}/{periodo}/`,
  tipo `zip`, sin comprobante, con SHA-256): es la evidencia de auditoría. Se lista en
  `GET /companies/{ruc}/sire/evidencias`.
- El PDF del portal se archiva en R2 la 1.ª vez que un admin/emisor lo descarga, como
  **copia informativa** (no sale del XML firmado: no es evidencia ante SUNAT).
- `getStatusCdr` devuelve `0013` («no pertenece a GEM») para facturas emitidas por otro
  sistema aunque SIRE las liste: no hay CDR por ese canal y NO es un rechazo. Un
  Aprobado nunca se degrada por una consulta (`_siguiente_estado`).
- No se implementa aceptar/reemplazar/registrar preliminar: el portal NO escribe
  en SIRE.

### Notas de la verificación end-to-end (2026-10-15)

- Las `SUNAT_CRED_*` del `.env` ahora llegan al entorno al arrancar
  (`get_settings`); antes, en local, el proveedor de secretos no las veía.
- Se añadió tolerancia de 30 s de desfase de reloj al validar el JWT
  (`LEEWAY_RELOJ_SEGUNDOS`); sin ella había 401 intermitentes con tokens recién
  emitidos.
- **Pendiente de infraestructura**: el bucket R2 debe tener CORS para el origen
  del frontend (GET); si no, el navegador bloquea la descarga del XML con URL
  firmada.

### P7 — Filtros server-side y paginación real ✅ HECHO (2026-10-17)

El servidor filtra, ordena, pagina y cuenta (`GET /companies/{ruc}/comprobantes`: `q`
sobre documento/cliente/RUC/OC, `n_sap`, `check_sap`, `moneda`, `respaldo`, `orden`,
`direccion`, `contar`) y calcula los indicadores (`GET …/comprobantes/resumen`). El frontend
pide una página (50 por defecto) más el resumen, en paralelo; ya no descarga todo.

Decisiones de diseño (no las revierta sin motivo):

- Migración `0008_rendimiento_listado.sql` (deshacer: `supabase/rollback/`): política RLS de
  lectura con `company_id in (select app.mis_company_ids())` (resuelve la pertenencia una vez
  por consulta; antes `is_company_member()` corría por fila y una página tardaba ~4 s con
  150 000 filas), índices que cubren los órdenes `fecha` y `numero`, y columnas
  `tiene_xml/tiene_cdr/tiene_pdf` en `comprobantes` que mantiene un trigger sobre `archivos`.
- El desempate del `order by` es `c.id` en el MISMO sentido que la columna y los nulos siguen
  al sentido: así Postgres recorre el índice al revés para `asc` sin ordenar todo.
- `count(*) over()` solo cuando `contar=true` (1.ª página o al cambiar de filtro); no se
  recuenta al paginar, ordenar ni cambiar el tamaño de página.
- Cada petición gasta pocos viajes a la base (cuenta, con la base remota ~300 ms cada uno):
  claims y rol en una sola consulta (`set_config('role', …)` equivale a `set local role`),
  empresa + membresía en una (`CompaniesRepository.get_access`), respaldos junto con la página.
- Límite conocido: `q` (`ilike '%x%'`) recorre las filas de la empresa (~1 s con 150 000).
  Bajo RLS Postgres no usa índices trigram con `ilike` (no es "leakproof"); la salida, si
  hiciera falta, es una función `SECURITY DEFINER` que compruebe la pertenencia a mano.
- `monto_total` es el importe total CON IGV (SIRE `Total CP` cuadra con la suma de sus
  componentes: verificado en 494 registros; el XML usa `PayableAmount`).

### P8 — Opcional / posterior

- Azure Key Vault: implementar `app/secrets/keyvault.py` (interfaz lista).
- Emisión real (`sendBill` con `sendSummary`/`getTicket`): la firma XML ya
  está; falta el flujo SOAP de envío. Requiere certificado `.pfx` con su
  contraseña (`CERT_PASSWORD` hoy vacía en el `.env` local) y activar
  `CONFIRM_PROD_EMISSION`.
- Correo SMTP del backend: hoy el frontend usa Resend directo; el stub
  `SmtpMailSender` puede quedarse así.

## 5. Verificación (Definition of Done de cualquier cambio)

```bash
cd factura-sunat-main-back
uv run pytest -q      # debe terminar sin fallos (527+ passed)
uv run ruff check     # All checks passed

cd ../factura-sunat-main
pnpm lint             # 0 errores
pnpm test             # pruebas unitarias de lib/ (vitest)
pnpm build            # compilación y tipos OK
```

Antes de commitear: `git status` no debe listar `.env`, `secrets/` ni binarios
de Node (`node.exe`, `pnpm.exe`, etc. — ya ignorados).

## 6. Datos útiles

- Migraciones aplicadas en el Supabase actual: `0001`–`0008`. La conexión vive
  en `DATABASE_URL` del `.env` local.
- El CDR de una compra (`origen=recibido`) no se sincroniza: lo informa el
  proveedor (decisión de diseño ya implementada).
- `billConsultService` solo existe en producción (también se usa en beta para
  consultas de solo lectura: decisión documentada en el README).
- Comunicación de errores SUNAT → estados: `app/sunat/states.py` (0→Aprobado,
  98/1033/0011/0127→Pendiente, resto→Rechazado).

### Respaldo y auditoría (2026-10-16)

- `POST /companies/{ruc}/comprobantes/import-cdr` (ZIP de `R-RUC-tipo-serie-número`) trae
  los CDR de comprobantes emitidos por otro sistema; `getStatusCdr` no los entrega (0013).
- `ComprobanteOut` trae `tiene_xml/tiene_cdr/tiene_pdf` (una consulta por página a
  `archivos`, no una por fila). `siguiente_estado` (público) nunca degrada un Aprobado.
- `scripts/respaldar_r2.py`: copia incremental del bucket a disco con manifiesto SHA-256.
- Frontend rediseñado (sin glassmorphism ni GSAP; `<dialog>` nativo; tokens en
  `globals.css`). Se eliminaron el XML fabricado en el navegador, el envío por correo (ruta
  pública a direcciones inventadas) y la ficha SUNAT con datos fijos. El lote ZIP incluye
  solo archivos reales + manifiesto.

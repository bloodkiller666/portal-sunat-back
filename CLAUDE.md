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

Backend (`uv run pytest -q` → 414 passed; `uv run ruff check` → limpio):

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
4. Migraciones `0001`–`0007` (las 0006 y 0007 ya están en el Supabase actual).
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

### P7 — Filtros server-side y paginación real (solo si la empresa crece)

Hoy el frontend carga hasta 1000 comprobantes y filtra en memoria. Si hace
falta más: ampliar `q` en el backend (razones sociales + orden de compra, hoy
solo `nombre_archivo` en `app/db/repositories/postgres.py`), añadir filtros
`n_sap`/`check_sap` a `FiltrosComprobantes` + repos + fakes + tests, y
paginación con contador en el frontend.

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
uv run pytest -q      # debe terminar sin fallos (414+ passed)
uv run ruff check     # All checks passed

cd ../factura-sunat-main
pnpm lint             # 0 errores
pnpm build            # compilación y tipos OK
```

Antes de commitear: `git status` no debe listar `.env`, `secrets/` ni binarios
de Node (`node.exe`, `pnpm.exe`, etc. — ya ignorados).

## 6. Datos útiles

- Migraciones aplicadas en el Supabase actual: `0001`–`0007`. La conexión vive
  en `DATABASE_URL` del `.env` local.
- El CDR de una compra (`origen=recibido`) no se sincroniza: lo informa el
  proveedor (decisión de diseño ya implementada).
- `billConsultService` solo existe en producción (también se usa en beta para
  consultas de solo lectura: decisión documentada en el README).
- Comunicación de errores SUNAT → estados: `app/sunat/states.py` (0→Aprobado,
  98/1033/0011/0127→Pendiente, resto→Rechazado).

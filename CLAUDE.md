# Guía para Claude Code — cómo terminar el Portal de Facturación SUNAT

Documento de handoff: contexto, estado real del proyecto y trabajo pendiente
para llegar a producción. Léelo completo antes de escribir código.

## 1. Qué es este proyecto

Portal multiempresa de comprobantes electrónicos peruanos (SUNAT), dividido en
dos repositorios:

| Repositorio | Ruta local | Despliegue |
| --- | --- | --- |
| **Backend** (este repo) | `factura-sunat-main-back` | Docker (por desplegar) |
| **Frontend** | `C:\Users\mcotaquispe\Downloads\Node.js\factura-sunat-main` | Vercel (ya hosteado) |

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

Backend (`uv run pytest -q` → 326 passed; `uv run ruff check` → limpio):

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

### P1 — Desplegar el backend

El `Dockerfile` y CI (`.github/workflows/ci.yml`) ya existen. Falta:

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
4. Aplicar migraciones `0001`–`0005`
   (`uv run python -m scripts.aplicar_migraciones`).
5. Rotar cualquier credencial que haya circulado por chat/correo antes del
   primer despliegue.

### P2 — Prueba end-to-end frontend ↔ backend

1. Backend local: `uv run fastapi dev`. Frontend local: `pnpm dev` con
   `.env.local` apuntando a `http://127.0.0.1:8000`.
2. Flujo a validar: login → empresas → listado → detalle con ítems →
   descargar PDF → descargar XML → importar XML → sincronizar SUNAT.
3. Ajustar `CORS_ORIGINS` si el navegador bloquea (el error es claro).

### P3 — Persistir el "Check SAP" (única marca que se pierde al recargar)

- Backend: `PATCH /api/v1/comprobantes/{id}` con body `{ "check_sap": bool }`
  (y opcionalmente `n_sap`), rol admin/emisor, actualizar columna y devolver
  `ComprobanteOut`. `check_sap`/`n_sap` ya son columnas de `comprobantes`.
  Pruebas en `tests/test_api_comprobantes.py`.
- Frontend: `onAlternarCheckSap` en `app/page.tsx` debe llamar al endpoint
  (hoy solo cambia estado local) con manejo de error 403 para `lector`.

### P4 — Fecha de vencimiento y forma de pago reales

El frontend muestra esos columnas con valores por defecto (`lib/api.ts`,
`mapComprobante`). Completarlos de verdad:

1. Parser UBL: `cac:Payment/cbc:PaymentDueDate` (vencimiento) y
   `cac:PaymentTerms/cbc:PaymentMeansID` o `cbc:ID` (forma de pago: Contado/
   Crédito con días) — tolerante a ausencia.
2. Migración `0006_comprobante_pago.sql`: `fecha_vencimiento date`,
   `forma_pago text` en `comprobantes` (+ actualizar `tests/test_migraciones.py`
   si valida columnas).
3. Guardarlos en `import_comprobante_xml` y exponerlos en `ComprobanteOut`
   (`app/services/results.py`) y `to_comprobante_out`.
4. Frontend: mapear en `mapComprobante` (ya tiene los campos).

### P5 — Diagnóstico SIRE (confirmar que la empresa está acogida)

Las credenciales API ya están en el `.env`
(`SUNAT_CRED_20514831620_CLIENT_ID/CLIENT_SECRET`) y el SOL user también.

1. Crear `scripts/sire_probe.py` (mismo estilo que `scripts/sunat_probe.py`):
   - Pedir token a `https://api-seguridad.sunat.gob.pe/v1/clientes_extranet/{ruc}/{client_id}/oauth2/token`
     con `grant_type=client_credentials`... (revisar el manual oficial del API
     SIRE: el token se genera con client_id + client_secret + RUC + usuario SOL
     + clave SOL; sigue exactamente lo que diga el manual v31/v28).
   - Llamar "Descargar propuesta" RVIE de un periodo (ej. 09-2026) y reportar:
     200/ticket → acogida; error "no incorporado" → no acogida.
2. **No loguear** el client_secret ni la clave SOL; reutilizar `redact`.

### P6 — Cliente SIRE (fase 3: poblar el portal con las ventas reales)

Depende de P5 (empresa acogida). Servicios RVIE: descargar propuesta →
aceptar/reemplazar → registrar preliminar; el flujo de descarga es asíncrono
con **tickets** (pedir → consultar estado → descargar ZIP particionado).
Guardar los registros en `comprobantes` con `origen=emitido` idempotente
(única por ambiente ya garantiza no duplicar). Diseñar con límites de tamaño
(similar a `paquetes.py`). Todo esto es backend; el frontend ya pinta los datos.

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
uv run pytest -q      # debe terminar sin fallos (326+ passed)
uv run ruff check     # All checks passed

cd ../factura-sunat-main
pnpm lint             # 0 errores
pnpm build            # compilación y tipos OK
```

Antes de commitear: `git status` no debe listar `.env`, `secrets/` ni binarios
de Node (`node.exe`, `pnpm.exe`, etc. — ya ignorados).

## 6. Datos útiles

- Migraciones aplicadas en el Supabase actual: `0001`–`0005`. La conexión vive
  en `DATABASE_URL` del `.env` local.
- El CDR de una compra (`origen=recibido`) no se sincroniza: lo informa el
  proveedor (decisión de diseño ya implementada).
- `billConsultService` solo existe en producción (también se usa en beta para
  consultas de solo lectura: decisión documentada en el README).
- Comunicación de errores SUNAT → estados: `app/sunat/states.py` (0→Aprobado,
  98/1033/0011/0127→Pendiente, resto→Rechazado).

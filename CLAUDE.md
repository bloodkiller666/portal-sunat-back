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
  Postgres con RLS), zeep (SOAP SUNAT), aioboto3 (Cloudflare R2), signxml. Tests: pytest + respx (SUNAT siempre mockeado).
- **Stack frontend**: Next.js 16 (App Router) + React 19 + Tailwind v4 + GSAP,
  `@supabase/supabase-js` para el login. Gestor: pnpm (nunca npm).
- **Empresa principal**: TDM ASFALTOS S.A.C., RUC `20514831620`. Sus
  credenciales (SOL, certificado, CLIENT_ID/CLIENT_SECRET de API SUNAT) están
  en el `.env` local — **nunca en el repo** (`.gitignore` ya lo cubre).

`README.md` de este repo documenta cada endpoint con ejemplos curl: es la
referencia de la API.

## 2. Estado actual (lo que YA funciona — no lo rehagas)

Backend (`uv run pytest -q` → 586 passed; `uv run ruff check` → limpio):

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
- **Sin PDF**: el portal no genera ni sirve PDF (se retiró el endpoint, el visor y el botón;
  `fpdf2` ya no es dependencia). Quien necesite el PDF lo pide al OSE.
- **Archivos en R2**: XML importado y CDR recibido se archivan (bucket
  privado) y se descargan con URL firmada (`POST /archivos/{id}/download-url`).
- **Respaldo desde Bizlinks (OSE)**: completa el XML firmado y el CDR que falten,
  por la API `invoker` o (opt-in) por el portal anónimo. Ver P9 abajo.
- **Firma XML** (`SunatXmlSigner`, XMLDSig) implementada y probada.
- **Auth**: JWT de Supabase (HS256 o ES256/RS256 vía JWKS) + RLS por empresa +
  roles `admin/emisor/lector`.

Frontend (ya conectado al backend):

- Login Supabase, selector multiempresa con rol, listado real con filtros y
  KPIs, detalle con ítems reales, descargas reales (XML/CDR
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
4. Migraciones `0001`–`0009` (las 0006, 0007, 0008 y 0009 ya están en el Supabase actual).
   `scripts.aplicar_migraciones` solo sirve para una base **vacía** (reaplica
   todo); en una base existente aplique únicamente las nuevas.
5. Rotar cualquier credencial que haya circulado por chat/correo antes del
   primer despliegue.

### P2 — Prueba end-to-end frontend ↔ backend ✅ HECHO (local)

Backend `uv run fastapi run --port 8000` con `CORS_ORIGINS=http://localhost:3000`;
frontend `pnpm dev` con `.env.local`. Validado con Edge headless (Playwright) y
por API: login → empresas → listado (249 comprobantes) → detalle → importar XML (archivado en R2) → sincronizar SIRE.
Falta repetirlo contra el dominio real tras desplegar (ver CORS de R2 abajo).

### P3 — N° SAP persistente ✅ HECHO (el Check SAP se retiró el 2026-10-19)

`PATCH /api/v1/comprobantes/{id}` (solo `n_sap`, admin/emisor; el
`lector` recibe 403). El N° SAP ya no se calcula ni se simula: entra por el Excel
(`/import-sap`, ver P10).

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
- `getStatusCdr` devuelve `0013` («no pertenece a GEM») para facturas emitidas por otro
  sistema aunque SIRE las liste: no hay CDR por ese canal y NO es un rechazo. Un
  Aprobado nunca se degrada por una consulta (`_siguiente_estado`).
- No se implementa aceptar/reemplazar/registrar preliminar: el portal NO escribe
  en SIRE.

### Respaldo de XML y CDR desde Bizlinks (OSE) ✅ HECHO (2026-10-19)

TDM emite a través de Bizlinks, que conserva el XML firmado y el CDR; SUNAT no los entrega
por API (`getStatusCdr` → `0013`). `POST /companies/{ruc}/bizlinks/respaldos/sync`
completa lo que falte y lo archiva en R2 (idempotente; lo no encontrado no se reintenta en
24 h). Dos canales con `BIZLINKS_CANAL`:

- `api` (por defecto): web service `invoker` del OSE. Requiere `BIZLINKS_SERVICE_URL` y
  `SUNAT_CRED_<RUC>_BIZLINKS_USER/_PASSWORD`. Sin eso responde 409.
- `portal` (opt-in): automatiza la consulta anónima de
  `https://sfe.bizlinks.com.pe/sfeperu/public/loginAnonimo.jsf` (JSF/PrimeFaces con
  `ViewState`): formulario → POST ajax del `loginForm` → `btnDescargarXML` / `btnDescargarCDR`
  → `descargarZip`. No necesita credenciales: usa los datos del comprobante que ya están
  en la base. **Verificado en vivo** con FF01-15979 y FF04-600 de Supabase: XML firmado (11
  KB) y CDR con código `0` idénticos a los del navegador.

Decisiones (no las revierta sin motivo):

- El portal es opt-in a propósito. Límites: pausa aleatoria entre peticiones, tope por día
  (`BIZLINKS_PORTAL_MAX_POR_DIA`, 50) y por llamada (5), cortacircuito de 24 h tras 403/429/
  CAPTCHA o cambio de página (`BizlinksBlockedError` / `BizlinksPortalChangedError`). Nunca se
  intenta saltar un bloqueo.
- Su servidor solo negocia TLS 1.2 con `AES256-SHA`; `BIZLINKS_PORTAL_TLS_COMPAT=true` baja el
  nivel de seguridad **solo en ese cliente** (el certificado se sigue verificando).
- Bitácora `bizlinks_consultas` (migración `0009`, ya aplicada): topes, cortacircuito y
  auditoría. No guarda credenciales ni contenido.
- Un fallo de R2 tras una consulta exitosa se registra como `error`/`storage_error`, **no**
  como `sin_resultado`: así se reintenta en la siguiente llamada.
- `POST /companies/{ruc}/sire/ventas/sync` acepta `"traer_respaldos": true`: registra las
  ventas y luego completa las nuevas. Un fallo de Bizlinks no invalida la sincronización
  (`respaldos_error`).
- Diagnóstico de un solo comprobante, sin escribir nada:
  `uv run python -m scripts.bizlinks_probe --serie FF04 --numero 600 --guardar`
  (los archivos van a `var/`, ignorado por git).

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
sobre documento/cliente/RUC/OC, `n_sap`, `vencimiento_desde/hasta`, `moneda`, `respaldo`, `orden`,
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

### P10 — Cambios del 2026-10-19 (vencimiento, N° SAP por Excel, sin PDF)

- Filtro por fecha de vencimiento (`vencimiento_desde/hasta`): un comprobante sin vencimiento usa su
  entra en ningún rango. Los indicadores (`/resumen`) siguen dependiendo solo de la emisión.
- `POST /companies/{ruc}/comprobantes/import-sap` (`app/services/sap.py`): el Excel de Contabilidad
  trae documento (serie-número) + N° SAP (+ RUC opcional). Una sentencia SQL con `unnest` actualiza
  el lote; lo que no está en el portal o es ilegible se cuenta (`no_encontradas`, `invalidas`) y
  nunca da error. Un RUC solo no identifica un documento: no se admite como llave.
- Check SAP retirado de la API y de la interfaz (la columna `check_sap` sigue en la base, sin uso).
- PDF retirado (endpoint, generador, visor, botón y `fpdf2`). `tiene_pdf`/`con_pdf` siguen en la
  API por compatibilidad (los PDF ya archivados no se borran), pero la interfaz no los muestra.
- Respaldo desde Bizlinks por el portal anónimo re-verificado en vivo el 2026-10-19 con una
  factura real de octubre (FF01-16077): XML firmado (13 KB) y CDR código 0. Se habilita con
  `BIZLINKS_CANAL=portal` (sigue siendo opt-in, con tope diario y cortacircuito).

### P8 — Opcional / posterior

- Azure Key Vault: implementar `app/secrets/keyvault.py` (interfaz lista).
- Emisión real (`sendBill` con `sendSummary`/`getTicket`): la firma XML ya
  está; falta el flujo SOAP de envío. Requiere certificado `.pfx` con su
  contraseña (`CERT_PASSWORD` hoy vacía en el `.env` local) y activar
  `CONFIRM_PROD_EMISSION`.
- Correo SMTP del backend: hoy el frontend usa Resend directo; el stub
  `SmtpMailSender` puede quedarse así.

### P9 — Respaldo de XML y CDR desde Bizlinks ✅ HECHO (2026-10-19)

Detalle en «Respaldo de XML y CDR desde Bizlinks» más abajo. Pendiente único: las
credenciales `R2_*` del `.env` local son de prueba (el access key no tiene 32 caracteres y R2
responde `InvalidArgument`), así que la prueba en vivo confirmó la descarga y la validación
del XML/CDR pero el archivado en R2 requiere claves reales; el servicio ya registra ese caso
como `storage_error` reintentable.

## 5. Verificación (Definition of Done de cualquier cambio)

```bash
cd factura-sunat-main-back
uv run pytest -q      # debe terminar sin fallos (577+ passed)
uv run ruff check     # All checks passed

cd ../factura-sunat-main
pnpm lint             # 0 errores
pnpm test             # pruebas unitarias de lib/ (vitest)
pnpm build            # compilación y tipos OK
```

Antes de commitear: `git status` no debe listar `.env`, `secrets/` ni binarios
de Node (`node.exe`, `pnpm.exe`, etc. — ya ignorados).

## 6. Datos útiles

- Migraciones aplicadas en el Supabase actual: `0001`–`0009`. La conexión vive
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

### P11 — Periodo SIRE ya cargado se rechaza (2026-10-20)

`POST /companies/{ruc}/sire/ventas/sync` responde 409 `periodo_ya_cargado` (antes de llamar a
SUNAT) si la empresa ya tiene comprobantes `fuente=sire` emitidos en ese mes. `"forzar": true`
lo permite (sigue siendo idempotente: solo agrega lo nuevo). Retomar un `ticket` no se valida.
Bizlinks ya omitía lo que tiene XML y CDR (las boletas no exigen CDR).

### P13 — Carpeta local `Descargas/AAAA/MM/RUC/SERIE-NÚMERO` (2026-10-20)

El frontend guarda el XML y el CDR reales en la PC (File System Access API, Chrome/Edge; el
usuario elige su Descargas una vez). `POST /carpeta-local/abrir` (`{"partes": [año, mes,
RUC, SERIE-NÚMERO]}`) abre esa carpeta en el Explorador, pero SOLO si el backend corre en la misma
PC (`LOCAL_ABRIR_CARPETA=true`; base `LOCAL_DESCARGAS_DIR`, por defecto `~/Downloads`). No toca la
base de datos (la apertura es inmediata): solo valida el JWT, limpia cada parte de separadores y
caracteres reservados, la confina a la carpeta base y abre únicamente una carpeta que ya exista.
Desplegado en un servidor responde 409 y el portal copia la ruta al portapapeles.

### P14 — Compras a proveedores desde SIRE (RCE) (2026-10-21)

Sección «Compras» separada de «Ventas» (el frontend manda `origen=emitido|recibido`; el estado
`FiltrosState.origen` es la sección, no un filtro «limpiable»). `POST /companies/{ruc}/sire/
compras/sync` reutiliza el flujo de ventas (`_sincronizar` en `app/services/sire.py`, con un
`_Libro` por registro) y `SireClient` con `libro="rce"`. Verificado contra el manual SIRE
Compras v28 (§5.34) y con una descarga real de TDM (202609: 589 filas, 80 columnas, UTF-8 sin
BOM): libro `080000`; tickets y descarga son los mismos servicios `rvierce` con `codLibro`.

Decisiones (no las revierta sin motivo):

- En compras el emisor es el proveedor; solo se guardan proveedores con RUC (tipo doc `6`), porque
  `ruc_emisor` exige 11 dígitos. El tipo 50 (declaración aduanera) y similares se omiten.
- Migración `0010`: `ruc_emisor` entra en la llave única (en 202609 hay 4 casos de la misma
  serie-número de proveedores distintos). Sigue siendo *constraint* porque el insert usa
  `on conflict on constraint`.
- Migración `0011`/`0012`: `tipo_cambio`, `monto_pen`, `emisor_tipo_doc`, `receptor_tipo_doc`.
  SIRE da `Total CP` en SOLES: `monto_total` = total / tipo de cambio (comprobado: FF02-00000610,
  68 411,09 / 3,402 = 20 109,08 USD, que es lo que Bizlinks reconoce). Rollbacks en
  `supabase/rollback/`.
- **El código nuevo selecciona las columnas de `0011`/`0012`: aplique las migraciones en Supabase
  ANTES de desplegar el backend** (solo las nuevas; nunca `aplicar_migraciones` en una base con datos).
- Bizlinks no aplica a compras (su consulta usa siempre el RUC de TDM como emisor).
- Las 116 ventas USD ya cargadas se corrigen con `scripts/corregir_montos_sire.py` (simula por
  defecto; relee los ZIP de evidencia de R2; requiere `0011` aplicada).

### P12 — El bucket guarda un documento por carpeta (2026-10-20)

`app/services/store.py` arma la clave como
`comprobantes/{RUC}/{AÑO}/{MES}/{SERIE}-{NÚMERO}/{archivo}`, así que el XML y el CDR del mismo
comprobante quedan juntos (`FF01-00016077/…xml` y `…-CDR.zip`). El RUC se conserva delante
porque el bucket es compartido entre empresas. Las evidencias de SIRE siguen aparte
(`sire/{RUC}/{PERIODO}/`). `archivos.storage_key` guarda la ruta de cada archivo, de modo que
lo ya archivado se sigue descargando igual.

`scripts/reorganizar_r2.py` lleva los objetos viejos (los que estaban en la carpeta del mes) al
esquema nuevo: simula por defecto, con `--aplicar` copia y actualiza `archivos.storage_key`, y
`--borrar-originales` elimina la ruta vieja solo después. Es idempotente.

# Portal de Facturación Electrónica — Backend (SUNAT)

Backend del portal multiempresa de comprobantes electrónicos del grupo. Se conecta
**directamente** a los servicios web oficiales de SUNAT (SOAP `billService` /
`billConsultService`), sin intermediarios ni scraping.

* **Stack:** Python 3.12, FastAPI, Pydantic v2, asyncpg, zeep (SOAP), lxml, httpx,
  pytest, ruff. Gestor de dependencias: **uv**.
* **Base de datos:** Supabase (Postgres) con Row Level Security por empresa.
* **Autenticación:** JWT de Supabase Auth; todos los endpoints salvo `/health` lo exigen.

## Alcance de esta fase (fase 1)

| Punto | Estado |
| --- | --- |
| A. Esqueleto, configuración tipada, logging y manejo de errores | Hecho |
| B. Migraciones SQL de Supabase + RLS por empresa y rol | Hecho |
| C. Cliente SOAP con WS-Security: consulta de estado y CDR | Hecho |
| D. Mapeo de estados SUNAT → frontend (Aprobado/Rechazado/Pendiente/Anulado) | Hecho |
| E. Endpoints (`/health`, empresas, `sunat/status`, `refresh-cdr`, listar/importar comprobantes) | Hecho |
| F. CORS configurable y OpenAPI en `/docs` | Hecho |
| G. README de puesta en marcha y pruebas | Hecho |

**Fuera de alcance (interfaces preparadas, sin implementar):** emisión real,
SAP, SIRE, guías de remisión y envío por correo. Ver `app/integrations/`.
La **firma XML** (`SunatXmlSigner`) ya está implementada. El portal **no genera PDF**
(se retiró el endpoint `GET /comprobantes/{id}/pdf` y `fpdf2`).

## Estructura

```
app/
  main.py                 # app factory, CORS, routers, manejadores de error
  core/                   # config (pydantic-settings), logging + redacción, JWT, errores
  api/  deps.py routers/  # dependencias y endpoints
  services/               # lógica de negocio (empresas, SUNAT, comprobantes, emisión)
  sunat/                  # endpoints oficiales, cliente SOAP, CDR, mapeo de estados
  db/                     # pool asyncpg con RLS, modelos y repositorios
  secrets/                # SecretsProvider (env) + stub de Azure Key Vault + certificado
  storage/                # Cloudflare R2 (bucket privado) con URLs firmadas
  integrations/           # firma XML (implementada), SAP y correo (stubs)
supabase/migrations/      # 0001 esquema, 0002 funciones/triggers, 0003 RLS, 0004 archivos
Dockerfile                # imagen de producción
.github/workflows/ci.yml  # lint + pruebas + build de la imagen
tests/                    # pruebas; SUNAT siempre mockeado
```

## Requisitos

* [uv](https://docs.astral.sh/uv/) (instala y gestiona Python 3.12 automáticamente).

## Puesta en marcha

```bash
uv sync                        # crea .venv e instala dependencias + Python 3.12
cp .env.example .env           # completa los valores (el .env nunca se versiona)
```

### 1. Configuración

Todas las variables están documentadas y comentadas en `.env.example`. Las más
importantes:

| Variable | Descripción |
| --- | --- |
| `SUNAT_ENV` | `beta` (por defecto) o `prod`. Controla a qué SUNAT se apunta. |
| `CONFIRM_PROD_EMISSION` | `true` habilita la **emisión** cuando `SUNAT_ENV=prod`. Sin esto, la emisión se bloquea con 403. |
| `SUPABASE_URL`, `SUPABASE_JWT_SECRET` | Verificación del JWT de Supabase (HS256). Para proyectos nuevos con signing keys asimétricas: `SUPABASE_JWT_ALG=ES256` y `SUPABASE_JWT_JWKS_URL` (o se deriva de `SUPABASE_URL`). |
| `DATABASE_URL` | Conexión Postgres de Supabase (usar el puerto directo 5432 o el pooler en modo sesión). |
| `CORS_ORIGINS` | Lista separada por comas o JSON. En `staging`/`prod` no se admite `*`. |
| `SUNAT_CRED_{RUC}_*` | Credenciales por empresa: `SOL_USER`, `SOL_PASSWORD`, `CERT_PATH`, `CERT_PASSWORD` (y `CLIENT_ID`/`CLIENT_SECRET` para la fase REST). |
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME` | Almacenamiento de archivos en Cloudflare R2 (bucket **privado**). `R2_SIGNED_URL_EXPIRES_SECONDS` controla la vigencia de las URLs firmadas. |

### 2. Credenciales por empresa

Nada de esto vive en el repositorio:

```bash
# .env (ignorado por git)
SUNAT_CRED_20123456789_SOL_USER=MIUSUARIO
SUNAT_CRED_20123456789_SOL_PASSWORD=...
SUNAT_CRED_20123456789_CERT_PATH=secrets/20123456789.pfx
SUNAT_CRED_20123456789_CERT_PASSWORD=...
```

El certificado `.pfx` se guarda en `secrets/` (también ignorado por git). El
usuario que viaja a SUNAT es siempre `RUC + usuario SOL` (ej. `20123456789MIUSUARIO`).

Para cambiar a Azure Key Vault más adelante solo hay que implementar
`app/secrets/keyvault.py`; la interfaz `SecretsProvider` no cambia.

## Migraciones de Supabase

Las migraciones están en `supabase/migrations/`. En el proyecto actual ya están
aplicadas (`0001`–`0004`). Para un proyecto nuevo, este paso es tuyo:

1. Crea el proyecto en [supabase.com](https://supabase.com) y anota la
   `DATABASE_URL`, `SUPABASE_URL` y el JWT secret.
2. Aplica las migraciones en orden:

```bash
# Opción A: Supabase CLI
supabase link --project-ref <project-ref>
supabase db push

# Opción B: psql directo
psql "$DATABASE_URL" -f supabase/migrations/0001_schema.sql
psql "$DATABASE_URL" -f supabase/migrations/0002_functions_triggers.sql
psql "$DATABASE_URL" -f supabase/migrations/0003_rls.sql
```

3. Da de alta la primera empresa y tu usuario. Con RLS activo, `authenticated`
   no puede crear empresas: hazlo con SQL directo o con `service_role`.

```sql
insert into public.companies (ruc, razon_social)
values ('20123456789', 'ACME SAC');

insert into public.user_companies (user_id, company_id, rol)
select auth_users.id, c.id, 'admin'
from auth.users auth_users, public.companies c
where auth_users.email = 'usuario@empresa.pe' and c.ruc = '20123456789';
```

Tablas: `companies`, `user_companies` (roles `admin`/`emisor`/`lector`),
`comprobantes`, `comprobante_items`, `sunat_envios` (bitácora de cada intento) y
`archivos` (referencia al objeto en Cloudflare R2). Los helpers `app.is_company_member` y
`app.has_company_role` son `security definer` para que las políticas de
`user_companies` no entren en recursión.

## Correr la API

```bash
uv run fastapi dev          # http://127.0.0.1:8000  (recarga automática)
uv run fastapi dev --port 8123
```

* Documentación interactiva: <http://127.0.0.1:8000/docs>
* Salud: `GET /health` (público, no toca base de datos ni SUNAT).

## Probar cada endpoint

### Obtener un token de Supabase

El backend no emite tokens: los emite Supabase Auth. Con un usuario ya creado:

```bash
TOKEN=$(curl -s -X POST "https://<project-ref>.supabase.co/auth/v1/token?grant_type=password" \
  -H "apikey: <anon-key>" -H "Content-Type: application/json" \
  -d '{"email":"usuario@empresa.pe","password":"<clave>"}' | python -c "import json,sys;print(json.load(sys.stdin)['access_token'])")
```

### `/health`

```bash
curl -s http://127.0.0.1:8000/health
# {"status":"ok","sunat_env":"beta","database_configured":true,"auth_configured":true,...}
```

### `GET /api/v1/companies`

```bash
curl -s http://127.0.0.1:8000/api/v1/companies -H "Authorization: Bearer $TOKEN"
# {"items":[{"ruc":"20123456789","razon_social":"ACME SAC","rol":"emisor",...}],"total":1}
```

Devuelve solo las empresas a las que pertenece el usuario (RLS + verificación en
la aplicación). Sin token: `401`.

### `GET /api/v1/companies/{ruc}/sunat/status`

Prueba de conexión y credenciales. Requiere rol `admin` o `emisor`.

```bash
curl -s "http://127.0.0.1:8000/api/v1/companies/20123456789/sunat/status" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{
  "ruc": "20123456789",
  "sunat_env": "beta",
  "estado": "autenticado",
  "autenticado": true,
  "metodo": "getStatusAR",
  "endpoint": "https://e-beta.sunat.gob.pe/ol-ti-itcpfegem-beta/billService",
  "detalle": {"codigo": "0", "mensaje": "..."},
  "credenciales": {
    "sol_user_configurado": true,
    "api_rest_configurada": false,
    "certificado": {"configurado": true, "abre_con_clave": true, "vigente": true, "dias_para_vencer": 420}
  },
  "latencia_ms": 210,
  "intento_id": 1
}
```

`estado` puede ser `autenticado`, `credenciales_invalidas`, `servicio_no_disponible`
o `sin_credenciales`. **No se devuelven la clave SOL, el usuario SOL ni el
contenido del certificado**: solo si existe, si abre con la clave y su vigencia.

Parámetros opcionales de la sonda (comprobante de prueba): no configurable por
query; se define con `SUNAT_PROBE_TIPO`, `SUNAT_PROBE_SERIE`, `SUNAT_PROBE_NUMERO`.

### `POST /api/v1/comprobantes/{id}/refresh-cdr`

Consulta SUNAT, actualiza el estado en la base y registra el intento. Requiere rol
`admin` o `emisor`.

```bash
curl -s -X POST "http://127.0.0.1:8000/api/v1/comprobantes/$ID/refresh-cdr" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{
  "estado_anterior": "pendiente",
  "estado_nuevo": "aprobado",
  "estado_label": "Aprobado",
  "cambio_estado": true,
  "cdr": {"codigo": "0", "descripcion": "La Factura numero F001-00000001, ha sido aceptada", "notas": []},
  "sunat_env": "beta",
  "intento": {"id": 2, "tipo_operacion": "consulta_cdr", "exito": true, "duracion_ms": 180}
}
```

Comportamiento ante errores:

| Situación | Respuesta |
| --- | --- |
| SUNAT aún no conoce el comprobante | `200` con estado `Pendiente` y el mensaje de SUNAT |
| SUNAT caído o timeout | `503 sunat_unavailable` (el intento queda registrado) |
| Credenciales rechazadas | `502 sunat_auth_error` |
| Comprobante de otra empresa / inexistente | `404` |
| Rol `lector` | `403` |

Una consulta **nunca revierte** un comprobante `Anulado`.

### `GET /api/v1/companies/{ruc}/comprobantes`

Lista paginada de los comprobantes de la empresa. Cualquier miembro (incluido
`lector`) puede consultarla.

```bash
curl -s "http://127.0.0.1:8000/api/v1/companies/20123456789/comprobantes?estado=aprobado&limit=50&offset=0" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{
  "items": [{
    "id": "…", "tipo_documento": "01", "serie": "F001", "correlativo": 1,
    "numero": "00000001", "nombre_archivo": "20123456789-01-F001-00000001",
    "estado": "aprobado", "estado_label": "Aprobado", "monto_total": "118.00",
    "fecha_emision": "2026-10-05", "moneda": "PEN", "sunat_ambiente": "beta"
  }],
  "limit": 50,
  "offset": 0,
  "has_more": false,
  "total": 1
}
```

Todo lo hace el servidor (filtrar, ordenar, paginar y contar): el cliente pide una página y
nunca descarga el conjunto completo.

| Parámetro | Qué hace |
| --- | --- |
| `estado` | `pendiente`, `aprobado`, `rechazado` o `anulado` |
| `fecha_desde`, `fecha_hasta` | Rango de fecha de emisión (invertido → `422`) |
| `tipo_documento` | `01`, `03`, `07`, `08` |
| `serie`, `moneda` | Serie exacta; `PEN`, `USD`… |
| `q` | Búsqueda parcial (mín. 2 caracteres) por N° de documento, cliente, RUC u orden de compra |
| `n_sap` | N° SAP (contiene) |
| `vencimiento_desde`, `vencimiento_hasta` | Rango de fecha de vencimiento (invertido → `422`); sin vencimiento informado (p. ej. al contado) rige la fecha de emisión |
| `respaldo` | `sin_xml`, `sin_cdr` o `completo` (XML y CDR archivados) |
| `orden`, `direccion` | `fecha` (por defecto), `numero`, `cliente` o `importe`; `asc` o `desc`. Las notas de crédito ordenan como negativas |
| `limit`, `offset` | Página (1–100, por defecto 50) |
| `contar` | `true` (por defecto) incluye `total`; pídelo solo en la 1.ª página o al cambiar de filtro, no en cada página |

```bash
curl -s "http://127.0.0.1:8000/api/v1/companies/20123456789/comprobantes\
?estado=pendiente&fecha_desde=2026-10-01&fecha_hasta=2026-10-31&tipo_documento=01&orden=importe&direccion=desc" \
  -H "Authorization: Bearer $TOKEN"
```

### `GET /api/v1/companies/{ruc}/comprobantes/resumen`

Indicadores del conjunto filtrado, calculados en la base (acepta los mismos filtros que el
listado; el portal solo envía el rango de fechas). Cualquier miembro puede consultarlo.

```json
{
  "total": 720,
  "por_estado": {"pendiente": 1, "aprobado": 686, "rechazado": 0, "anulado": 33},
  "facturado_neto": {"PEN": "44224155.32", "USD": "2971065.88"},
  "cobertura": {"exigibles": 687, "con_xml": 0, "con_cdr": 0, "con_pdf": 3}
}
```

`facturado_neto` resta las notas de crédito y excluye anulados y rechazados; `cobertura` se
mide sobre lo exigible (todo menos los anulados). `monto_total` de cada comprobante es el
**importe total con IGV** (en SIRE, `Total CP`: se verificó que cuadra con la suma de sus
componentes en los 494 registros de prueba).

#### Rendimiento a gran escala

Medido con 150 000 comprobantes sintéticos en una transacción revertida (migración
`0008_rendimiento_listado.sql`): una página pasó de ~3,7 s a ~2 ms, el total (`count`) de
~3 s a ~60 ms y el resumen tarda ~110 ms. Lo que lo logra: una política RLS que resuelve las
empresas del usuario una vez por consulta (no una vez por fila), un índice que cubre el orden
por defecto y las columnas `tiene_xml/tiene_cdr/tiene_pdf` que mantiene un trigger de
`archivos`. Cada petición además gasta menos viajes a la base (claims en una sola consulta,
empresa y membresía en una, respaldos junto con la página): con la base remota cada viaje
cuesta ~300 ms. Límite conocido: la búsqueda parcial (`q`) recorre las filas de la empresa
(~1 s con 150 000); bajo RLS Postgres no puede usar índices trigram con `ilike`.
Para deshacer la migración: `supabase/rollback/0008_rendimiento_listado.down.sql`.

### `POST /api/v1/companies/{ruc}/comprobantes/refresh`

Sincroniza de una vez el estado en SUNAT de los comprobantes **pendientes** de la
empresa (admin/emisor). Es el botón "Actualizar todo" del panel.

```bash
curl -s -X POST "http://127.0.0.1:8000/api/v1/companies/20123456789/comprobantes/refresh?limit=25" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{
  "ruc": "20123456789", "total": 3, "procesados": 3, "con_cambio": 2, "fallidos": 0,
  "detenido_por": null,
  "items": [{"numero": "20123456789-01-F001-00000001", "estado_anterior": "pendiente",
             "estado_nuevo": "aprobado", "cambio_estado": true, "ok": true, "cdr_codigo": "0"}],
  "consultado_at": "2026-10-14T15:04:05Z"
}
```

Un fallo de infraestructura (**SUNAT caído**, **credenciales rechazadas**) **detiene**
el lote y se reporta en `detenido_por`; los comprobantes restantes no se consultan
para no encadenar timeouts. Un error puntual de un comprobante se registra en
`items[].error_code` y el lote continúa.

### `POST /api/v1/companies/{ruc}/comprobantes/import`

Importa un comprobante desde su **XML UBL 2.1** (factura, boleta o nota de
crédito/débito). Requiere rol `admin` o `emisor`.

```bash
curl -s -X POST "http://127.0.0.1:8000/api/v1/companies/20123456789/comprobantes/import" \
  -H "Authorization: Bearer $TOKEN" \
  -F "archivo=@factura.xml;type=application/xml"
```

Responde `201` con el comprobante creado (estado inicial `pendiente`, ambiente
según `SUNAT_ENV`). El RUC del emisor del XML debe coincidir con el de la
empresa de la ruta; un XML repetido devuelve `409 conflict` y un XML inválido
`422 invalid_document`. El XML original se archiva en el bucket privado si R2
está configurado (si no, la importación responde igual y el fallo queda en el log).

### `GET /api/v1/comprobantes/{id}`

Detalle completo de un comprobante: cabecera + líneas del detalle (`items`),
extraídas del XML UBL al importarlo y guardadas en `comprobante_items`.
Cualquier miembro (incluido `lector`).

```bash
curl -s "http://127.0.0.1:8000/api/v1/comprobantes/$ID" -H "Authorization: Bearer $TOKEN"
```

```json
{
  "id": "…", "tipo_documento": "01", "serie": "F001", "numero": "00000001",
  "razon_social_emisor": "ACME SAC", "razon_social_receptor": "CLIENTE EJEMPLO S.A.C.",
  "monto_total": "188.80", "estado": "pendiente", "estado_label": "Pendiente",
  "items": [{
    "item_nro": 1, "codigo": "PROD-01", "descripcion": "Cemento Portland Tipo I",
    "cantidad": "2.00", "unidad_medida": "NIU", "valor_unitario": "50.000000",
    "precio_unitario": "59.000000", "igv": "18.00", "tip_afectacion_igv": "10",
    "total": "100.00"
  }]
}
```

### `GET /api/v1/comprobantes/{id}/archivos`

Lista los archivos almacenados del comprobante (XML importado, CDR recibido). Cualquier miembro, incluido `lector`.

```bash
curl -s "http://127.0.0.1:8000/api/v1/comprobantes/$ID/archivos" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{
  "comprobante_id": "…",
  "items": [{
    "id": "…", "tipo": "xml", "nombre": "20123456789-01-F001-00000001.xml",
    "mime_type": "application/xml", "size_bytes": 1432, "hash_sha256": "…",
    "estado": "subido", "subido_at": "2026-10-14T15:04:05Z"
  }],
  "total": 1
}
```

### `POST /api/v1/archivos/{id}/download-url`

El bucket es privado: la descarga se hace con una URL firmada temporal (por
defecto 15 minutos, `R2_SIGNED_URL_EXPIRES_SECONDS`). Sin credenciales de R2 la
respuesta es `503 storage_not_configured`.

```bash
curl -s -X POST "http://127.0.0.1:8000/api/v1/archivos/$ARCHIVO_ID/download-url" \
  -H "Authorization: Bearer $TOKEN"
# {"url":"https://<bucket>.r2.cloudflarestorage.com/...?X-Amz-Signature=...","expires_in":900,...}
```

### `GET /api/v1/comprobantes/{id}/envios`

Bitácora de trazabilidad: cada intento contra SUNAT de ese comprobante (código,
mensaje, latencia, error). Sin tramas SOAP y sin credenciales.

```bash
curl -s "http://127.0.0.1:8000/api/v1/comprobantes/$ID/envios?limit=20" \
  -H "Authorization: Bearer $TOKEN"
# {"items":[{"id":1,"tipo_operacion":"consulta_cdr","exito":true,"codigo":"0",...}],"total":1}
```

### `POST /api/v1/companies/{ruc}/comprobantes/import-cdr`

Carga masiva de **CDR** (constancias de recepción) desde un ZIP. Es la vía para los
comprobantes que emite otro sistema (OSE, PSE, SAP): SUNAT responde `0013` a la consulta
`getStatusCdr` y no entrega su CDR, así que hay que traerlo de quien los envió.

Cada archivo debe llamarse como lo entrega SUNAT, `R-{RUC}-{tipo}-{serie}-{número}`
(`.zip` o `.xml`). Se asocia al comprobante **emitido** de la empresa, se archiva en R2
con su SHA-256 y actualiza el estado (un Aprobado nunca se degrada). Un CDR que ya existe
no se pisa; uno con problemas no detiene el resto. Solo `admin`/`emisor`.

```bash
curl -s -X POST "http://127.0.0.1:8000/api/v1/companies/$RUC/comprobantes/import-cdr" \
  -H "Authorization: Bearer $TOKEN" -F "paquete=@cdrs.zip"
# {"total":3,"asociados":2,"ya_tenian":0,"sin_comprobante":1,"con_error":0,"items":[...]}
```

El listado y el detalle de comprobantes traen además `tiene_xml`, `tiene_cdr` y
`tiene_pdf`: qué respaldos hay archivados para cada uno.

### `POST /api/v1/companies/{ruc}/bizlinks/respaldos/sync`

Recupera desde **Bizlinks** (el OSE que firma y declara los comprobantes de la empresa)
el XML firmado y el CDR que falten de los comprobantes emitidos, y los archiva en R2. Es
idempotente: lo ya archivado no se vuelve a pedir, y lo que no se encontró no se reintenta
en 24 h. El XML se valida contra el comprobante (RUC, tipo, serie y correlativo) y, si el
comprobante vino de SIRE, carga sus ítems. Solo `admin`/`emisor`.

```bash
curl -s -X POST "http://127.0.0.1:8000/api/v1/companies/$RUC/bizlinks/respaldos/sync?limit=5" \
  -H "Authorization: Bearer $TOKEN"
# {"canal":"portal","revisados":1,"completos":1,"xml_guardados":1,"cdr_guardados":1,
#  "cuota_restante":49,"items":[{"numero":"20514831620-01-FF04-00000600",...}]}
```

Hay dos canales (`BIZLINKS_CANAL`):

| Canal | Qué es | Requiere |
| --- | --- | --- |
| `api` (por defecto) | Web service `invoker` del OSE | `BIZLINKS_SERVICE_URL` y `SUNAT_CRED_<RUC>_BIZLINKS_USER/_PASSWORD` |
| `portal` | Consulta anónima del portal público `loginAnonimo.jsf` | Nada (los datos del comprobante ya están en la base) |

El canal `portal` automatiza la misma consulta que haría una persona en
<https://sfe.bizlinks.com.pe/sfeperu/public/loginAnonimo.jsf> y es **opt-in**. Se aplican
límites para que la empresa no parezca un scraping masivo: pausa aleatoria entre
peticiones, tope por día y por llamada, cortacircuito de 24 h si el portal responde
403/429/CAPTCHA o cambia de forma (nunca se intenta saltar un bloqueo). Cada intento queda
en `bizlinks_consultas` (migración `0009`).

Diagnóstico de un solo comprobante, sin escribir nada:

```bash
uv run python -m scripts.bizlinks_probe --serie FF04 --numero 600 --guardar
```

La sincronización de SIRE puede pedirlo en la misma llamada con `"traer_respaldos": true`:
registra las ventas y luego completa las que acaban de entrar; un fallo de Bizlinks no
invalida la sincronización (se informa en `respaldos_error`).

### `GET /api/v1/companies/{ruc}/sire/evidencias`

Lista los ZIP originales de SUNAT que cada sincronización de SIRE archivó (evidencia de
auditoría, con su hash). Se descargan con `POST /archivos/{id}/download-url`.

### Distribución del bucket

Cada comprobante tiene su propia carpeta, con el XML y el CDR dentro:

```
comprobantes/{RUC}/{AÑO}/{MES}/{SERIE}-{NÚMERO}/
    {RUC}-{TIPO}-{SERIE}-{NÚMERO}.xml      # XML firmado (importado o recuperado del OSE)
    {RUC}-{TIPO}-{SERIE}-{NÚMERO}-CDR.zip  # CDR de SUNAT
```

Por ejemplo, la factura `FF01-16077` de septiembre de 2026 queda en
`comprobantes/20514831620/2026/09/FF01-00016077/`. El RUC va delante porque el bucket es
compartido entre empresas. Las evidencias de SIRE van aparte, `sire/{RUC}/{PERIODO}/`, y
las claves se guardan en `archivos.storage_key`: cambiar la distribución no rompe lo ya
archivado (cada fila conserva su ruta y su URL firmada).

Los archivos que ya estaban en la carpeta del mes se llevan a la carpeta del documento con:

```bash
uv run python -m scripts.reorganizar_r2                        # solo muestra el plan
uv run python -m scripts.reorganizar_r2 --aplicar              # copia y corrige la base
uv run python -m scripts.reorganizar_r2 --aplicar --borrar-originales
```

Copia cada objeto y después actualiza `archivos.storage_key`; los originales no se borran
salvo que se pida, y solo cuando la copia y la fila ya están en su sitio. Es idempotente.

### Respaldo del bucket

```bash
uv run python -m scripts.respaldar_r2 --destino D:\respaldos\portal-r2
```

Descarga de forma incremental lo que falte del bucket y deja un `manifiesto.csv` con el
SHA-256 de cada archivo. Es de solo lectura sobre R2; conviene programarlo (Programador de
tareas de Windows o cron) como copia de seguridad de la evidencia.

### `PATCH /api/v1/comprobantes/{id}`

Persiste el N° SAP de un comprobante. Solo `admin`/`emisor`; el `lector` recibe 403.
`"n_sap": ""` lo limpia. (El Check SAP se retiró: el N° SAP lo dice todo.)

```bash
curl -s -X PATCH "http://127.0.0.1:8000/api/v1/comprobantes/$ID" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"n_sap": "4500926001"}'
```

### `POST /api/v1/companies/{ruc}/comprobantes/import-sap`

Importación masiva de N° SAP desde el Excel de Contabilidad (el frontend lee el `.xlsx` y
envía las filas). Cada fila trae el **documento** (`F001-123`, `f001-00000123` y similares) y
el **N° SAP**; el **RUC** es opcional y afina el emparejamiento (debe ser el del cliente o el
emisor del comprobante). Solo `admin`/`emisor`; hasta 20 000 filas por llamada.

```bash
curl -s -X POST "http://127.0.0.1:8000/api/v1/companies/20514831620/comprobantes/import-sap" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"filas": [{"documento": "F001-123", "ruc": "20100000001", "n_sap": "4500000001"},
                 {"documento": "B001-45", "n_sap": "4500000002"}]}'
# {"ruc": "...", "recibidas": 2, "actualizadas": 1, "no_encontradas": 1, "invalidas": 0}
```

Lo que **no está en el portal** o no se puede leer (sin documento o sin N° SAP) se omite y se
cuenta: nunca da error ni detiene la importación. Una sola sentencia SQL actualiza todo el lote.

### `POST /api/v1/companies/{ruc}/sire/ventas/sync`

Trae del SIRE de SUNAT (propuesta RVIE) las ventas reales de un periodo y las
registra como comprobantes (`origen=emitido`, `fuente=sire`, ambiente `prod`).
Solo lectura hacia SUNAT, idempotente y solo `admin`/`emisor`. Requiere
`SUNAT_CRED_<RUC>_CLIENT_ID/CLIENT_SECRET` (Credenciales de API SUNAT).

```bash
curl -s -X POST "http://127.0.0.1:8000/api/v1/companies/$RUC/sire/ventas/sync" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"periodo": "202609"}'
# {"periodo":"202609","ticket":"20260300000078","estado":"completado",
#  "total_filas":247,"nuevos":247,"ya_existian":0,"anulados":13,"omitidos":{},...}
```

Si SUNAT todavía genera el archivo responde `"estado":"en_proceso"` con el
`ticket`; repita la llamada enviando `"ticket"` para retomarla sin pedir otra
propuesta. Estados: `completado`, `en_proceso`, `sin_datos`. Diagnóstico previo:
`uv run python -m scripts.sire_probe --ruc $RUC --periodo 202609`.

## Conectar el frontend

El frontend nunca habla con SUNAT: habla con esta API con un JWT de Supabase.

### 1. Habilitar CORS

```bash
# .env — el origen exacto del SPA, sin barra final (varios separados por coma)
CORS_ORIGINS=http://localhost:5173
```

En `staging`/`prod` no se admite `*`. El preflight `OPTIONS` se responde y la
respuesta expone `X-Request-Id` para correlacionar con los logs.

### 2. Obtener el token (Supabase Auth)

El login lo maneja **Supabase**, no esta API: el backend solo **verifica** el JWT.

```ts
import { createClient } from "@supabase/supabase-js";

const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY);
const { data } = await supabase.auth.signInWithPassword({ email, password });
const token = data.session?.access_token; // va como Bearer en cada request
```

El backend exige `aud=authenticated` y verifica con `SUPABASE_JWT_SECRET` (HS256) o
el JWKS del proyecto (ES256/RS256, según `SUPABASE_JWT_ALG`). Si el token expira,
supabase-js lo refresca solo (`getSession` / `onAuthStateChange`).

### 3. Llamar a la API

```ts
const BASE = "http://127.0.0.1:8000";

async function api(ruta: string, token: string, init: RequestInit = {}) {
  const res = await fetch(`${BASE}${ruta}`, {
    ...init,
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
  });
  const cuerpo = res.status === 204 ? null : await res.json();
  if (!res.ok) throw new Error(cuerpo?.error?.message ?? res.statusText);
  return cuerpo;
}
```

| Necesidad del frontend | Llamada |
| --- | --- |
| ¿El backend está vivo? | `GET /health` (público, sin token) |
| Selector de empresa y rol del usuario | `GET /api/v1/companies` |
| Botón "Probar conexión SUNAT" (admin/emisor) | `GET /api/v1/companies/{ruc}/sunat/status` |
| Botón "Actualizar estado/CDR" (admin/emisor) | `POST /api/v1/comprobantes/{id}/refresh-cdr` |
| Listado de comprobantes (cualquier rol) | `GET /api/v1/companies/{ruc}/comprobantes` |
| Modal de detalle con ítems (cualquier rol) | `GET /api/v1/comprobantes/{id}` |
| Botón "Actualizar pendientes" (admin/emisor) | `POST /api/v1/companies/{ruc}/comprobantes/refresh` |
| Importar XML UBL (admin/emisor) | `POST /api/v1/companies/{ruc}/comprobantes/import` |
| Importar N° SAP desde Excel (admin/emisor) | `POST /api/v1/companies/{ruc}/comprobantes/import-sap` |
| Importar lote ZIP de XML (admin/emisor) | `POST /api/v1/companies/{ruc}/comprobantes/import-paquete` |
| Archivos del comprobante (cualquier rol) | `GET /api/v1/comprobantes/{id}/archivos` |
| Botón "Descargar XML/CDR" (cualquier rol) | `POST /api/v1/archivos/{id}/download-url` |
| Historial de intentos SUNAT (cualquier rol) | `GET /api/v1/comprobantes/{id}/envios` |
| ¿La instancia está lista? (infraestructura) | `GET /health/ready` |

```ts
const { items } = await api("/api/v1/companies", token);
const estado = await api("/api/v1/companies/20123456789/sunat/status", token);
const sync = await api(`/api/v1/comprobantes/${id}/refresh-cdr`, token, { method: "POST" });
```

El rol `lector` recibe `403` en los dos últimos: muestre esos botones solo si
`rol` es `admin` o `emisor`.

### 4. Formato de error (uno solo para todo)

```json
{ "error": { "code": "forbidden", "message": "...", "details": {}, "request_id": "..." } }
```

| HTTP | `code` | Qué mostrar |
| --- | --- | --- |
| `401` | `unauthenticated` | Redirigir al login |
| `403` | `forbidden` | "Su rol no permite esta acción" (`details.rol`) |
| `404` | `not_found` | "El comprobante no existe o no es de sus empresas" |
| `409` | `credentials_not_configured` | "La empresa no tiene credenciales SUNAT" |
| `502` | `sunat_auth_error` | "Credenciales SUNAT inválidas" |
| `503` | `auth_not_configured` / `database_not_configured` | Problema de configuración del backend |
| `503` | `sunat_unavailable` | "SUNAT no responde; reintente" |

### 5. Estados listos para pintar (punto D)

El backend entrega `estado` en minúsculas para la lógica y `estado_label` para la
UI: `pendiente`/Pendiente, `aprobado`/Aprobado, `rechazado`/Rechazado,
`anulado`/Anulado.

### 6. Sin esto el frontend recibirá `503`

- `SUPABASE_JWT_SECRET` (o JWKS) — sin él, **todo** endpoint autenticado da
  `503 auth_not_configured`.
- `DATABASE_URL` — sin ella, los endpoints de datos dan `503 database_not_configured`.
- Credenciales `SUNAT_CRED_<RUC>_*` de la empresa — sin ellas, `sunat/status`
  reporta `sin_credenciales`.
- Migraciones aplicadas y una fila en `companies` + `user_companies` para el usuario.

## Pruebas y calidad

```bash
uv run pytest          # sin red y sin Postgres
uv run ruff check
```

Diagnóstico manual adicional: `scripts/sunat_probe.py` (credenciales/servicios
SUNAT), `scripts/aplicar_migraciones.py` (aplica migraciones) y
`scripts/e2e_supabase.py` (humo end-to-end contra Supabase real, con limpieza).

Las llamadas a SUNAT se mockean con `respx` y un WSDL de prueba
(`tests/fixtures/wsdl/`); el CDR se construye en memoria desde
`tests/fixtures/cdr/`. La base de datos se reemplaza por repositorios en memoria
que además emulan RLS (`tests/fakes.py`).

## Despliegue

El backend se despliega como **contenedor** (`Dockerfile` en la raíz) y no guarda
nada en disco: todo el estado vive en Supabase y en el bucket privado de R2, así
que se puede escalar con réplicas sin sesiones pegajosas.

```bash
docker build -t portal-facturacion .
docker run --env-file .env -p 8000:8000 portal-facturacion
```

`PORT` (por defecto 8000) y `WEB_CONCURRENCY` (por defecto 1) los inyecta la
plataforma. Cada worker es un proceso independiente; las llamadas SOAP de SUNAT
corren en un pool de hilos, por lo que un worker atiende varias consultas a la
vez sin bloquearse.

### En un VPS con Docker Compose (HTTPS automático)

`docker-compose.yml` levanta la API y **Caddy** delante (certificado Let's Encrypt
automático). La API no publica puertos; solo Caddy expone 80/443. Corre con el
sistema de archivos de solo lectura, sin capacidades de Linux y con 512 MB de tope.

```bash
# 1. DNS: api.midominio.com -> IP del VPS (puertos 80 y 443 abiertos)
git clone https://github.com/bloodkiller666/portal-sunat-back.git && cd portal-sunat-back
cp .env.prod.example .env.prod      # complételo; NUNCA se versiona
export API_DOMAIN=api.midominio.com # o póngalo en un archivo .env junto al compose
docker compose up -d --build
curl https://api.midominio.com/health
```

Luego, en Vercel: `NEXT_PUBLIC_API_URL=https://api.midominio.com` y volver a
desplegar el frontend. `CORS_ORIGINS` debe ser el dominio exacto del frontend.
Para actualizar: `git pull && docker compose up -d --build`. Las migraciones SQL
**no** se aplican solas: en una base existente aplique solo las nuevas de
`supabase/migrations/`.

Verificado en local con esta misma configuración (`ENVIRONMENT=prod`): health y
ready, docs apagados, CORS solo para el dominio del frontend, JWT ES256, listado,
PDF, consulta SOAP real a SUNAT y sincronización SIRE a través del proxy. El caché
de WSDL va a `/tmp` (`SUNAT_WSDL_DIR`) porque el resto del sistema es de solo lectura.

### Sondas de salud

| Ruta | Uso | Qué comprueba |
| --- | --- | --- |
| `GET /health` | Liveness (healthcheck del contenedor) | Solo que el proceso responde. No toca Postgres: un problema de base no reinicia el contenedor. |
| `GET /health/ready` | Readiness (tráfico de la plataforma) | `select 1` a Postgres y que la verificación de JWT esté configurada. `503` mientras no esté listo. |

### Checklist antes de desplegar

1. **Migraciones aplicadas** (`0001`–`0004`): `supabase db push` o
   `uv run python scripts/aplicar_migraciones.py`.
2. **Variables de entorno de producción** completas (ver tabla abajo).
3. **Empresas y usuarios** dados de alta en `companies` / `user_companies`.
4. **Credenciales SOL y certificados** cargados como variables de entorno o en
   el proveedor de secretos de la plataforma; nunca en la imagen.
5. **`CONFIRM_PROD_EMISSION`** decidido a conciencia: con `SUNAT_ENV=prod` la
   emisión queda bloqueada (403) hasta activarlo.
6. **Rotar** cualquier credencial que haya circulado por chat, correo o capturas
   (clave SOL, `SUPABASE_JWT_SECRET`, tokens de R2) **antes** del primer despliegue.

### Variables críticas en producción

| Variable | Valor recomendado | Por qué |
| --- | --- | --- |
| `ENVIRONMENT` | `prod` | Activa las validaciones de coherencia (CORS sin `*`, etc.). |
| `SUNAT_ENV` | `prod` | Apunta a los servicios reales de SUNAT. |
| `CONFIRM_PROD_EMISSION` | `false` hasta validar | Candado de la emisión real (regla 4). |
| `DATABASE_URL` | pooler de Supabase (modo sesión) | Sin ella, todo endpoint con datos responde `503`. |
| `SUPABASE_URL` + `SUPABASE_JWT_ALG`/`SUPABASE_JWT_JWKS_URL` | signing keys asimétricas (`ES256`) | Verificación del JWT sin compartir el secreto simétrico. |
| `CORS_ORIGINS` | dominio exacto del SPA | En `staging`/`prod` no se admite `*`. |
| `LOG_JSON` | `true` | Logs de una línea listos para el agregador. |
| `DOCS_ENABLED` | `false` | No publicar `/docs` ni `/openapi.json` sin token. |
| `SUNAT_VERIFY_SSL` | `true` | Validar el certificado de los servicios de SUNAT. |
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME` | bucket privado | Sin esto, el archivado de XML/CDR y las URLs firmadas responden `503` (la importación y el refresco siguen funcionando). |

Al arrancar, la aplicación registra `WARNING` por cada configuración faltante o
peligrosa (por ejemplo `DOCS_ENABLED=true` en un entorno de producción), así que
un `docker run` en falso muestra en el log lo que falta.

## Seguridad

| Regla | Cómo se cumple |
| --- | --- |
| 1. Sin secretos en el repo | `.env` y `secrets/` ignorados; `.env.example` sin valores; ninguna columna de credenciales en la base. |
| 2. No loguear secretos | `app/core/redaction.py`: filtro de logging + registro de valores conocidos; las tramas SOAP se guardan sin el header `wsse:Security` (`app/sunat/xmlutils.py`). |
| 3. Credenciales por RUC | `SecretsProvider` con `EnvSecretsProvider`; stub de Azure Key Vault listo para reemplazarlo. |
| 4. `SUNAT_ENV` | `beta` por defecto; `ensure_emission_allowed` bloquea la emisión en `prod` salvo `CONFIRM_PROD_EMISSION=true`. |
| 5. Solo servicios oficiales | URLs de `cpe.sunat.gob.pe` / `orientacion.sunat.gob.pe` en `app/sunat/endpoints.py`; sin scraping. |
| 6. Documentación oficial | URLs y parámetros tomados de la documentación vigente; lo dudoso queda marcado como pregunta abierta. |
| 7. JWT + pertenencia | `JWT` de Supabase + verificación de membresía y rol; además RLS en Postgres. |
| 8. Pruebas con SUNAT mockeado | `tests/` (ver arriba). |

## Integración SUNAT (lo que hay que saber)

URLs oficiales usadas (ver `app/sunat/endpoints.py`):

| Servicio | Beta | Producción |
| --- | --- | --- |
| Facturas/boletas (`billService`) | `https://e-beta.sunat.gob.pe/ol-ti-itcpfegem-beta/billService` | `https://e-factura.sunat.gob.pe/ol-ti-itcpfegem/billService` |
| Consulta de estado/CDR (`billConsultService`) | **no existe**: se usa la URL de producción (solo lectura) | `https://e-factura.sunat.gob.pe/ol-it-wsconscpegem/billConsultService` |
| Validez (`billValidService`) | no se usa | `https://e-factura.sunat.gob.pe/ol-it-wsconsvalidcpe/billValidService` |

Autenticación: WS-Security `UsernameToken` con `Username = RUC + usuario SOL` y
`PasswordText` con la clave SOL.

Consecuencia importante: `billConsultService` solo está publicado en producción,
así que el cliente apunta a esa URL **también cuando `SUNAT_ENV=beta`**. Es una
decisión del proyecto: consultar el estado/CDR es una operación de **solo lectura**
que no emite ni altera comprobantes tributarios. Como el *endpoint* es de
producción, requiere credenciales SOL de producción; las de beta no autentican
contra producción. Para probar credenciales en beta, `/sunat/status` sigue usando
`billService.getStatusAR(ruc, tipo, serie, numero)` del beta, que sí es el ambiente de la empresa.

> La URL `https://e-factura.sunat.gob.pe/ol-it-wswebconsvalidcpe/billConsultService`
> **no existe**: responde `404 Not Found` (verificado el 2026-10-07). El segmento
> correcto del servicio de consulta es **`ol-it-wsconscpegem`**, cuyo WSDL se declara
> con `name="billConsultService"` y expone `getStatus` y `getStatusCdr`.

Parámetros de `getStatusCdr`: `rucComprobante`, `tipoComprobante`,
`serieComprobante` y `numeroComprobante` (**entero**). El WSDL real **no** declara
`montoTotal`, así que no se envía (confirmado contra producción el 2026-10-13).

`SUNAT_WSDL_DIR` permite guardar los WSDL en disco (evita descargarlos en cada
arranque y hace que las pruebas no toquen la red).

## Mapeo de estados (punto D)

| Código SUNAT | Significado | Estado del frontend |
| --- | --- | --- |
| `0` | Procesó correctamente | Aprobado |
| `98` | En proceso | Pendiente |
| Otro, con mensaje "no existe" / "en proceso" / "no ha sido informado" | SUNAT todavía no tiene el comprobante | Pendiente |
| Resto de códigos | Rechazo / error de proceso | Rechazado |
| Baja aceptada (comunicación de baja) | Fuera de alcance (fase posterior) | Anulado |

El mapeo vive en `app/sunat/states.py` y está cubierto por pruebas.

## Limitaciones conocidas y dudas abiertas

1. **`/sunat/status` y `refresh-cdr` verificados de punta a punta** con una cuenta
   SOL real en producción (`getStatus`→`0011`, `getStatusCdr`→`0127`). Beta solo
   acepta el usuario demo `MODDATOS`, así que una cuenta real no autentica ahí.
2. **Migraciones aplicadas** (`0001`–`0004`) contra Supabase; además se validan de
   forma estática en `tests/test_migraciones.py`.
3. **Consulta de CDR en beta (decisión tomada).** `billConsultService` solo existe
   en producción, así que el cliente usa esa URL también en beta (solo lectura).
   El `/sunat/status` de una empresa en beta se sigue validando contra el
   `billService` de beta. Consecuencia práctica: para que `refresh-cdr` funcione en
   beta hacen falta credenciales SOL **de producción**.
4. **`getStatusCdr` (corregido con respuesta real).** Se envían `rucComprobante`,
   `tipoComprobante`, `serieComprobante` y `numeroComprobante` (entero). `montoTotal`
   **no** se envía: el WSDL real de producción no lo declara.
5. **`Anulado`** se resuelve con el flujo de comunicación de baja, que pertenece a
   la fase de emisión.
6. **Azure Key Vault, SAP y correo** siguen como interfaces con
   `NotImplementedError`. El **PDF** (`SunatPdfRenderer`) y la **firma XML**
   (`SunatXmlSigner`) ya están implementados.

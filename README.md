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
| E. Endpoints (`/health`, empresas, `sunat/status`, `refresh-cdr`) | Hecho |
| F. CORS configurable y OpenAPI en `/docs` | Hecho |
| G. README de puesta en marcha y pruebas | Hecho |

**Fuera de alcance (interfaces preparadas, sin implementar):** emisión real,
firma del XML (`signxml`), PDF, SharePoint/Microsoft Graph, SAP, SIRE, guías de
remisión y envío por correo. Ver `app/integrations/`.

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
  integrations/           # interfaces pendientes (SharePoint, PDF, SAP, correo, firma)
supabase/migrations/      # 0001 esquema, 0002 funciones/triggers, 0003 RLS
tests/                    # 155 pruebas; SUNAT siempre mockeado
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

Las migraciones están en `supabase/migrations/`. Aún **no se han ejecutado** (el
proyecto Supabase todavía no existe), así que este paso es tuyo:

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
`archivos` (referencia a SharePoint). Los helpers `app.is_company_member` y
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
  "metodo": "getStatus",
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
uv run pytest          # 164 pruebas, sin red y sin Postgres
uv run ruff check
```

Las llamadas a SUNAT se mockean con `respx` y un WSDL de prueba
(`tests/fixtures/wsdl/`); el CDR se construye en memoria desde
`tests/fixtures/cdr/`. La base de datos se reemplaza por repositorios en memoria
que además emulan RLS (`tests/fakes.py`).

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
`billService.getStatus(fileName)` del beta, que sí es el ambiente de la empresa.

> La URL `https://e-factura.sunat.gob.pe/ol-it-wswebconsvalidcpe/billConsultService`
> **no existe**: responde `404 Not Found` (verificado el 2026-10-07). El segmento
> correcto del servicio de consulta es **`ol-it-wsconscpegem`**, cuyo WSDL se declara
> con `name="billConsultService"` y expone `getStatus` y `getStatusCdr`.

Parámetros de `getStatusCdr` (estándar UBL 2.1 de SUNAT): `rucComprobante`,
`tipoComprobante`, `serieComprobante` y `numeroComprobante`; `montoTotal` solo se
envía si está disponible.

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

1. **`/sunat/status` con credenciales reales no está verificado de punta a punta.**
   Se probó con SUNAT mockeado (firma, WS-Security, tramas y fallos). Falta una
   prueba manual con un usuario SOL secundario de beta.
2. **Las migraciones no se han ejecutado** contra un Postgres real (el proyecto
   Supabase aún no existe). Se validan de forma estática en `tests/test_migraciones.py`.
3. **Consulta de CDR en beta (decisión tomada).** `billConsultService` solo existe
   en producción, así que el cliente usa esa URL también en beta (solo lectura).
   El `/sunat/status` de una empresa en beta se sigue validando contra el
   `billService` de beta. Consecuencia práctica: para que `refresh-cdr` funcione en
   beta hacen falta credenciales SOL **de producción**.
4. **Parámetros de `getStatusCdr` (decisión tomada).** Se envían los 4 parámetros
   estándar y `montoTotal` solo si existe. Falta confirmarlo con una respuesta real
   de SUNAT.
5. **`Anulado`** se resuelve con el flujo de comunicación de baja, que pertenece a
   la fase de emisión.
6. **Azure Key Vault, SharePoint, SAP, PDF, correo y firma** están como interfaces
   con `NotImplementedError`.

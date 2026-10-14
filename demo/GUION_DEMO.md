# Guion de demostración (sin frontend)

Todo lo que se muestra sale del backend real: FastAPI + Supabase (Postgres con
RLS) + SUNAT (servicios SOAP de consulta). No hay SPA, así que se usa la página
`demo/index.html` como cliente y los scripts de consola.

Tres actos, ~10 minutos en total:

| Acto | Qué demuestra | Duración |
| --- | --- | --- |
| 1. Página de demo | Flujo completo con resultados en pantalla | 4 min |
| 2. Scripts de consola | Credenciales SUNAT y prueba e2e con salida real | 3 min |
| 3. Pruebas y calidad | Suite automática y linter | 2 min |

---

## Antes de empezar (una sola vez, con antelación)

### 1. Datos que necesitas a mano

- **RUC de la empresa demo**: lo da `GET /companies` (en este proyecto: `20514831620`).
- **Usuario y contraseña** de un usuario de Supabase Auth que sea miembro de esa
  empresa (p. ej. `demo@empresa.pe`, rol `admin`).
- **UUID del usuario** (Dashboard → Authentication → Users).
- **Anon key** del proyecto (Dashboard → Project Settings → API → `anon public`).

### 2. `.env` (nunca se versiona)

```bash
DATABASE_URL=postgresql://...        # cadena de Supabase
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_JWT_ALG=ES256               # o HS256 + SUPABASE_JWT_SECRET
CORS_ORIGINS=http://localhost:5173   # el origen donde sirves la demo
SUNAT_ENV=beta
SUNAT_CRED_<RUC>_SOL_USER=...
SUNAT_CRED_<RUC>_SOL_PASSWORD=...
```

### 3. Migraciones aplicadas

```bash
uv run python -m scripts.aplicar_migraciones            # aplica 0001..0004
uv run python -m scripts.aplicar_migraciones --desde 4  # solo una migración
```

### 4. Comprobación previa (hazla la noche antes)

```bash
uv run pytest -q          # 261 passed
uv run ruff check         # All checks passed
```

---

## Acto 1 — Página de demo

### Arrancar los dos procesos (dos terminales)

```bash
# Terminal A: la API
uv run fastapi dev                 # http://127.0.0.1:8000

# Terminal B: sirve la página (origen http://localhost:5173)
uv run python -m http.server 5173 --directory demo
```

Abre <http://localhost:5173>.

### Guion de clics

1. **Backend**: deja `http://127.0.0.1:8000`. Comenta que debe estar en
   `CORS_ORIGINS`; si no, el navegador bloquea las llamadas (CORS).
2. **Supabase (login)**: completa `SUPABASE_URL`, la **anon key**, el email y la
   contraseña. Pulsa **Iniciar sesión**. Aparece: *Sesión iniciada como ... (<uuid>)*.
   > Punto clave: el backend **no** emite tokens; solo **verifica** el JWT de
   > Supabase. Nunca ve la contraseña.
3. Pulsa **Ejecutar demostración**. Se ejecutan en cadena y se pintan con código
   de estado, milisegundos y JSON:
   - `GET /health` → 200, público, sin token.
   - `GET /api/v1/companies` → 200, empresas del usuario y su rol.
   - `GET /api/v1/companies/{ruc}/sunat/status` → 200, **credenciales SUNAT
     validadas de verdad** (`autenticado: true`).
   - `GET /api/v1/companies/{ruc}/comprobantes` → 200, listado.
   - `POST /api/v1/comprobantes/{id}/refresh-cdr` → 200, estado anterior → nuevo.
   - `POST /api/v1/companies/{ruc}/comprobantes/import` → **201**, factura UBL
     creada (la página ajusta el RUC y usa el correlativo siguiente).
4. Enseña los dos o tres JSON desplegables: fíjate en que **nunca aparecen la
   clave SOL, el usuario SOL ni las tramas SOAP** (están redactados).
5. **Momentos "wow" opcionales**:
   - Vuelve a pulsar **Ejecutar demostración**: el import usa otro correlativo,
     así que vuelve a dar 201. En la colección de Postman, reenviar el **mismo**
     XML da **409** (unicidad por empresa y ambiente).
   - Cambia a un usuario con rol `lector`: `sunat/status`, `refresh-cdr` e
     `import` responden **403**. Se ve la autorización por rol.
   - Deja `SUPABASE_URL` vacío y pulsa **Iniciar sesión**: error claro de login.

### Plan B si falla algo

- **Sin internet o SUNAT caído**: el paso SUNAT puede dar `503`; explica que el
  intento queda registrado y el comprobante no se rompe. Sigue con el resto.
  `refresh-cdr` de un comprobante que SUNAT aún no conoce deja el estado en
  *Pendiente*, no falla.
- **CORS**: si todo da "Error de red/CORS", sirve la página desde otro puerto y
  añádelo a `CORS_ORIGINS`, o usa `--directory demo` en el 5173.
- **Login**: si el proyecto exige confirmar correo, confirma el usuario antes.

---

## Acto 2 — Scripts de consola (salida real)

```bash
# 1) ¿Las credenciales SOL autentican en beta y/o prod?
uv run python -m scripts.sunat_probe --ruc <RUC> --env both

# 2) Consulta un comprobante real y muestra el estado que vería el frontend
uv run python -m scripts.sunat_probe --ruc <RUC> --env prod \
    --tipo 01 --serie F001 --numero 1

# 3) Prueba de humo end-to-end contra Supabase + SUNAT (limpia su dato al final)
uv run python -m scripts.e2e_supabase --ruc <RUC> --user-id <UUID>
uv run python -m scripts.e2e_supabase --ruc <RUC> --user-id <UUID> --skip-sunat
```

Salida esperada del 3 (exit 0):

```
[OK ] GET /health
[OK ] GET /companies -> 1 empresa(s)
[OK ] empresa esperada presente
[OK ] GET comprobantes -> 1 item(s)
[OK ] GET sunat/status -> autenticado
[OK ] POST refresh-cdr -> pendiente
[OK ] POST import UBL -> 201
[OK ] import duplicado -> 409
RESULTADO: TODO OK
```

---

## Acto 3 — Pruebas y calidad

```bash
uv run pytest -q       # 261 passed: SUNAT mockeado con respx, sin red ni Postgres
uv run ruff check      # All checks passed
```

Enseña `http://127.0.0.1:8000/docs` (OpenAPI) para cerrar: cada endpoint está
documentado y se puede probar con el botón **Authorize** pegando el token.

---

## Colección de Postman (alternativa)

1. Importa `demo/Portal.postman_collection.json` y
   `demo/Portal.postman_environment.json`.
2. Selecciona el environment **Portal (local)** y completa `supabase_url`,
   `supabase_anon_key`, `email` y `password`.
3. Ejecuta las peticiones en orden `0 → 9`: el login guarda el token, `Companies`
   guarda el RUC, el listado guarda el id y `Archivos` guarda el id del archivo.
4. Para el paso 6, el **RUC del XML** debe ser el de la empresa; ajusta
   `xml_path` a un archivo con ese RUC (la página web lo hace sola, Postman no).

---

## Cierre: qué queda fuera (decirlo explícitamente)

- Emisión, firma XML, PDF, correo y guías: fase posterior.
- **Anulado** requiere el flujo de comunicación de baja (fase de emisión).
- Verificación TLS de Supabase con CA propio: pendiente (`prod-ca-2021.crt`).
- Pendiente tuyo: rotar la **clave SOL** que estuvo expuesta en el chat (el
  `.env.example` versionado ya quedó con valores de ejemplo).

## Novedades que puedes lucir (si alcanza el tiempo)

- **Descarga con URL firmada**: el XML importado y el CDR recibido se archivan en
  el bucket privado; `POST /api/v1/archivos/{id}/download-url` devuelve una URL
  temporal (15 min).
- **Trazabilidad**: `GET /api/v1/comprobantes/{id}/envios` muestra cada intento
  contra SUNAT con su código, latencia y error.
- **Actualizar pendientes**: `POST /api/v1/companies/{ruc}/comprobantes/refresh`
  sincroniza todos los pendientes de la empresa de una sola vez y se detiene si
  SUNAT cae.
- **Filtros** en el listado: `estado`, `fecha_desde`, `fecha_hasta`,
  `tipo_documento`, `serie` y `q`.
- **Readiness**: `/health/ready` verifica la base de datos y la configuración de
  JWT (es el healthcheck del contenedor).
- **Despliegue**: `Dockerfile` + CI en `.github/workflows/ci.yml` (lint, pruebas y
  build de la imagen).

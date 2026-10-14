# Imagen de producción del backend del Portal de Facturación Electrónica.
# Build:  docker build -t portal-facturacion .
# Run:    docker run --env-file .env -p 8000:8000 portal-facturacion
FROM python:3.12-slim

# uv (gestor de dependencias del proyecto) copiado desde su imagen oficial.
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # El entorno virtual del proyecto vive en el Python del sistema: `uvicorn`
    # queda en el PATH y se evita una copia del intérprete.
    UV_PROJECT_ENVIRONMENT=/usr/local \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Capa de dependencias: cambia solo cuando cambian pyproject.toml / uv.lock.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Código de la aplicación (tests, demo y WSDL locales quedan fuera por .dockerignore).
COPY app ./app
COPY supabase ./supabase
COPY scripts ./scripts

# Usuario sin privilegios: el contenedor no necesita escribir en disco.
RUN useradd --create-home --uid 10001 portal && chown -R portal:portal /app
USER portal

EXPOSE 8000

# Liveness: /health no toca la base de datos, así que un problema de Postgres no
# reinicia el contenedor. El readiness (/health/ready) lo usa la plataforma.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

# Un proceso por contenedor; se escala con réplicas. PORT lo inyectan las PaaS.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-1} --proxy-headers --forwarded-allow-ips='*'"]

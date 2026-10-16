# Arranca el backend FastAPI en localhost (sin Docker, sin administrador).
#
#   .\start-local.ps1              # http://127.0.0.1:8000
#   .\start-local.ps1 -Port 8001   # si el 8000 esta ocupado
#
# Requisitos: uv instalado (https://astral.sh/uv) y el .env en esta carpeta.
# Comprobacion: abre http://127.0.0.1:8000/health (debe responder JSON de estado).
param(
    [int]$Port = 8000
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path .env)) { throw "Falta el archivo .env en $PSScriptRoot" }

Write-Host "Instalando/verificando dependencias (uv sync)..."
uv sync
if ($LASTEXITCODE -ne 0) { throw "uv sync fallo." }

Write-Host ""
Write-Host "============================================================"
Write-Host " Backend local:  http://127.0.0.1:$Port"
Write-Host " Salud:          http://127.0.0.1:$Port/health"
Write-Host " Docs:           http://127.0.0.1:$Port/docs"
Write-Host " Para apagar:    Ctrl+C"
Write-Host "============================================================"
Write-Host ""

uv run fastapi run --host 127.0.0.1 --port $Port

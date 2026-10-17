# Enciende el backend en Docker y lo expone con HTTPS para el frontend de Vercel.
#
#   .\demo-start.ps1                      # tunel Cloudflare (URL nueva en cada inicio)
#   .\demo-start.ps1 -Ngrok -Dominio xxx.ngrok-free.app -Token <authtoken>   # URL fija
#
# Requisitos: Docker Desktop abierto y un .env en esta carpeta (el de siempre).
param(
    [switch]$Ngrok,
    [string]$Dominio = "",
    [string]$Token = "",
    [string]$Frontend = "https://factura-sunat.vercel.app"
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

docker info *> $null
if ($LASTEXITCODE -ne 0) { throw "Docker no esta corriendo. Abra Docker Desktop y vuelva a intentar." }
if (-not (Test-Path .env)) { throw "Falta el archivo .env en $PSScriptRoot" }
if ($Ngrok -and (-not $Dominio -or -not $Token)) { throw "Con -Ngrok indique -Dominio y -Token." }

# .env.prod = tu .env + ajustes de produccion (queda ignorado por git y por la imagen).
$ajustes = @(
    "", "ENVIRONMENT=prod", "SUNAT_ENV=prod", "CONFIRM_PROD_EMISSION=false", "DEBUG=false",
    "DOCS_ENABLED=false", "LOG_JSON=true", "SUNAT_VERIFY_SSL=true", "CORS_ORIGINS=$Frontend",
    # Conexiones a la base ya abiertas al arrancar: con la base remota cada una tarda 1-2 s.
    "DATABASE_POOL_MIN_SIZE=4"
)
(Get-Content .env) + $ajustes | Set-Content .env.prod -Encoding utf8

$perfil = if ($Ngrok) { "ngrok" } else { "quick" }
$env:NGROK_DOMAIN = $Dominio
$env:NGROK_AUTHTOKEN = $Token
Write-Host "Construyendo y arrancando (perfil: $perfil)..."
docker compose -f docker-compose.demo.yml --profile $perfil up -d --build
if ($LASTEXITCODE -ne 0) { throw "docker compose fallo." }

$url = $null
if ($Ngrok) {
    $url = "https://$Dominio"
} else {
    Write-Host "Esperando la URL publica del tunel..."
    for ($i = 0; $i -lt 30 -and -not $url; $i++) {
        Start-Sleep 2
        $logs = docker compose -f docker-compose.demo.yml --profile quick logs tunnel 2>&1 | Out-String
        if ($logs -match "https://[a-z0-9-]+\.trycloudflare\.com") { $url = $Matches[0] }
    }
}
if (-not $url) { throw "No se obtuvo la URL del tunel. Revise: docker compose -f docker-compose.demo.yml logs" }

Start-Sleep 3
try {
    $salud = Invoke-RestMethod "$url/health" -TimeoutSec 20
    Write-Host "Salud: $($salud.status) (entorno: $($salud.environment))"
} catch {
    Write-Host "Aviso: la URL aun no responde (el tunel puede tardar unos segundos). Reintente en un momento."
}

Set-Clipboard $url
Write-Host ""
Write-Host "============================================================"
Write-Host " API publica:  $url   (copiada al portapapeles)"
Write-Host " En Vercel -> Settings -> Environment Variables:"
Write-Host "   NEXT_PUBLIC_API_URL = $url"
Write-Host " y luego: Deployments -> Redeploy."
Write-Host " Para apagar:  .\demo-stop.ps1"
Write-Host "============================================================"

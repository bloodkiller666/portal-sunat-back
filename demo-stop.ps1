# Apaga la API y el tunel, y borra el .env.prod temporal (contiene copias de secretos).
Set-Location $PSScriptRoot
docker compose -f docker-compose.demo.yml --profile quick --profile ngrok down
Remove-Item .env.prod -ErrorAction SilentlyContinue
Write-Host "Backend detenido. Vercel ya no podra consultar datos hasta que lo vuelva a encender."

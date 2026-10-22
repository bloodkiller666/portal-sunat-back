# Registra (o quita) la tarea programada de Windows que ejecuta la automatizacion del portal.
#
#   .\scripts\registrar_tarea_windows.ps1                 # crea la tarea: 09:00, 13:00 y 17:00
#   .\scripts\registrar_tarea_windows.ps1 -Horas 08:30,12:00,16:30
#   .\scripts\registrar_tarea_windows.ps1 -Probar          # la ejecuta ahora mismo, una vez
#   .\scripts\registrar_tarea_windows.ps1 -Quitar          # la elimina
#
# La tarea ejecuta `scripts.automatizar --tarea auto --aplicar`: SIRE una vez al dia (a partir de
# las 9:00), el correo en cada corrida y la importacion de lo nuevo. Ademas de los horarios se
# ejecuta 5 minutos despues de iniciar sesion, y "StartWhenAvailable" lanza una corrida perdida
# en cuanto la PC vuelve a estar disponible: si estuvo apagada, el correo se acumula en Outlook y
# la proxima corrida lo baja. Corre con TU usuario y sesion iniciada (Outlook clasico lo necesita).
#
# El resultado queda en: Descargas\FacturasProveedores\_automatizacion\automatizacion.log
# Variable opcional: AUTOMATIZACION_USER_ID (usuario "Automatizacion" de Supabase) en el .env.

param(
    [string[]]$Horas = @("09:00", "13:00", "17:00"),
    [switch]$Quitar,
    [switch]$Probar
)

$ErrorActionPreference = "Stop"
$nombre = "Portal SUNAT - Automatizacion"

if ($Quitar) {
    Unregister-ScheduledTask -TaskName $nombre -Confirm:$false
    Write-Host "Tarea eliminada: $nombre"
    return
}

$repo = Split-Path -Parent $PSScriptRoot
$uv = (Get-Command uv -ErrorAction Stop).Source
$argumentos = "run --with pywin32 python -m scripts.automatizar --tarea auto --aplicar"

$accion = New-ScheduledTaskAction -Execute $uv -Argument $argumentos -WorkingDirectory $repo
$disparadores = @()
foreach ($hora in $Horas) { $disparadores += New-ScheduledTaskTrigger -Daily -At $hora }
$alIniciar = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$alIniciar.Delay = "PT5M"
$disparadores += $alIniciar

$ajustes = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $nombre -Action $accion -Trigger $disparadores `
    -Settings $ajustes -Principal $principal -Force `
    -Description "SIRE (ventas y compras), correo de proveedores e importacion al portal" | Out-Null

Write-Host "Tarea registrada: $nombre"
Write-Host "Horarios: $($Horas -join ', ') y 5 minutos despues de iniciar sesion"
Write-Host "Registro: $env:USERPROFILE\Downloads\FacturasProveedores\_automatizacion\automatizacion.log"

if ($Probar) {
    Start-ScheduledTask -TaskName $nombre
    Write-Host "Ejecutando una vez ahora..."
}

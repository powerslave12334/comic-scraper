# Espera a que termine el backfill de vcp (PID pasado por argumento o fijo)
# y lanza la sincronizacion completa de vermangasporno.
# Se ejecuta DESACoplado (Start-Process) -> sobrevive a los fallos de notificacion.
param([int]$EsperarPID = 15608)

$dir = 'J:\proyect x\scripts'
$log = Join-Path $dir 'vmp_sync_log.txt'
$done = Join-Path $dir 'vmp_sync_done.txt'
Set-Location $dir
Remove-Item $done -Force -ErrorAction SilentlyContinue

"[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Cadena: esperando al backfill de vcp (PID $EsperarPID)..." | Out-File -Append -FilePath $log -Encoding utf8
Wait-Process -Id $EsperarPID -ErrorAction SilentlyContinue
"[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Backfill vcp terminado (o proceso inexistente). Iniciando sync COMPLETO de vmp..." | Out-File -Append -FilePath $log -Encoding utf8

python -u scraper.py --site vmp --mode http >> $log 2>&1
$code = $LASTEXITCODE
"[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] SYNC VMP TERMINADO (exit=$code)" | Out-File -Append -FilePath $log -Encoding utf8
New-Item -ItemType File -Path $done -Force | Out-Null

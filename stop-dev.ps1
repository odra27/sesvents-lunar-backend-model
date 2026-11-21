# Matikan server backend LUNAR (:8000), termasuk proses yang tertinggal di latar belakang.
# Pemakaian: .\stop-dev.ps1 atau klik stop-dev.cmd
$procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'")
$ids = @($procs | Where-Object { $_.CommandLine -match 'serve\.py|uvicorn app\.main:app' } | ForEach-Object { $_.ProcessId })
# anak proses (reloader/worker uvicorn)
do {
    $kids = @($procs | Where-Object { $ids -contains $_.ParentProcessId -and $ids -notcontains $_.ProcessId })
    $ids += @($kids | ForEach-Object { $_.ProcessId })
} while ($kids.Count -gt 0)

if ($ids.Count -eq 0) { Write-Host 'Tidak ada server backend LUNAR yang berjalan.' }
else {
    Write-Host "Mematikan $($ids.Count) proses server..." -ForegroundColor Yellow
    $ids | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
}
if (Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue) {
    Write-Host 'port 8000 masih dipakai program lain' -ForegroundColor Red
} else {
    Write-Host 'port 8000 bebas' -ForegroundColor Green
}

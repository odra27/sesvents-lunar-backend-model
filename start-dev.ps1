# Jalankan backend LUNAR (API + model isyarat) untuk development, di jendela PowerShell baru.
# Pemakaian (dari root repo):  .\start-dev.ps1   atau klik dua kali start-dev.cmd
#   Backend -> http://127.0.0.1:8000  (backend\run-dev.ps1, memuat backend\.env)
# Frontend ada di repo terpisah (Frontend): jalankan `npm run dev` di sana (http://localhost:5174,
# /api otomatis diproksikan ke backend ini).
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$backend = Join-Path $root 'backend'

# Pasang venv + dependency bila belum ada (sekali saja).
if (-not (Test-Path (Join-Path $backend '.venv\Scripts\python.exe'))) {
    Write-Host 'Venv backend belum ada, menjalankan setup-dev.ps1...' -ForegroundColor Yellow
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $backend 'setup-dev.ps1')
}

$inner = "`$Host.UI.RawUI.WindowTitle = 'LUNAR backend :8000'; Set-Location -LiteralPath '$backend'; .\run-dev.ps1"
Start-Process powershell -ArgumentList '-NoExit', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', $inner

Write-Host ''
Write-Host 'Backend dibuka di jendela baru: http://127.0.0.1:8000 (butuh ~30 detik untuk memuat model).' -ForegroundColor Green

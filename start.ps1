# Запуск: .\start.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Remove-Item Env:APP_PASSWORD -ErrorAction SilentlyContinue
Remove-Item Env:SECRET_KEY -ErrorAction SilentlyContinue
Remove-Item Env:MAIL_PASSWORD -ErrorAction SilentlyContinue
Remove-Item Env:MAIL_USER -ErrorAction SilentlyContinue
Remove-Item Env:AI_API_KEY -ErrorAction SilentlyContinue

# Stop hung listeners on 3001
Get-NetTCPConnection -LocalPort 3001 -State Listen -ErrorAction SilentlyContinue |
  ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
  Write-Host "Creating .venv..."
  python -m venv .venv
  & .\.venv\Scripts\python.exe -c "import urllib.request; urllib.request.getproxies=lambda: {}; from pip._internal.cli.main import main; raise SystemExit(main(['install','-r','requirements.txt']))"
}
if (-not (Test-Path ".\.env")) {
  Copy-Item .env.example .env
}

$env:DATA_DIR = "./data"
$port = if ($env:PORT) { $env:PORT } else { "3001" }
Write-Host "http://127.0.0.1:$port  (Ctrl+C to stop)"
& .\.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port $port

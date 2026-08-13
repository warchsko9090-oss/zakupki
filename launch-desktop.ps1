# Restart zakupki server and open Overview in the browser.
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

# Clear process env overrides so .env wins
foreach ($k in @("APP_PASSWORD", "SECRET_KEY", "MAIL_PASSWORD", "MAIL_USER", "AI_API_KEY")) {
    Remove-Item "Env:$k" -ErrorAction SilentlyContinue
}
$env:DATA_DIR = "./data"
$env:PORT = "3001"
$url = "http://127.0.0.1:3001/"

# Stop previous listeners on 3001
Get-NetTCPConnection -LocalPort 3001 -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object {
        try { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue } catch {}
    }
Start-Sleep -Seconds 1

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$uvicorn = Join-Path $PSScriptRoot ".venv\Scripts\uvicorn.exe"
if (-not (Test-Path $uvicorn)) {
    if (-not (Test-Path $python)) {
        throw "Missing .venv. Run start.bat once first."
    }
    throw "Missing uvicorn in .venv."
}
if (-not (Test-Path (Join-Path $PSScriptRoot ".env"))) {
    Copy-Item (Join-Path $PSScriptRoot ".env.example") (Join-Path $PSScriptRoot ".env") -Force
}

# Start server in background (no console window)
$logOut = Join-Path $PSScriptRoot "data\desktop-launch.out.log"
$logErr = Join-Path $PSScriptRoot "data\desktop-launch.err.log"
New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot "data") | Out-Null
Start-Process -FilePath $uvicorn `
    -ArgumentList @("app.main:app", "--host", "127.0.0.1", "--port", "3001") `
    -WorkingDirectory $PSScriptRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $logOut `
    -RedirectStandardError $logErr

# Wait until Overview responds
$ok = $false
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 -MaximumRedirection 5
        if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500) {
            $ok = $true
            break
        }
    } catch {
        # still starting
    }
}

Start-Process $url
if (-not $ok) {
    # Browser opened anyway; server may still be warming up
    exit 0
}

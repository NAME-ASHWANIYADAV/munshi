<#
.SYNOPSIS
    Run MunshiJi locally: FastAPI backend + Vite frontend.

.DESCRIPTION
    Starts the API on http://localhost:8000 (docs at /docs) and the companion screen on
    http://localhost:5173. Both reload on change. Ctrl+C stops the frontend; the backend window
    is separate so you can watch its logs during the demo.

.EXAMPLE
    pwsh scripts/dev.ps1
    pwsh scripts/dev.ps1 -ApiOnly
#>
[CmdletBinding()]
param(
    [switch]$ApiOnly,
    [switch]$FrontendOnly,
    [int]$ApiPort = 8000
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root 'backend'
$frontend = Join-Path $root 'frontend'
$python = Join-Path $backend '.venv\Scripts\python.exe'

if (-not (Test-Path $python)) {
    throw 'Virtualenv missing. Run: pwsh scripts/setup.ps1'
}

if (-not $FrontendOnly) {
    Write-Host "Starting API on http://localhost:$ApiPort  (docs: /docs)" -ForegroundColor Cyan
    $apiArgs = @('-m', 'uvicorn', 'munshiji.main:app', '--reload', '--port', "$ApiPort")
    if ($ApiOnly) {
        Push-Location $backend
        try { & $python @apiArgs } finally { Pop-Location }
        return
    }
    Start-Process -FilePath $python -ArgumentList $apiArgs -WorkingDirectory $backend
    Start-Sleep -Seconds 2
}

if (-not $ApiOnly) {
    if (-not (Test-Path (Join-Path $frontend 'package.json'))) {
        throw 'frontend/package.json not found. Run: pwsh scripts/setup.ps1'
    }
    Write-Host 'Starting companion screen on http://localhost:5173' -ForegroundColor Cyan
    Push-Location $frontend
    try { & npm run dev } finally { Pop-Location }
}

<#
.SYNOPSIS
    One-command setup for MunshiJi, from a clean checkout.

.DESCRIPTION
    Creates the Python virtualenv, installs the backend (editable), builds the demo database with
    180 days of realistic shop history, and installs frontend dependencies.

    No API keys are required: with none configured the whole product runs on its offline providers.
    Add keys to .env afterwards to light up Sarvam, Cognee and n8n.

.EXAMPLE
    pwsh scripts/setup.ps1
    pwsh scripts/setup.ps1 -SkipFrontend
#>
[CmdletBinding()]
param(
    [switch]$SkipFrontend,
    [switch]$SkipSeed,
    [int]$Days = 180
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root 'backend'
$frontend = Join-Path $root 'frontend'
$python = Join-Path $backend '.venv\Scripts\python.exe'

function Write-Step($message) {
    Write-Host ''
    Write-Host "==> $message" -ForegroundColor Cyan
}

Write-Step 'Checking prerequisites'
$pythonVersion = (& python --version) 2>&1
if ($LASTEXITCODE -ne 0) { throw 'Python 3.12+ is required but was not found on PATH.' }
Write-Host "    $pythonVersion"

Write-Step 'Creating virtualenv'
if (-not (Test-Path $python)) {
    & python -m venv (Join-Path $backend '.venv')
    Write-Host '    created backend/.venv'
} else {
    Write-Host '    backend/.venv already exists'
}

Write-Step 'Installing backend dependencies'
& $python -m pip install --quiet --upgrade pip
& $python -m pip install --quiet -r (Join-Path $backend 'requirements.txt')
Push-Location $backend
try { & $python -m pip install --quiet -e . } finally { Pop-Location }
Write-Host '    backend installed (editable)'

Write-Step 'Preparing configuration'
$envFile = Join-Path $root '.env'
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $root '.env.example') $envFile
    Write-Host '    created .env from .env.example (offline mode; add keys to go live)'
} else {
    Write-Host '    .env already present, left untouched'
}

if (-not $SkipSeed) {
    Write-Step "Building the demo database ($Days days of history)"
    Push-Location $backend
    try { & $python -m munshiji.cli seed --reset --days $Days } finally { Pop-Location }
}

if (-not $SkipFrontend) {
    Write-Step 'Installing frontend dependencies'
    if (Test-Path (Join-Path $frontend 'package.json')) {
        Push-Location $frontend
        try { & npm install --silent } finally { Pop-Location }
        Write-Host '    frontend ready'
    } else {
        Write-Host '    no frontend/package.json yet, skipping' -ForegroundColor Yellow
    }
}

Write-Host ''
Write-Host 'Setup complete.' -ForegroundColor Green
Write-Host '  Run the stack   :  pwsh scripts/dev.ps1'
Write-Host '  Run the demo    :  pwsh scripts/demo.ps1'
Write-Host '  Run the tests   :  backend/.venv/Scripts/python.exe -m pytest'

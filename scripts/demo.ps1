<#
.SYNOPSIS
    Replay the full MunshiJi demo in the terminal — no browser, no network, no API keys.

.DESCRIPTION
    Runs the two-conversation story end to end:
      Call 1  — merchant asks how today went; MunshiJi finds the dip, spots 12 dormant regulars,
                proposes a win-back offer, gets approval, and sends it through the action layer.
      (time passes — outcomes land)
      Call 2  — merchant calls back; MunshiJi recalls the offer from memory and reports what it earned.

    This is the safety net for demo day: if the venue Wi-Fi dies, this still runs.

.EXAMPLE
    pwsh scripts/demo.ps1
    pwsh scripts/demo.ps1 -Reseed
#>
[CmdletBinding()]
param(
    [switch]$Reseed,
    [switch]$English
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root 'backend'
$python = Join-Path $backend '.venv\Scripts\python.exe'

if (-not (Test-Path $python)) { throw 'Virtualenv missing. Run: pwsh scripts/setup.ps1' }

Push-Location $backend
try {
    if ($Reseed) { & $python -m munshiji.cli seed --reset }
    $demoArgs = @('-m', 'munshiji.cli', 'demo')
    if ($English) { $demoArgs += @('--language', 'en-IN') }
    & $python @demoArgs
} finally {
    Pop-Location
}

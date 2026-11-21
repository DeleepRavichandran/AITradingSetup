<#
  Run any nseql command with the settings from .env, e.g.
      powershell -ExecutionPolicy Bypass -File scripts\windows\nseql.ps1 kite-login
      powershell -ExecutionPolicy Bypass -File scripts\windows\nseql.ps1 import-kite kite_2026 --symbols RELIANCE,TCS --start 2025-01-01 --licence "Kite Connect, personal research"
#>
. (Join-Path $PSScriptRoot "env.ps1")
$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
& (Join-Path $repo ".venv\Scripts\python.exe") -m nseql.cli @args
exit $LASTEXITCODE

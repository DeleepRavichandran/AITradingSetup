<#
  Start the API (port 8000, this machine only) and the dashboard (http://localhost:3000) in two windows.
      powershell -ExecutionPolicy Bypass -File scripts\windows\start.ps1
  Close the two windows to stop. Paper sessions and the kill switch are saved to disk and resume next time;
  days missed while the laptop was off are caught up after the next Kite login.
#>
param([switch]$Hidden)          # -Hidden: used by the logon auto-start; no windows, no browser
$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$vpy = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $vpy)) { throw "Run scripts\windows\setup.ps1 first" }
$envScript = Join-Path $PSScriptRoot "env.ps1"
$style = if ($Hidden) { "Hidden" } else { "Normal" }
$logs = Join-Path $repo "experiments\logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null
Start-Process powershell -WindowStyle $style -WorkingDirectory $repo -ArgumentList @("-NoExit", "-ExecutionPolicy", "Bypass", "-Command",
    ". '$envScript'; & '$vpy' -m nseql.cli serve --host 127.0.0.1 --port 8000 *>> '$logs\api.log'")
if (Test-Path (Join-Path $repo "apps\web\.next")) {
    Start-Process powershell -WindowStyle $style -WorkingDirectory (Join-Path $repo "apps\web") -ArgumentList @("-NoExit", "-ExecutionPolicy", "Bypass", "-Command", "npm.cmd run start *>> '$logs\web.log'")
    if (-not $Hidden) { Start-Sleep -Seconds 6; Start-Process "http://localhost:3000/today" }
} else {
    Write-Warning "The dashboard is not built (no Node.js?). The API is running at http://127.0.0.1:8000."
}

<#
  Daily self-update of the lab (installed as a scheduled task by assistant_setup.ps1; you never need to run it).

  After the close on trading days: waits for the evening review to finish, pulls the latest tested code of the branch
  that is checked out, rebuilds the dashboard, restarts the lab, and checks it is healthy. If the new version does not
  come up within 3 minutes it goes back to the previous version automatically. Never runs during market hours, never
  overwrites local changes, never touches .env. Every step is written to experiments\logs\update.log.
#>
param([switch]$Force)
$ErrorActionPreference = "Continue"
$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$vpy = Join-Path $repo ".venv\Scripts\python.exe"
$logs = Join-Path $repo "experiments\logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$log = Join-Path $logs "update.log"
function Log($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $m" | Out-File -FilePath $log -Append -Encoding utf8 }
Set-Location $repo

$now = Get-Date
$mins = $now.Hour * 60 + $now.Minute
if (-not $Force -and $now.DayOfWeek -notin @("Saturday", "Sunday") -and $mins -ge (9 * 60) -and $mins -lt (15 * 60 + 40)) {
    Log "skipped: market hours"; exit 0
}

function Stop-Lab {
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='node.exe'" |
        Where-Object { $_.CommandLine -match 'nseql|next' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 3
}
function Start-Lab { & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "start.ps1") -Hidden }
function Healthy {
    for ($k = 0; $k -lt 36; $k++) {
        try { if ((Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/health" -TimeoutSec 5).status -eq "ok") { return $true } } catch {}
        Start-Sleep -Seconds 5
    }
    return $false
}
function Build {
    & $vpy -m pip install -e . -q 2>&1 | Out-File -FilePath $log -Append -Encoding utf8
    Push-Location (Join-Path $repo "apps\web")
    & npm.cmd install --no-audit --no-fund 2>&1 | Out-File -FilePath $log -Append -Encoding utf8
    & npm.cmd run build 2>&1 | Out-File -FilePath $log -Append -Encoding utf8
    $ok = $LASTEXITCODE -eq 0
    Pop-Location
    return $ok
}

# 1. let the evening review finish (it runs as its own process after 15:30)
for ($k = 0; $k -lt 60; $k++) {
    $busy = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'daily-review' }
    if (-not $busy) { break }
    if ($k -eq 0) { Log "waiting for the daily review to finish" }
    Start-Sleep -Seconds 60
}

# 2. anything new?
$branch = (git rev-parse --abbrev-ref HEAD).Trim()
$before = (git rev-parse HEAD).Trim()
git fetch origin $branch 2>&1 | Out-File -FilePath $log -Append -Encoding utf8
$target = (git rev-parse "origin/$branch").Trim()
if ($target -eq $before) {
    Log "up to date on $branch ($($before.Substring(0,7)))"
    if (-not (Healthy)) { Log "lab was not running: starting it"; Start-Lab }
    exit 0
}
if (git status --porcelain --untracked-files=no) { Log "NOT updated: local changes in tracked files (left untouched)"; exit 1 }

# 3. update, rebuild, restart, check
Log "updating $branch $($before.Substring(0,7)) -> $($target.Substring(0,7))"
Stop-Lab
git merge --ff-only "origin/$branch" 2>&1 | Out-File -FilePath $log -Append -Encoding utf8
if ($LASTEXITCODE -ne 0) { Log "NOT updated: cannot fast-forward; restarting the current version"; Start-Lab; exit 1 }
$built = Build
Start-Lab
if ($built -and (Healthy)) { Log "updated and healthy at $($target.Substring(0,7))"; exit 0 }

# 4. roll back
Log "new version unhealthy (build ok: $built): rolling back to $($before.Substring(0,7))"
Stop-Lab
git reset --hard $before 2>&1 | Out-File -FilePath $log -Append -Encoding utf8
Build | Out-Null
Start-Lab
if (Healthy) { Log "rolled back and healthy at $($before.Substring(0,7))" } else { Log "ROLLBACK ALSO UNHEALTHY: needs attention" }
exit 1

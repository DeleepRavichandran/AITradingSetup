<#
  One-time setup so the assistant can run and watch the lab for you. Run once, after market hours, from the lab folder:
      powershell -ExecutionPolicy Bypass -File scripts\windows\assistant_setup.ps1

  It does four things:
   1. Safety check: refuses if the lab runs without a password (NSEQL_ALLOW_INSECURE_LOCAL=true).
   2. Creates a READ-ONLY key (NSEQL_VIEWER_TOKEN in .env) for the assistant. With it, the assistant can see everything
      (prices, account, positions, trades, reviews) and change nothing. It is copied to your clipboard, never printed.
   3. Installs the daily self-update (scheduled task "NSE AI Quant Lab daily update", weekdays 16:10; see daily_update.ps1).
   4. Opens a private, encrypted link from the internet to the lab API only (Tailscale Funnel to 127.0.0.1:8000). Every
      request still needs the key. The dashboard and your Angel One login are not exposed.
  Undo: Unregister-ScheduledTask 'NSE AI Quant Lab daily update'; tailscale funnel reset; remove NSEQL_VIEWER_TOKEN from .env.
#>
param([switch]$Force)
$ErrorActionPreference = "Stop"
$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$envFile = Join-Path $repo ".env"
if (-not (Test-Path $envFile)) { throw ".env not found: run scripts\windows\setup.ps1 first" }
$now = Get-Date
$mins = $now.Hour * 60 + $now.Minute
if (-not $Force -and $now.DayOfWeek -notin @("Saturday", "Sunday") -and $mins -ge (9 * 60) -and $mins -lt (15 * 60 + 35)) {
    throw "Please run this after 15:35 (it restarts the lab). To run it anyway: add -Force"
}

# 1. safety
$text = Get-Content $envFile -Raw
if ($text -match "(?m)^\s*NSEQL_ALLOW_INSECURE_LOCAL\s*=\s*true") { throw "The lab runs without a password (NSEQL_ALLOW_INSECURE_LOCAL=true). Remove that line from .env first." }
if ($text -notmatch "(?m)^\s*NSEQL_API_TOKEN\s*=\s*\S{16,}") { throw "No dashboard password (NSEQL_API_TOKEN) in .env: run scripts\windows\setup.ps1 first." }

# 2. read-only key
if ($text -match "(?m)^\s*NSEQL_VIEWER_TOKEN\s*=\s*(\S{24,})\s*$") {
    $viewer = $Matches[1]
    Write-Host "Read-only key already exists (kept)."
} else {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $viewer = -join ($bytes | ForEach-Object { $_.ToString("x2") })
    if ($text -match "(?m)^\s*NSEQL_VIEWER_TOKEN\s*=.*$") { $text = $text -replace "(?m)^\s*NSEQL_VIEWER_TOKEN\s*=.*$", "NSEQL_VIEWER_TOKEN=$viewer" }
    else { $text = $text.TrimEnd() + "`r`nNSEQL_VIEWER_TOKEN=$viewer`r`n" }
    Set-Content -Path $envFile -Value $text -Encoding utf8 -NoNewline
    Write-Host "Created a read-only key for the assistant (NSEQL_VIEWER_TOKEN in .env)."
}

# 3. daily self-update
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$(Join-Path $PSScriptRoot 'daily_update.ps1')`""
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "16:10"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName "NSE AI Quant Lab daily update" -Action $action -Trigger $trigger -Settings $settings `
    -Description "Updates the lab to the latest tested version after market close and restarts it (rolls back if unhealthy). Paper trading only." -Force | Out-Null
Write-Host "Daily self-update installed (weekdays 16:10)."

# 4. secure link (Tailscale Funnel)
$ts = (Get-Command tailscale -ErrorAction SilentlyContinue).Source
if (-not $ts -and (Test-Path "C:\Program Files\Tailscale\tailscale.exe")) { $ts = "C:\Program Files\Tailscale\tailscale.exe" }
if (-not $ts) {
    Write-Host "Installing Tailscale (free)..."
    winget install --id Tailscale.Tailscale -e --accept-source-agreements --accept-package-agreements
    $ts = "C:\Program Files\Tailscale\tailscale.exe"
}
if (-not (Test-Path $ts)) { throw "Tailscale did not install. Install it from https://tailscale.com/download/windows and run this script again." }
$st = & $ts status 2>&1 | Out-String
if ($st -match "Logged out|NeedsLogin|not logged in|stopped") {
    Write-Host "Sign in to Tailscale in the browser window that opens (Google or Microsoft account is fine), then come back here."
    & $ts up
}
Write-Host "Opening the secure link to the lab API (if a browser page asks to enable Funnel, click Enable)..."
& $ts funnel --bg 8000
$fs = & $ts funnel status 2>&1 | Out-String
$url = ([regex]::Match($fs, "https://[a-z0-9.-]+\.ts\.net")).Value

# restart the lab so it loads the key
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='node.exe'" | Where-Object { $_.CommandLine -match 'nseql|next' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 3
& powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "start.ps1") -Hidden

Set-Clipboard -Value $viewer
Write-Host ""
Write-Host "Done."
Write-Host "  Your lab's address:  $url"
Write-Host "  The read-only key is now on your clipboard (it is also in .env as NSEQL_VIEWER_TOKEN)."
Write-Host ""
Write-Host "Tell the assistant the address above. It will open a secure form for the key: paste it there (Ctrl+V), never in the chat."

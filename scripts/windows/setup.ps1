<#
  NSE AI Quant Lab - one-time setup on a Windows laptop.

  Run from the repository folder in PowerShell:
      powershell -ExecutionPolicy Bypass -File scripts\windows\setup.ps1            # native Python + Node
      powershell -ExecutionPolicy Bypass -File scripts\windows\setup.ps1 -Docker    # Docker Desktop instead

  Native mode: everything works except running AI-generated strategy code, which needs Linux
  namespaces (the sandbox refuses to run it on Windows; it fails closed). Use -Docker for that.
  This script does not place orders, does not touch broker settings and never enables live trading.
  STATUS: written for Windows 10/11; not yet run on a real Windows machine.
#>
param([switch]$Docker, [switch]$SkipWeb, [switch]$NoAutostart)
$ErrorActionPreference = "Stop"
$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
Set-Location $repo
function Have($name) { [bool](Get-Command $name -ErrorAction SilentlyContinue) }

# ---------------------------------------------------------------- .env with a generated dashboard password
if (-not (Test-Path .env)) { Copy-Item .env.example .env; Write-Host "Created .env from .env.example" }
$envText = Get-Content .env -Raw
if ($envText -match "(?m)^NSEQL_API_TOKEN=\s*$") {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $tok = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    $envText = $envText -replace "(?m)^NSEQL_API_TOKEN=\s*$", "NSEQL_API_TOKEN=$tok"
    Set-Content -Path .env -Value $envText -NoNewline
    Write-Host "Generated your dashboard password (NSEQL_API_TOKEN) in .env - keep it private."
}
if ($envText -match "(?m)^NSEQL_SCHEDULER=false") {
    (Get-Content .env -Raw) -replace "(?m)^NSEQL_SCHEDULER=false", "NSEQL_SCHEDULER=true" | Set-Content .env -NoNewline
    Write-Host "Turned on the paper-trading scheduler (NSEQL_SCHEDULER=true). It never places real orders."
}

# ---------------------------------------------------------------- Angel One (asked once; stored only in .env)
function Set-EnvValue($name, $value) {
    $text = Get-Content .env -Raw
    if ($text -match "(?m)^$name=") { $text = $text -replace "(?m)^$name=.*$", "$name=$value" } else { $text = $text.TrimEnd() + "`r`n$name=$value`r`n" }
    Set-Content -Path .env -Value $text -NoNewline
}
$angelMissing = @("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN", "ANGEL_TOTP_SECRET") | Where-Object { (Get-Content .env -Raw) -notmatch "(?m)^$_=\S+" }
if ($angelMissing) {
    Write-Host ""
    Write-Host "Angel One login details (asked once, saved only in .env on this laptop; press Enter to skip):"
    foreach ($k in $angelMissing) {
        $prompt = @{ ANGEL_API_KEY = "  SmartAPI API key"; ANGEL_CLIENT_CODE = "  Angel One client ID"; ANGEL_PIN = "  4-digit MPIN";
                     ANGEL_TOTP_SECRET = "  TOTP secret (text under the QR code)" }[$k]
        $secure = Read-Host $prompt -AsSecureString
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
        if ($plain) { Set-EnvValue $k $plain.Trim() }
    }
}
if (-not (@("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN", "ANGEL_TOTP_SECRET") | Where-Object { (Get-Content .env -Raw) -notmatch "(?m)^$_=\S+" })) {
    Set-EnvValue "NSEQL_LIVE_SCREENING" "angelone"
    Write-Host "Live screening with Angel One will start automatically (simulated orders only)."
}

# ---------------------------------------------------------------- Docker Desktop
if ($Docker) {
    if (-not (Have docker)) { throw "Docker Desktop was not found. Install it (it uses WSL2) from https://www.docker.com/products/docker-desktop/, start it, then run this again." }
    docker compose --env-file .env -f infra/docker-compose.yml up -d --build
    if ($LASTEXITCODE -ne 0) { throw "docker compose failed (is Docker Desktop running?)" }
    Write-Host ""
    Write-Host "Dashboard: http://localhost:8080   (password = NSEQL_API_TOKEN in .env)"
    Write-Host "Commands run inside the container, e.g.:  docker compose --env-file .env -f infra/docker-compose.yml exec api nseql kite-login"
    exit 0
}

# ---------------------------------------------------------------- Python 3.12+
$py = $null
foreach ($cand in @(@("py", "-3.13"), @("py", "-3.12"), @("python"))) {
    if (-not (Have $cand[0])) { continue }
    $extra = if ($cand.Count -gt 1) { $cand[1..($cand.Count - 1)] } else { @() }
    try { $ok = & $cand[0] @extra -c "import sys; print(sys.version_info >= (3, 12))" 2>$null } catch { $ok = $null }
    if ($ok -eq "True") { $py = $cand; break }
}
if (-not $py) {
    throw "Python 3.12 or newer was not found. Install it with:  winget install Python.Python.3.13   (or from python.org; tick 'Add to PATH'), open a NEW PowerShell window, and run this again."
}
$pyExtra = if ($py.Count -gt 1) { $py[1..($py.Count - 1)] } else { @() }
if (-not (Test-Path .venv\Scripts\python.exe)) {
    Write-Host "Creating the Python environment in .venv ..."
    & $py[0] @pyExtra -m venv .venv
}
$vpy = Join-Path $repo ".venv\Scripts\python.exe"
# "python -m pip" instead of pip.exe: some company/antivirus policies block pip.exe but allow python.exe
& $vpy -m pip install --upgrade pip
& $vpy -m pip install -e ".[llm]"
if ($LASTEXITCODE -ne 0) { throw "pip install failed - see the messages above" }
& $vpy -c "import nseql.cli, zoneinfo; zoneinfo.ZoneInfo('Asia/Kolkata'); print('Python packages OK')"

# ---------------------------------------------------------------- dashboard (Node.js)
if (-not $SkipWeb) {
    if (-not (Have node)) {
        Write-Warning "Node.js was not found, so the dashboard was not built. Install it with:  winget install OpenJS.NodeJS.LTS  then run this again (or add -SkipWeb)."
    } else {
        Push-Location apps\web
        try {
            npm.cmd ci --no-audit --no-fund
            npm.cmd run build
            if ($LASTEXITCODE -ne 0) { throw "dashboard build failed" }
        } finally { Pop-Location }
    }
}

# ---------------------------------------------------------------- check the Angel One login, then auto-start
if ((Get-Content .env -Raw) -match "(?m)^NSEQL_LIVE_SCREENING=angelone") {
    & (Join-Path $PSScriptRoot "nseql.ps1") angel-check
    if ($LASTEXITCODE -ne 0) { Write-Warning "Angel One login failed - check the four values in .env and run setup again." }
}
if (-not $NoAutostart) {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$(Join-Path $PSScriptRoot 'start.ps1')`" -Hidden"
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Days 0)
    Register-ScheduledTask -TaskName "NSE AI Quant Lab" -Action $action -Trigger $trigger -Settings $settings -Description "Starts the research lab at logon (paper trading only)" -Force | Out-Null
    Write-Host "Auto-start installed: the lab starts by itself when you log in to Windows (remove with: Unregister-ScheduledTask 'NSE AI Quant Lab')."
}

Write-Host ""
Write-Host "Setup finished."
Write-Host "  Start everything:   powershell -ExecutionPolicy Bypass -File scripts\windows\start.ps1"
Write-Host "  Run a command:      powershell -ExecutionPolicy Bypass -File scripts\windows\nseql.ps1 kite-login"
Write-Host "  Dashboard:          http://localhost:3000   (password = NSEQL_API_TOKEN in .env)"

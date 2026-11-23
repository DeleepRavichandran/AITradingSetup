<#
  Load .env into this PowerShell process for the NSE AI Quant Lab (dot-source it: . scripts\windows\env.ps1).

  Two values are never loaded, whatever .env says:
    HUMAN_APPROVAL_SECRET   must not be visible to the API, agents or research code (it signs live approvals)
    LIVE_TRADING_ENABLED    always forced to false here
  Values already set in the environment win over .env.
#>
$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$envFile = Join-Path $repo ".env"
if (-not (Test-Path $envFile)) { throw ".env not found in $repo - run scripts\windows\setup.ps1 first" }
foreach ($line in Get-Content $envFile) {
    if ($line -match '^\s*#' -or $line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') { continue }
    $k = $Matches[1]; $v = $Matches[2].Trim().Trim('"').Trim("'")
    if ($k -in @("HUMAN_APPROVAL_SECRET", "LIVE_TRADING_ENABLED") -or $v -eq "") { continue }
    if (-not [Environment]::GetEnvironmentVariable($k, "Process")) { [Environment]::SetEnvironmentVariable($k, $v, "Process") }
}
$env:LIVE_TRADING_ENABLED = "false"
Remove-Item Env:HUMAN_APPROVAL_SECRET -ErrorAction SilentlyContinue
# keep data next to the repository, whatever folder the command is started from
if (-not [System.IO.Path]::IsPathRooted($env:NSEQL_DATA_DIR)) { $env:NSEQL_DATA_DIR = Join-Path $repo ($(if ($env:NSEQL_DATA_DIR) { $env:NSEQL_DATA_DIR } else { "datasets" })) }
if (-not [System.IO.Path]::IsPathRooted($env:NSEQL_EXPERIMENTS_DIR)) { $env:NSEQL_EXPERIMENTS_DIR = Join-Path $repo ($(if ($env:NSEQL_EXPERIMENTS_DIR) { $env:NSEQL_EXPERIMENTS_DIR } else { "experiments" })) }

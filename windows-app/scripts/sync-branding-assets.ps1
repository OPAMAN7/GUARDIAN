param(
    [string]$PiIp,
    [string]$PiUser = "pi",
    [string]$RemoteRoot = "/home/pi/guardian-shield"
)

$ErrorActionPreference = "Stop"

function Write-Info($msg) { Write-Host "[INFO] $msg" -ForegroundColor Cyan }
function Write-Ok($msg) { Write-Host "[OK]   $msg" -ForegroundColor Green }
function Write-Err($msg) { Write-Host "[ERR]  $msg" -ForegroundColor Red }

if ([string]::IsNullOrWhiteSpace($PiIp)) {
    $PiIp = Read-Host "Enter Raspberry Pi IP"
}

if ([string]::IsNullOrWhiteSpace($PiIp)) {
    Write-Err "Pi IP is required"
    exit 1
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir "..\..")
$assetsPath = Join-Path $repoRoot "branding-assets"

if (-not (Test-Path $assetsPath)) {
    Write-Err "branding-assets folder not found at $assetsPath"
    exit 1
}

$remoteScpTarget = "${PiUser}@${PiIp}:${RemoteRoot}"

Get-Command ssh -ErrorAction Stop | Out-Null
Get-Command scp -ErrorAction Stop | Out-Null

Write-Info "Ensuring remote path exists"
ssh "$PiUser@$PiIp" "mkdir -p $RemoteRoot"

Write-Info "Syncing branding-assets to $remoteScpTarget"
scp -r "$assetsPath" "$remoteScpTarget"

Write-Ok "Branding assets synced"
Write-Host "Next on Pi: sudo bash $RemoteRoot/pi-agent/scripts/apply-branding.sh --assets $RemoteRoot/branding-assets --owner-user pi --hostname aman-test --homepage http://192.168.137.1:3000" -ForegroundColor White

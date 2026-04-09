param(
    [string]$PiIp,
    [string]$PiUser = "pi",
    [string]$ParentIp = "192.168.137.1",
    [string]$Hostname = "aman-test",
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
$validateScript = Join-Path $scriptDir "validate-branding-assets.ps1"
$syncScript = Join-Path $scriptDir "sync-branding-assets.ps1"

Write-Info "Step 1/3: Validate branding assets"
powershell -ExecutionPolicy Bypass -File $validateScript

Write-Info "Step 2/3: Sync branding assets"
powershell -ExecutionPolicy Bypass -File $syncScript -PiIp $PiIp -PiUser $PiUser -RemoteRoot $RemoteRoot

Write-Info "Step 3/3: Apply branding on Pi"
$cmd = "cd $RemoteRoot && chmod +x pi-agent/scripts/apply-branding.sh && sudo bash pi-agent/scripts/apply-branding.sh --assets $RemoteRoot/branding-assets --owner-user $PiUser --hostname $Hostname --homepage http://$ParentIp:3000"
ssh -t "$PiUser@$PiIp" $cmd

Write-Ok "Remote branding apply flow completed"
Write-Host "Recommended next step on Pi: sudo reboot" -ForegroundColor White

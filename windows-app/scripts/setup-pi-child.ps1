param(
    [string]$PiIp,
    [string]$PiUser = "pi",
    [string]$ParentIp = "192.168.137.1",
    [string]$RemoteRoot = "/home/pi/guardian-shield",
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"

function Write-Info($msg) { Write-Host "[INFO] $msg" -ForegroundColor Cyan }
function Write-Ok($msg) { Write-Host "[OK]   $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "[WARN] $msg" -ForegroundColor Yellow }
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

Write-Info "Repo root: $repoRoot"
Write-Info "Target Pi: $PiUser@$PiIp"
Write-Info "Remote path: $RemoteRoot"
Write-Info "Parent IP for agent: $ParentIp"

$remoteScpTarget = "${PiUser}@${PiIp}:${RemoteRoot}"

# Quick command checks
Get-Command ssh -ErrorAction Stop | Out-Null
Get-Command scp -ErrorAction Stop | Out-Null

# Ensure remote folder exists
Write-Info "Creating remote target directories"
ssh "$PiUser@$PiIp" "mkdir -p $RemoteRoot"

# Copy required folders for this phase
Write-Info "Copying pi-agent folder"
scp -r "$repoRoot\pi-agent" "$remoteScpTarget"

if (Test-Path "$repoRoot\branding-assets") {
    Write-Info "Copying branding-assets folder"
    scp -r "$repoRoot\branding-assets" "$remoteScpTarget"
} else {
    Write-Warn "branding-assets folder not found locally, skipping"
}

Write-Ok "File transfer completed"

if (-not $SkipInstall) {
    Write-Info "Running install-agent.sh on Pi"
    ssh -t "$PiUser@$PiIp" "cd $RemoteRoot/pi-agent/scripts && chmod +x install-agent.sh && sudo bash install-agent.sh $ParentIp"

    Write-Info "Checking guardian-agent status"
    ssh -t "$PiUser@$PiIp" "sudo systemctl status guardian-agent --no-pager"
}

Write-Ok "Pi child setup flow completed"
Write-Host "Next: open dashboard at http://localhost:3000 and confirm Pi device is online." -ForegroundColor White

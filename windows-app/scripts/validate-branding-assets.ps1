$ErrorActionPreference = "Stop"

function Write-Ok($msg) { Write-Host "[OK]   $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "[WARN] $msg" -ForegroundColor Yellow }
function Write-Err($msg) { Write-Host "[ERR]  $msg" -ForegroundColor Red }

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir "..\..")
$assetsRoot = Join-Path $repoRoot "branding-assets"

$requiredFiles = @(
    "logo\brand-logo.svg",
    "logo\brand-logo.png",
    "icons\app-icon-512.png",
    "icons\app-icon-256.png",
    "icons\app-icon-128.png",
    "icons\app-icon-64.png",
    "icons\app-icon-32.png",
    "wallpaper\wallpaper-1920x1080.png",
    "login\login-bg-1920x1080.png",
    "boot-splash\splash-1920x1080.png",
    "tokens\colors.json",
    "fonts\headline-font-name.txt",
    "fonts\body-font-name.txt"
)

if (-not (Test-Path $assetsRoot)) {
    Write-Err "branding-assets folder not found: $assetsRoot"
    exit 1
}

$missing = @()
foreach ($rel in $requiredFiles) {
    $full = Join-Path $assetsRoot $rel
    if (-not (Test-Path $full)) {
        $missing += $rel
    }
}

if ($missing.Count -gt 0) {
    Write-Err "Missing required files:"
    $missing | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    exit 1
}

Write-Ok "All required files exist"

Add-Type -AssemblyName System.Drawing

function Check-Dim([string]$relativePath, [int]$w, [int]$h) {
    $full = Join-Path $assetsRoot $relativePath
    $img = [System.Drawing.Image]::FromFile($full)
    try {
        if ($img.Width -eq $w -and $img.Height -eq $h) {
            Write-Ok "$relativePath dimensions are ${w}x${h}"
        } else {
            Write-Warn "$relativePath dimensions are $($img.Width)x$($img.Height), expected ${w}x${h}"
        }
    }
    finally {
        $img.Dispose()
    }
}

Check-Dim "wallpaper\wallpaper-1920x1080.png" 1920 1080
Check-Dim "login\login-bg-1920x1080.png" 1920 1080
Check-Dim "boot-splash\splash-1920x1080.png" 1920 1080
Check-Dim "icons\app-icon-512.png" 512 512
Check-Dim "icons\app-icon-256.png" 256 256
Check-Dim "icons\app-icon-128.png" 128 128
Check-Dim "icons\app-icon-64.png" 64 64
Check-Dim "icons\app-icon-32.png" 32 32

Write-Ok "Branding assets validation completed"

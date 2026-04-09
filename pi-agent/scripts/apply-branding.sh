#!/bin/bash
# Guardian Shield - Raspberry Pi visual branding apply script
# Safe visual-only changes: hostname, wallpaper, login background, Chromium homepage policy, MOTD.
# Does NOT alter core agent logic or command execution model.

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()   { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
error() { echo -e "${RED}[ERR]${NC} $1"; exit 1; }

ASSETS_DIR="/home/pi/guardian-shield/branding-assets"
OWNER_USER="pi"
HOSTNAME_VALUE=""
HOMEPAGE_URL="http://localhost:3000"
BRAND_DIR="/opt/guardian-branding"

usage() {
  cat <<EOF
Usage:
  sudo bash pi-agent/scripts/apply-branding.sh [options]

Options:
  --assets <path>       Branding assets directory (default: ${ASSETS_DIR})
  --owner-user <user>   Desktop owner user (default: ${OWNER_USER})
  --hostname <name>     Optional hostname to set (example: guardian-kid-pi)
  --homepage <url>      Chromium homepage policy (default: ${HOMEPAGE_URL})
  --help                Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --assets)
      ASSETS_DIR="$2"
      shift 2
      ;;
    --owner-user)
      OWNER_USER="$2"
      shift 2
      ;;
    --hostname)
      HOSTNAME_VALUE="$2"
      shift 2
      ;;
    --homepage)
      HOMEPAGE_URL="$2"
      shift 2
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      error "Unknown option: $1"
      ;;
  esac
done

[[ $EUID -ne 0 ]] && error "Run as root: sudo bash .../apply-branding.sh"

[[ -d "$ASSETS_DIR" ]] || error "Assets directory not found: $ASSETS_DIR"

need_file() {
  local f="$1"
  if [[ ! -f "$f" ]]; then
    error "Missing required asset: $f"
  fi
}

need_file "$ASSETS_DIR/logo/brand-logo.png"
need_file "$ASSETS_DIR/wallpaper/wallpaper-1920x1080.png"
need_file "$ASSETS_DIR/login/login-bg-1920x1080.png"
need_file "$ASSETS_DIR/boot-splash/splash-1920x1080.png"

info "Creating branding deployment folder: $BRAND_DIR"
mkdir -p "$BRAND_DIR"
rsync -a --delete "$ASSETS_DIR/" "$BRAND_DIR/"
log "Assets copied to $BRAND_DIR"

if [[ -n "$HOSTNAME_VALUE" ]]; then
  info "Applying hostname: $HOSTNAME_VALUE"
  echo "$HOSTNAME_VALUE" > /etc/hostname

  if grep -q '^127.0.1.1' /etc/hosts; then
    sed -i "s/^127.0.1.1.*/127.0.1.1\t${HOSTNAME_VALUE}/" /etc/hosts
  else
    echo -e "127.0.1.1\t${HOSTNAME_VALUE}" >> /etc/hosts
  fi

  hostnamectl set-hostname "$HOSTNAME_VALUE" || true
  log "Hostname applied (reboot recommended)"
fi

# Desktop wallpaper auto-apply script
WALL_SCRIPT="/usr/local/bin/guardian-apply-wallpaper.sh"
cat > "$WALL_SCRIPT" <<'EOF'
#!/bin/bash
WALL_FILE="/opt/guardian-branding/wallpaper/wallpaper-1920x1080.png"
if command -v pcmanfm >/dev/null 2>&1 && [[ -f "$WALL_FILE" ]]; then
  pcmanfm --set-wallpaper "$WALL_FILE" --wallpaper-mode=fit 2>/dev/null || true
fi
EOF
chmod +x "$WALL_SCRIPT"

AUTOSTART_FILE="/etc/xdg/autostart/guardian-branding.desktop"
cat > "$AUTOSTART_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=Guardian Branding
Exec=/usr/local/bin/guardian-apply-wallpaper.sh
X-GNOME-Autostart-enabled=true
NoDisplay=true
EOF
log "Desktop wallpaper autostart configured"

# LightDM login background and logo (if lightdm-gtk-greeter exists)
GREETER_CFG="/etc/lightdm/lightdm-gtk-greeter.conf"
if [[ -f "$GREETER_CFG" ]]; then
  info "Configuring LightDM greeter branding"
  cp -n "$GREETER_CFG" "${GREETER_CFG}.guardian.bak" || true

  if grep -q '^background=' "$GREETER_CFG"; then
    sed -i "s|^background=.*|background=${BRAND_DIR}/login/login-bg-1920x1080.png|" "$GREETER_CFG"
  else
    echo "background=${BRAND_DIR}/login/login-bg-1920x1080.png" >> "$GREETER_CFG"
  fi

  if grep -q '^logo=' "$GREETER_CFG"; then
    sed -i "s|^logo=.*|logo=${BRAND_DIR}/logo/brand-logo.png|" "$GREETER_CFG"
  else
    echo "logo=${BRAND_DIR}/logo/brand-logo.png" >> "$GREETER_CFG"
  fi

  log "LightDM branding configured"
else
  warn "LightDM greeter config not found, skipped login branding"
fi

# Chromium managed policy
POLICY_DIR="/etc/chromium/policies/managed"
mkdir -p "$POLICY_DIR"
cat > "$POLICY_DIR/guardian_branding.json" <<EOF
{
  "HomepageIsNewTabPage": false,
  "HomepageLocation": "${HOMEPAGE_URL}",
  "RestoreOnStartup": 4,
  "RestoreOnStartupURLs": ["${HOMEPAGE_URL}"]
}
EOF
log "Chromium homepage policy set to ${HOMEPAGE_URL}"

# Boot splash (best-effort)
if [[ -f "/usr/share/plymouth/themes/pix/splash.png" ]]; then
  cp -n /usr/share/plymouth/themes/pix/splash.png /usr/share/plymouth/themes/pix/splash.png.guardian.bak || true
  cp "$BRAND_DIR/boot-splash/splash-1920x1080.png" /usr/share/plymouth/themes/pix/splash.png
  if command -v update-initramfs >/dev/null 2>&1; then
    update-initramfs -u || true
  fi
  log "Boot splash applied (plymouth pix theme)"
else
  warn "Plymouth pix splash path not found, skipped boot splash patch"
fi

# MOTD brand line
cat > /etc/motd <<EOF
Guardian Shield Device
Managed endpoint: authorized monitoring enabled
Brand deployment: ${BRAND_DIR}
EOF
log "MOTD branding applied"

# Ensure desktop owner can read assets
chown -R root:root "$BRAND_DIR"
chmod -R a+r "$BRAND_DIR"

log "Branding apply complete"
info "Next: reboot recommended"
info "Command: sudo reboot"

#!/bin/bash
# ============================================================
# 🛡️ Guardian Shield — Raspberry Pi 4 Deployment Script
# Run this on the Raspberry Pi as root (sudo bash deploy-rpi.sh)
# ============================================================

set -e  # Exit on any error

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log()   { echo -e "${GREEN}[✓]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
error() { echo -e "${RED}[✗]${NC} $1"; exit 1; }
info()  { echo -e "${BLUE}[→]${NC} $1"; }

echo ""
echo -e "${BLUE}╔══════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║   🛡️  Guardian Shield — RPi4 Deployment     ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════╝${NC}"
echo ""

# ── Check root ──────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
   error "Please run as root: sudo bash deploy-rpi.sh"
fi

INSTALL_DIR="/opt/guardian-shield"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

pick_secret() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32
    else
        tr -dc 'A-Za-z0-9' </dev/urandom | head -c 64
    fi
}

# ── 1. System Update & Dependencies ─────────────────────────
info "Updating system packages..."
apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip python3-venv \
    nodejs npm \
    git curl wget \
    libffi-dev libssl-dev \
    scrot imagemagick \
    clamav clamav-daemon \
    xvfb x11-utils \
    net-tools \
    2>/dev/null
log "System dependencies installed"

# ── 2. Node.js version check (need 18+) ─────────────────────
NODE_VER=$(node --version | sed 's/v//' | cut -d. -f1)
if [ "$NODE_VER" -lt 18 ]; then
    warn "Node.js $NODE_VER found — upgrading to Node 20 LTS..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt-get install -y nodejs
    log "Node.js upgraded to $(node --version)"
else
    log "Node.js $(node --version) ✓"
fi

# ── 3. Copy app files to /opt ────────────────────────────────
info "Installing Guardian Shield to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
rm -rf "$INSTALL_DIR/agent" "$INSTALL_DIR/server" "$INSTALL_DIR/dashboard"

# Support both layouts:
# 1) New split layout: GUARDIAN/pi-agent + GUARDIAN/parent-dashboard
# 2) Legacy layout:    GUARDIAN/PI + GUARDIAN/WINDOWS
if [[ -d "$SCRIPT_DIR/../agent" ]]; then
    AGENT_SRC="$(cd "$SCRIPT_DIR/../agent" && pwd)"
elif [[ -d "$REPO_ROOT/pi-agent/agent" ]]; then
    AGENT_SRC="$REPO_ROOT/pi-agent/agent"
elif [[ -d "$REPO_ROOT/PI/agent" ]]; then
    AGENT_SRC="$REPO_ROOT/PI/agent"
else
    error "Agent source folder not found. Expected PI/agent."
fi

if [[ -d "$REPO_ROOT/parent-dashboard/server-ts" ]]; then
    SERVER_SRC="$REPO_ROOT/parent-dashboard/server-ts"
elif [[ -d "$SCRIPT_DIR/server" ]]; then
    SERVER_SRC="$SCRIPT_DIR/server"
elif [[ -d "$REPO_ROOT/WINDOWS/server" ]]; then
    SERVER_SRC="$REPO_ROOT/WINDOWS/server"
else
    error "Server source folder not found. Copy full GUARDIAN repo or include server/ beside deploy-rpi.sh."
fi

if [[ -d "$REPO_ROOT/parent-dashboard/web-ts" ]]; then
    DASHBOARD_SRC="$REPO_ROOT/parent-dashboard/web-ts"
elif [[ -d "$SCRIPT_DIR/dashboard" ]]; then
    DASHBOARD_SRC="$SCRIPT_DIR/dashboard"
elif [[ -d "$REPO_ROOT/WINDOWS/dashboard" ]]; then
    DASHBOARD_SRC="$REPO_ROOT/WINDOWS/dashboard"
else
    error "Dashboard source folder not found. Copy full GUARDIAN repo or include dashboard/ beside deploy-rpi.sh."
fi

cp -r "$AGENT_SRC"     "$INSTALL_DIR/agent"
cp -r "$SERVER_SRC"    "$INSTALL_DIR/server"
cp -r "$DASHBOARD_SRC" "$INSTALL_DIR/dashboard"
cp    "$REPO_ROOT/README.md" "$INSTALL_DIR/" 2>/dev/null || true
log "Files copied to $INSTALL_DIR"

# ── 4. Python virtual environment ───────────────────────────
info "Setting up Python virtual environment..."
cd "$INSTALL_DIR/agent"
python3 -m venv venv
source venv/bin/activate

# mss requires X display — on Pi headless, use scrot fallback
pip install --quiet --upgrade pip
pip install --quiet \
    Pillow \
    python-socketio[client] \
    websocket-client \
    watchdog \
    psutil \
    requests \
    aiohttp \
    cryptography

# mss needs X — try it, fall back to scrot
pip install --quiet mss || warn "mss failed (no display) — will use scrot fallback"

deactivate
log "Python environment ready"

# ── 5. Server — npm install ──────────────────────────────────
info "Installing server Node.js dependencies..."
cd "$INSTALL_DIR/server"
# Remove node_modules (Windows builds won't work on ARM)
rm -rf node_modules
npm install --silent

# Create/refresh secure runtime secrets for server<->agent auth
if [[ ! -f ".env" ]]; then
    JWT_SECRET="$(pick_secret)"
    AGENT_SHARED_SECRET="$(pick_secret)"
    cat > .env <<EOF
PORT=3000
NODE_ENV=production
JWT_SECRET=${JWT_SECRET}
ADMIN_USERNAME=admin
ADMIN_PASSWORD=admin123
AGENT_SHARED_SECRET=${AGENT_SHARED_SECRET}
AGENT_AUTH_REQUIRED=true
SCREENSHOT_RETENTION_DAYS=7
MAX_SCREENSHOT_SIZE_MB=5
EOF
    log "Created server .env with generated secrets"
else
    if ! grep -q '^AGENT_SHARED_SECRET=' .env; then
        AGENT_SHARED_SECRET="$(pick_secret)"
        echo "AGENT_SHARED_SECRET=${AGENT_SHARED_SECRET}" >> .env
        log "Added AGENT_SHARED_SECRET to existing server .env"
    fi
fi
log "Server dependencies installed"

# ── 6. Dashboard — npm install ───────────────────────────────
info "Building dashboard..."
cd "$INSTALL_DIR/dashboard"
rm -rf node_modules
npm install --silent
npm run build --silent 2>/dev/null || warn "Vite build skipped — running dev mode"
log "Dashboard ready"

# ── 7. Configure agent for Raspberry Pi ─────────────────────
info "Configuring agent..."
PIIP=$(hostname -I | awk '{print $1}')
AGENT_SHARED_SECRET=$(grep '^AGENT_SHARED_SECRET=' "$INSTALL_DIR/server/.env" | head -n1 | cut -d= -f2-)
cat > "$INSTALL_DIR/agent/config.json" <<EOF
{
    "server_url": "http://${PIIP}:3000",
    "device_name": "$(hostname)",
    "screenshot_interval": 5,
    "screenshot_quality": 60,
    "auth_token": "${AGENT_SHARED_SECRET}",
    "blocklist_update_interval": 3600,
    "download_watch_paths": ["/home/pi/Downloads"],
    "quarantine_path": "/opt/guardian-shield/quarantine",
    "log_level": "INFO",
    "stealth_mode": false,
    "platform": "linux-arm",
    "use_scrot_fallback": true
}
EOF
mkdir -p /opt/guardian-shield/quarantine
log "Agent configured (server at http://${PIIP}:3000)"

# ── 8. Systemd service — Guardian Server ────────────────────
info "Creating systemd service for server..."
cat > /etc/systemd/system/guardian-server.service <<EOF
[Unit]
Description=Guardian Shield — Node.js Server
After=network.target
Wants=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/guardian-shield/server
ExecStart=/usr/bin/node index.js
Restart=always
RestartSec=5
Environment=NODE_ENV=production
Environment=PORT=3000
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
log "guardian-server.service created"

# ── 9. Systemd service — Guardian Agent ─────────────────────
info "Creating systemd service for agent..."

# Create a headless X wrapper (Xvfb) for screenshot support
cat > /opt/guardian-shield/agent/start_agent.sh <<'SHEOF'
#!/bin/bash
# Start virtual display for headless screenshot capture
export DISPLAY=:99
Xvfb :99 -screen 0 1920x1080x24 &
XVFB_PID=$!
sleep 2

cd /opt/guardian-shield/agent
source venv/bin/activate
python3 main.py

kill $XVFB_PID 2>/dev/null || true
SHEOF
chmod +x /opt/guardian-shield/agent/start_agent.sh

cat > /etc/systemd/system/guardian-agent.service <<EOF
[Unit]
Description=Guardian Shield — Python Monitoring Agent
After=network.target guardian-server.service
Wants=guardian-server.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/guardian-shield/agent
ExecStart=/opt/guardian-shield/agent/start_agent.sh
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
log "guardian-agent.service created"

# ── 10. Enable and start services ───────────────────────────
info "Enabling services to start on boot..."
systemctl daemon-reload
systemctl enable guardian-server.service
systemctl enable guardian-agent.service

systemctl start guardian-server.service
sleep 3

if systemctl is-active --quiet guardian-server.service; then
    log "guardian-server is RUNNING ✓"
else
    warn "guardian-server failed to start — check: sudo journalctl -u guardian-server -n 30"
fi

systemctl start guardian-agent.service
sleep 2

if systemctl is-active --quiet guardian-agent.service; then
    log "guardian-agent is RUNNING ✓"
else
    warn "guardian-agent failed to start — check: sudo journalctl -u guardian-agent -n 30"
fi

# ── 11. Update ClamAV definitions ───────────────────────────
info "Updating ClamAV virus definitions (this may take a few minutes)..."
systemctl stop clamav-freshclam 2>/dev/null || true
freshclam --quiet || warn "ClamAV update failed — run 'sudo freshclam' manually later"
log "ClamAV definitions updated"

# ── Done! ────────────────────────────────────────────────────
PIIP=$(hostname -I | awk '{print $1}')
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   ✅  Guardian Shield Deployed Successfully!             ║${NC}"
echo -e "${GREEN}╠══════════════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║                                                          ║${NC}"
echo -e "${GREEN}║  Dashboard URL : http://${PIIP}:3000                     ${NC}"
echo -e "${GREEN}║  Login         : admin / admin123                        ${NC}"
echo -e "${GREEN}║  Agent Config  : /opt/guardian-shield/agent/config.json  ${NC}"
echo -e "${GREEN}║  Server Logs   : sudo journalctl -u guardian-server -f   ${NC}"
echo -e "${GREEN}║  Agent Logs    : sudo journalctl -u guardian-agent -f    ${NC}"
echo -e "${GREEN}║                                                          ${NC}"
echo -e "${GREEN}║  ⚠️  Change the default password after first login!      ${NC}"
echo -e "${GREEN}║                                                          ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""

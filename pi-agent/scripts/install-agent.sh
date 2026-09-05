#!/bin/bash
# ============================================================
# 🛡️ Guardian Shield — Raspberry Pi Agent Installer
# Run this on the Pi: sudo bash install-agent.sh <WINDOWS_PC_IP> [AGENT_SHARED_SECRET]
#
# Example: sudo bash install-agent.sh 192.168.1.100
# ============================================================

set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log()   { echo -e "${GREEN}[✓]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
error() { echo -e "${RED}[✗]${NC} $1"; exit 1; }
info()  { echo -e "${BLUE}[→]${NC} $1"; }

echo ""
echo -e "${BLUE}╔══════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║  🛡️  Guardian Shield — Agent Install (Pi)   ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════╝${NC}"
echo ""

# ── Check root ──────────────────────────────────────────────
[[ $EUID -ne 0 ]] && error "Run as root: sudo bash install-agent.sh <WINDOWS_IP>"

# ── Get Windows PC IP from argument ─────────────────────────
WINDOWS_IP="${1}"
AGENT_SHARED_SECRET="${2:-}"
if [ -z "$WINDOWS_IP" ]; then
    echo ""
    echo "  No IP provided. Using Windows Mobile Hotspot default: 192.168.137.1"
    echo "  (This works when your Windows PC hotspot is turned ON)"
    echo "  To use a different IP: sudo bash install-agent.sh 192.168.1.XXX"
    echo ""
    WINDOWS_IP="192.168.137.1"
fi
info "Windows server will be at: http://${WINDOWS_IP}:3000"
if [ -n "$AGENT_SHARED_SECRET" ]; then
    info "Agent shared auth token provided"
else
    warn "No agent shared token provided (OK unless server enforces AGENT_AUTH_REQUIRED=true)"
fi

INSTALL_DIR="/opt/guardian-agent"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── 1. Install system packages ───────────────────────────────
info "Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip python3-venv \
    scrot grim imagemagick \
    xvfb x11-utils \
    clamav \
    wget curl net-tools \
    2>/dev/null
log "System packages installed"

# Academic-progress accuracy, installed separately so a missing package in one
# Pi OS release cannot fail the whole apt line above. The progress collectors
# work without it — they just count wall-clock time instead of trimming the
# minutes the child was away from the machine.
apt-get install -y -qq xprintidle 2>/dev/null \
    && log "Idle detection installed (academic time will be idle-trimmed)" \
    || warn "xprintidle unavailable — academic time counts wall-clock minutes"

# ── 2. Copy agent files ──────────────────────────────────────
info "Installing agent to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cp -r "$SCRIPT_DIR/../agent/." "$INSTALL_DIR/"
mkdir -p "$INSTALL_DIR/quarantine"
log "Agent files copied"

# ── 3. Python virtual environment ───────────────────────────
info "Setting up Python environment..."
cd "$INSTALL_DIR"
python3 -m venv venv
source venv/bin/activate
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
pip install --quiet mss || warn "mss skipped (no display) — scrot fallback active"
deactivate
log "Python environment ready"

# ── 4. Write config.json with Windows IP ────────────────────
info "Configuring agent to connect to Windows server..."
cat > "$INSTALL_DIR/config.json" <<EOF
{
    "server_url": "http://${WINDOWS_IP}:3000",
    "device_name": "$(hostname)",
    "screenshot_interval": 5,
    "screenshot_quality": 60,
    "auth_token": "${AGENT_SHARED_SECRET}",
    "blocklist_update_interval": 3600,
    "download_watch_paths": ["/home/pi/Downloads"],
    "quarantine_path": "/opt/guardian-agent/quarantine",
    "log_level": "INFO",
    "stealth_mode": false,
    "platform": "linux-arm",
    "use_scrot_fallback": true
}
EOF
log "Config written — pointing to http://${WINDOWS_IP}:3000"

# ── 5. Create Xvfb + agent start script ─────────────────────
cat > "$INSTALL_DIR/start.sh" <<'SHEOF'
#!/bin/bash
# Prefer real Wayland desktop session when available; fallback to Xvfb.
XVFB_PID=""
if [ -S /run/user/1000/wayland-0 ]; then
    export XDG_RUNTIME_DIR=/run/user/1000
    export WAYLAND_DISPLAY=wayland-0
else
    export DISPLAY=:99
    Xvfb :99 -screen 0 1920x1080x24 &
    XVFB_PID=$!
    sleep 2
fi

cd /opt/guardian-agent
source venv/bin/activate
# --no-watchdog is deliberate: systemd (Restart=always) is the single supervisor.
# Without this flag main.py spawns its own detached watchdog, which restarts the
# agent independently of systemd. Both then bring an agent up under the same
# device_name, and when the stale socket disconnects the server marks the live
# device offline — that was the "goes offline every few minutes" flap.
python3 main.py --no-watchdog

if [ -n "$XVFB_PID" ]; then
    kill $XVFB_PID 2>/dev/null || true
fi
SHEOF
chmod +x "$INSTALL_DIR/start.sh"
log "Start script created"

# ── 6. Create systemd service ────────────────────────────────
info "Creating systemd service (auto-start on boot)..."
cat > /etc/systemd/system/guardian-agent.service <<EOF
[Unit]
Description=Guardian Shield — Python Monitoring Agent
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/guardian-agent
ExecStart=/opt/guardian-agent/start.sh
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable guardian-agent.service
systemctl start guardian-agent.service
sleep 3

if systemctl is-active --quiet guardian-agent.service; then
    log "guardian-agent is RUNNING ✓"
else
    warn "Agent failed to start immediately — may need server running first"
    warn "Check: sudo journalctl -u guardian-agent -n 30"
fi

# ── Done ─────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  ✅  Guardian Agent Installed on Raspberry Pi!       ║${NC}"
echo -e "${GREEN}╠══════════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║                                                      ║${NC}"
echo -e "${GREEN}║  Sending screenshots to: http://${WINDOWS_IP}:3000   ${NC}"
echo -e "${GREEN}║  Device name: $(hostname)                            ${NC}"
if [ -n "$AGENT_SHARED_SECRET" ]; then
echo -e "${GREEN}║  Agent auth token: configured                        ${NC}"
else
echo -e "${GREEN}║  Agent auth token: not configured                    ${NC}"
fi
echo -e "${GREEN}║                                                      ║${NC}"
echo -e "${GREEN}║  Logs: sudo journalctl -u guardian-agent -f          ║${NC}"
echo -e "${GREEN}║  Stop: sudo systemctl stop guardian-agent            ║${NC}"
echo -e "${GREEN}║                                                      ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${YELLOW}  → Now start the server on your Windows PC first!    ${NC}"
echo ""

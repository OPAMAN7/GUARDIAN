# PLAN TO SUCCESS (MASTER RUNBOOK)

This is the only planning/operation guide.
All other planning docs were removed.

## Scope Locked
1. Keep clean folder split.
2. No custom-command system work.
3. Build custom-branded Raspberry Pi OS look with minimal-risk changes.
4. Full parent dashboard GUI overhaul (design comes from Stitch).
5. Keep core parent-child tracking stable.

## Folder Model (Final)
GUARDIAN/
- parent-dashboard/
  - server-ts/
  - web-ts/
  - shared-types/
- windows-app/
  - scripts/
  - watchdog/
  - launcher/
  - config/
- pi-agent/
  - agent/
  - scripts/
  - docs/
- docs/
  - plan to success.md

## Progress
1. Folder split: completed.
2. Legacy mixed folders removed: completed.
3. Script path refactor for new folders: completed.
4. Next active step: real parent-child validation using Raspberry Pi.
5. Waiting input: Raspberry Pi IP after OS install.

---

## Step-by-Step (Execution Order)

## Step 0 - Fresh Raspberry Pi OS install (do this first if Pi is blank)
Goal: prepare Pi for remote setup.

Selected OS variant:
- Raspberry Pi OS with Desktop

1. Flash Raspberry Pi OS to SD card.
2. In Raspberry Pi Imager advanced options:
- set hostname
- enable SSH
- set username/password
- set Wi-Fi (if not using ethernet)
3. Boot Pi and verify it appears on your network.
4. From Windows PowerShell verify SSH:
- ssh <PI_USER>@<PI_IP>

Step 0.1 click-by-click in Raspberry Pi Imager:
1. Open Raspberry Pi Imager.
2. Click Choose Device and select your Pi model.
3. Click Choose OS and select Raspberry Pi OS (64-bit) with Desktop.
4. Click Choose Storage and select your SD card.
5. Click Next, then Edit Settings.
6. In General tab:
- set hostname: aman-test
- set username: pi
- set strong password
- set Wi-Fi SSID/password if needed
- set timezone and keyboard layout
7. In Services tab:
- enable SSH
- choose password authentication
8. Save settings and write image.
9. Insert SD card into Pi and power on.
10. Wait 1-2 minutes, then discover Pi IP from router/hotspot panel.
11. Test from Windows PowerShell:
- ssh pi@<PI_IP>

Step 0.2 find Pi IP from Windows (PowerShell):
1. Refresh ARP table:
- arp -a
2. Optional ping sweep for hotspot subnet:
- 1..254 | ForEach-Object { ping -n 1 -w 30 192.168.137.$_ > $null }
- arp -a
3. Try SSH to likely candidates:
- ssh pi@192.168.137.X
4. If hostname resolves:
- ping aman-test.local

Tip:
- New Pi entries usually appear as dynamic hosts in 192.168.137.x range on hotspot mode.

Pass criteria:
- You can SSH into Pi from Windows.

---

## Step 0.5 - Branding asset prep (before visual customization)
Goal: keep branding rollout clean and repeatable.

Prepare these assets now:
1. Logo SVG and PNG
2. App icon set (512, 256, 128, 64, 32)
3. Wallpaper (1920x1080)
4. Login background image
5. Boot splash image
6. Color tokens file (primary, accent, alert, background)
7. Typography selection (headline + body)

Recommended workspace location:
- /home/pi/guardian-shield/branding-assets

Pass criteria:
- All files are ready before Step 5 branding changes start.

Validate assets on Windows before copying to Pi:
- powershell -ExecutionPolicy Bypass -File windows-app/scripts/validate-branding-assets.ps1

Sync branding-assets only (fast iteration):
- powershell -ExecutionPolicy Bypass -File windows-app/scripts/sync-branding-assets.ps1 -PiIp <PI_IP> -PiUser pi

---

## Step 1 - Parent machine baseline (Windows)
Goal: parent dashboard runs from new structure only.

1. Install prerequisites:
- Node.js LTS
- Python 3.10+

2. Run setup:
- windows-app/scripts/install-server.bat

3. Start parent server:
- windows-app/scripts/start-server.bat

4. Verify:
- Open http://localhost:3000
- Login works
- No crash in watchdog terminal

Pass criteria:
- Parent dashboard is running from new folder paths.

Hotspot quick check on Windows (PowerShell):
- ipconfig | findstr /i "IPv4"
- If hotspot is active, parent IP is commonly 192.168.137.1.

---

## Step 2 - Stop self-tracking mode
Goal: parent PC should not be child in production.

1. If running local simulation, stop it:
- windows-app/scripts/stop-live-control.bat

2. Do not run live simulation for production tests:
- windows-app/scripts/start-live-control.bat is for local demo only.

Pass criteria:
- No EDGE-LIVE-CONTROLLED style local child device in production validation.

Exact command (Windows):
- windows-app/scripts/stop-live-control.bat

---

## Step 3 - Connect real Raspberry Pi child
Goal: child telemetry comes from Pi only.

1. Copy repo (or pi-agent) to Pi.
2. On Pi run:
- cd /path/to/GUARDIAN/pi-agent/scripts
- chmod +x install-agent.sh
- sudo bash install-agent.sh <WINDOWS_PARENT_IP>

Hotspot-mode exact command (Pi):
- sudo bash install-agent.sh 192.168.137.1

3. Optional token mode:
- sudo bash install-agent.sh <WINDOWS_PARENT_IP> <AGENT_SHARED_SECRET>

4. Verify service:
- sudo systemctl status guardian-agent
- sudo journalctl -u guardian-agent -f

Quick health checks:
- curl -I http://192.168.137.1:3000/api/health
- ping -c 3 192.168.137.1

Pass criteria:
- Pi appears online in dashboard.
- Live feed and alerts come from Pi.

Exact command pack for your confirmed path:

On Windows PowerShell:
- scp -r "C:\Users\Aman\Desktop\GUARDIAN" pi@<PI_IP>:/home/pi/guardian-shield
- ssh pi@<PI_IP>

On Raspberry Pi shell:
- cd /home/pi/guardian-shield/pi-agent/scripts
- chmod +x install-agent.sh
- sudo bash install-agent.sh 192.168.137.1
- sudo systemctl status guardian-agent
- sudo journalctl -u guardian-agent -f

Optional automation from Windows PowerShell:
- powershell -ExecutionPolicy Bypass -File windows-app/scripts/setup-pi-child.ps1 -PiIp <PI_IP> -PiUser pi -ParentIp 192.168.137.1

Until Pi IP is known:
- Keep using <PI_IP> placeholder.
- Once OS install is done, replace <PI_IP> in the same commands.

---

## Step 4 - Networking rule (critical)
1. Never set Pi server URL to localhost for parent mode.
2. On Pi, localhost means Pi itself, not your Windows parent.
3. Use Windows parent IP (example hotspot default often 192.168.137.1).

Rule reminder:
- Never use localhost in pi-agent config for parent mode.
- Parent IP must be reachable from Pi over the same network.

Pass criteria:
- Pi config points to Windows parent IP on port 3000.

---

## Step 5 - Branding the Raspberry Pi OS (minimal-risk)
Goal: looks fully yours without breaking system stability.

Allowed changes now:
1. Hostname and visible device name.
2. Boot splash art.
3. Login screen background/branding.
4. Wallpaper and icon set.
5. Browser home/start page branding.
6. Theme colors/fonts.

Do not do in this phase:
1. Kernel rebuild.
2. Package manager/core distro surgery.
3. Security disablement.

Pass criteria:
- Device looks branded and still updates/runs reliably.

Run command (on Pi):
- cd /home/pi/guardian-shield
- chmod +x pi-agent/scripts/apply-branding.sh
- sudo bash pi-agent/scripts/apply-branding.sh --assets /home/pi/guardian-shield/branding-assets --owner-user pi --hostname aman-test --homepage http://192.168.137.1:3000

One-command remote flow from Windows:
- powershell -ExecutionPolicy Bypass -File windows-app/scripts/apply-branding-remote.ps1 -PiIp <PI_IP> -PiUser pi -ParentIp 192.168.137.1 -Hostname aman-test

---

## Step 6 - Parent dashboard full GUI overhaul
Goal: complete visual redesign while retaining stable behavior.

Input required from you:
1. Stitch sitemap.
2. Desktop/mobile layouts.
3. Components and states.
4. Tokens (color/type/spacing).
5. Interaction rules.

Use this file to provide the full input package:
- docs/stitch-handoff-template.md

Build order:
1. Apply tokens.
2. Rebuild nav and layout shell.
3. Redesign core pages:
- Live
- Devices
- Blocklist
- Alerts
- History/Timeline
4. Keep API behavior compatible first.

Pass criteria:
- Dashboard is fully rebranded and secure-looking without feature regressions.

---

## Step 7 - QA checklist
1. Parent server starts cleanly from windows-app/scripts.
2. Pi agent starts and auto-restarts on failure.
3. Live stream works.
4. Blocklist update reaches Pi.
5. NUH UH page behavior still works.
6. Alerts appear correctly.
7. Login and session are stable.

Pass criteria:
- End-to-end parent-child flow passes on real devices.

---

## Single Source Rule
1. This file is the only active runbook.
2. Any future steps must be appended here only.
3. Avoid creating duplicate planning docs.

---

## Live Tick Checklist

Parent baseline:
- [ ] Install Node.js LTS on Windows
- [ ] Install Python 3.10+ on Windows
- [ ] Run windows-app/scripts/install-server.bat
- [ ] Run windows-app/scripts/start-server.bat
- [ ] Confirm http://localhost:3000 opens

Stop self-tracking:
- [ ] Run windows-app/scripts/stop-live-control.bat
- [ ] Confirm local simulation mode is not active

Pi OS and network:
- [ ] Flash Raspberry Pi OS Desktop
- [ ] Enable SSH in Raspberry Pi Imager
- [ ] Boot Pi and get Pi IP
- [ ] Verify ssh pi@<PI_IP> works

Pi child setup:
- [ ] Copy repo to /home/pi/guardian-shield
- [ ] Run sudo bash install-agent.sh 192.168.137.1
- [ ] Check guardian-agent service status
- [ ] Confirm Pi appears online on dashboard

Branding prep:
- [ ] Place logo files in branding-assets/logo
- [ ] Place icon files in branding-assets/icons
- [ ] Place wallpaper/login/splash files in branding-assets
- [ ] Fill branding-assets/tokens/colors.json

Branding apply:
- [ ] Run sudo bash pi-agent/scripts/apply-branding.sh with hostname aman-test
- [ ] Reboot Pi
- [ ] Verify branded wallpaper/login/splash/homepage

Final QA:
- [ ] Live feed from Pi updates
- [ ] Blocklist update reaches Pi
- [ ] NUH UH page behavior works
- [ ] Alerts show on dashboard

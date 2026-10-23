# Anti Gravity — Network Recalibration Decision Log

## Date: 2026-10-21

## Context

The `GUARD.bat` / `GUARD.py` network recalibration (menu option 4) was not reliably re-pointing the Pi agent at the dashboard when the Wi-Fi network changed. This document captures every problem found and the fix applied.

---

## Problems Found

### 1. Stale DNS & ARP caches (CRITICAL)

**Problem:** When the laptop switches from one Wi-Fi to another, Windows keeps the old DNS resolution for the Pi hostname and old ARP entries for IP→MAC mappings. The recalibration would probe the Pi using cached data from the *previous* network, either connecting to the wrong device or timing out on a ghost IP.

**Fix:** Added `flush_network_caches()` — flushes both `ipconfig /flushdns` and `netsh interface ip delete arpcache` at the start of every recalibration. Non-fatal if they fail (some machines need elevation).

---

### 2. TCP probe timeout too short (0.3s)

**Problem:** Both `detect_pi_ip()` and `_quick_detect_pi_ip()` used a 0.3-second TCP timeout on port 22. On a freshly associated network (e.g., mobile hotspot just started, Pi's DHCP lease still settling), the Pi's SSH server often takes 0.5–1.5s to respond. The probe would silently fail and fall through to a full subnet scan — or worse, declare the Pi unreachable.

**Fix:**
- Increased base timeout from 0.3s → 0.8s for quick detection.
- Added **retry logic** with increasing timeouts: `[0.8s, 1.5s, 2.5s]` across up to 3 attempts, with a back-off delay between retries.
- Subnet scan is now a last resort, only after all retry rounds fail.

---

### 3. No validation that the host IP is actually bound

**Problem:** `resolve_host_ip()` could return a cached `last_host_ip` or a subnet-matched address that belongs to an adapter that is no longer connected (e.g., the laptop was on 192.168.1.x, now it's on 10.x.x.x, but the old Wi-Fi adapter still enumerates). The Pi would be told to report to an IP that doesn't exist on any active interface.

**Fix:** Added `verify_local_ip_bound()` which tries to `bind()` a socket to the IP. If it fails, the recalibration falls back to a different local IP from the fresh candidate list and logs a clear warning.

---

### 4. No end-to-end verification after recalibration

**Problem:** `recalibrate_pi_sync()` would patch the Pi's config.json, restart the service, and declare "Sync complete" without ever checking:
- Is the Pi agent service actually running?
- Can the Pi reach the dashboard over HTTP?

The deploy path (`deploy_pi_agent`) had this check (`verify_pi_online`), but recalibrate didn't. So a "successful" recalibration could silently leave a broken setup.

**Fix:** `recalibrate_pi_sync()` now calls `verify_pi_online()` at the end, which:
1. Checks `systemctl is-active guardian-agent` on the Pi.
2. Runs a `curl` from the Pi to `http://<host_ip>:3000/api/health`.
3. Checks if the local dashboard is listening on port 3000.

---

### 5. Local Pi agent config left stale

**Problem:** After a successful recalibration, `pi-agent/agent/config.json` in the repo could still point to the old IP (e.g., `http://198.18.0.1:3000`). The `apply_ip_locally()` function should fix this, but if it silently failed, the next `deploy` would push the stale IP back to the Pi — undoing the recalibration.

**Fix:** After saving the config, recalibration now reads back `pi-agent/agent/config.json` and warns if `server_url` doesn't match the expected value.

---

### 6. No CLI `recalibrate` command

**Problem:** `GUARD.bat recalibrate` was not a recognized command. The only way to recalibrate was through the interactive menu (option 4). This meant:
- No scripting/automation support.
- No quick command-line fix when the network changes.

**Fix:** Added `recalibrate` (plus aliases `recal`, `resync`, `network`) to `run_cli()`. Updated `GUARD.bat` header comments to document it.

---

## Files Changed

| File | Change |
|------|--------|
| `GUARD.py` | Added `flush_network_caches()`, `verify_local_ip_bound()`. Rewrote `detect_pi_ip()` with retries. Rewrote `recalibrate_pi_sync()` with all 5 phases. Increased `_quick_detect_pi_ip` timeout. Added `recalibrate` CLI command. |
| `GUARD.bat` | Updated header comments with the new `recalibrate` subcommand. |

---

## How to Test

```bash
# From the command line (new!):
GUARD.bat recalibrate

# Or from the interactive menu:
GUARD.bat
# Then press 4 (Recalibrate Network IP Sync)
```

### Expected output with the fixes:

```
Flushing DNS and ARP caches...
Local IPv4 candidates: 192.168.137.1, 10.0.0.5
Pi found at 192.168.137.100
Subnet aligned: host=192.168.137.1, pi=192.168.137.100

[INIT] NETWORK RECALIBRATION
  -> Flush network caches...
  -> Resolve active host network...
  -> Find Pi endpoint...
  -> Validate host IP binding...
  -> Patch Pi server_url...
  -> Commit sync profile...
  -> End-to-end verification...

Updating local repo configs (source of truth):
  - pi-agent/agent/config.json: http://old.ip:3000 -> http://192.168.137.1:3000
  - windows-app local-agent-config.json: already localhost (ok)

Patching config.json on the Pi...
  server_url=http://192.168.137.1:3000
Restarting guardian-agent...
  guardian-agent: active (enabled=enabled, main.py processes=1)

Sync complete. Pi now points to http://192.168.137.1:3000

Verifying deployment...
  Pi service 'guardian-agent': active
  Pi -> dashboard http://192.168.137.1:3000/api/health: 200
  Local dashboard port 3000: listening
Verification PASSED.
```

---

## Decision Summary

Every fix targets a real failure mode observed in the field:

1. **DNS/ARP flush** → stale cache from hotel Wi-Fi pointed at wrong device
2. **Longer timeouts + retries** → hotspot startup race condition
3. **IP binding check** → ghost adapter from disconnected network
4. **End-to-end verify** → silent config patch failure went unnoticed
5. **Local config staleness warning** → next deploy undid the recalibration
6. **CLI command** → no way to script or quick-fix from terminal

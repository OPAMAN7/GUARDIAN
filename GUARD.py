#!/usr/bin/env python3
"""
GUARD - Guardian unified sci-fi launcher
- Launch dashboard, Windows agent tools, and Pi agent sync
- Recalibrate Pi<->laptop IP mapping when Wi-Fi changes
- Shows task status with green DONE markers
"""

from __future__ import annotations

import json
import ipaddress
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = ROOT_DIR / "windows-app" / "config" / "guard_runtime.json"

DASHBOARD_SCRIPT = ROOT_DIR / "windows-app" / "scripts" / "start-server.bat"
WINDOWS_AGENT_SCRIPT = ROOT_DIR / "windows-app" / "scripts" / "start-live-control.bat"
STOP_WINDOWS_AGENT_SCRIPT = ROOT_DIR / "windows-app" / "scripts" / "stop-live-control.bat"

# Local copies of the agent configs that GUARD keeps in sync with the chosen IP.
PI_AGENT_DIR = ROOT_DIR / "pi-agent" / "agent"
PI_AGENT_CONFIG = PI_AGENT_DIR / "config.json"
WINDOWS_AGENT_CONFIG = ROOT_DIR / "windows-app" / "config" / "local-agent-config.json"

# The dashboard the browser actually loads. server-ts serves web-ts/dist as static
# files, so editing web-ts/main.js changes NOTHING until Vite rebuilds dist. Nothing
# in start-server.bat did that, which is how a week-old bundle stayed live while the
# server moved on (BUG-29). GUARD now builds it whenever it is stale.
WEB_DIR = ROOT_DIR / "parent-dashboard" / "web-ts"
WEB_DIST = WEB_DIR / "dist"
WEB_SOURCE_GLOBS = ("*.js", "*.ts", "*.css", "*.html", "*.json")

# Server-side secrets (Gemini key, Telegram bot token) live here and nowhere else.
# The file is gitignored on purpose: it must never reach GitHub, and nothing in it
# is ever bundled into the browser build.
SERVER_ENV_PATH = ROOT_DIR / "parent-dashboard" / "server-ts" / ".env"

# Where an SSH data pull lands. One timestamped folder per run, never overwritten,
# so two pulls can be compared when something looks wrong.
DATA_EXPORT_DIR = ROOT_DIR / "data-exports"

DEFAULTS = {
    "pi_hostname": "aman-test",
    "pi_username": "pi",
    "pi_password": "1234",
    # Cached last-known addresses; both are re-validated (and re-detected)
    # on every deploy/recalibrate, so a stale value is never fatal.
    "last_pi_ip": "192.168.1.15",
    "last_host_ip": "192.168.1.10",
    # Single source of truth for where the agents connect (the Windows dashboard).
    # server_ip = "auto" picks the local IPv4 that shares the Pi's /24.
    "server_ip": "auto",
    "server_port": "3000",
    # Where the agent lives on the Pi and how it is managed.
    "pi_install_dir": "/opt/guardian-agent",
    "pi_service": "guardian-agent",
}

STATUS = {
    "dashboard": False,
    "windows_agent": False,
    "pi_agent": False,
    "recalibrated": False,
    "data_pulled": False,
}

AUTO_REDIRECT_DONE = False


class C:
    RESET = "\x1b[0m"
    CYAN = "\x1b[96m"
    BLUE = "\x1b[94m"
    GREEN = "\x1b[92m"
    YELLOW = "\x1b[93m"
    RED = "\x1b[91m"
    DIM = "\x1b[2m"
    BOLD = "\x1b[1m"


def enable_ansi() -> None:
    if os.name == "nt":
        os.system("")


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def print_banner() -> None:
    print(C.CYAN + C.BOLD)
    print("============================================================")
    print("  GUARD :: GUARDIAN UNIFIED RUNTIME AND DEPLOY CONSOLE")
    print("============================================================")
    print(C.RESET + C.DIM + "Sci-fi control panel for Dashboard + Pi sync + Windows agent" + C.RESET)
    print()


def status_tag(done: bool) -> str:
    if done:
        return C.GREEN + "[✓ DONE]" + C.RESET
    return C.DIM + "[....]" + C.RESET


def _telegram_tag() -> str:
    """Menu badge for the Telegram channel, read live from server-ts/.env."""
    try:
        has_token, has_chat, enabled = telegram_state()
    except Exception:
        return C.DIM + "[TG  ]" + C.RESET
    if has_token and has_chat:
        return (C.GREEN + "[TG ON]" + C.RESET) if enabled else (C.YELLOW + "[TG MUTE]" + C.RESET)
    return C.DIM + "[TG OFF]" + C.RESET


def _datapull_tag() -> str:
    """Menu badge for the SSH data pull — green once one has run this session."""
    if STATUS["data_pulled"]:
        return C.GREEN + "[PULL ✓]" + C.RESET
    return C.BLUE + "[PULL]" + C.RESET


def draw_menu(cfg: Dict[str, str]) -> None:
    print_banner()
    print(f" {status_tag(STATUS['dashboard'])} 1. Launch Dashboard Core")
    print(f" {status_tag(STATUS['windows_agent'])} 2. Launch Windows Agent")
    print(f" {status_tag(STATUS['pi_agent'])} 3. Launch Pi Agent Sync (full: upload + install + run)")
    print(f" {status_tag(STATUS['recalibrated'])} 4. Recalibrate Network IP Sync")
    print(f" {status_tag(all_core_done())} 5. Run Full Init Sequence")
    print(f" {C.GREEN}[PUSH]{C.RESET} D. Deploy Pi Agent (upload all files + run)")
    print(f" {C.BLUE}[CFG ]{C.RESET} I. Set Server IP (source of truth)")
    print(f" {C.BLUE}[OPEN]{C.RESET} 6. Open Dashboard (localhost)")
    print(f" {C.GREEN}[GRADE]{C.RESET} A. Open Academic Progress (app + web + YouTube grading)")
    print(f" {_telegram_tag()} M. Connect Telegram Alerts (phone notifications)")
    print(f" {_datapull_tag()} P. Pull Device Data via SSH (progress spool + logs)")
    print(f" {C.BLUE}[INFO]{C.RESET} 7. Show Network Report")
    print(f" {C.YELLOW}[DIAG]{C.RESET} T. Troubleshoot Pi (status + agent log)")
    print(f" {C.BLUE}[CFG ]{C.RESET} 8. Update Pi Hostname/User")
    print(f" {C.YELLOW}[STOP]{C.RESET} 9. Close All Systems")
    print(f" {C.RED}[EXIT]{C.RESET} 0. Exit")
    print()
    server_ip = cfg.get("server_ip", "auto")
    print(C.DIM + f"Pi host: {cfg['pi_hostname']} | user: {cfg['pi_username']} | "
          f"server IP: {server_ip}:{cfg.get('server_port','3000')} | cached Pi IP: {cfg.get('last_pi_ip','-')}" + C.RESET)
    print()


def all_core_done() -> bool:
    return STATUS["dashboard"] and STATUS["windows_agent"] and STATUS["pi_agent"]


def load_cfg() -> Dict[str, str]:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        save_cfg(DEFAULTS)
        return dict(DEFAULTS)

    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        merged = dict(DEFAULTS)
        merged.update({k: str(v) for k, v in data.items() if k in DEFAULTS})
        return merged
    except Exception:
        return dict(DEFAULTS)


def save_cfg(cfg: Dict[str, str]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def boot_sequence(name: str, steps: List[str]) -> None:
    """Show planned phases without claiming they succeeded in advance."""
    print(C.CYAN + f"[PLAN] {name}" + C.RESET)
    for step in steps:
        print(C.DIM + f"  - {step}" + C.RESET)
    print()


def launch_batch(script: Path, title: str) -> bool:
    if not script.exists():
        print(C.RED + f"Missing script: {script}" + C.RESET)
        return False

    cmd = f'start "{title}" cmd /k "{script}"'
    subprocess.Popen(cmd, shell=True)
    return True


def get_local_ipv4s() -> List[str]:
    ips: List[str] = []
    try:
        out = subprocess.check_output(["ipconfig"], text=True, errors="ignore", timeout=20)
        ips = re.findall(r"IPv4[^:]*:\s*([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", out)
    except (OSError, subprocess.SubprocessError):
        ips = []

    if not ips:
        # Fallback for non-Windows shells or localized ipconfig output.
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                ips = [s.getsockname()[0]]
            finally:
                s.close()
        except Exception:
            ips = []

    valid = []
    for ip in ips:
        if ip.startswith("127."):
            continue
        if ip.startswith("169.254."):
            continue
        valid.append(ip)

    seen = set()
    ordered = []
    for ip in valid:
        if ip not in seen:
            seen.add(ip)
            ordered.append(ip)
    return ordered


def flush_network_caches() -> None:
    """Flush DNS and ARP caches so stale entries from the previous network
    cannot mislead Pi detection or make a dead IP appear reachable.

    Both commands require elevation on a locked-down machine, but on a normal
    home/dev Windows box they succeed silently. Failures are non-fatal."""
    for cmd in ("ipconfig /flushdns", "netsh interface ip delete arpcache"):
        try:
            subprocess.run(
                cmd, shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            pass


def verify_local_ip_bound(ip: str) -> bool:
    """Return True if *ip* is actually assigned to a local interface right now.

    Catches the case where the cached host IP belongs to a network we have
    since disconnected from — subnet-matching would still pick it if the old
    adapter is still enumerated but down."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind((ip, 0))
            return True
        except OSError:
            return False
        finally:
            s.close()
    except Exception:
        return False


def same_subnet_24(ip1: str, ip2: str) -> bool:
    try:
        return ".".join(ip1.split(".")[:3]) == ".".join(ip2.split(".")[:3])
    except Exception:
        return False


def tcp_open(ip: str, port: int, timeout: float = 0.2) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def resolve_hostname_ip(hostname: str) -> Optional[str]:
    try:
        return socket.gethostbyname(hostname)
    except Exception:
        return None


def scan_subnet_for_ssh(prefix: str, max_workers: int = 96) -> List[str]:
    ips = [f"{prefix}.{i}" for i in range(1, 255)]
    found: List[str] = []

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        jobs = {ex.submit(tcp_open, ip, 22, 0.12): ip for ip in ips}
        for future in as_completed(jobs):
            ip = jobs[future]
            ok = False
            try:
                ok = bool(future.result())
            except Exception:
                ok = False
            if ok:
                found.append(ip)
    return sorted(found)


def choose_host_ip(local_ips: List[str], pi_ip: str) -> str:
    for ip in local_ips:
        if same_subnet_24(ip, pi_ip):
            return ip
    return local_ips[0]


def detect_pi_ip(cfg: Dict[str, str], local_ips: List[str],
                 retries: int = 2, verbose: bool = True) -> Optional[str]:
    """Probe one explicitly selected Pi IP or the cached IP only.

    Asking before the first probe prevents a random SSH server on another
    adapter/subnet from being mistaken for the Pi.
    """
    cached = (cfg.get("last_pi_ip") or "").strip()
    choice = input(f"Pi IP to use [auto = cached {cached or 'not set'}]: ").strip()
    if not choice or choice.lower() == "auto":
        target = cached
        if not target:
            print(C.RED + "No cached Pi IP is set. Enter the Pi IP manually." + C.RESET)
            return None
        source = "cached"
    else:
        try:
            target = str(ipaddress.IPv4Address(choice))
        except ipaddress.AddressValueError:
            print(C.RED + "Invalid Pi IPv4 address. No other addresses will be scanned." + C.RESET)
            return None
        source = "manual"

    timeouts = [0.8, 1.5, 2.5]
    for attempt in range(retries + 1):
        timeout = timeouts[min(attempt, len(timeouts) - 1)]
        if attempt and verbose:
            print(C.DIM + f"  Retry {attempt}/{retries} to {target} (timeout {timeout}s)..." + C.RESET)
            time.sleep(float(attempt))
        if tcp_open(target, 22, timeout):
            if verbose:
                print(C.GREEN + f"SSH port answered at {target} ({source} IP)." + C.RESET)
            return target

    print(C.RED + f"No SSH response at selected Pi IP {target}. No other IPs were tried." + C.RESET)
    return None


def _run_hidden(command: str) -> int:
    return subprocess.run(
        command,
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode


def _kill_cmd_window_by_title(title: str) -> bool:
    rc = _run_hidden(f'taskkill /F /T /FI "WINDOWTITLE eq {title}*"')
    return rc == 0


def _kill_listeners_on_port(port: int) -> int:
    killed = 0
    try:
        out = subprocess.check_output(["netstat", "-ano", "-p", "tcp"], text=True, errors="ignore")
        pids = set()
        for line in out.splitlines():
            s = line.strip()
            if f":{port}" not in s or "LISTENING" not in s:
                continue
            parts = s.split()
            if not parts:
                continue
            pid = parts[-1]
            if pid.isdigit():
                pids.add(pid)

        for pid in pids:
            rc = _run_hidden(f"taskkill /F /T /PID {pid}")
            if rc == 0:
                killed += 1
    except Exception:
        return killed

    return killed


def _quick_detect_pi_ip(cfg: Dict[str, str]) -> Optional[str]:
    """Check only GUARD's cached Pi address; never resolve or scan other hosts."""
    cached = (cfg.get("last_pi_ip") or "").strip()
    if cached and tcp_open(cached, 22, 0.8):
        return cached
    return None


def recalibrate_pi_sync(cfg: Dict[str, str], restart_agent: bool) -> bool:
    """Re-point the Pi agent at the current dashboard IP and restart it.

    This is the lightweight counterpart of deploy_pi_agent: no file upload,
    just IP resolution, config patch, service restart, and verification.

    Improvements over the original implementation:
    - Flushes DNS and ARP caches before probing so a network change is visible.
    - Validates that the chosen host IP is actually bound to a local interface.
    - Uses retrying Pi detection with increasing timeouts for settling networks.
    - Ends with a full verify_pi_online check (Pi service + Pi→dashboard curl).
    - Warns if the local pi-agent/agent/config.json was left stale.
    """
    if not (shutil.which("ssh") or shutil.which("plink")):
        print(C.RED + "No SSH client found in PATH (need OpenSSH 'ssh' or PuTTY 'plink')." + C.RESET)
        return False

    # ── Phase 0: flush stale caches from the previous network ─────────────
    print(C.BLUE + "Flushing DNS and ARP caches..." + C.RESET)
    flush_network_caches()

    # ── Phase 1: discover the local network and the Pi ────────────────────
    local_ips = get_local_ipv4s()
    if not local_ips:
        print(C.RED + "No active local IPv4 found — is Wi-Fi/ethernet connected?" + C.RESET)
        return False

    print(C.BLUE + f"Local IPv4 candidates: {', '.join(local_ips)}" + C.RESET)
    pi_ip = detect_pi_ip(cfg, local_ips, retries=2, verbose=True)
    if not pi_ip:
        print(C.RED + "Could not determine Pi IP." + C.RESET)
        return False
    print(C.GREEN + f"Pi found at {pi_ip}" + C.RESET)

    # ── Phase 2: resolve the host (dashboard) IP ──────────────────────────
    host_ip = resolve_host_ip(cfg, local_ips, pi_ip)
    if not host_ip:
        print(C.RED + "Could not determine the dashboard host IP." + C.RESET)
        return False

    # Never publish a URL that is stale or guessed from an unrelated adapter.
    if not verify_local_ip_bound(host_ip):
        print(C.RED + f"Cannot use {host_ip}: it is not bound to an active local interface." + C.RESET)
        return False

    port = cfg.get("server_port", "3000")
    if not tcp_open("127.0.0.1", int(port), 1.0):
        print(C.RED + f"Dashboard is not listening on port {port}; start it before recalibrating." + C.RESET)
        return False
    auto_server_ip = (cfg.get("server_ip", "auto") or "auto").strip().lower() == "auto"
    if auto_server_ip and not same_subnet_24(host_ip, pi_ip):
        print(C.RED + f"Automatic address selection found host={host_ip} and Pi={pi_ip} "
              "on different /24 networks." + C.RESET)
        print(C.YELLOW + "Connect the PC and Pi to the same network, or set a fixed, "
              "Pi-reachable dashboard IP with menu option I / GUARD.bat ip <addr>." + C.RESET)
        return False
    if same_subnet_24(host_ip, pi_ip):
        print(C.GREEN + f"Subnet aligned: host={host_ip}, pi={pi_ip}" + C.RESET)
    else:
        print(C.YELLOW + f"Using explicitly configured host={host_ip} for Pi={pi_ip}; "
              "reachability will be checked after the update." + C.RESET)

    boot_sequence("NETWORK RECALIBRATION", [
        "Flush network caches",
        "Resolve active host network",
        "Find Pi endpoint",
        "Validate host IP binding",
        "Patch Pi server_url",
        "Commit sync profile",
        "End-to-end verification",
    ])

    # ── Phase 3: patch only the IP explicitly selected above.
    if not remote_configure_and_start(cfg, pi_ip, cfg["pi_install_dir"], host_ip,
                                      port, cfg["pi_service"], restart_agent):
        print(C.RED + "Pi configuration/restart failed. No other Pi IP was tried "
              "and no local IP settings were changed." + C.RESET)
        return False

    # ── Phase 4: verify end-to-end before committing local settings.
    if not verify_pi_online(cfg, pi_ip, host_ip, port, require_reachability=True):
        print(C.RED + "Recalibration did not pass service and dashboard reachability checks; "
              "local IP settings were not changed." + C.RESET)
        return False

    print(C.BLUE + "Updating local repo configs (source of truth):" + C.RESET)
    for note in apply_ip_locally(host_ip, port):
        print("  - " + note)
    cfg["last_pi_ip"] = pi_ip
    cfg["last_host_ip"] = host_ip
    save_cfg(cfg)
    print(C.GREEN + f"Recalibration verified. Pi points to http://{host_ip}:{port}" + C.RESET)
    return True


def resolve_host_ip(cfg: Dict[str, str], local_ips: List[str], pi_ip: str) -> Optional[str]:
    """Single source of truth for the dashboard IP the agents connect to.

    If server_ip is a fixed address, use it verbatim. Otherwise ("auto")
    pick the local IPv4 that shares the Pi's /24 so the Pi can reach us.
    """
    fixed = (cfg.get("server_ip", "auto") or "auto").strip().lower()
    if fixed and fixed != "auto":
        return cfg["server_ip"].strip()
    if not local_ips:
        return None
    return choose_host_ip(local_ips, pi_ip)


def apply_ip_locally(host_ip: str, port: str) -> List[str]:
    """Keep the repo's agent configs in sync with the chosen host IP so a
    fresh redeploy is always correct. Returns human-readable notes."""
    notes: List[str] = []
    server_url = f"http://{host_ip}:{port}"

    # Pi agent config (this is what gets deployed to the Pi).
    try:
        if PI_AGENT_CONFIG.exists():
            data = json.loads(PI_AGENT_CONFIG.read_text(encoding="utf-8"))
        else:
            data = {}
        old = data.get("server_url", "")
        data["server_url"] = server_url
        PI_AGENT_CONFIG.write_text(json.dumps(data, indent=4), encoding="utf-8")
        notes.append(f"pi-agent/agent/config.json: {old or '(none)'} -> {server_url}")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"pi-agent config update FAILED: {exc}")

    # Windows agent runs on the same box as the dashboard, so it should keep
    # using localhost. Only rewrite it if it was pointed somewhere stale.
    try:
        if WINDOWS_AGENT_CONFIG.exists():
            data = json.loads(WINDOWS_AGENT_CONFIG.read_text(encoding="utf-8"))
            local_url = f"http://localhost:{port}"
            changed = False
            if data.get("server_url") not in (local_url, f"http://127.0.0.1:{port}"):
                data["server_url"] = local_url
                changed = True
            if "edge_dashboard_url" in data and data["edge_dashboard_url"] not in (
                local_url,
                f"http://127.0.0.1:{port}",
            ):
                data["edge_dashboard_url"] = local_url
                changed = True
            if changed:
                WINDOWS_AGENT_CONFIG.write_text(json.dumps(data, indent=4), encoding="utf-8")
                notes.append(f"windows-app local-agent-config.json: server_url -> {local_url}")
            else:
                notes.append("windows-app local-agent-config.json: already localhost (ok)")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"windows agent config update FAILED: {exc}")

    return notes


# ─── Password-capable SSH / SCP layer ────────────────────────────────────────
# Windows OpenSSH cannot take a password on the command line. If PuTTY
# (pscp/plink) or sshpass is installed we use the configured password for an
# unattended run; otherwise we fall back to OpenSSH, which prompts for the
# password interactively (you'll type it once for upload and once for the
# combined remote step).

# plink/pscp in -batch mode abort outright when the host key is not already in
# the registry ("The server's host key is not cached"). Prime it once per IP by
# running a throwaway login with "y" on stdin, which is what the user would type.
_HOSTKEY_PRIMED: set = set()


def _ssh_password(cfg: Dict[str, str]) -> str:
    return (cfg.get("pi_password") or "").strip()


def _prime_host_key(cfg: Dict[str, str], ip: str) -> None:
    if ip in _HOSTKEY_PRIMED:
        return
    _HOSTKEY_PRIMED.add(ip)
    if not (shutil.which("plink") and _ssh_password(cfg)):
        return
    try:
        subprocess.run(
            ["plink", "-ssh", "-pw", _ssh_password(cfg),
             f"{cfg['pi_username']}@{ip}", "exit"],
            input="y\n", text=True, timeout=25,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def sudo_preamble(cfg: Dict[str, str]) -> str:
    """Shell snippet that pre-authorises sudo for the rest of the session.

    On stock Raspberry Pi OS the SSH user has NOPASSWD sudo and this is a no-op.
    When sudo *does* want a password, a non-interactive `sudo` would silently
    read EOF and fail — which is exactly what produced the old
    "check SSH+sudo password" dead end. Seeding the credential cache once up
    front means every later `sudo -n` in the same command succeeds.
    """
    pw = _ssh_password(cfg)
    if not pw:
        return "true"
    return "printf '%s\\n' " + shlex.quote(pw) + " | sudo -S -p '' -v 2>/dev/null || true"


def ssh_exec(cfg: Dict[str, str], ip: str, remote_command: str,
             tty: bool = False, timeout: int = 240) -> int:
    user = cfg["pi_username"]
    pw = _ssh_password(cfg)
    _prime_host_key(cfg, ip)
    try:
        if pw and shutil.which("plink"):
            cmd = ["plink", "-ssh", "-batch", "-pw", pw, f"{user}@{ip}", remote_command]
            return subprocess.run(cmd, timeout=timeout).returncode
        if pw and shutil.which("sshpass"):
            cmd = ["sshpass", "-p", pw, "ssh",
                   "-o", "StrictHostKeyChecking=accept-new",
                   "-o", "ConnectTimeout=8", f"{user}@{ip}", remote_command]
            return subprocess.run(cmd, timeout=timeout).returncode
        # OpenSSH fallback — will prompt for the password interactively.
        cmd = ["ssh", "-o", "ConnectTimeout=8"]
        if tty:
            cmd.append("-tt")
        cmd.extend([f"{user}@{ip}", remote_command])
        return subprocess.run(cmd, timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        print(C.RED + f"SSH timed out after {timeout}s ({ip})." + C.RESET)
        return 124
    except OSError as exc:
        print(C.RED + f"SSH failed to launch: {exc}" + C.RESET)
        return 127


def scp_push(cfg: Dict[str, str], ip: str, local_paths: List[str],
             remote_dir: str, timeout: int = 600) -> int:
    user = cfg["pi_username"]
    pw = _ssh_password(cfg)
    dest = f"{user}@{ip}:{remote_dir}"
    _prime_host_key(cfg, ip)
    try:
        if pw and shutil.which("pscp"):
            cmd = ["pscp", "-batch", "-pw", pw, "-r", *local_paths, dest]
            return subprocess.run(cmd, timeout=timeout).returncode
        if pw and shutil.which("sshpass"):
            cmd = ["sshpass", "-p", pw, "scp",
                   "-o", "StrictHostKeyChecking=accept-new",
                   "-o", "ConnectTimeout=8", "-r", *local_paths, dest]
            return subprocess.run(cmd, timeout=timeout).returncode
        cmd = ["scp", "-o", "ConnectTimeout=8", "-r", *local_paths, dest]
        return subprocess.run(cmd, timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        print(C.RED + f"Upload timed out after {timeout}s ({ip})." + C.RESET)
        return 124
    except OSError as exc:
        print(C.RED + f"scp failed to launch: {exc}" + C.RESET)
        return 127


def scp_pull(cfg: Dict[str, str], ip: str, remote_paths: List[str],
             local_dir: str, timeout: int = 600) -> int:
    """
    Download from the Pi. The mirror image of scp_push, using the same three-way
    fallback (pscp with the stored password, sshpass, then plain scp which will
    prompt) so a data pull works on exactly the machines a deploy works on.

    Remote paths are quoted for the remote shell but the LOCAL destination is
    passed as a separate argv entry, never string-concatenated — a Windows path
    with a space in it is otherwise silently split into two arguments.
    """
    user = cfg["pi_username"]
    pw = _ssh_password(cfg)
    sources = [f"{user}@{ip}:{p}" for p in remote_paths]
    Path(local_dir).mkdir(parents=True, exist_ok=True)
    _prime_host_key(cfg, ip)
    try:
        if pw and shutil.which("pscp"):
            cmd = ["pscp", "-batch", "-pw", pw, "-r", *sources, local_dir]
            return subprocess.run(cmd, timeout=timeout).returncode
        if pw and shutil.which("sshpass"):
            cmd = ["sshpass", "-p", pw, "scp",
                   "-o", "StrictHostKeyChecking=accept-new",
                   "-o", "ConnectTimeout=8", "-r", *sources, local_dir]
            return subprocess.run(cmd, timeout=timeout).returncode
        cmd = ["scp", "-o", "ConnectTimeout=8", "-r", *sources, local_dir]
        return subprocess.run(cmd, timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        print(C.RED + f"Download timed out after {timeout}s ({ip})." + C.RESET)
        return 124
    except OSError as exc:
        print(C.RED + f"scp failed to launch: {exc}" + C.RESET)
        return 127


def _collect_agent_files() -> List[str]:
    """All files from the Pi agent folder only, minus caches and local state."""
    skip_names = {"__pycache__", "warning_counts.json", "kill_attempts.txt"}
    skip_suffixes = (".pyc", ".log", ".db", ".db-shm", ".db-wal")
    files: List[str] = []
    if not PI_AGENT_DIR.exists():
        return files
    for entry in sorted(PI_AGENT_DIR.iterdir()):
        if entry.name in skip_names:
            continue
        if entry.is_file() and entry.suffix in skip_suffixes:
            continue
        # Do not ship the local config.json — the Pi keeps its own device
        # identity/token; we patch its server_url separately after upload.
        if entry.name == "config.json":
            continue
        files.append(str(entry))
    return files


def ssh_capture(cfg: Dict[str, str], ip: str, remote_command: str,
                timeout: int = 30) -> Tuple[int, str]:
    """Like ssh_exec but captures combined stdout/stderr for verification."""
    user = cfg["pi_username"]
    pw = _ssh_password(cfg)
    _prime_host_key(cfg, ip)
    try:
        if pw and shutil.which("plink"):
            cmd = ["plink", "-ssh", "-batch", "-pw", pw, f"{user}@{ip}", remote_command]
        elif pw and shutil.which("sshpass"):
            cmd = ["sshpass", "-p", pw, "ssh",
                   "-o", "StrictHostKeyChecking=accept-new",
                   "-o", "ConnectTimeout=8", f"{user}@{ip}", remote_command]
        else:
            cmd = ["ssh", "-o", "ConnectTimeout=8", f"{user}@{ip}", remote_command]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return res.returncode, ((res.stdout or "") + (res.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return 124, f"(timeout after {timeout}s)"
    except OSError as exc:
        return 127, str(exc)


def _remote_config_patch(install_dir: str, host_ip: str, port: str,
                         service: str, restart: bool) -> str:
    """Legacy single-shot builder, kept only so old call sites keep working.

    Prefer remote_configure_and_start(): it runs the same work in separate
    phases so a failure names itself instead of collapsing into one message.
    """
    return _cfg_patch_cmd(install_dir, host_ip, port)


def _cfg_patch_cmd(install_dir: str, host_ip: str, port: str) -> str:
    """Patch server_url in the Pi's own config.json, preserving identity keys."""
    py_lines = [
        "import json,os",
        "p=os.path.join(%r,'config.json')" % install_dir,
        "d={}",
        "try:\n    d=json.load(open(p))\nexcept Exception:\n    d={}",
        "d['server_url']=%r" % f"http://{host_ip}:{port}",
        "d.setdefault('device_name', os.uname().nodename)",
        "d.setdefault('screenshot_interval', 5)",
        "d.setdefault('screenshot_quality', 60)",
        "d.setdefault('auth_token', '')",
        "d.setdefault('blocklist_update_interval', 3600)",
        "d.setdefault('download_watch_paths', ['/home/pi/Downloads'])",
        "d.setdefault('quarantine_path', os.path.join(%r,'quarantine'))" % install_dir,
        "d.setdefault('log_level', 'INFO')",
        "d.setdefault('stealth_mode', False)",
        "d.setdefault('platform', 'linux-arm')",
        "d.setdefault('use_scrot_fallback', True)",
        "d.pop('_note', None)",
        "json.dump(d, open(p,'w'), indent=4)",
        "print('GUARDIAN_CFG_OK server_url=' + d['server_url'])",
    ]
    # A real newline-separated script, fed on stdin, so the try/except above is
    # legal Python. The old ";"-joined -c form could not contain a try block.
    py = "\n".join(py_lines)
    return ("sudo -n mkdir -p " + shlex.quote(install_dir + "/quarantine") +
            " && printf '%s' " + shlex.quote(py) + " | sudo -n python3 -")


def _pip_refresh_cmd(install_dir: str) -> str:
    """Refresh venv packages, bounded so an offline Pi cannot stall the deploy.

    The old version ran an unbounded `pip install -r` inside the same SSH call
    as the config patch. On a Pi joined to the Windows hotspot with no internet
    route, pip's retries outlived the 180s SSH timeout and the whole step was
    reported as an SSH/sudo failure.
    """
    pip = install_dir + "/venv/bin/pip"
    req = install_dir + "/requirements.txt"
    return ("if [ -x " + shlex.quote(pip) + " ] && [ -f " + shlex.quote(req) + " ]; then "
            "sudo -n " + shlex.quote(pip) + " install -q --timeout 8 --retries 1 -r "
            + shlex.quote(req) + " >/dev/null 2>&1 "
            "&& echo GUARDIAN_PIP=ok || echo GUARDIAN_PIP=skipped; "
            "else echo GUARDIAN_PIP=novenv; fi")


def _housekeeping_cmd(install_dir: str) -> str:
    """Best-effort tidy-up. Every step is `|| true` — none of it is fatal."""
    start_sh = install_dir + "/start.sh"
    sed_expr = r's|^([[:space:]]*(\./)?(venv/bin/)?python3?[[:space:]]+main\.py)[[:space:]]*$|\1 --no-watchdog|'
    return "; ".join([
        # Existing installs keep their original start.sh, so patch --no-watchdog
        # in. Two supervisors produced duplicate agents and the offline flap.
        ("if [ -f " + shlex.quote(start_sh) + " ]; then sudo -n sed -i -E "
         + shlex.quote(sed_expr) + " " + shlex.quote(start_sh) + " || true; fi"),
        "sudo -n chmod +x " + shlex.quote(start_sh) + " 2>/dev/null || true",
        # Retire any detached watchdog left over from the old layout.
        # The bracket is load-bearing: `pkill -f guardian_watchdog.py` also
        # matches THIS command line (the remote shell was invoked with that
        # text in argv), so the plain form made the SSH session SIGTERM itself
        # before the restart ever ran — the Pi then never came back online and
        # GUARD blamed the SSH/sudo password. `[g]uardian...` cannot match the
        # literal `[g]uardian...` in our own argv, so only real strays die.
        "sudo -n pkill -f '[g]uardian_watchdog\\.py' >/dev/null 2>&1 || true",
        # Stale bytecode from a different CPython shadows freshly uploaded .py files.
        "sudo -n rm -rf " + shlex.quote(install_dir + "/__pycache__") + " 2>/dev/null || true",
        "true",
    ])


def _restart_cmd(service: str, install_dir: str, settle: int = 12) -> str:
    """Restart the unit, then poll until it settles and print a marker.

    Deliberately always exits 0. The old chain ended on `systemctl is-active`,
    whose exit code 3 for a dead unit was indistinguishable from "the config
    patch failed" — that is what printed the misleading SSH/sudo message.
    """
    svc = shlex.quote(service)
    return "; ".join([
        "sudo -n systemctl daemon-reload >/dev/null 2>&1 || true",
        "sudo -n systemctl enable " + svc + " >/dev/null 2>&1 || true",
        "sudo -n systemctl restart " + svc + " >/dev/null 2>&1 || "
        "sudo -n systemctl start " + svc + " >/dev/null 2>&1 || true",
        f"for i in $(seq 1 {settle}); do "
        "  s=$(systemctl is-active " + svc + " 2>/dev/null); "
        '  if [ "$s" = "active" ]; then break; fi; '
        '  if [ "$s" = "failed" ]; then break; fi; '
        "  sleep 1; "
        "done",
        'echo "GUARDIAN_SVC=$(systemctl is-active ' + svc + ' 2>/dev/null)"',
        'echo "GUARDIAN_ENABLED=$(systemctl is-enabled ' + svc + ' 2>/dev/null)"',
        "echo GUARDIAN_PROCS=$(pgrep -fc 'main.py' 2>/dev/null || echo 0)",
        "true",
    ])


def pi_journal_tail(cfg: Dict[str, str], pi_ip: str, service: str,
                    lines: int = 40) -> None:
    """Print the unit's own log. Without this the caller only ever learned
    *that* something failed, never *what*."""
    print(C.YELLOW + f"--- journalctl -u {service} -n {lines} (from the Pi) ---" + C.RESET)
    cmd = (f"sudo -n journalctl -u {shlex.quote(service)} -n {lines} --no-pager "
           f"2>/dev/null || journalctl -u {shlex.quote(service)} -n {lines} --no-pager")
    if not _unattended_ssh(cfg):
        # Capturing would hide OpenSSH's password prompt; let it stream instead.
        ssh_exec(cfg, pi_ip, sudo_preamble(cfg) + "; " + cmd, tty=True, timeout=60)
        print(C.YELLOW + "--- end of log ---" + C.RESET)
        return
    _, out = ssh_capture(cfg, pi_ip, cmd, timeout=45)
    print(out or C.DIM + "(no log output — unit may never have started)" + C.RESET)
    print(C.YELLOW + "--- end of log ---" + C.RESET)


def probe_ssh_and_sudo(cfg: Dict[str, str], pi_ip: str) -> Tuple[bool, bool, str]:
    """Separate 'can I log in' from 'can I sudo'. Returns (ssh_ok, sudo_ok, detail)."""
    rc, out = ssh_capture(cfg, pi_ip, "echo GUARDIAN_SSH_OK; id -un", timeout=30)
    if "GUARDIAN_SSH_OK" not in out:
        return False, False, out or f"ssh exit {rc}"

    rc2, out2 = ssh_capture(
        cfg, pi_ip,
        sudo_preamble(cfg) + "; sudo -n true 2>&1 && echo GUARDIAN_SUDO_OK",
        timeout=30,
    )
    return True, ("GUARDIAN_SUDO_OK" in out2), (out2 or f"sudo exit {rc2}")


def _unattended_ssh(cfg: Dict[str, str]) -> bool:
    """True when we can run SSH non-interactively and capture output.

    Windows OpenSSH cannot accept a password on the command line, so without
    plink or sshpass every remote call needs a human at the keyboard. In that
    mode output capture would swallow the password prompt and hang, so the
    phased/captured path must not be used.
    """
    return bool(_ssh_password(cfg)) and bool(shutil.which("plink") or shutil.which("sshpass"))


def _combined_cmd(install: str, host_ip: str, port: str, service: str,
                  restart: bool) -> str:
    """Single-prompt variant for interactive OpenSSH: one connection, distinct
    exit codes so the caller can still name the failing phase.
      92 = config patch failed, 91 = service did not come up, 0 = success.
    """
    parts = [
        _cfg_patch_cmd(install, host_ip, port) + " || exit 92",
        _housekeeping_cmd(install),
        _pip_refresh_cmd(install),
    ]
    if restart:
        parts.append(_restart_cmd(service, install))
        parts.append('if [ "$(systemctl is-active ' + shlex.quote(service) +
                     ' 2>/dev/null)" = "active" ]; then exit 0; else exit 91; fi')
    else:
        parts.append("exit 0")
    return "; ".join(parts)


def remote_configure_and_start(cfg: Dict[str, str], pi_ip: str, install: str,
                               host_ip: str, port: str, service: str,
                               restart: bool = True) -> bool:
    """Configure + start the Pi agent in named phases.

    Every phase reports its own failure, so "Remote configure/restart failed"
    can no longer stand in for four unrelated causes.
    """
    if not _unattended_ssh(cfg):
        print(C.YELLOW + "No plink/sshpass found — using interactive OpenSSH. "
              "You'll be asked for the Pi password once." + C.RESET)
        print(C.DIM + "Install PuTTY (plink+pscp) to make this fully automatic." + C.RESET)
        rc = ssh_exec(cfg, pi_ip,
                      sudo_preamble(cfg) + "; " +
                      _combined_cmd(install, host_ip, port, service, restart),
                      tty=True, timeout=600)
        if rc == 0:
            print(C.GREEN + f"  {service}: active" + C.RESET)
            return True
        if rc == 92:
            print(C.RED + f"Could not write {install}/config.json on the Pi "
                  "(sudo refused, or the install directory is missing)." + C.RESET)
        elif rc == 91:
            print(C.RED + f"  {service} did not come up." + C.RESET)
            pi_journal_tail(cfg, pi_ip, service, 40)
        else:
            print(C.RED + f"SSH to {cfg['pi_username']}@{pi_ip} failed (exit {rc}). "
                  "The address may be stale, SSH may be resetting connections, or "
                  "the login may be rejected." + C.RESET)
        return False

    ssh_ok, sudo_ok, detail = probe_ssh_and_sudo(cfg, pi_ip)
    if not ssh_ok:
        print(C.RED + f"SSH login to {cfg['pi_username']}@{pi_ip} failed." + C.RESET)
        print(C.DIM + (detail[-600:] if detail else "(no output)") + C.RESET)
        print(C.DIM + "Fix: check the Pi password (menu 8 / guard_runtime.json) "
              "and that sshd is running on the Pi." + C.RESET)
        return False
    if not sudo_ok:
        print(C.RED + f"SSH works but sudo was refused on {pi_ip}." + C.RESET)
        print(C.DIM + (detail[-600:] if detail else "(no output)") + C.RESET)
        print(C.DIM + "Fix: the stored pi_password must be this user's real "
              "password, or give the user NOPASSWD sudo." + C.RESET)
        return False

    pre = sudo_preamble(cfg)

    print(C.BLUE + "Patching config.json on the Pi..." + C.RESET)
    rc, out = ssh_capture(cfg, pi_ip,
                          pre + "; " + _cfg_patch_cmd(install, host_ip, port),
                          timeout=90)
    if "GUARDIAN_CFG_OK" not in out:
        print(C.RED + f"Could not write {install}/config.json" + C.RESET)
        print(C.DIM + (out[-800:] if out else f"(no output, exit {rc})") + C.RESET)
        return False
    print(C.GREEN + "  " + out.split("GUARDIAN_CFG_OK", 1)[1].strip() + C.RESET)

    ssh_exec(cfg, pi_ip, pre + "; " + _housekeeping_cmd(install), timeout=120)

    if not restart:
        return True

    print(C.BLUE + f"Restarting {service}..." + C.RESET)
    rc, out = ssh_capture(cfg, pi_ip, pre + "; " + _restart_cmd(service, install),
                          timeout=180)
    state = _marker(out, "GUARDIAN_SVC")
    enabled = _marker(out, "GUARDIAN_ENABLED")
    procs = _marker(out, "GUARDIAN_PROCS")

    if state == "active":
        print(C.GREEN + f"  {service}: active (enabled={enabled or '?'}, "
              f"main.py processes={procs or '?'})" + C.RESET)
        return True

    print(C.RED + f"  {service} did not come up — state: {state or 'unknown'}" + C.RESET)
    noise = [l for l in (out or "").splitlines()
             if l.strip() and not l.strip().startswith("GUARDIAN_")]
    if noise:
        print(C.DIM + "\n".join(noise[-8:]) + C.RESET)
    pi_journal_tail(cfg, pi_ip, service, 40)
    return False


def _marker(text: str, key: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""




def verify_pi_online(cfg: Dict[str, str], pi_ip: str, host_ip: str, port: str,
                     require_reachability: bool = False) -> bool:
    """Confirm the service and Pi-to-dashboard path without hiding SSH prompts."""
    svc = cfg["pi_service"]
    print(C.BLUE + "Verifying deployment..." + C.RESET)
    dash_ok = tcp_open("127.0.0.1", int(port), 1.0)

    if _unattended_ssh(cfg):
        rc, out = ssh_capture(cfg, pi_ip,
                              f"systemctl is-active {shlex.quote(svc)}", timeout=25)
        active = out.splitlines()[-1].strip() if out else ""
        svc_ok = (active == "active") or (rc == 0 and "inactive" not in out and "failed" not in out)
        health = (f"curl -m 5 -s -o /dev/null -w '%{{http_code}}' "
                  f"http://{host_ip}:{port}/api/health || echo down")
        _, out2 = ssh_capture(cfg, pi_ip, health, timeout=20)
        code = out2.splitlines()[-1].strip() if out2 else ""
        reach_ok = code.startswith("2")
        print(f"  Pi service '{svc}': " +
              (C.GREEN + "active" + C.RESET if svc_ok else C.RED + f"NOT active ({active or 'unknown'})" + C.RESET))
        print(f"  Pi -> dashboard http://{host_ip}:{port}/api/health: " +
              ((C.GREEN + code + C.RESET) if reach_ok else (C.RED + (code or "no response") + C.RESET)))
    else:
        # Capturing OpenSSH hides its password prompt. Stream this check to the
        # terminal and use the remote exit status as the end-to-end result.
        print(C.YELLOW + "OpenSSH will prompt for the Pi password to verify service and dashboard reachability." + C.RESET)
        remote = (
            f"state=$(systemctl is-active {shlex.quote(svc)} 2>/dev/null); "
            f"echo \"Pi service {svc}: $state\"; "
            f"[ \"$state\" = active ] || exit 91; "
            f"code=$(curl -m 5 -s -o /dev/null -w '%{{http_code}}' "
            f"http://{host_ip}:{port}/api/health || echo down); "
            f"echo \"Pi -> dashboard HTTP: $code\"; "
            "case \"$code\" in 2*) exit 0;; *) exit 92;; esac"
        )
        rc = ssh_exec(cfg, pi_ip, remote, tty=True, timeout=45)
        svc_ok = rc == 0
        reach_ok = rc == 0
        print("  Remote service and Pi -> dashboard checks: " +
              (C.GREEN + "passed" + C.RESET if rc == 0
               else C.RED + f"failed (SSH/remote exit {rc})" + C.RESET))

    print(f"  Local dashboard port {port}: " +
          (C.GREEN + "listening" + C.RESET if dash_ok
           else C.RED + "not listening (start it with option 1)" + C.RESET))
    ok = svc_ok and (not require_reachability or (reach_ok and dash_ok))
    print((C.GREEN + "Verification PASSED." if ok
           else C.RED + "Verification FAILED — review the lines above.") + C.RESET)
    return ok


def _pi_is_installed(cfg: Dict[str, str], pi_ip: str) -> Optional[bool]:
    """True if the Pi already has a working install (venv + start.sh)."""
    install = cfg["pi_install_dir"]
    probe = (f"test -x {shlex.quote(install + '/start.sh')} && "
             f"test -x {shlex.quote(install + '/venv/bin/python3')} && "
             "echo INSTALLED || echo MISSING")
    rc, out = ssh_capture(cfg, pi_ip, probe, timeout=25)
    if rc not in (0, 1) or not out:
        return None
    return "INSTALLED" in out


def bootstrap_pi_install(cfg: Dict[str, str], pi_ip: str, host_ip: str) -> bool:
    """Run the full installer on a Pi that has never been set up.

    Uploads pi-agent/ (agent + scripts) to a staging dir, then runs
    install-agent.sh, which creates the venv, start.sh and systemd unit.
    """
    installer = ROOT_DIR / "pi-agent" / "scripts" / "install-agent.sh"
    if not installer.exists():
        print(C.RED + f"Installer not found: {installer}" + C.RESET)
        return False

    staging = "/tmp/guardian-bootstrap"
    print(C.YELLOW + "Pi has no agent install yet — running first-time setup "
          "(installs packages + venv, this takes a few minutes)." + C.RESET)

    prep = (f"rm -rf {shlex.quote(staging)} && "
            f"mkdir -p {shlex.quote(staging + '/agent')} {shlex.quote(staging + '/scripts')}")
    if ssh_exec(cfg, pi_ip, prep, tty=True, timeout=60) != 0:
        print(C.RED + "Could not create the staging directory on the Pi." + C.RESET)
        return False

    agent_files = _collect_agent_files()
    if PI_AGENT_CONFIG.exists():
        # Bootstrap only: seed config.json so the installer has a base to work
        # from. Later deploys never overwrite the Pi's own config.
        agent_files = agent_files + [str(PI_AGENT_CONFIG)]
    if scp_push(cfg, pi_ip, agent_files, staging + "/agent/") != 0:
        print(C.RED + "Failed to upload agent files for bootstrap." + C.RESET)
        return False
    if scp_push(cfg, pi_ip, [str(installer)], staging + "/scripts/") != 0:
        print(C.RED + "Failed to upload the installer." + C.RESET)
        return False

    run = (f"cd {shlex.quote(staging + '/scripts')} && "
           f"sudo -n bash install-agent.sh {shlex.quote(host_ip)}")
    if ssh_exec(cfg, pi_ip, sudo_preamble(cfg) + "; " + run, tty=True, timeout=1800) != 0:
        print(C.RED + "install-agent.sh failed. Check output above / "
              "`sudo journalctl -u guardian-agent -n 40` on the Pi." + C.RESET)
        return False

    print(C.GREEN + "First-time install finished." + C.RESET)
    return True


def deploy_pi_agent(cfg: Dict[str, str], restart: bool = True) -> bool:
    """Upload every file from pi-agent/agent/ to the Pi and (re)start it.

    Steps: find the online Pi -> resolve the dashboard host IP (single source
    of truth) -> sync repo configs -> scp agent files -> patch the Pi's
    server_url -> restart the service -> verify.
    """
    if not (shutil.which("ssh") or shutil.which("plink")):
        print(C.RED + "Need OpenSSH (ssh/scp) or PuTTY (plink/pscp) on PATH." + C.RESET)
        return False

    files = _collect_agent_files()
    if not files:
        print(C.RED + f"No agent files found in {PI_AGENT_DIR}" + C.RESET)
        return False

    local_ips = get_local_ipv4s()
    if not local_ips:
        print(C.RED + "No active local IPv4 found." + C.RESET)
        return False
    print(C.BLUE + f"Local IPv4 candidates: {', '.join(local_ips)}" + C.RESET)

    pi_ip = detect_pi_ip(cfg, local_ips)
    if not pi_ip:
        print(C.RED + "Could not find the Pi over SSH (is it powered on / same network?)." + C.RESET)
        return False
    print(C.GREEN + f"Pi found at {pi_ip}" + C.RESET)

    host_ip = resolve_host_ip(cfg, local_ips, pi_ip)
    if not host_ip:
        print(C.RED + "Could not determine the dashboard host IP." + C.RESET)
        return False
    port = cfg.get("server_port", "3000")
    install = cfg["pi_install_dir"]
    svc = cfg["pi_service"]

    if same_subnet_24(host_ip, pi_ip):
        print(C.GREEN + f"Subnet aligned: host={host_ip}, pi={pi_ip}" + C.RESET)
    else:
        print(C.YELLOW + f"Host {host_ip} and Pi {pi_ip} are on different /24 — "
              "the Pi may not reach the dashboard. Continuing." + C.RESET)

    boot_sequence("PI AGENT DEPLOY", [
        f"Sync repo configs to http://{host_ip}:{port}",
        f"Upload {len(files)} agent files",
        "Patch Pi server_url",
        "Restart guardian-agent service",
    ])

    # 1) Keep the repo's configs correct for any future clean redeploy.
    print(C.BLUE + "Updating local repo configs (source of truth):" + C.RESET)
    for note in apply_ip_locally(host_ip, port):
        print("  - " + note)

    # 2) First-time Pi? Run the full installer instead of a plain file sync.
    installed = _pi_is_installed(cfg, pi_ip)
    if installed is None:
        print(C.RED + "Could not query the Pi over SSH (auth failed or host unreachable)." + C.RESET)
        return False
    if not installed:
        if not bootstrap_pi_install(cfg, pi_ip, host_ip):
            return False

    # 3) Make sure the install dir exists and is writable by our SSH user.
    mkdir = (f"sudo mkdir -p {shlex.quote(install)} && "
             f"sudo chown -R {shlex.quote(cfg['pi_username'])} {shlex.quote(install)}")
    if ssh_exec(cfg, pi_ip, mkdir, tty=True, timeout=60) != 0:
        print(C.RED + "Could not prepare the install directory on the Pi." + C.RESET)
        return False

    # 4) Upload all agent files (config.json excluded to keep Pi identity).
    print(C.CYAN + f"Uploading {len(files)} files -> {pi_ip}:{install}/ ..." + C.RESET)
    if scp_push(cfg, pi_ip, files, install.rstrip("/") + "/") != 0:
        print(C.RED + "Upload failed (check SSH password / connectivity)." + C.RESET)
        return False
    print(C.GREEN + "Upload complete." + C.RESET)

    # 4b) Bounded dependency refresh. Never fatal, never blocking for long.
    print(C.BLUE + "Refreshing Python dependencies (bounded, optional)..." + C.RESET)
    _, pip_out = ssh_capture(cfg, pi_ip,
                             sudo_preamble(cfg) + "; " + _pip_refresh_cmd(install),
                             timeout=240)
    pip_state = _marker(pip_out, "GUARDIAN_PIP") or "unknown"
    note = {"ok": C.GREEN + "updated", "skipped": C.YELLOW + "skipped (no internet on the Pi is fine)",
            "novenv": C.YELLOW + "no venv yet"}.get(pip_state, C.YELLOW + pip_state)
    print("  pip: " + note + C.RESET)

    # 5) Patch server_url on the Pi + restart the service.
    if not remote_configure_and_start(cfg, pi_ip, install, host_ip, port, svc, restart):
        print(C.RED + "Deploy stopped: the agent is not running on the Pi." + C.RESET)
        print(C.DIM + "The log above is the real reason. Common ones: a missing "
              "Python package (rerun with option D after `GUARD.bat diag`), or "
              "start.sh/venv missing from a half-finished install." + C.RESET)
        return False

    cfg["last_pi_ip"] = pi_ip
    cfg["last_host_ip"] = host_ip
    save_cfg(cfg)
    print(C.GREEN + f"Deploy complete. Pi agent points to http://{host_ip}:{port}" + C.RESET)

    return verify_pi_online(cfg, pi_ip, host_ip, port)


def action_deploy_pi(cfg: Dict[str, str]) -> None:
    ok = deploy_pi_agent(cfg, restart=True)
    STATUS["pi_agent"] = ok or STATUS["pi_agent"]


def action_set_server_ip(cfg: Dict[str, str]) -> None:
    print(C.BLUE + "Set the dashboard IP that all agents connect to (source of truth)." + C.RESET)
    print(C.DIM + f"Current: server_ip={cfg.get('server_ip','auto')} port={cfg.get('server_port','3000')}" + C.RESET)
    print(C.DIM + "Enter a fixed IP (e.g. 192.168.137.1), or 'auto' to match the Pi's subnet at deploy time." + C.RESET)
    val = input(f"Server IP [{cfg.get('server_ip','auto')}]: ").strip()
    port = input(f"Server port [{cfg.get('server_port','3000')}]: ").strip()

    if val:
        cfg["server_ip"] = val
    if port and port.isdigit():
        cfg["server_port"] = port
    save_cfg(cfg)

    fixed = (cfg.get("server_ip", "auto") or "auto").strip().lower()
    if fixed and fixed != "auto":
        print(C.BLUE + "Propagating to local repo configs now:" + C.RESET)
        for note in apply_ip_locally(cfg["server_ip"].strip(), cfg.get("server_port", "3000")):
            print("  - " + note)
        print(C.DIM + "Run option D (Deploy) to push this to the live Pi." + C.RESET)
    else:
        print(C.DIM + "server_ip=auto — the host IP is resolved when you Deploy/Recalibrate." + C.RESET)
    print(C.GREEN + "Server IP settings saved." + C.RESET)


# ─── Dashboard bundle (web-ts/dist) ──────────────────────────────────────────
# server-ts serves web-ts/dist as static files, so the browser runs the LAST BUILD,
# never main.js directly. A stale bundle is invisible in the worst way: the page
# loads, REST-driven parts (device list, app inventory) look healthy, and only the
# socket contract silently mismatches — which is exactly the live mirror. BUG-29.

def _newest_mtime(paths) -> Tuple[float, str]:
    newest, who = 0.0, ""
    for path in paths:
        try:
            if not path.is_file():
                continue
            m = path.stat().st_mtime
        except OSError:
            continue
        if m > newest:
            newest, who = m, path.name
    return newest, who


def _newest_web_source() -> Tuple[float, str]:
    """Newest mtime among the dashboard sources, and the file it came from."""
    if not WEB_DIR.is_dir():
        return 0.0, ""
    candidates: List[Path] = []
    for pattern in WEB_SOURCE_GLOBS:
        candidates.extend(WEB_DIR.glob(pattern))
    for sub in ("src", "styles", "public", "components"):
        sub_dir = WEB_DIR / sub
        if sub_dir.is_dir():
            candidates.extend(
                p for p in sub_dir.rglob("*")
                if p.suffix in (".js", ".ts", ".css", ".html")
            )
    return _newest_mtime(candidates)


def _newest_web_build() -> float:
    if not WEB_DIST.is_dir():
        return 0.0
    return _newest_mtime(WEB_DIST.rglob("*"))[0]


def web_bundle_state() -> Tuple[str, str]:
    """One of 'nosrc' | 'missing' | 'stale' | 'fresh', plus a one-line reason."""
    src_at, src_name = _newest_web_source()
    if not src_at:
        return "nosrc", "parent-dashboard/web-ts has no sources to build"
    built_at = _newest_web_build()
    if not built_at:
        return "missing", "web-ts/dist does not exist — the dashboard has never been built"
    if src_at > built_at + 1:
        return "stale", (f"{src_name} is newer than the last build "
                         f"({_ago(src_at)} vs {_ago(built_at)})")
    return "fresh", f"built {_ago(built_at)}"


def _ago(when: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(when))


def _npm_cmd() -> Optional[str]:
    return shutil.which("npm") or shutil.which("npm.cmd")


def build_web_dashboard(force: bool = False, quiet: bool = False) -> bool:
    """Rebuild web-ts/dist when it is older than the sources. Returns False only
    when a build was needed and could not be produced."""
    state, why = web_bundle_state()
    if state == "nosrc":
        print(C.YELLOW + f"  Dashboard UI: {why}" + C.RESET)
        return True
    if state == "fresh" and not force:
        if not quiet:
            print(C.GREEN + f"  Dashboard UI: up to date ({why})." + C.RESET)
        return True

    print(C.YELLOW + f"  Dashboard UI needs a rebuild — {why}." + C.RESET)
    print(C.DIM + "  The browser loads web-ts/dist, not main.js, so without this the "
                  "page runs old code." + C.RESET)
    npm = _npm_cmd()
    if not npm:
        print(C.RED + "  npm was not found on PATH, so the UI cannot be built here."
              + C.RESET)
        print(C.YELLOW + "  Run this once, then start the dashboard again:" + C.RESET)
        print(C.CYAN + "    cd parent-dashboard\\web-ts && npm install && npm run build"
              + C.RESET)
        return False

    try:
        if not (WEB_DIR / "node_modules").is_dir():
            print(C.CYAN + "  Installing dashboard dependencies (first run only)..."
                  + C.RESET)
            if subprocess.run([npm, "install"], cwd=str(WEB_DIR), timeout=900).returncode:
                print(C.RED + "  npm install failed." + C.RESET)
                return False
        print(C.CYAN + "  Building the dashboard UI (vite build)..." + C.RESET)
        rc = subprocess.run([npm, "run", "build"], cwd=str(WEB_DIR), timeout=600).returncode
    except subprocess.TimeoutExpired:
        print(C.RED + "  The UI build timed out." + C.RESET)
        return False
    except OSError as exc:
        print(C.RED + f"  Could not launch npm: {exc}" + C.RESET)
        return False

    if rc:
        print(C.RED + f"  vite build failed (exit {rc}). The dashboard will keep "
              "serving the previous bundle." + C.RESET)
        return False
    state, why = web_bundle_state()
    print(C.GREEN + f"  Dashboard UI rebuilt ({why})." + C.RESET)
    return True


def print_web_bundle_line() -> None:
    """Local, instant, and the first thing to check when the live view is blank:
    is the browser even running the current dashboard code?"""
    state, why = web_bundle_state()
    if state == "fresh":
        print("  Dashboard UI: " + C.GREEN + "current" + C.RESET + C.DIM + f" ({why})" + C.RESET)
        return
    if state == "nosrc":
        print("  Dashboard UI: " + C.YELLOW + why + C.RESET)
        return
    print("  Dashboard UI: " + C.RED + state.upper() + C.RESET + f" — {why}")
    print(C.YELLOW + "  The browser serves web-ts/dist, so it is running OLD code. "
          "Socket features (the live mirror above all) can fail while the rest of the "
          "page looks fine. Fix: " + C.CYAN + "GUARD.bat build" + C.RESET)


def maybe_open_dashboard() -> None:
    global AUTO_REDIRECT_DONE
    if AUTO_REDIRECT_DONE:
        return
    if all_core_done():
        AUTO_REDIRECT_DONE = True
        print(C.GREEN + "All core modules initialized. Opening http://localhost:3000 ..." + C.RESET)
        webbrowser.open("http://localhost:3000")


def action_launch_dashboard() -> None:
    boot_sequence("DASHBOARD CORE", [
        "Validate runtime",
        "Open protected watchdog channel",
        "Spin API and Web UI",
    ])
    # Build the UI before the server starts serving it. Skipping this is what left a
    # week-old bundle live: the socket contract had moved on, so the live mirror sat
    # on "Waiting for screenshots..." forever while everything else looked fine.
    if not build_web_dashboard():
        print(C.YELLOW + "  Starting anyway with the existing bundle — the live view "
              "may not work until the UI is built." + C.RESET)
    ok = launch_batch(DASHBOARD_SCRIPT, "GUARD-DASHBOARD")
    STATUS["dashboard"] = ok


def action_open_progress(cfg: Dict[str, str]) -> None:
    """Open the dashboard directly on the Academic Progress page.

    Academic progress is a standard part of the product, so it gets a first-class
    entry here rather than living behind "open the dashboard and go find a tab".
    The bundle check is not optional: the browser serves web-ts/dist, so on a
    stale bundle this page would simply not exist and the menu would appear to do
    nothing (BUG-29).
    """
    port = cfg.get("server_port", "3000")
    print_web_bundle_line()
    state, _ = web_bundle_state()
    if state not in ("fresh", "nosrc"):
        print(C.YELLOW + "Building the dashboard UI first so the Progress page "
              "exists in the bundle..." + C.RESET)
        build_web_dashboard(quiet=False)
    if not tcp_open("127.0.0.1", int(port), 1.0):
        print(C.YELLOW + f"Nothing is listening on port {port} — start the "
              "dashboard with option 1 first." + C.RESET)
        return
    url = f"http://localhost:{port}/#progress"
    print(C.GREEN + f"Opening {url}" + C.RESET)
    print(C.DIM + "Grades need uploaded activity: the Pi agent ships app, browser "
          "and YouTube history on a 30s cadence once it is running." + C.RESET)
    webbrowser.open(url)


def action_launch_windows_agent() -> None:
    boot_sequence("WINDOWS AGENT", [
        "Load local control profile",
        "Prepare screen interception pipeline",
        "Initialize safety hooks",
    ])
    ok = launch_batch(WINDOWS_AGENT_SCRIPT, "GUARD-WIN-AGENT")
    STATUS["windows_agent"] = ok


def action_launch_pi_agent(cfg: Dict[str, str]) -> None:
    """Option 3 — full Pi sync: upload every agent file, install it if this is a
    fresh Pi, point it at this machine, start it and verify.

    This used to be a config-only recalibration, which is why pressing 3 could
    never bring a fresh or half-installed Pi online: it restarted a unit that
    might not exist yet and then blamed the SSH/sudo password.
    """
    ok = deploy_pi_agent(cfg, restart=True)
    if not ok:
        print(C.YELLOW + "Full sync did not complete. Trying config-only "
              "recalibration so the IP is at least correct..." + C.RESET)
        ok = recalibrate_pi_sync(cfg, restart_agent=True)
    STATUS["pi_agent"] = ok
    STATUS["recalibrated"] = STATUS["recalibrated"] or ok


def action_recalibrate(cfg: Dict[str, str]) -> None:
    """Option 4 — lightweight: just re-point the Pi's server_url and restart.
    Use option 3 / D when the agent code itself changed."""
    ok = recalibrate_pi_sync(cfg, restart_agent=True)
    STATUS["recalibrated"] = ok


def _diag_probe(install: str, svc: str) -> str:
    """Everything worth knowing about the Pi install, as echo=value lines."""
    return "; ".join([
        f"echo DIR=$( [ -d {shlex.quote(install)} ] && echo yes || echo no )",
        f"echo START_SH=$( [ -x {shlex.quote(install + '/start.sh')} ] && echo yes || echo no )",
        f"echo VENV=$( [ -x {shlex.quote(install + '/venv/bin/python3')} ] && echo yes || echo no )",
        f"echo MAIN=$( [ -f {shlex.quote(install + '/main.py')} ] && echo yes || echo no )",
        f"echo UNIT=$( [ -f /etc/systemd/system/{svc}.service ] && echo yes || echo no )",
        f"echo GUARDIAN_SVC=$(systemctl is-active {shlex.quote(svc)} 2>/dev/null)",
        f"echo GUARDIAN_ENABLED=$(systemctl is-enabled {shlex.quote(svc)} 2>/dev/null)",
        "echo GUARDIAN_PROCS=$(pgrep -fc 'main.py' 2>/dev/null || echo 0)",
        "echo PY=$(python3 -V 2>&1)",
        # Capture backend. A Wayland Pi needs grim plus a live compositor socket;
        # without both, the agent still connects and looks online while the live
        # view never paints, so these three lines are the first thing to read
        # when the mirror is blank.
        "echo GRIM=$(command -v grim >/dev/null && echo yes || echo no)",
        "echo SCROT=$(command -v scrot >/dev/null && echo yes || echo no)",
        "echo WAYLAND_SOCKETS=$(ls -d /run/user/*/wayland-* 2>/dev/null | tr '\\n' ',' | sed 's/,$//' || true)",
        f"echo CAPTURE_FAIL=$(journalctl -u {shlex.quote(svc)} -n 200 --no-pager 2>/dev/null "
        "| grep -c 'returned no frame' || true)",
        "echo -n 'SERVER_URL='; sudo -n grep -o '\"server_url\"[^,]*' "
        + shlex.quote(install + "/config.json") + " 2>/dev/null || echo '(no config.json)'",
        # Academic progress is part of the standard install, so the probe reports
        # it like every other core subsystem rather than leaving it invisible:
        # are the three collector modules there, is the flag on, did the
        # collectors actually start, and is anything stuck in the spool.
        f"echo PROGRESS_FILES=$(ls {shlex.quote(install + '/usage_tracker.py')} "
        f"{shlex.quote(install + '/browser_history.py')} {shlex.quote(install + '/spool.py')} "
        "2>/dev/null | wc -l)/3",
        "echo -n 'PROGRESS_CFG='; sudo -n grep -o '\"enable_progress_tracking\"[^,]*' "
        + shlex.quote(install + "/config.json") + " 2>/dev/null || echo '(key absent - defaults to on)'",
        f"echo PROGRESS_READY=$(journalctl -u {shlex.quote(svc)} -n 400 --no-pager 2>/dev/null "
        "| grep -c 'progress collectors ready' || true)",
        "echo PROGRESS_QUEUED=$(sudo -n cat /root/.guardian-shield/progress/*.jsonl "
        "2>/dev/null | wc -l)",
        "echo XPRINTIDLE=$(command -v xprintidle >/dev/null && echo yes "
        "|| echo 'no - academic time not idle-trimmed')",
    ])


def pi_diag(cfg: Dict[str, str]) -> bool:
    """One-shot Pi health dump: install state, service state, config, log tail."""
    print_web_bundle_line()
    local_ips = get_local_ipv4s()
    pi_ip = _quick_detect_pi_ip(cfg) or (detect_pi_ip(cfg, local_ips) if local_ips else None)
    if not pi_ip:
        print(C.RED + "Pi not reachable over SSH (port 22). Is it powered on and "
              "on the same network?" + C.RESET)
        return False
    print(C.GREEN + f"Pi reachable at {pi_ip}" + C.RESET)

    if not _unattended_ssh(cfg):
        print(C.YELLOW + "No plink/sshpass — running the probe interactively; "
              "enter the Pi password when asked." + C.RESET)
        install = cfg["pi_install_dir"]
        svc = cfg["pi_service"]
        ssh_exec(cfg, pi_ip, sudo_preamble(cfg) + "; " + _diag_probe(install, svc),
                 tty=True, timeout=90)
        pi_journal_tail(cfg, pi_ip, svc, 40)
        return True

    ssh_ok, sudo_ok, detail = probe_ssh_and_sudo(cfg, pi_ip)
    print("  SSH login: " + (C.GREEN + "ok" if ssh_ok else C.RED + "FAILED") + C.RESET)
    print("  sudo:      " + (C.GREEN + "ok" if sudo_ok else C.RED + "REFUSED") + C.RESET)
    if not ssh_ok or not sudo_ok:
        print(C.DIM + (detail or "")[-600:] + C.RESET)
        return False

    install = cfg["pi_install_dir"]
    svc = cfg["pi_service"]
    _, out = ssh_capture(cfg, pi_ip, sudo_preamble(cfg) + "; " + _diag_probe(install, svc),
                         timeout=60)
    for line in (out or "").splitlines():
        if "=" in line:
            print("  " + line.strip())

    if _marker(out, "GUARDIAN_SVC") != "active":
        pi_journal_tail(cfg, pi_ip, svc, 40)
        return False

    # Service up but capture dead == "device online, blank mirror". Say so plainly
    # instead of reporting a clean bill of health.
    fails = _marker(out, "CAPTURE_FAIL") or "0"
    if fails.isdigit() and int(fails) > 0:
        print(C.YELLOW + f"  Screen capture failed {fails}x in the last 200 log lines — "
              "the device will look online with a blank live view." + C.RESET)
        if _marker(out, "GRIM") == "no":
            print(C.YELLOW + "  grim is not installed; a Wayland desktop cannot be "
                  "captured without it (sudo apt install grim)." + C.RESET)
        elif not (_marker(out, "WAYLAND_SOCKETS") or "").strip():
            print(C.YELLOW + "  No Wayland socket under /run/user — no desktop session "
                  "is logged in, so there is nothing to capture yet." + C.RESET)
        pi_journal_tail(cfg, pi_ip, svc, 40)
    return True


def show_network_report(cfg: Dict[str, str]) -> None:
    local_ips = get_local_ipv4s()
    port = cfg.get("server_port", "3000")
    print(C.BLUE + "Network report:" + C.RESET)
    print(f"  Local IPv4: {', '.join(local_ips) if local_ips else '-'}")
    print(f"  server_ip setting: {cfg.get('server_ip','auto')} (port {port})")
    print(f"  Cached Pi IP: {cfg.get('last_pi_ip','-')}")
    print(f"  Last host IP used: {cfg.get('last_host_ip','-')}")
    resolved = resolve_hostname_ip(cfg["pi_hostname"])
    print(f"  Pi hostname resolve ({cfg['pi_hostname']}): {resolved or 'not resolved'}")

    try:
        listening = tcp_open("127.0.0.1", int(port), 1.0)
    except ValueError:
        listening = False
    print(f"  Dashboard on 127.0.0.1:{port}: " +
          (C.GREEN + "listening" + C.RESET if listening else C.YELLOW + "not listening" + C.RESET))
    print_web_bundle_line()

    pi_ip = _quick_detect_pi_ip(cfg)
    print(f"  Pi SSH: " + (C.GREEN + f"reachable at {pi_ip}" + C.RESET if pi_ip
                           else C.YELLOW + "not reachable from cache/hostname" + C.RESET))
    if pi_ip and local_ips:
        host_ip = resolve_host_ip(cfg, local_ips, pi_ip)
        print(f"  Agents will be pointed at: http://{host_ip}:{port}")
    try:
        cur = json.loads(PI_AGENT_CONFIG.read_text(encoding="utf-8")).get("server_url", "-")
        print(f"  pi-agent/agent/config.json server_url: {cur}")
    except Exception:
        print("  pi-agent/agent/config.json server_url: (unreadable)")


def run_full_sequence(cfg: Dict[str, str]) -> None:
    action_launch_dashboard()
    action_launch_windows_agent()
    # Full init pushes the current agent code + IP to the Pi, then falls back
    # to a config-only recalibration if the upload path is unavailable.
    if not deploy_pi_agent(cfg, restart=True):
        print(C.YELLOW + "Deploy incomplete — falling back to IP recalibration only." + C.RESET)
        action_launch_pi_agent(cfg)
    else:
        STATUS["pi_agent"] = True
        STATUS["recalibrated"] = True


def action_update_target(cfg: Dict[str, str]) -> None:
    print(C.BLUE + "Update Pi target settings" + C.RESET)
    host = input(f"Pi hostname [{cfg['pi_hostname']}]: ").strip()
    user = input(f"Pi username [{cfg['pi_username']}]: ").strip()

    if host:
        cfg["pi_hostname"] = host
    if user:
        cfg["pi_username"] = user

    save_cfg(cfg)
    print(C.GREEN + "Pi target settings saved." + C.RESET)


# ─── Telegram alerts ─────────────────────────────────────────────────────────
# The parent's phone channel. Credentials are written into server-ts/.env, which
# is gitignored and read only by the Node server — deliberately NOT into
# guard_runtime.json, because that file is a plain runtime config that gets opened,
# copied and pasted into chats when something breaks, and because load_cfg() would
# need a DEFAULTS entry for every key (a silent-drop trap). One secret, one home.


def _env_lines() -> List[str]:
    if not SERVER_ENV_PATH.exists():
        return []
    try:
        return SERVER_ENV_PATH.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []


def read_server_env() -> Dict[str, str]:
    """Parse server-ts/.env into a dict. Comments and blank lines are ignored."""
    out: Dict[str, str] = {}
    for line in _env_lines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        out[key.strip()] = value.strip()
    return out


def upsert_server_env(updates: Dict[str, str], header: str = "") -> None:
    """
    Set keys in server-ts/.env, rewriting a key in place if it is already there and
    appending the rest. Existing lines, comments and order are preserved: this file
    also holds JWT_SECRET and the admin password, so it is edited surgically and
    never regenerated from a template.
    """
    lines = _env_lines()
    remaining = dict(updates)
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.partition("=")[0].strip()
        if key in remaining:
            lines[idx] = f"{key}={remaining.pop(key)}"

    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        if header:
            lines.append(header)
        for key, value in remaining.items():
            lines.append(f"{key}={value}")

    SERVER_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" via open() rather than Path.write_text(newline=...), which only
    # gained the newline argument on Python 3.10; dotenv is happy with LF on Windows.
    with open(SERVER_ENV_PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")


def mask_secret(value: str) -> str:
    """Enough to recognise a key, never enough to use it."""
    if not value:
        return "(not set)"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}…{value[-3:]} ({len(value)} chars)"


def telegram_state() -> Tuple[bool, bool, bool]:
    """(has token, has chat id, alerts enabled) straight from server-ts/.env."""
    env = read_server_env()
    enabled = str(env.get("TELEGRAM_ALERTS", "true")).strip().lower() != "false"
    return bool(env.get("TELEGRAM_BOT_TOKEN")), bool(env.get("TELEGRAM_CHAT_ID")), enabled


def telegram_api(token: str, method: str, payload: Optional[dict] = None,
                 timeout: float = 12.0) -> Tuple[bool, dict, str]:
    """
    One call to the Bot API from GUARD itself, so a token can be verified without
    starting the dashboard. Returns (ok, result, detail) and never raises; the
    error text never includes the URL, because the URL carries the token.
    """
    import urllib.request
    import urllib.error

    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8", "replace"))
        except Exception:
            return False, {}, f"Telegram said HTTP {exc.code}"
        return False, {}, str(body.get("description") or f"HTTP {exc.code}")
    except urllib.error.URLError as exc:
        return False, {}, f"cannot reach Telegram ({exc.reason})"
    except Exception as exc:  # timeouts, bad JSON, anything else
        return False, {}, f"call failed ({type(exc).__name__})"

    if not body.get("ok"):
        return False, {}, str(body.get("description") or "Telegram refused the call")
    return True, body.get("result") or {}, ""


def discover_chat_ids(token: str) -> List[Tuple[str, str]]:
    """
    Read the bot's pending updates and pull out every chat that has messaged it.
    This exists so the parent never has to hunt for a numeric chat id: send the bot
    any message from the phone, come back here, and the id is found automatically.
    """
    ok, result, _detail = telegram_api(token, "getUpdates", {"limit": 20, "timeout": 0})
    if not ok or not isinstance(result, list):
        return []
    found: Dict[str, str] = {}
    for update in result:
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            chat = ((update.get(key) or {}).get("chat")) or {}
            cid = chat.get("id")
            if cid is None:
                continue
            label = chat.get("title") or " ".join(
                filter(None, [chat.get("first_name"), chat.get("last_name")])
            ) or chat.get("username") or chat.get("type") or "chat"
            found[str(cid)] = str(label)
    return list(found.items())


def print_telegram_help() -> None:
    print(C.DIM + "  How to get the two values (one-time, about two minutes):" + C.RESET)
    print(C.DIM + "   1. In Telegram, open @BotFather -> /newbot -> pick any name." + C.RESET)
    print(C.DIM + "      BotFather replies with a token like 7712345678:AAF..." + C.RESET)
    print(C.DIM + "   2. Open YOUR new bot and send it any message (say 'hi')." + C.RESET)
    print(C.DIM + "      That is what lets GUARD find your chat id by itself." + C.RESET)
    print()


def action_telegram(cfg: Dict[str, str]) -> None:
    """Connect (or re-test, or mute) the Telegram alert channel."""
    print(C.BLUE + C.BOLD + "Connect Telegram alerts" + C.RESET)
    print(C.DIM + "Device alerts (malware, blocked site, app timer, blocked app) get" + C.RESET)
    print(C.DIM + "pushed to your phone. Credentials are stored server-side only, in" + C.RESET)
    print(C.DIM + f"{SERVER_ENV_PATH.name} — gitignored, never sent to the browser." + C.RESET)
    print()

    env = read_server_env()
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = env.get("TELEGRAM_CHAT_ID", "")
    has_token, has_chat, enabled = telegram_state()

    print(f"  Current bot token : {mask_secret(token)}")
    print(f"  Current chat id   : {chat_id or '(not set)'}")
    print(f"  Alerts            : {'ENABLED' if enabled else 'MUTED (TELEGRAM_ALERTS=false)'}")
    print()

    if has_token and has_chat:
        print(" 1. Send a test message now")
        print(" 2. Replace the token / chat id")
        print(" 3. " + ("Mute alerts" if enabled else "Un-mute alerts"))
        print(" 0. Back")
        pick = input(C.BOLD + "Choose: " + C.RESET).strip()
        if pick == "0" or pick == "":
            return
        if pick == "1":
            _telegram_send_test(token, chat_id)
            return
        if pick == "3":
            upsert_server_env({"TELEGRAM_ALERTS": "false" if enabled else "true"})
            print(C.GREEN + ("Alerts muted." if enabled else "Alerts un-muted.") + C.RESET)
            print(C.YELLOW + "Restart the dashboard (option 1) for this to take effect." + C.RESET)
            return
        # pick == "2" falls through to the setup prompts below.

    print_telegram_help()
    entered = input("Bot token from @BotFather (blank = keep current): ").strip()
    if entered:
        token = entered
    if not token:
        print(C.RED + "No token, nothing to save." + C.RESET)
        return
    if ":" not in token or len(token) < 20:
        print(C.RED + "That does not look like a bot token (expected 123456:AA...)." + C.RESET)
        print(C.DIM + "Copy the whole line BotFather sent, including the digits before the colon." + C.RESET)
        return

    ok, me, detail = telegram_api(token, "getMe")
    if not ok:
        print(C.RED + f"Telegram rejected that token: {detail}" + C.RESET)
        print(C.DIM + "Nothing was saved. Check the token and try again." + C.RESET)
        return
    bot_name = me.get("username") or me.get("first_name") or "bot"
    print(C.GREEN + f"Token accepted — bot is @{bot_name}" + C.RESET)

    entered_chat = input(f"Chat id (blank = find it automatically) [{chat_id or 'none'}]: ").strip()
    if entered_chat:
        chat_id = entered_chat
    else:
        print(C.DIM + f"  Looking for chats that have messaged @{bot_name}..." + C.RESET)
        found = discover_chat_ids(token)
        if not found:
            print(C.YELLOW + "  No messages found yet." + C.RESET)
            print(C.DIM + f"  Open @{bot_name} in Telegram, send it 'hi', then run this option again." + C.RESET)
            if not chat_id:
                print(C.DIM + "  The token was NOT saved, so nothing is half-configured." + C.RESET)
                return
        elif len(found) == 1:
            chat_id, label = found[0]
            print(C.GREEN + f"  Found: {label} -> {chat_id}" + C.RESET)
        else:
            for i, (cid, label) in enumerate(found, start=1):
                print(f"   {i}. {label} -> {cid}")
            sel = input("  Which chat should get the alerts? [1]: ").strip() or "1"
            if not sel.isdigit() or not 1 <= int(sel) <= len(found):
                print(C.RED + "  Not a listed choice — nothing saved." + C.RESET)
                return
            chat_id = found[int(sel) - 1][0]

    upsert_server_env(
        {
            "TELEGRAM_BOT_TOKEN": token,
            "TELEGRAM_CHAT_ID": chat_id,
            "TELEGRAM_ALERTS": "true",
            "TELEGRAM_MAX_PER_HOUR": env.get("TELEGRAM_MAX_PER_HOUR", "20"),
        },
        header=(
            "# ─── Telegram alerts ────────────────────────────────────────────────────\n"
            "# Written by GUARD (option M). Server-side only; this file is gitignored and\n"
            "# nothing here is ever bundled into the browser. TELEGRAM_ALERTS=false mutes\n"
            "# the channel while keeping the credentials. TELEGRAM_ALERT_TYPES can hold a\n"
            "# comma-separated allowlist (malware,dangerous_file,blocked_site,app_blocked,\n"
            "# app_timer_expired,blocking_error); empty means forward everything."
        ),
    )
    print(C.GREEN + f"Saved to {SERVER_ENV_PATH}" + C.RESET)
    _telegram_send_test(token, chat_id)
    print(C.YELLOW + "Restart the dashboard (option 1) — the server reads .env at startup." + C.RESET)


def _telegram_send_test(token: str, chat_id: str) -> bool:
    """Prove the pair works end to end, from GUARD, without the server running."""
    if not token or not chat_id:
        print(C.RED + "Need both a token and a chat id to test." + C.RESET)
        return False
    print(C.DIM + "  Sending test message..." + C.RESET)
    ok, _result, detail = telegram_api(
        token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": (
                "✅ <b>GUARDIAN connected</b>\n"
                "This chat will receive device alerts from your Guardian dashboard."
            ),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )
    if ok:
        print(C.GREEN + "  Delivered — check your phone." + C.RESET)
        return True
    print(C.RED + f"  Not delivered: {detail}" + C.RESET)
    if "chat not found" in detail.lower():
        print(C.DIM + "  Open the bot in Telegram and send it a message first, then re-test." + C.RESET)
    return False


# ─── SSH data transfer (Pi → this machine) ───────────────────────────────────
# Pulls the monitored device's own records off the Pi: the progress spools the
# agent has not yet drained over the socket, the history cursor, the agent log and
# the service journal. Read-only on the Pi apart from one /tmp staging directory,
# which is removed afterwards. Nothing is restarted and nothing is killed — this
# is a diagnostic and archival path, so it must be safe to run at any moment.
#
# The agent runs as root, so its state lives under /root and is not readable by
# the SSH user. The staging step therefore copies with sudo, hands ownership to the
# SSH user, and tars the result: one file to fetch instead of a dozen sudo reads.

# What gets collected, as (remote directory, folder name inside the export). The
# agent's own state comes first; the same paths under the SSH user's home are
# collected too, because an agent started by hand (not by systemd) writes there and
# an export that quietly skipped it would look like "the child did nothing".
PI_DATA_SOURCES = [
    ("/root/.guardian-shield/progress", "progress"),
    ("/root/.guardian-shield/logs", "logs"),
    ("$HOME/.guardian-shield/progress", "progress-user"),
    ("$HOME/.guardian-shield/logs", "logs-user"),
]


def _pi_export_cmd(cfg: Dict[str, str], stage: str, archive: str) -> str:
    """The remote side of a data pull: stage, describe, archive, hand over.

    Built as one "; "-joined line like every other remote command here, NOT as a
    bash -lc '...' wrapper: sudo_preamble() itself contains single quotes, so a
    single-quoted wrapper would terminate early and the password would end up as
    an argument. Callers prepend the preamble exactly as the deploy path does.

    Every copy ends in `|| true`. A Pi that never ran the app collector has no
    usage.jsonl, and that is a normal export, not a failure — the MANIFEST is what
    says which parts came back.
    """
    install = cfg["pi_install_dir"]
    service = cfg["pi_service"]
    user = cfg["pi_username"]
    q_stage = shlex.quote(stage)
    q_archive = shlex.quote(archive)
    q_svc = shlex.quote(service)

    parts = [
        # No `pkill` anywhere in this chain, by design: an export must never be
        # able to stop the agent it is exporting from (BUG-23).
        f"rm -rf {q_stage} {q_archive} 2>/dev/null || true",
        "mkdir -p " + " ".join(
            shlex.quote(f"{stage}/{dest}") for _src, dest in PI_DATA_SOURCES
        ) + f" {shlex.quote(stage + '/agent')} {shlex.quote(stage + '/service')}",
    ]
    for src, dest in PI_DATA_SOURCES:
        # Double quotes, not shlex.quote: "$HOME" has to expand on the Pi. These
        # are fixed literals from this file, never user input.
        parts.append(f'sudo -n cp -a "{src}/." "{stage}/{dest}/" 2>/dev/null || true')
    for name in ("config.json", "warning_counts.json", "kill_attempts.txt"):
        parts.append(
            f"sudo -n cp -a {shlex.quote(install + '/' + name)} "
            f"{shlex.quote(stage + '/agent/')} 2>/dev/null || true"
        )
    parts += [
        f"{{ sudo -n journalctl -u {q_svc} -n 2000 --no-pager 2>/dev/null "
        f"|| journalctl -u {q_svc} -n 2000 --no-pager 2>/dev/null "
        f"|| echo '(journal unavailable)'; }} > {shlex.quote(stage + '/service/journal.txt')}",
        f"{{ systemctl is-active {q_svc} 2>/dev/null; systemctl is-enabled {q_svc} "
        f"2>/dev/null; }} > {shlex.quote(stage + '/service/state.txt')} || true",
        f"{{ uname -a; date -Is; uptime; df -h /; }} "
        f"> {shlex.quote(stage + '/service/host.txt')} 2>/dev/null || true",
        # Sizes and paths of everything staged, so the local side can prove what
        # arrived without opening a single one of the child's records.
        f"find {q_stage} -type f -printf '%s\\t%P\\n' 2>/dev/null | sort -k2 "
        f"> {shlex.quote(stage + '/MANIFEST.txt')} || true",
        f"sudo -n chown -R {shlex.quote(user + ':' + user)} {q_stage} 2>/dev/null || true",
        f"tar -czf {q_archive} -C {shlex.quote(str(PurePosixPath(stage).parent))} "
        f"{shlex.quote(PurePosixPath(stage).name)} 2>/dev/null",
        # Staging is deleted immediately: it is a plaintext copy of the child's
        # records sitting in a world-readable /tmp until it goes.
        f"rm -rf {q_stage} 2>/dev/null || true",
        f"echo GUARDIAN_EXPORT=$( [ -s {q_archive} ] && echo ok || echo empty )",
        f"echo GUARDIAN_BYTES=$(stat -c %s {q_archive} 2>/dev/null || echo 0)",
    ]
    return "; ".join(parts)


def _safe_extract(archive: Path, dest: Path) -> Tuple[int, int]:
    """Unpack the export with Python's tarfile, never Windows `tar`.

    Two reasons. Older Windows builds have no bsdtar at all, and the ones that do
    handle a Linux-made .tar.gz inconsistently. And a tarball is a list of paths a
    remote machine chose: a member named ../../something would otherwise be written
    outside the export folder. Python 3.12+ enforces that with filter="data"; on
    older interpreters the same check is done by hand, because the Windows Pythons
    in the field are not all 3.12.

    Returns (files written, members rejected).
    """
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    written = 0
    rejected = 0
    with tarfile.open(archive, "r:gz") as tf:
        members = []
        for m in tf.getmembers():
            if m.issym() or m.islnk():
                rejected += 1          # a link could point anywhere on this disk
                continue
            target = (root / m.name).resolve()
            if target != root and root not in target.parents:
                rejected += 1
                continue
            members.append(m)
            if m.isfile():
                written += 1
        try:
            tf.extractall(dest, members=members, filter="data")
        except TypeError:
            # Python < 3.12: no filter argument. The loop above already did the
            # containment check, so this is the same guarantee, hand-rolled.
            tf.extractall(dest, members=members)
    return written, rejected


def _export_summary(folder: Path) -> List[str]:
    """One line per part of the export, counting rows rather than reading them.

    Deliberately shallow: this prints how much came back, never what it says. The
    child's browsing history is in these files and the terminal is not the place
    for it — the dashboard is.
    """
    lines: List[str] = []
    for sub, label in (("progress", "progress spool (root)"),
                       ("progress-user", "progress spool (user)"),
                       ("logs", "agent logs (root)"),
                       ("logs-user", "agent logs (user)"),
                       ("agent", "agent config"),
                       ("service", "service state")):
        d = folder / sub
        if not d.is_dir():
            continue
        files = sorted(p for p in d.rglob("*") if p.is_file())
        if not files:
            continue
        total = sum(p.stat().st_size for p in files)
        rows = 0
        for p in files:
            if p.suffix == ".jsonl":
                try:
                    with open(p, "r", encoding="utf-8", errors="replace") as fh:
                        rows += sum(1 for line in fh if line.strip())
                except OSError:
                    pass
        detail = f"{len(files)} file(s), {total // 1024 or 1} KB"
        if rows:
            detail += f", {rows} queued record(s)"
        lines.append(f"  {label:<24} {detail}")
    return lines


def pull_pi_data(cfg: Dict[str, str], pi_ip: str) -> Optional[Path]:
    """Stage on the Pi, fetch one tarball, unpack it here. Returns the folder.

    One archive rather than a dozen scp reads: each remote path would otherwise
    need its own sudo round trip, and a partial set of them is indistinguishable
    from "the child used nothing today".
    """
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    stage = f"/tmp/guardian-export-{stamp}"
    archive = f"{stage}.tar.gz"
    dest = DATA_EXPORT_DIR / stamp

    print(C.BLUE + f"Staging the export on the Pi ({pi_ip})..." + C.RESET)
    cmd = sudo_preamble(cfg) + "; " + _pi_export_cmd(cfg, stage, archive)
    if _unattended_ssh(cfg):
        rc, out = ssh_capture(cfg, pi_ip, cmd, timeout=180)
    else:
        # OpenSSH will prompt for the password; capturing would hide the prompt,
        # so let it stream and read the outcome from the archive instead.
        print(C.YELLOW + "No plink/sshpass — enter the Pi password when asked."
              + C.RESET)
        rc = ssh_exec(cfg, pi_ip, cmd, tty=True, timeout=300)
        out = ""

    state = _marker(out, "GUARDIAN_EXPORT")
    if state == "empty":
        print(C.RED + "The Pi produced an empty archive — nothing was readable."
              + C.RESET)
        print(C.DIM + "Usually sudo: run option 3 (verify) to check SSH+sudo."
              + C.RESET)
        return None
    if out and state != "ok":
        print(C.RED + "The staging step did not report success." + C.RESET)
        print(C.DIM + (out or f"ssh exit {rc}")[-600:] + C.RESET)
        return None

    remote_bytes = _marker(out, "GUARDIAN_BYTES")
    if remote_bytes.isdigit():
        print(C.DIM + f"  archive on the Pi: {int(remote_bytes) // 1024 or 1} KB"
              + C.RESET)

    print(C.BLUE + "Downloading..." + C.RESET)
    landing = DATA_EXPORT_DIR / f".incoming-{stamp}"
    if scp_pull(cfg, pi_ip, [archive], str(landing)) != 0:
        print(C.RED + "Download failed. The Pi still holds the archive at "
              + archive + C.RESET)
        return None

    local_archive = landing / f"guardian-export-{stamp}.tar.gz"
    if not local_archive.exists():
        found = sorted(landing.glob("*.tar.gz"))
        if not found:
            print(C.RED + "scp reported success but no archive arrived." + C.RESET)
            return None
        local_archive = found[0]

    print(C.BLUE + "Unpacking..." + C.RESET)
    try:
        written, rejected = _safe_extract(local_archive, dest)
    except (tarfile.TarError, OSError) as exc:
        print(C.RED + f"The archive could not be read: {exc}" + C.RESET)
        print(C.DIM + f"It is kept at {local_archive} in case it is salvageable."
              + C.RESET)
        return None
    if rejected:
        print(C.YELLOW + f"  {rejected} archive entry(ies) refused as unsafe "
              "paths — reported, not silently dropped." + C.RESET)

    # The export unpacks as .../<stamp>/guardian-export-<stamp>/... because tar
    # keeps the staging folder name. Flatten that so the printed path is the data.
    inner = dest / f"guardian-export-{stamp}"
    if inner.is_dir():
        for entry in list(inner.iterdir()):
            target = dest / entry.name
            if not target.exists():
                shutil.move(str(entry), str(target))
        try:
            inner.rmdir()
        except OSError:
            pass

    # Remove the tarball from the Pi. Its contents are a plaintext copy of the
    # child's records; leaving it in /tmp would outlive the reason it existed.
    ssh_exec(cfg, pi_ip, f"rm -f {shlex.quote(archive)}", timeout=30)
    shutil.rmtree(landing, ignore_errors=True)

    print(C.GREEN + f"Export complete — {written} file(s)." + C.RESET)
    for line in _export_summary(dest):
        print(line)
    print(C.DIM + "  (MANIFEST.txt lists every file with its size.)" + C.RESET)
    print(C.GREEN + "Saved to: " + C.RESET + str(dest))
    return dest


def action_pull_data(cfg: Dict[str, str]) -> None:
    """Menu/CLI entry point for a data pull."""
    print(C.BLUE + "Pull monitored-device data from the Pi over SSH" + C.RESET)
    print(C.DIM + "Read-only on the Pi: nothing is stopped, restarted or deleted "
          "except the temporary archive this creates." + C.RESET)

    pi_ip = _quick_detect_pi_ip(cfg)
    if not pi_ip:
        local_ips = get_local_ipv4s()
        pi_ip = detect_pi_ip(cfg, local_ips) if local_ips else None
    if not pi_ip:
        print(C.RED + "The Pi could not be reached over SSH." + C.RESET)
        print(C.DIM + f"Tried {cfg.get('last_pi_ip', '-')} and "
              f"{cfg['pi_hostname']}. Put the Pi on the same Wi-Fi and retry, "
              "or run option 3 to verify." + C.RESET)
        return

    folder = pull_pi_data(cfg, pi_ip)
    if folder is None:
        return
    STATUS["data_pulled"] = True
    print(C.DIM + "This is an archive copy. The dashboard's own database is "
          "still the live record — a pull changes nothing on either side."
          + C.RESET)


def action_close_all_systems(cfg: Dict[str, str]) -> None:
    global AUTO_REDIRECT_DONE
    boot_sequence("GLOBAL SHUTDOWN", [
        "Stop local live-control tasks",
        "Terminate dashboard sockets",
        "Stop remote Pi guardian-agent",
        "Reset GUARD runtime status",
    ])

    local_stop_notes = []

    if STOP_WINDOWS_AGENT_SCRIPT.exists():
        rc = subprocess.run(["cmd", "/c", str(STOP_WINDOWS_AGENT_SCRIPT)]).returncode
        local_stop_notes.append("Windows agent stop script: OK" if rc == 0 else "Windows agent stop script: failed")
    else:
        local_stop_notes.append("Windows agent stop script: missing")

    closed_dash = _kill_cmd_window_by_title("GUARD-DASHBOARD")
    closed_win = _kill_cmd_window_by_title("GUARD-WIN-AGENT")
    killed_port_3000 = _kill_listeners_on_port(3000)

    local_stop_notes.append(f"Dashboard terminal closed: {'yes' if closed_dash else 'no/none'}")
    local_stop_notes.append(f"Windows agent terminal closed: {'yes' if closed_win else 'no/none'}")
    local_stop_notes.append(f"Processes killed on port 3000: {killed_port_3000}")

    remote_note = "Pi agent stop skipped"
    if shutil.which("ssh") or shutil.which("plink"):
        pi_ip = _quick_detect_pi_ip(cfg)
        if pi_ip:
            rc = ssh_exec(cfg, pi_ip, f"sudo systemctl stop {shlex.quote(cfg['pi_service'])}",
                          tty=True, timeout=90)
            remote_note = "Pi guardian-agent stopped" if rc == 0 else "Pi guardian-agent stop failed"
        else:
            remote_note = "Pi IP not reachable; remote stop skipped"
    else:
        remote_note = "SSH not found; remote stop skipped"

    STATUS["dashboard"] = False
    STATUS["windows_agent"] = False
    STATUS["pi_agent"] = False
    STATUS["recalibrated"] = False
    AUTO_REDIRECT_DONE = False

    print(C.YELLOW + "Shutdown report:" + C.RESET)
    for line in local_stop_notes:
        print(f"  - {line}")
    print(f"  - {remote_note}")


def run_cli(argv: List[str]) -> int:
    """Non-interactive entry points used by GUARD.bat <command>."""
    enable_ansi()
    cfg = load_cfg()
    cmd = argv[0].lower()

    if cmd in ("deploy", "push", "upload"):
        return 0 if deploy_pi_agent(cfg, restart=True) else 1

    if cmd == "verify":
        local_ips = get_local_ipv4s()
        pi_ip = _quick_detect_pi_ip(cfg) or (detect_pi_ip(cfg, local_ips) if local_ips else None)
        if not pi_ip:
            print(C.RED + "Pi not reachable." + C.RESET)
            return 1
        host_ip = resolve_host_ip(cfg, local_ips, pi_ip)
        if not host_ip:
            print(C.RED + "Could not determine the dashboard host IP." + C.RESET)
            return 1
        return 0 if verify_pi_online(cfg, pi_ip, host_ip, cfg.get("server_port", "3000")) else 1

    if cmd == "ip":
        if len(argv) < 2:
            print(f"Current server IP: {cfg.get('server_ip','auto')}:{cfg.get('server_port','3000')}")
            print("Usage: GUARD.bat ip <address|auto> [port]")
            return 0
        cfg["server_ip"] = argv[1].strip()
        if len(argv) > 2 and argv[2].isdigit():
            cfg["server_port"] = argv[2]
        save_cfg(cfg)
        fixed = cfg["server_ip"].lower()
        if fixed != "auto":
            for note in apply_ip_locally(cfg["server_ip"], cfg.get("server_port", "3000")):
                print("  - " + note)
        print(C.GREEN + f"server_ip set to {cfg['server_ip']}:{cfg.get('server_port','3000')}" + C.RESET)
        print(C.DIM + "Run `GUARD.bat deploy` to push this to the Pi." + C.RESET)
        return 0

    if cmd in ("build", "web", "ui"):
        return 0 if build_web_dashboard(force="force" in argv[1:], quiet=False) else 1

    if cmd in ("status", "report"):
        show_network_report(cfg)
        return 0

    if cmd in ("diag", "doctor", "logs"):
        return 0 if pi_diag(cfg) else 1

    if cmd in ("progress", "academic", "academics", "grades"):
        action_open_progress(cfg)
        return 0

    if cmd in ("telegram", "tg", "alerts"):
        # `GUARD.bat telegram test` re-tests the saved pair without the prompts.
        if len(argv) > 1 and argv[1].lower() == "test":
            env = read_server_env()
            ok = _telegram_send_test(
                env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", "")
            )
            return 0 if ok else 1
        action_telegram(cfg)
        return 0

    if cmd in ("datapull", "pull", "fetch", "export"):
        # TRANSFER-DATA.bat lands here. Exit code matters: this is the one command
        # likely to be put in a scheduled task, and a silent 0 on a failed pull
        # would look like a nightly archive that is quietly empty.
        pi_ip = _quick_detect_pi_ip(cfg)
        if not pi_ip:
            local_ips = get_local_ipv4s()
            pi_ip = detect_pi_ip(cfg, local_ips) if local_ips else None
        if not pi_ip:
            print(C.RED + "Pi not reachable — nothing pulled." + C.RESET)
            return 1
        return 0 if pull_pi_data(cfg, pi_ip) else 1

    if cmd in ("pi-ip", "piip"):
        if len(argv) < 2:
            print(C.RED + "Usage: GUARD.bat pi-ip <current-Pi-IPv4>" + C.RESET)
            return 2
        try:
            cfg["last_pi_ip"] = str(ipaddress.IPv4Address(argv[1]))
        except ipaddress.AddressValueError:
            print(C.RED + f"Invalid IPv4 address: {argv[1]}" + C.RESET)
            return 2
        save_cfg(cfg)
        print(C.GREEN + f"Pi target IP saved as {cfg['last_pi_ip']}." + C.RESET)
        print(C.DIM + "This updates GUARD's connection target; the Pi's DHCP/static "
              "address is managed by the network/router." + C.RESET)
        return 0

    if cmd in ("recalibrate", "recal", "resync", "network"):
        cfg_fresh = load_cfg()
        ok = recalibrate_pi_sync(cfg_fresh, restart_agent=True)
        return 0 if ok else 1

    print(C.RED + f"Unknown command: {cmd}" + C.RESET)
    print("Commands: deploy | verify | recalibrate | pi-ip <addr> | diag | build | progress | "
          "telegram [test] | datapull | ip <addr> [port] | status")
    return 2


def main() -> None:
    enable_ansi()
    cfg = load_cfg()

    while True:
        clear_screen()
        draw_menu(cfg)
        choice = input(C.BOLD + "Select option: " + C.RESET).strip()
        print()

        if choice.lower() == "d":
            action_deploy_pi(cfg)
        elif choice.lower() == "i":
            action_set_server_ip(cfg)
        elif choice.lower() == "t":
            pi_diag(cfg)
        elif choice.lower() == "a":
            action_open_progress(cfg)
        elif choice.lower() == "m":
            action_telegram(cfg)
        elif choice.lower() == "p":
            action_pull_data(cfg)
        elif choice == "1":
            action_launch_dashboard()
        elif choice == "2":
            action_launch_windows_agent()
        elif choice == "3":
            action_launch_pi_agent(cfg)
        elif choice == "4":
            action_recalibrate(cfg)
        elif choice == "5":
            run_full_sequence(cfg)
        elif choice == "6":
            webbrowser.open("http://localhost:3000")
        elif choice == "7":
            show_network_report(cfg)
        elif choice == "8":
            action_update_target(cfg)
        elif choice == "9":
            action_close_all_systems(cfg)
        elif choice == "0":
            print(C.YELLOW + "GUARD console shutdown." + C.RESET)
            return
        else:
            print(C.RED + "Unknown option." + C.RESET)

        maybe_open_dashboard()
        input("\nPress Enter to continue...")


if __name__ == "__main__":
    try:
        if len(sys.argv) > 1:
            sys.exit(run_cli(sys.argv[1:]))
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(0)

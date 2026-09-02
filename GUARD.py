#!/usr/bin/env python3
"""
GUARD - Guardian unified sci-fi launcher
- Launch dashboard, Windows agent tools, and Pi agent sync
- Recalibrate Pi<->laptop IP mapping when Wi-Fi changes
- Shows task status with green DONE markers
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
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
    print(C.CYAN + f"[INIT] {name}" + C.RESET)
    for step in steps:
        print(C.DIM + f"  -> {step}..." + C.RESET)
        time.sleep(0.28)
    print(C.GREEN + f"  -> {name} sequence complete" + C.RESET)
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


def detect_pi_ip(cfg: Dict[str, str], local_ips: List[str]) -> Optional[str]:
    cached = cfg.get("last_pi_ip", "").strip()
    if cached and tcp_open(cached, 22, 0.3):
        return cached

    resolved = resolve_hostname_ip(cfg["pi_hostname"])
    if resolved and tcp_open(resolved, 22, 0.3):
        return resolved

    prefixes = sorted({".".join(ip.split(".")[:3]) for ip in local_ips})
    own = set(local_ips)
    for prefix in prefixes:
        print(C.DIM + f"Scanning {prefix}.0/24 for Pi SSH..." + C.RESET)
        # Never treat this machine's own SSH server as the Pi.
        hits = [ip for ip in scan_subnet_for_ssh(prefix) if ip not in own]
        if not hits:
            continue
        if len(hits) == 1:
            return hits[0]

        print(C.YELLOW + "Multiple SSH hosts found:" + C.RESET)
        for idx, ip in enumerate(hits, start=1):
            print(f"  {idx}. {ip}")
        pick = input("Choose Pi IP number (or Enter to skip): ").strip()
        if pick.isdigit() and 1 <= int(pick) <= len(hits):
            return hits[int(pick) - 1]

    manual = input("Pi IP not auto-detected. Enter Pi IP manually: ").strip()
    if manual:
        return manual
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
    cached = cfg.get("last_pi_ip", "").strip()
    if cached and tcp_open(cached, 22, 0.3):
        return cached

    resolved = resolve_hostname_ip(cfg["pi_hostname"])
    if resolved and tcp_open(resolved, 22, 0.3):
        return resolved

    return None


def recalibrate_pi_sync(cfg: Dict[str, str], restart_agent: bool) -> bool:
    if not (shutil.which("ssh") or shutil.which("plink")):
        print(C.RED + "No SSH client found in PATH (need OpenSSH 'ssh' or PuTTY 'plink')." + C.RESET)
        return False

    local_ips = get_local_ipv4s()
    if not local_ips:
        print(C.RED + "No active local IPv4 found." + C.RESET)
        return False

    print(C.BLUE + f"Local IPv4 candidates: {', '.join(local_ips)}" + C.RESET)
    pi_ip = detect_pi_ip(cfg, local_ips)
    if not pi_ip:
        print(C.RED + "Could not determine Pi IP." + C.RESET)
        return False

    host_ip = resolve_host_ip(cfg, local_ips, pi_ip)
    if not host_ip:
        print(C.RED + "Could not determine the dashboard host IP." + C.RESET)
        return False
    port = cfg.get("server_port", "3000")

    if same_subnet_24(host_ip, pi_ip):
        print(C.GREEN + f"Subnet aligned: host={host_ip}, pi={pi_ip}" + C.RESET)
    else:
        print(C.YELLOW + f"Different /24 detected: host={host_ip}, pi={pi_ip}. Continuing anyway." + C.RESET)

    boot_sequence("NETWORK RECALIBRATION", [
        "Resolve active host network",
        "Find Pi endpoint",
        "Patch Pi server_url",
        "Commit sync profile",
    ])

    print(C.BLUE + "Updating local repo configs (source of truth):" + C.RESET)
    for note in apply_ip_locally(host_ip, port):
        print("  - " + note)

    # /opt/guardian-agent/config.json is root-owned on deployed Pi setups.
    if not remote_configure_and_start(cfg, pi_ip, cfg["pi_install_dir"], host_ip,
                                      port, cfg["pi_service"], restart_agent):
        print(C.RED + "Pi config/restart did not complete — see the cause above." + C.RESET)
        return False

    cfg["last_pi_ip"] = pi_ip
    cfg["last_host_ip"] = host_ip
    save_cfg(cfg)
    print(C.GREEN + f"Sync complete. Pi now points to http://{host_ip}:{port}" + C.RESET)
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
            print(C.RED + f"SSH to {cfg['pi_username']}@{pi_ip} failed (exit {rc}) — "
                  "wrong password, or the Pi is unreachable." + C.RESET)
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




def verify_pi_online(cfg: Dict[str, str], pi_ip: str, host_ip: str, port: str) -> bool:
    """Confirm the agent service is running and the Pi can see the dashboard."""
    svc = cfg["pi_service"]
    print(C.BLUE + "Verifying deployment..." + C.RESET)

    rc, out = ssh_capture(cfg, pi_ip, f"systemctl is-active {shlex.quote(svc)}", timeout=25)
    active = out.splitlines()[-1].strip() if out else ""
    svc_ok = (active == "active") or (rc == 0 and "inactive" not in out and "failed" not in out)
    print(f"  Pi service '{svc}': " +
          (C.GREEN + "active" + C.RESET if svc_ok else C.RED + f"NOT active ({active or 'unknown'})" + C.RESET))

    health = (f"curl -m 5 -s -o /dev/null -w '%{{http_code}}' "
              f"http://{host_ip}:{port}/api/health || echo down")
    _, out2 = ssh_capture(cfg, pi_ip, health, timeout=20)
    code = out2.splitlines()[-1].strip() if out2 else ""
    reach_ok = code.startswith("2")
    print(f"  Pi -> dashboard http://{host_ip}:{port}/api/health: " +
          ((C.GREEN + code + C.RESET) if reach_ok else (C.YELLOW + (code or "no response") + C.RESET)))

    dash_ok = tcp_open("127.0.0.1", int(port), 1.0)
    print(f"  Local dashboard port {port}: " +
          (C.GREEN + "listening" + C.RESET if dash_ok
           else C.YELLOW + "not listening (start it with option 1)" + C.RESET))

    # The Pi agent is what this function is verifying. A dashboard that happens
    # not to be running is a local matter and must not fail the deploy — that
    # false negative is why `GUARD.bat deploy` reported failure after a good push.
    ok = svc_ok
    if ok and not dash_ok:
        print(C.YELLOW + "Pi agent is up, but nothing is listening on port "
              f"{port} here — start the dashboard with option 1 or the Pi has "
              "nowhere to report to." + C.RESET)
    print((C.GREEN + "Verification PASSED." if ok
           else C.YELLOW + "Verification incomplete — review the lines above.") + C.RESET)
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

    print(C.RED + f"Unknown command: {cmd}" + C.RESET)
    print("Commands: deploy | verify | diag | build | ip <addr> [port] | status")
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

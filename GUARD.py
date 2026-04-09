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

DEFAULTS = {
    "pi_hostname": "aman-test",
    "pi_username": "pi",
    "last_pi_ip": "",
    "last_host_ip": "",
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
    print(f" {status_tag(STATUS['pi_agent'])} 3. Launch Pi Agent Sync")
    print(f" {status_tag(STATUS['recalibrated'])} 4. Recalibrate Network IP Sync")
    print(f" {status_tag(all_core_done())} 5. Run Full Init Sequence")
    print(f" {C.BLUE}[OPEN]{C.RESET} 6. Open Dashboard (localhost)")
    print(f" {C.BLUE}[INFO]{C.RESET} 7. Show Network Report")
    print(f" {C.BLUE}[CFG ]{C.RESET} 8. Update Pi Hostname/User")
    print(f" {C.YELLOW}[STOP]{C.RESET} 9. Close All Systems")
    print(f" {C.RED}[EXIT]{C.RESET} 0. Exit")
    print()
    print(C.DIM + f"Pi host: {cfg['pi_hostname']} | user: {cfg['pi_username']} | cached Pi IP: {cfg.get('last_pi_ip','-')}" + C.RESET)
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
    out = subprocess.check_output(["ipconfig"], text=True, errors="ignore")
    ips = re.findall(r"IPv4[^:]*:\s*([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", out)

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
    for prefix in prefixes:
        print(C.DIM + f"Scanning {prefix}.0/24 for Pi SSH..." + C.RESET)
        hits = scan_subnet_for_ssh(prefix)
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


def run_ssh(user: str, ip: str, remote_command: str, force_tty: bool = False) -> int:
    cmd = ["ssh"]
    if force_tty:
        cmd.append("-tt")
    cmd.extend([f"{user}@{ip}", remote_command])
    return subprocess.run(cmd).returncode


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
    if not shutil_which("ssh"):
        print(C.RED + "OpenSSH client not found in PATH." + C.RESET)
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

    host_ip = choose_host_ip(local_ips, pi_ip)
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

    py_code = (
        "import json; "
        "p='/opt/guardian-agent/config.json'; "
        "d=json.load(open(p,'r',encoding='utf-8')); "
        f"d['server_url']='http://{host_ip}:3000'; "
        "json.dump(d,open(p,'w',encoding='utf-8'),indent=4); "
        "print('server_url=',d.get('server_url',''))"
    )

    # /opt/guardian-agent/config.json is root-owned on deployed Pi setups.
    rc1 = run_ssh(cfg["pi_username"], pi_ip, f"sudo python3 -c {shlex.quote(py_code)}", force_tty=True)
    if rc1 != 0:
        print(C.RED + "Failed to update Pi config.json server_url (check SSH/sudo password)." + C.RESET)
        return False

    if restart_agent:
        rc2 = run_ssh(cfg["pi_username"], pi_ip, "sudo systemctl restart guardian-agent", force_tty=True)
        if rc2 != 0:
            print(C.RED + "Could not restart guardian-agent on Pi." + C.RESET)
            return False

    cfg["last_pi_ip"] = pi_ip
    cfg["last_host_ip"] = host_ip
    save_cfg(cfg)
    print(C.GREEN + f"Sync complete. Pi now points to http://{host_ip}:3000" + C.RESET)
    return True


def shutil_which(binary: str) -> bool:
    paths = os.environ.get("PATH", "").split(os.pathsep)
    candidates = [binary]
    if os.name == "nt" and not binary.lower().endswith(".exe"):
        candidates.append(binary + ".exe")
    for d in paths:
        for c in candidates:
            p = Path(d) / c
            if p.exists():
                return True
    return False


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
    ok = recalibrate_pi_sync(cfg, restart_agent=True)
    STATUS["pi_agent"] = ok


def action_recalibrate(cfg: Dict[str, str]) -> None:
    ok = recalibrate_pi_sync(cfg, restart_agent=True)
    STATUS["recalibrated"] = ok


def show_network_report(cfg: Dict[str, str]) -> None:
    local_ips = get_local_ipv4s()
    print(C.BLUE + "Network report:" + C.RESET)
    print(f"  Local IPv4: {', '.join(local_ips) if local_ips else '-'}")
    print(f"  Cached Pi IP: {cfg.get('last_pi_ip','-')}")
    print(f"  Last host IP used: {cfg.get('last_host_ip','-')}")
    resolved = resolve_hostname_ip(cfg["pi_hostname"])
    print(f"  Pi hostname resolve ({cfg['pi_hostname']}): {resolved or 'not resolved'}")


def run_full_sequence(cfg: Dict[str, str]) -> None:
    action_launch_dashboard()
    action_launch_windows_agent()
    action_launch_pi_agent(cfg)


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
    if shutil_which("ssh"):
        pi_ip = _quick_detect_pi_ip(cfg)
        if pi_ip:
            rc = run_ssh(cfg["pi_username"], pi_ip, "sudo systemctl stop guardian-agent", force_tty=True)
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


def main() -> None:
    enable_ansi()
    cfg = load_cfg()

    while True:
        clear_screen()
        draw_menu(cfg)
        choice = input(C.BOLD + "Select option: " + C.RESET).strip()
        print()

        if choice == "1":
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
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(0)

#!/usr/bin/env python3
"""Password-based SSH/SFTP helper for the Guardian Pi.

Windows OpenSSH cannot take a password on the command line and the box has no
sshpass/plink, so this uses paramiko. Credentials come from
windows-app/config/guard_runtime.json (pi_username / pi_password / last_pi_ip),
with optional overrides via GUARDIAN_PI_IP / GUARDIAN_PI_USER / GUARDIAN_PI_PASS.

Usage:
    python tools/pi_ssh.py run "<remote command>"      # stream output
    python tools/pi_ssh.py sudo "<remote command>"     # run with sudo -S
    python tools/pi_ssh.py put <local> <remote>
    python tools/pi_ssh.py get <remote> <local>
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "windows-app" / "config" / "guard_runtime.json"


def creds() -> tuple[str, str, str]:
    data = {}
    try:
        data = json.loads(CFG.read_text(encoding="utf-8"))
    except Exception:
        pass
    ip = os.environ.get("GUARDIAN_PI_IP") or data.get("last_pi_ip") or "192.168.1.15"
    user = os.environ.get("GUARDIAN_PI_USER") or data.get("pi_username") or "pi"
    pw = os.environ.get("GUARDIAN_PI_PASS") or data.get("pi_password") or ""
    return str(ip), str(user), str(pw)


def connect() -> paramiko.SSHClient:
    ip, user, pw = creds()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(ip, username=user, password=pw, timeout=15,
                   banner_timeout=20, auth_timeout=20, look_for_keys=False,
                   allow_agent=False)
    return client


def run(client: paramiko.SSHClient, command: str, timeout: int = 120,
        sudo: bool = False) -> int:
    _, _, pw = creds()
    if sudo:
        command = "sudo -S -p '' bash -lc " + shlex.quote(command)
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout,
                                                get_pty=False)
    if sudo and pw:
        stdin.write(pw + "\n")
        stdin.flush()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    rc = stdout.channel.recv_exit_status()
    if out:
        sys.stdout.write(out)
        if not out.endswith("\n"):
            sys.stdout.write("\n")
    if err.strip():
        sys.stdout.write("[stderr] " + err.strip() + "\n")
    return rc


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2

    mode = argv[0]
    client = connect()
    try:
        if mode == "run":
            return run(client, argv[1], timeout=int(os.environ.get("PI_TIMEOUT", "120")))
        if mode == "sudo":
            return run(client, argv[1], timeout=int(os.environ.get("PI_TIMEOUT", "300")), sudo=True)
        if mode in ("put", "get"):
            sftp = client.open_sftp()
            try:
                if mode == "put":
                    sftp.put(argv[1], argv[2])
                    print(f"uploaded {argv[1]} -> {argv[2]}")
                else:
                    sftp.get(argv[1], argv[2])
                    print(f"downloaded {argv[1]} -> {argv[2]}")
            finally:
                sftp.close()
            return 0
        print(f"unknown mode: {mode}")
        return 2
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

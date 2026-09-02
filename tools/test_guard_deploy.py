"""Offline dry-run of GUARD deploy orchestration.

The sandbox has no LAN route to the Pi, so this stubs the SSH/SCP layer and
asserts the deploy sequence, the commands issued, and the IP propagation.
Run: python3 tools/test_guard_deploy.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import GUARD  # noqa: E402

CALLS = []


def main() -> int:
    HOST, PI = "192.168.1.10", "192.168.1.15"

    GUARD.get_local_ipv4s = lambda: [HOST, "172.20.10.3"]
    GUARD.detect_pi_ip = lambda cfg, ips: PI
    GUARD.tcp_open = lambda ip, port, timeout=0.2: True
    GUARD.save_cfg = lambda cfg: None
    GUARD.boot_sequence = lambda name, steps: None

    def fake_exec(cfg, ip, cmd, tty=False, timeout=240):
        CALLS.append(("ssh", ip, cmd))
        return 0

    def fake_capture(cfg, ip, cmd, timeout=30):
        CALLS.append(("cap", ip, cmd))
        if "start.sh" in cmd:
            return 0, "INSTALLED"
        if "is-active" in cmd:
            return 0, "active"
        if "curl" in cmd:
            return 0, "200"
        return 0, ""

    def fake_scp(cfg, ip, paths, remote_dir, timeout=600):
        CALLS.append(("scp", ip, f"{len(paths)} files -> {remote_dir}"))
        return 0

    GUARD.ssh_exec = fake_exec
    GUARD.ssh_capture = fake_capture
    GUARD.scp_push = fake_scp

    cfg = dict(GUARD.DEFAULTS)
    ok = GUARD.deploy_pi_agent(cfg, restart=True)

    print("\n=== call sequence ===")
    for kind, ip, detail in CALLS:
        print(f"[{kind}] {ip}: {detail[:110]}")

    checks = {
        "deploy returned True": ok,
        "probed install state": any("start.sh" in c[2] for c in CALLS if c[0] == "cap"),
        "uploaded agent files": any(c[0] == "scp" and "/opt/guardian-agent/" in c[2] for c in CALLS),
        "patched server_url to host": any(f"http://{HOST}:3000" in c[2] for c in CALLS),
        "restarted service": any("systemctl restart guardian-agent" in c[2] for c in CALLS),
        "verified is-active": any("is-active" in c[2] for c in CALLS if c[0] == "cap"),
        "verified pi->dashboard": any("curl" in c[2] for c in CALLS if c[0] == "cap"),
        "cached pi ip": cfg["last_pi_ip"] == PI,
        "cached host ip": cfg["last_host_ip"] == HOST,
        "no config.json uploaded": not any(
            p.endswith("config.json") for p in GUARD._collect_agent_files()),
    }

    print("\n=== assertions ===")
    failed = 0
    for name, passed in checks.items():
        print(("  PASS  " if passed else "  FAIL  ") + name)
        failed += 0 if passed else 1
    print(("\nALL CHECKS PASSED" if not failed else f"\n{failed} CHECK(S) FAILED"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

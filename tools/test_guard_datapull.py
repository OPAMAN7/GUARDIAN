"""Offline checks for GUARD's SSH data-pull path.

Two things here can hurt, and neither shows up in a successful live run:

  1. The remote command. It is a string assembled from config values and handed to
     a shell running as root on the child's device. A `pkill` in it once killed the
     SSH session it was travelling on (BUG-23); a stray quote would send the sudo
     password to the wrong place. So most of this suite reads the generated command
     rather than running anything.

  2. The unpack. A tarball is a list of paths that a *remote* machine chose. One
     member named ../../GUARD.py would overwrite this repo. That is tested with a
     real malicious archive, built here.

No network and no SSH: subprocess and the ssh helpers are stubbed.
Run: python3 tools/test_guard_datapull.py
"""
import io
import os
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import GUARD  # noqa: E402

PASS = 0
FAIL = 0

CFG = {
    "pi_hostname": "aman-test",
    "pi_username": "pi",
    "pi_password": "s3cret pass",
    "pi_install_dir": "/opt/guardian-agent",
    "pi_service": "guardian-agent",
    "server_port": "3000",
    "last_pi_ip": "192.168.1.50",
}


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


def test_remote_command():
    print("\nThe remote command -- it runs as root on the child's device")
    cmd = GUARD._pi_export_cmd(CFG, "/tmp/guardian-export-X", "/tmp/guardian-export-X.tar.gz")

    # BUG-23: a bare pkill in an SSH chain killed the session carrying it. An
    # export has no business stopping anything at all.
    check("no pkill anywhere in the chain", "pkill" not in cmd, cmd[:200])
    check("no killall either", "killall" not in cmd)
    check("nothing is stopped or restarted",
          "systemctl stop" not in cmd and "systemctl restart" not in cmd, cmd[:200])
    check("the service is only ever queried",
          "is-active" in cmd and "is-enabled" in cmd)

    # An `rm -rf` on a path that came out empty would be `rm -rf /`.
    for frag in ("rm -rf ", "rm -f "):
        for piece in cmd.split(frag)[1:]:
            target = piece.split()[0].strip("'\"")
            check(f"'{frag.strip()}' target is inside /tmp ({target[:40]})",
                  target.startswith("/tmp/"), target)

    check("the staging directory is under /tmp", "/tmp/guardian-export-X" in cmd)
    check("staging is deleted after the tar", cmd.rindex("rm -rf") > cmd.index("tar -czf"))
    check("the archive is created from the parent directory",
          "tar -czf" in cmd and "-C /tmp" in cmd, cmd[cmd.index("tar -czf"):][:80])
    check("ownership is handed back to the SSH user", "chown -R pi:pi" in cmd)
    check("a success marker is echoed for the local side to read",
          "echo GUARDIAN_EXPORT=" in cmd and "echo GUARDIAN_BYTES=" in cmd)

    # Every copy is optional: a Pi that never ran the web collector has no
    # web.jsonl, and that is a normal export.
    copies = [ln for ln in cmd.split("; ") if ln.startswith("sudo -n cp")]
    check("every copy tolerates a missing source",
          copies and all("|| true" in c for c in copies), str(len(copies)))
    check("both the root and the user spool are collected",
          "/root/.guardian-shield/progress" in cmd and '"$HOME/.guardian-shield/progress' in cmd)
    check("$HOME is left unquoted so the Pi expands it",
          "'$HOME" not in cmd, "shlex.quote would have frozen it as a literal")
    check("the agent's own config comes along", "/opt/guardian-agent/config.json" in cmd)
    check("the journal is captured with a fallback for a sudo-less user",
          cmd.count("journalctl -u guardian-agent") == 2, cmd)
    check("a manifest is generated", "MANIFEST.txt" in cmd)


def test_no_quote_breakage():
    print("\nQuoting -- the sudo password travels in this string")
    # sudo_preamble contains single quotes. A bash -lc '...' wrapper would end at
    # the first of them and leak the password as an argument.
    cmd = GUARD.sudo_preamble(CFG) + "; " + GUARD._pi_export_cmd(CFG, "/tmp/e", "/tmp/e.tar.gz")
    check("the command is not wrapped in single-quoted bash -lc",
          "bash -lc '" not in cmd, cmd[:120])
    check("the password is quoted for the shell", "'s3cret pass'" in cmd, cmd[:120])
    check("quotes are balanced", cmd.count("'") % 2 == 0, str(cmd.count("'")))

    # A service or install path with a space must survive as one argument.
    spacey = dict(CFG, pi_service="my service", pi_install_dir="/opt/guardian agent")
    cmd2 = GUARD._pi_export_cmd(spacey, "/tmp/e", "/tmp/e.tar.gz")
    check("a service name with a space is quoted", "'my service'" in cmd2, cmd2[:200])
    check("an install path with a space is quoted", "'/opt/guardian agent/config.json'" in cmd2)
    check("quotes stay balanced with awkward config", cmd2.count("'") % 2 == 0)


def _make_tar(path, members):
    """members: list of (name, bytes) — or (name, None) for a directory."""
    with tarfile.open(path, "w:gz") as tf:
        for name, data in members:
            info = tarfile.TarInfo(name)
            if data is None:
                info.type = tarfile.DIRTYPE
                tf.addfile(info)
            else:
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))


def test_extract_is_contained():
    print("\nUnpacking -- the archive's paths were chosen by another machine")
    tmp = Path(tempfile.mkdtemp(prefix="guard-tar-"))
    good = tmp / "good.tar.gz"
    _make_tar(good, [
        ("guardian-export-X/progress/usage.jsonl", b'{"a":1}\n{"a":2}\n'),
        ("guardian-export-X/logs/agent.log", b"hello\n"),
        ("guardian-export-X/MANIFEST.txt", b"15\tprogress/usage.jsonl\n"),
    ])
    dest = tmp / "out"
    written, rejected = GUARD._safe_extract(good, dest)
    check("a normal archive unpacks", written == 3 and rejected == 0, f"{written}/{rejected}")
    check("the files are really there",
          (dest / "guardian-export-X" / "progress" / "usage.jsonl").exists())

    evil = tmp / "evil.tar.gz"
    _make_tar(evil, [
        ("../../escaped.txt", b"pwned"),
        ("/absolute.txt", b"pwned"),
        ("guardian-export-X/fine.txt", b"ok"),
    ])
    dest2 = tmp / "out2"
    written2, rejected2 = GUARD._safe_extract(evil, dest2)
    check("path-traversal members are refused", rejected2 >= 1, str(rejected2))
    check("the harmless member still lands", (dest2 / "guardian-export-X" / "fine.txt").exists())
    check("nothing was written outside the destination",
          not (tmp / "escaped.txt").exists() and not (tmp.parent / "escaped.txt").exists())
    check("the refusal is counted, not swallowed", written2 + rejected2 == 3,
          f"{written2}+{rejected2}")

    # A symlink in a tar is a way to make a later member write anywhere on disk.
    link = tmp / "link.tar.gz"
    with tarfile.open(link, "w:gz") as tf:
        info = tarfile.TarInfo("guardian-export-X/evil-link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tf.addfile(info)
    _w, rejected3 = GUARD._safe_extract(link, tmp / "out3")
    check("a symlink member is refused", rejected3 == 1, str(rejected3))
    check("no symlink was created", not (tmp / "out3" / "guardian-export-X" / "evil-link").exists())


def test_summary_counts_without_reading():
    print("\nThe printed summary -- counts, never contents")
    tmp = Path(tempfile.mkdtemp(prefix="guard-sum-"))
    (tmp / "progress").mkdir()
    (tmp / "logs").mkdir()
    secret = "https://reddit.com/r/teenagers/comments/abc/private_thread"
    (tmp / "progress" / "web.jsonl").write_text(
        "\n".join(f'{{"url":"{secret}","n":{i}}}' for i in range(5)) + "\n",
        encoding="utf-8",
    )
    (tmp / "progress" / "history_state.json").write_text("{}", encoding="utf-8")
    (tmp / "logs" / "agent.log").write_text("x" * 3000, encoding="utf-8")

    lines = GUARD._export_summary(tmp)
    text = "\n".join(lines)
    check("the spool is reported", "progress spool (root)" in text, text)
    check("jsonl rows are counted", "5 queued record(s)" in text, text)
    check("the log folder is reported", "agent logs (root)" in text, text)
    check("no record content is printed", secret not in text, text)
    check("absent folders are simply not listed", "progress spool (user)" not in text, text)
    check("a folder with no files is not listed",
          (lambda: ((tmp / "empty").mkdir(exist_ok=True),
                    "empty" not in "\n".join(GUARD._export_summary(tmp)))[1])())


def test_pull_orchestration():
    print("\nThe pull itself -- with SSH and scp stubbed out")
    tmp = Path(tempfile.mkdtemp(prefix="guard-pull-"))
    GUARD.DATA_EXPORT_DIR = tmp / "data-exports"

    calls = {"capture": [], "exec": [], "pull": []}

    def fake_capture(cfg, ip, command, timeout=30):
        calls["capture"].append(command)
        return 0, "GUARDIAN_EXPORT=ok\nGUARDIAN_BYTES=2048\n"

    def fake_exec(cfg, ip, command, tty=False, timeout=240):
        calls["exec"].append(command)
        return 0

    def fake_pull(cfg, ip, remote_paths, local_dir, timeout=600):
        calls["pull"].append((remote_paths, local_dir))
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        name = os.path.basename(remote_paths[0])
        stem = name[: -len(".tar.gz")]
        _make_tar(Path(local_dir) / name, [
            (f"{stem}/progress/usage.jsonl", b'{"app":"code"}\n'),
            (f"{stem}/service/state.txt", b"active\nenabled\n"),
            (f"{stem}/MANIFEST.txt", b"15\tprogress/usage.jsonl\n"),
        ])
        return 0

    saved = (GUARD.ssh_capture, GUARD.ssh_exec, GUARD.scp_pull, GUARD._unattended_ssh)
    GUARD.ssh_capture = fake_capture
    GUARD.ssh_exec = fake_exec
    GUARD.scp_pull = fake_pull
    GUARD._unattended_ssh = lambda cfg: True
    try:
        folder = GUARD.pull_pi_data(dict(CFG), "192.168.1.50")
    finally:
        (GUARD.ssh_capture, GUARD.ssh_exec, GUARD.scp_pull, GUARD._unattended_ssh) = saved

    check("the pull reports a folder", folder is not None, str(folder))
    if folder:
        check("the export folder is timestamped, one per run",
              folder.parent.name == "data-exports" and len(folder.name) == 17, folder.name)
        check("the tar's own top folder is flattened away",
              (folder / "progress" / "usage.jsonl").exists(),
              str(sorted(p.name for p in folder.iterdir())))
        check("the manifest is at the top level", (folder / "MANIFEST.txt").exists())
        check("the incoming tarball is cleaned up",
              not any(p.name.startswith(".incoming-") for p in folder.parent.iterdir()),
              str(sorted(p.name for p in folder.parent.iterdir())))
    check("exactly one archive was fetched", len(calls["pull"]) == 1, str(calls["pull"]))
    check("the archive path is the one the remote command built",
          calls["pull"] and calls["pull"][0][0][0].startswith("/tmp/guardian-export-"),
          str(calls["pull"]))
    check("the remote tarball is deleted afterwards",
          any(c.startswith("rm -f /tmp/guardian-export-") for c in calls["exec"]),
          str(calls["exec"]))
    check("the cleanup removes only that one file",
          all("-rf" not in c for c in calls["exec"] if c.startswith("rm ")), str(calls["exec"]))


def test_failure_is_reported_not_swallowed():
    print("\nFailure paths -- a nightly archive must never be quietly empty")
    tmp = Path(tempfile.mkdtemp(prefix="guard-fail-"))
    GUARD.DATA_EXPORT_DIR = tmp / "data-exports"
    saved = (GUARD.ssh_capture, GUARD.ssh_exec, GUARD.scp_pull, GUARD._unattended_ssh)
    GUARD._unattended_ssh = lambda cfg: True
    GUARD.ssh_exec = lambda *a, **k: 0
    try:
        GUARD.ssh_capture = lambda *a, **k: (0, "GUARDIAN_EXPORT=empty\nGUARDIAN_BYTES=0\n")
        GUARD.scp_pull = lambda *a, **k: 0
        check("an empty remote archive is a failure, not an empty folder",
              GUARD.pull_pi_data(dict(CFG), "1.2.3.4") is None)

        GUARD.ssh_capture = lambda *a, **k: (255, "Permission denied (publickey).")
        check("an SSH refusal is a failure", GUARD.pull_pi_data(dict(CFG), "1.2.3.4") is None)

        GUARD.ssh_capture = lambda *a, **k: (0, "GUARDIAN_EXPORT=ok\nGUARDIAN_BYTES=2048\n")
        GUARD.scp_pull = lambda *a, **k: 1
        check("a failed download is a failure", GUARD.pull_pi_data(dict(CFG), "1.2.3.4") is None)

        def pull_nothing(cfg, ip, remote_paths, local_dir, timeout=600):
            Path(local_dir).mkdir(parents=True, exist_ok=True)
            return 0
        GUARD.scp_pull = pull_nothing
        check("scp claiming success with no file is still a failure",
              GUARD.pull_pi_data(dict(CFG), "1.2.3.4") is None)

        def pull_garbage(cfg, ip, remote_paths, local_dir, timeout=600):
            Path(local_dir).mkdir(parents=True, exist_ok=True)
            (Path(local_dir) / os.path.basename(remote_paths[0])).write_bytes(b"not a tarball")
            return 0
        GUARD.scp_pull = pull_garbage
        check("a corrupt archive is reported instead of crashing",
              GUARD.pull_pi_data(dict(CFG), "1.2.3.4") is None)
    finally:
        (GUARD.ssh_capture, GUARD.ssh_exec, GUARD.scp_pull, GUARD._unattended_ssh) = saved


def test_wiring():
    print("\nWiring -- an option nobody can reach is not a feature")
    root = Path(__file__).resolve().parents[1]
    source = (root / "GUARD.py").read_text(encoding="utf-8")
    check("the menu lists the pull option", "P. Pull Device Data via SSH" in source)
    check("the menu dispatches 'p'",
          'choice.lower() == "p"' in source and "action_pull_data(cfg)" in source)
    check("the CLI accepts several obvious names",
          '"datapull", "pull", "fetch", "export"' in source)
    check("the CLI help mentions it", "datapull" in source.split("Commands: ")[1])
    check("the CLI returns non-zero when nothing is pulled",
          "return 0 if pull_pi_data(cfg, pi_ip) else 1" in source)

    bat = root / "TRANSFER-DATA.bat"
    check("TRANSFER-DATA.bat exists", bat.exists())
    text = bat.read_text(encoding="utf-8") if bat.exists() else ""
    check("it calls the datapull command", "python GUARD.py datapull" in text)
    check("it forwards its exit code", "exit /b %RC%" in text)
    check("it checks for Python first", "where python" in text)
    check("it does not pause when given arguments (schedulable)",
          'if "%~1"=="" (' in text, "an unconditional pause would hang a task")
    check("GUARD.bat documents the command", "GUARD.bat datapull" in
          (root / "GUARD.bat").read_text(encoding="utf-8"))

    ignore = (root / ".gitignore").read_text(encoding="utf-8")
    check("data-exports is gitignored -- these are the child's real records",
          "data-exports/" in ignore, ignore)
    check("a stray export tarball is gitignored too",
          "guardian-export-*.tar.gz" in ignore)

    check("no runtime-config key was added for this",
          "data_export" not in source.split("DEFAULTS = {")[1].split("}")[0],
          "load_cfg() drops any guard_runtime.json key missing from DEFAULTS")


def main():
    print("=" * 66)
    print("GUARD SSH data pull -- offline suite")
    print("=" * 66)
    test_remote_command()
    test_no_quote_breakage()
    test_extract_is_contained()
    test_summary_counts_without_reading()
    test_pull_orchestration()
    test_failure_is_reported_not_swallowed()
    test_wiring()
    print("\n" + "=" * 66)
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

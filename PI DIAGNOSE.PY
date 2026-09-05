import paramiko
import time

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('192.168.1.3', username='pi', password='1234', timeout=10)

def run(cmd):
    _, o, e = ssh.exec_command(cmd, timeout=10)
    out = o.read().decode('utf-8', errors='ignore')
    err = e.read().decode('utf-8', errors='ignore')
    return (out + err).strip()

print("=== AGENT LOG (last 60 lines) ===")
print(run("cat /home/pi/.guardian-shield/logs/agent_20260826.log 2>/dev/null | tail -60 || journalctl -u guardian-agent -n 50 --no-pager 2>/dev/null | tail -50"))

print("\n=== ACTIVE PROCESSES ON PI ===")
print(run("ps aux | grep -i guardian | grep -v grep"))

print("\n=== XDOTOOL CHECK ===")
print(run("which xdotool && echo FOUND || echo NOT_FOUND"))
print(run("which wmctrl && echo FOUND || echo NOT_FOUND"))
print(run("which xprop && echo FOUND || echo NOT_FOUND"))

print("\n=== TKINTER CHECK ===")
print(run("python3 -c 'import tkinter; print(\"tkinter OK:\", tkinter.TkVersion)' 2>&1"))

print("\n=== DISPLAY ENV FROM PID ===")
pid = run("systemctl show -p MainPID --value guardian-agent").strip()
print("Agent PID:", pid)
if pid and pid != "0":
    cmd = 'cat /proc/' + pid + '/environ 2>/dev/null | tr "\\0" "\\n" | grep -E "DISPLAY|WAYLAND|XDG|DBUS"'
    print(run(cmd))

print("\n=== DESKTOP ENV ===")
print(run("echo DISPLAY=$DISPLAY; echo WAYLAND=$WAYLAND_DISPLAY; who"))
print(run("ls /run/user/ 2>/dev/null"))
print(run("ls /run/user/1000/ 2>/dev/null | head -10"))

print("\n=== SERVER CONNECTIVITY FROM PI ===")
print(run("python3 -c 'import urllib.request; r=urllib.request.urlopen(\"http://192.168.1.10:3000/api/health\", timeout=3); print(r.read().decode()[:500])' 2>&1"))

print("\n=== PI CONFIG.JSON ===")
print(run("cat /home/pi/guardian-shield/pi-agent/agent/config.json"))

ssh.close()
print("\n=== DONE ===")


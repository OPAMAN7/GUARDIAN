"""
Guardian Shield — Persistence Module
Registers the agent as a system service for auto-start on boot.
- Windows: Windows Service via pywin32
- Ubuntu: systemd unit file
Cannot be easily terminated by non-admin users.
"""

import os
import sys
import platform
import logging
import subprocess
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger("guardian.persistence")

SERVICE_NAME = "GuardianShield"
SERVICE_DISPLAY = "Guardian Shield Monitoring Service"
SERVICE_DESC = "Guardian Shield — Screen monitoring and protection service"

SYSTEMD_UNIT = f"""[Unit]
Description={SERVICE_DESC}
After=network.target graphical.target
Wants=graphical.target

[Service]
Type=simple
ExecStart={{python_path}} {{agent_path}}
Restart=always
RestartSec=3
User=root
Environment=DISPLAY=:0
Environment=XAUTHORITY=/run/user/1000/.Xauthority
WorkingDirectory={{agent_dir}}
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""

WATCHDOG_SYSTEMD_UNIT = f"""[Unit]
Description=Guardian Shield Watchdog
After={SERVICE_NAME}.service
BindsTo={SERVICE_NAME}.service

[Service]
Type=simple
ExecStart={{python_path}} {{watchdog_path}}
Restart=always
RestartSec=2
User=root
WorkingDirectory={{agent_dir}}
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""


def install_windows_service():
    """
    Install the agent as a Windows Service.
    Requires Administrator privileges and pywin32.
    """
    if platform.system() != "Windows":
        logger.error("Windows service installation only works on Windows")
        return False

    try:
        import win32serviceutil
        import win32service
        import servicemanager
    except ImportError:
        logger.error(
            "pywin32 is required for Windows service installation. "
            "Install it with: pip install pywin32"
        )
        return False

    # Create the Windows service wrapper script
    agent_dir = Path(__file__).parent
    service_script = agent_dir / "win_service.py"

    service_code = '''"""Guardian Shield Windows Service Wrapper"""
import sys
import os
import time
import logging
from pathlib import Path

import win32serviceutil
import win32service
import win32event
import servicemanager

# Add agent directory to path
agent_dir = str(Path(__file__).parent)
if agent_dir not in sys.path:
    sys.path.insert(0, agent_dir)


class GuardianShieldService(win32serviceutil.ServiceFramework):
    _svc_name_ = "''' + SERVICE_NAME + '''"
    _svc_display_name_ = "''' + SERVICE_DISPLAY + '''"
    _svc_description_ = "''' + SERVICE_DESC + '''"

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
        self.running = True

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.hWaitStop)
        self.running = False

    def SvcDoRun(self):
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, "")
        )
        self.main()

    def main(self):
        from main import run_agent
        try:
            run_agent(as_service=True, stop_event=self.hWaitStop)
        except Exception as e:
            servicemanager.LogErrorMsg(f"Guardian Shield error: {e}")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(GuardianShieldService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(GuardianShieldService)
'''
    service_script.write_text(service_code, encoding="utf-8")
    logger.info(f"Created Windows service wrapper: {service_script}")

    # Install the service
    try:
        python_exe = sys.executable
        subprocess.run(
            [python_exe, str(service_script), "install"],
            check=True, capture_output=True, text=True,
        )
        logger.info("Windows service installed successfully")

        # Set to auto-start
        subprocess.run(
            [python_exe, str(service_script), "update", "--startup", "auto"],
            check=True, capture_output=True, text=True,
        )
        logger.info("Windows service set to auto-start")

        # Configure service recovery (restart on failure)
        subprocess.run(
            ["sc", "failure", SERVICE_NAME,
             "reset=", "86400",
             "actions=", "restart/3000/restart/3000/restart/5000"],
            check=True, capture_output=True, text=True,
        )
        logger.info("Windows service recovery configured")

        # Start the service
        subprocess.run(
            [python_exe, str(service_script), "start"],
            check=True, capture_output=True, text=True,
        )
        logger.info("Windows service started")

        return True

    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to install Windows service: {e.stderr}")
        return False


def install_linux_service():
    """
    Install the agent as a systemd service on Linux.
    Requires root privileges.
    """
    if platform.system() != "Linux":
        logger.error("Systemd service installation only works on Linux")
        return False

    if os.geteuid() != 0:
        logger.error("Root privileges required. Run with sudo.")
        return False

    agent_dir = Path(__file__).parent
    python_path = sys.executable
    agent_path = agent_dir / "main.py"
    watchdog_path = agent_dir / "watchdog.py"

    # Write main agent service
    unit_content = SYSTEMD_UNIT.format(
        python_path=python_path,
        agent_path=agent_path,
        agent_dir=agent_dir,
    )
    unit_file = Path(f"/etc/systemd/system/{SERVICE_NAME.lower()}.service")
    unit_file.write_text(unit_content, encoding="utf-8")
    logger.info(f"Created systemd unit: {unit_file}")

    # Write watchdog service
    wd_unit_content = WATCHDOG_SYSTEMD_UNIT.format(
        python_path=python_path,
        watchdog_path=watchdog_path,
        agent_dir=agent_dir,
    )
    wd_unit_file = Path(f"/etc/systemd/system/{SERVICE_NAME.lower()}-watchdog.service")
    wd_unit_file.write_text(wd_unit_content, encoding="utf-8")
    logger.info(f"Created watchdog systemd unit: {wd_unit_file}")

    # Reload systemd, enable, and start
    try:
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", f"{SERVICE_NAME.lower()}.service"], check=True)
        subprocess.run(["systemctl", "enable", f"{SERVICE_NAME.lower()}-watchdog.service"], check=True)
        subprocess.run(["systemctl", "start", f"{SERVICE_NAME.lower()}.service"], check=True)
        subprocess.run(["systemctl", "start", f"{SERVICE_NAME.lower()}-watchdog.service"], check=True)
        logger.info("Systemd services installed, enabled, and started")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to configure systemd: {e}")
        return False


def uninstall_linux_service():
    """Remove the systemd services."""
    if os.geteuid() != 0:
        logger.error("Root privileges required.")
        return False

    try:
        for svc in [f"{SERVICE_NAME.lower()}-watchdog", SERVICE_NAME.lower()]:
            subprocess.run(["systemctl", "stop", f"{svc}.service"], capture_output=True)
            subprocess.run(["systemctl", "disable", f"{svc}.service"], capture_output=True)
            unit_file = Path(f"/etc/systemd/system/{svc}.service")
            if unit_file.exists():
                unit_file.unlink()
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        logger.info("Systemd services uninstalled")
        return True
    except Exception as e:
        logger.error(f"Failed to uninstall: {e}")
        return False


def uninstall_windows_service():
    """Remove the Windows service."""
    agent_dir = Path(__file__).parent
    service_script = agent_dir / "win_service.py"

    try:
        python_exe = sys.executable
        subprocess.run(
            [python_exe, str(service_script), "stop"],
            capture_output=True, text=True,
        )
        subprocess.run(
            [python_exe, str(service_script), "remove"],
            check=True, capture_output=True, text=True,
        )
        logger.info("Windows service uninstalled")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to uninstall: {e.stderr}")
        return False


def install():
    """Install the agent as a system service (auto-detects OS)."""
    if platform.system() == "Windows":
        return install_windows_service()
    elif platform.system() == "Linux":
        return install_linux_service()
    else:
        logger.error(f"Unsupported platform: {platform.system()}")
        return False


def uninstall():
    """Uninstall the agent service (auto-detects OS)."""
    if platform.system() == "Windows":
        return uninstall_windows_service()
    elif platform.system() == "Linux":
        return uninstall_linux_service()
    else:
        logger.error(f"Unsupported platform: {platform.system()}")
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "install":
            install()
        elif cmd == "uninstall":
            uninstall()
        else:
            print(f"Usage: {sys.argv[0]} [install|uninstall]")
    else:
        print(f"Usage: {sys.argv[0]} [install|uninstall]")

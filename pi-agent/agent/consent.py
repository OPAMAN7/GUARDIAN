"""
Guardian Shield — Stealth Consent Module
Shows a consent dialog to both the monitored user AND the admin (parent/boss)
before enabling stealth monitoring mode.
Both parties must agree for stealth mode to activate.
Uses a simple tkinter dialog on Windows.
"""

import os
import sys
import json
import logging
import platform
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional

logger = logging.getLogger("guardian.consent")

CONSENT_DIR = Path.home() / ".guardian-shield" / "consent"
CONSENT_FILE = CONSENT_DIR / "consent_record.json"


def _get_consent_record() -> dict:
    """Load existing consent record."""
    CONSENT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if CONSENT_FILE.exists():
            return json.loads(CONSENT_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_consent_record(record: dict):
    """Save consent record to disk."""
    CONSENT_DIR.mkdir(parents=True, exist_ok=True)
    CONSENT_FILE.write_text(json.dumps(record, indent=2), encoding="utf-8")


def show_consent_dialog(is_admin: bool = False) -> bool:
    """
    Show a consent dialog using tkinter.
    
    Args:
        is_admin: If True, shows the admin (parent/boss) version of the dialog.
    
    Returns:
        True if the user consents.
    """
    result = {"consented": False}
    
    try:
        import tkinter as tk
        from tkinter import messagebox
        
        root = tk.Tk()
        root.withdraw()  # Hide main window
        
        if is_admin:
            title = "Guardian Shield — Admin Consent"
            message = (
                "GUARDIAN SHIELD — STEALTH MODE CONSENT\n\n"
                "You are about to enable STEALTH monitoring mode.\n\n"
                "This will:\n"
                "• Hide all visible indicators of monitoring\n"
                "• Run the agent without a system tray icon\n"
                "• Continue to capture screenshots and block sites\n\n"
                "⚠️ LEGAL NOTICE:\n"
                "You confirm that you own this device or have explicit\n"
                "written consent from the device user to enable stealth\n"
                "monitoring. Unauthorized stealth surveillance may\n"
                "violate privacy laws in your jurisdiction.\n\n"
                "Do you consent to enable stealth mode?"
            )
        else:
            title = "Guardian Shield — Monitoring Notice"
            message = (
                "GUARDIAN SHIELD — MONITORING DISCLOSURE\n\n"
                "A monitoring application is being installed on this device.\n\n"
                "This application will:\n"
                "• Capture periodic screenshots of your screen\n"
                "• Block access to certain websites\n"
                "• Scan downloaded files for malware\n"
                "• Report activity to an administrator\n\n"
                "The administrator (parent/employer) has requested\n"
                "that monitoring run in STEALTH mode (hidden).\n\n"
                "Your consent is required to proceed.\n"
                "If you do not consent, monitoring will run in VISIBLE mode\n"
                "with a system tray indicator.\n\n"
                "Do you consent to stealth monitoring?"
            )
        
        result["consented"] = messagebox.askyesno(title, message)
        root.destroy()
        
    except ImportError:
        # tkinter not available — fall back to console
        logger.warning("tkinter not available, using console consent")
        print("\n" + "=" * 55)
        if is_admin:
            print("  GUARDIAN SHIELD — ADMIN STEALTH CONSENT")
            print("=" * 55)
            print("  You are enabling STEALTH monitoring mode.")
            print("  Confirm you own this device or have user consent.")
        else:
            print("  GUARDIAN SHIELD — USER MONITORING NOTICE")
            print("=" * 55)
            print("  A monitoring app will run on this device.")
            print("  The admin requests stealth (hidden) mode.")
        print("=" * 55)
        answer = input("\n  Do you consent? (yes/no): ").strip().lower()
        result["consented"] = answer in ("yes", "y")
    
    except Exception as e:
        logger.error(f"Consent dialog failed: {e}")
        # Default to non-stealth (visible mode) if dialog fails
        result["consented"] = False
    
    return result["consented"]


def check_stealth_consent() -> bool:
    """
    Check if stealth mode has been properly consented to.
    
    Returns:
        True if both admin and user have consented to stealth mode.
    """
    record = _get_consent_record()
    return (
        record.get("admin_consented", False) and
        record.get("user_consented", False) and
        record.get("stealth_enabled", False)
    )


def request_stealth_consent(is_admin_setup: bool = False) -> bool:
    """
    Full consent workflow for stealth mode.
    Must be called during initial setup.
    
    Args:
        is_admin_setup: True if the admin is setting up stealth mode.
    
    Returns:
        True if stealth mode was fully consented.
    """
    record = _get_consent_record()
    
    if is_admin_setup:
        # Admin (parent/boss) consent
        admin_consent = show_consent_dialog(is_admin=True)
        record["admin_consented"] = admin_consent
        record["admin_consent_time"] = datetime.now().isoformat()
        record["admin_hostname"] = platform.node()
        
        if admin_consent:
            logger.info("Admin consented to stealth mode")
            print("\n✅ Admin consent recorded.")
            print("Now, the device USER must also consent.")
            print("Run this tool on the target device with --user-consent flag.\n")
        else:
            logger.info("Admin declined stealth mode")
            record["stealth_enabled"] = False
            print("\n❌ Stealth mode NOT enabled. Agent will run in visible mode.")
        
        _save_consent_record(record)
        return admin_consent

    else:
        # User (child/employee) consent
        if not record.get("admin_consented", False):
            print("\n⚠️  Admin has not consented yet. Run with --admin-consent first.\n")
            return False
        
        user_consent = show_consent_dialog(is_admin=False)
        record["user_consented"] = user_consent
        record["user_consent_time"] = datetime.now().isoformat()
        
        if user_consent:
            record["stealth_enabled"] = True
            logger.info("User consented to stealth mode")
            print("\n✅ Both parties consented. Stealth mode ENABLED.")
        else:
            record["stealth_enabled"] = False
            logger.info("User declined stealth mode — running in visible mode")
            print("\n📺 User declined stealth. Agent will run in VISIBLE mode.")
        
        _save_consent_record(record)
        return user_consent


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    if "--admin-consent" in sys.argv:
        request_stealth_consent(is_admin_setup=True)
    elif "--user-consent" in sys.argv:
        request_stealth_consent(is_admin_setup=False)
    elif "--check" in sys.argv:
        stealth = check_stealth_consent()
        print(f"Stealth mode: {'ENABLED' if stealth else 'DISABLED'}")
        record = _get_consent_record()
        print(json.dumps(record, indent=2))
    else:
        print("Usage:")
        print(f"  {sys.argv[0]} --admin-consent   # Admin approves stealth")
        print(f"  {sys.argv[0]} --user-consent    # User acknowledges stealth")
        print(f"  {sys.argv[0]} --check           # Check consent status")

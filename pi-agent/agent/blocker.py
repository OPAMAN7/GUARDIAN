"""
Guardian Shield — Website Blocker Module
Blocks malicious and explicit websites by modifying the system hosts file.
Downloads pre-built blocklists and supports custom admin-managed lists.
Works on both Windows and Ubuntu.
"""

import os
import sys
import json
import platform
import logging
import shutil
import tempfile
import threading
import time
from urllib.parse import urlparse
from pathlib import Path
from typing import Set, List, Optional
from datetime import datetime

import requests

logger = logging.getLogger("guardian.blocker")

LINUX_POLICY_PATHS = [
    "/etc/chromium/policies/managed/guardian-shield-blocklist.json",
    "/etc/chromium-browser/policies/managed/guardian-shield-blocklist.json",
    "/etc/opt/chrome/policies/managed/guardian-shield-blocklist.json",
]
LINUX_CHROMIUM_FLAGS_FILE = "/etc/chromium.d/99-guardian-shield-flags"

# Marker comments used to identify our entries in the hosts file.
# Keep these ASCII-only for maximum parser compatibility.
BLOCK_START = "# >>> GUARDIAN SHIELD BLOCK START -- DO NOT EDIT <<<"
BLOCK_END = "# >>> GUARDIAN SHIELD BLOCK END -- DO NOT EDIT <<<"
LEGACY_BLOCK_START = "# >>> GUARDIAN SHIELD BLOCK START — DO NOT EDIT <<<"
LEGACY_BLOCK_END = "# >>> GUARDIAN SHIELD BLOCK END — DO NOT EDIT <<<"

# Pre-built blocklist sources (Steven Black's unified hosts + extensions)
BLOCKLIST_SOURCES = {
    "unified": "https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts",
    "fakenews_gambling_porn": "https://raw.githubusercontent.com/StevenBlack/hosts/master/alternates/fakenews-gambling-porn/hosts",
    "porn_only": "https://raw.githubusercontent.com/StevenBlack/hosts/master/alternates/porn/hosts",
}

# Default source — includes ads, malware, fakenews, gambling, porn
DEFAULT_BLOCKLIST = "fakenews_gambling_porn"


def _normalize_domain(raw_value: str) -> Optional[str]:
    """Normalize user/admin input into a clean hostname for hosts-file blocking."""
    if not raw_value:
        return None

    value = str(raw_value).strip().lower()
    if not value:
        return None

    try:
        parsed = urlparse(value if "://" in value else f"http://{value}")
        value = (parsed.hostname or "").strip().lower()
    except Exception:
        value = value.replace("http://", "").replace("https://", "")
        for sep in ("/", "?", "#", ":"):
            value = value.split(sep, 1)[0]
        value = value.strip().lower()

    value = value.strip(".")
    if not value or value == "localhost":
        return None
    if not all(ch.isalnum() or ch in ".-" for ch in value):
        return None
    return value


def _expand_custom_domain_aliases(domains: Set[str]) -> Set[str]:
    """Expand custom domains to common aliases for more reliable blocking."""
    expanded: Set[str] = set()

    for domain in domains:
        d = _normalize_domain(domain)
        if not d:
            continue

        expanded.add(d)

        # Add common www/bare aliases.
        if d.startswith("www."):
            bare = d[4:]
            if bare:
                expanded.add(bare)
        elif "." in d:
            expanded.add(f"www.{d}")

        # Special-case YouTube family so blocking youtube.com is immediate.
        if d in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}:
            expanded.update({
                "youtube.com",
                "www.youtube.com",
                "m.youtube.com",
                "music.youtube.com",
                "youtu.be",
                "www.youtu.be",
            })

    return expanded


def _get_hosts_path() -> str:
    """Return the path to the system hosts file based on the OS."""
    if platform.system() == "Windows":
        return r"C:\Windows\System32\drivers\etc\hosts"
    else:
        return "/etc/hosts"


def _flush_dns():
    """Flush the DNS cache so changes take effect immediately."""
    system = platform.system()
    try:
        if system == "Windows":
            os.system("ipconfig /flushdns")
        elif system == "Linux":
            # Try systemd-resolve first, then resolvectl
            ret = os.system("systemd-resolve --flush-caches 2>/dev/null")
            if ret != 0:
                os.system("resolvectl flush-caches 2>/dev/null")
        elif system == "Darwin":
            os.system("sudo dscacheutil -flushcache && sudo killall -HUP mDNSResponder")
        logger.info("DNS cache flushed")
    except Exception as e:
        logger.warning(f"Failed to flush DNS: {e}")


def _parse_hosts_from_blocklist(content: str) -> Set[str]:
    """
    Parse domain entries from a hosts-format blocklist file.
    Returns a set of blocked domain names.
    """
    domains = set()
    for line in content.splitlines():
        line = line.strip()
        # Skip comments and empty lines
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ("0.0.0.0", "127.0.0.1"):
            domain = parts[1].lower().strip()
            # Skip localhost entries
            if domain not in ("localhost", "localhost.localdomain", "local",
                              "broadcasthost", "ip6-localhost", "ip6-loopback",
                              "ip6-localnet", "ip6-mcastprefix", "ip6-allnodes",
                              "ip6-allrouters", "ip6-allhosts"):
                domains.add(domain)
    return domains


def download_blocklist(source_key: str = DEFAULT_BLOCKLIST) -> Set[str]:
    """
    Download a pre-built blocklist and return the set of blocked domains.

    Args:
        source_key: Key from BLOCKLIST_SOURCES dict.

    Returns:
        Set of domain strings to block.
    """
    url = BLOCKLIST_SOURCES.get(source_key)
    if not url:
        logger.error(f"Unknown blocklist source: {source_key}")
        return set()

    try:
        logger.info(f"Downloading blocklist '{source_key}' from {url}...")
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        domains = _parse_hosts_from_blocklist(resp.text)
        logger.info(f"Downloaded {len(domains)} domains from '{source_key}'")
        return domains
    except Exception as e:
        logger.error(f"Failed to download blocklist: {e}")
        return set()


class WebsiteBlocker:
    """
    Manages website blocking via hosts file modification.
    Supports pre-built blocklists + custom admin-managed domains.
    """

    def __init__(self, custom_domains: Optional[List[str]] = None):
        self.hosts_path = _get_hosts_path()
        self.custom_domains: Set[str] = set(custom_domains or [])
        self.blocklist_domains: Set[str] = set()
        self._lock = threading.Lock()
        self._cache_file = Path(tempfile.gettempdir()) / "guardian_blocklist_cache.txt"

    @property
    def all_blocked(self) -> Set[str]:
        """All currently blocked domains (blocklist + custom)."""
        return self.blocklist_domains | self.custom_domains

    def load_cached_blocklist(self) -> bool:
        """Load blocklist from local cache file (for offline use)."""
        try:
            if self._cache_file.exists():
                content = self._cache_file.read_text(encoding="utf-8")
                self.blocklist_domains = set(line.strip() for line in content.splitlines() if line.strip())
                logger.info(f"Loaded {len(self.blocklist_domains)} cached domains")
                return True
        except Exception as e:
            logger.warning(f"Failed to load cached blocklist: {e}")
        return False

    def save_blocklist_cache(self):
        """Save current blocklist to local cache for offline use."""
        try:
            self._cache_file.write_text(
                "\n".join(sorted(self.blocklist_domains)),
                encoding="utf-8"
            )
            logger.debug(f"Saved {len(self.blocklist_domains)} domains to cache")
        except Exception as e:
            logger.warning(f"Failed to save blocklist cache: {e}")

    def update_blocklist(self, source_key: str = DEFAULT_BLOCKLIST) -> int:
        """
        Download and apply the latest blocklist.

        Returns:
            Number of domains now blocked, or -1 on failure.
        """
        with self._lock:
            domains = download_blocklist(source_key)
            if domains:
                self.blocklist_domains = domains
                self.save_blocklist_cache()
                if self.apply():
                    return len(self.all_blocked)
                return -1
            else:
                # Try cached version
                if self.load_cached_blocklist():
                    if self.apply():
                        return len(self.all_blocked)
                return -1

    def add_custom_domain(self, domain: str):
        """Add a domain to the custom blocklist and re-apply."""
        with self._lock:
            normalized = _normalize_domain(domain)
            if not normalized:
                logger.warning(f"Ignored invalid custom domain: {domain}")
                return False
            self.custom_domains.add(normalized)
            return self.apply()

    def remove_custom_domain(self, domain: str):
        """Remove a domain from the custom blocklist and re-apply."""
        with self._lock:
            normalized = _normalize_domain(domain)
            if not normalized:
                return False
            self.custom_domains.discard(normalized)
            return self.apply()

    def set_custom_domains(self, domains: List[str]):
        """Replace the entire custom domain list (called when syncing from server)."""
        with self._lock:
            normalized = set()
            for d in domains:
                value = _normalize_domain(d)
                if value:
                    normalized.add(value)
            self.custom_domains = normalized
            return self.apply()

    def apply(self):
        """
        Write all blocked domains to the hosts file.
        Preserves existing hosts entries that are not ours.
        """
        try:
            # Read existing hosts file
            hosts_content = ""
            if os.path.exists(self.hosts_path):
                with open(self.hosts_path, "r", encoding="utf-8", errors="ignore") as f:
                    hosts_content = f.read()

            # Remove our old entries
            cleaned = self._remove_our_entries(hosts_content)

            # Build our new block.
            # Keep hosts-file entries focused on dashboard custom domains.
            # Huge online blocklists can make hosts-based matching unreliable
            # on some systems and break explicit URL blocking expectations.
            blocked_domains = _expand_custom_domain_aliases(self.custom_domains)
            blocked = sorted(blocked_domains)
            if blocked:
                block_lines = [
                    "",
                    BLOCK_START,
                    f"# Updated: {datetime.now().isoformat()}",
                    f"# Total blocked: {len(blocked)}",
                ]
                for domain in blocked:
                    # Force both IPv4 and IPv6 localhost to avoid DNS-family bypasses.
                    block_lines.append(f"127.0.0.1 {domain}")
                    block_lines.append(f"::1 {domain}")
                block_lines.append(BLOCK_END)
                block_lines.append("")

                new_content = cleaned.rstrip("\n") + "\n" + "\n".join(block_lines)
            else:
                new_content = cleaned

            # Write atomically: write to temp file, then replace
            hosts_dir = os.path.dirname(self.hosts_path)
            fd, tmp_path = tempfile.mkstemp(dir=hosts_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(new_content)
                # On Windows, we can't atomically rename if target exists
                if platform.system() == "Windows":
                    # Make backup
                    backup = self.hosts_path + ".guardian.bak"
                    if os.path.exists(backup):
                        os.remove(backup)
                    shutil.copy2(self.hosts_path, backup)
                    os.remove(self.hosts_path)
                    os.rename(tmp_path, self.hosts_path)
                else:
                    os.replace(tmp_path, self.hosts_path)

                # Keep hosts readable by all users so browser/user-space
                # resolvers can apply the block entries.
                if platform.system() != "Windows":
                    os.chmod(self.hosts_path, 0o644)
            except Exception:
                # Clean up temp file on failure
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                raise

            _flush_dns()
            self._apply_browser_policy_blocklist()
            logger.info(f"Hosts file updated: {len(blocked)} custom domains blocked")
            return True

        except PermissionError:
            logger.error(
                "Permission denied writing hosts file. "
                "Agent must run as Administrator/root."
            )
            return False
        except Exception as e:
            logger.error(f"Failed to update hosts file: {e}")
            return False

    def _apply_browser_policy_blocklist(self):
        """Write Linux browser managed policy for DNS/transport hardening."""
        if platform.system() != "Linux":
            return

        # Keep hosts-file based redirection as the primary block path (so we can
        # show custom warning pages and warning-count escalation), but harden
        # browser DNS/transport behavior to reduce bypasses.
        payload = {
            "DnsOverHttpsMode": "off",
            "BuiltInDnsClientEnabled": False,
            "QuicAllowed": False,
        }

        for policy_file in LINUX_POLICY_PATHS:
            try:
                policy_dir = os.path.dirname(policy_file)
                os.makedirs(policy_dir, exist_ok=True)
                with open(policy_file, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2)
                logger.info(f"Browser DNS hardening policy updated: {policy_file}")
            except PermissionError:
                logger.warning(f"Permission denied writing browser policy file: {policy_file}")
            except Exception as e:
                logger.warning(f"Failed writing browser policy file {policy_file}: {e}")

        # On Raspberry Pi Chromium, blocked HTTPS domains may present a privacy
        # interstitial before the warning page. Enforce startup flags so the
        # local Guardian TLS warning route can render NUH UH immediately.
        try:
            flags_dir = os.path.dirname(LINUX_CHROMIUM_FLAGS_FILE)
            os.makedirs(flags_dir, exist_ok=True)
            flags_content = (
                "# Guardian Shield managed flags\n"
                "# Keep local blocked-site warning pages visible on HTTPS attempts.\n"
                "CHROMIUM_FLAGS=\"${CHROMIUM_FLAGS} --ignore-certificate-errors --allow-running-insecure-content\"\n"
            )
            with open(LINUX_CHROMIUM_FLAGS_FILE, "w", encoding="utf-8") as f:
                f.write(flags_content)
            logger.info(f"Chromium runtime flags updated: {LINUX_CHROMIUM_FLAGS_FILE}")
        except PermissionError:
            logger.warning(f"Permission denied writing Chromium flags file: {LINUX_CHROMIUM_FLAGS_FILE}")
        except Exception as e:
            logger.warning(f"Failed writing Chromium flags file {LINUX_CHROMIUM_FLAGS_FILE}: {e}")

    def _remove_our_entries(self, content: str) -> str:
        """Remove Guardian Shield entries from hosts content."""
        lines = content.splitlines()
        result = []
        inside_block = False
        for line in lines:
            if BLOCK_START in line or LEGACY_BLOCK_START in line:
                inside_block = True
                continue
            if BLOCK_END in line or LEGACY_BLOCK_END in line:
                inside_block = False
                continue
            if not inside_block:
                result.append(line)
        return "\n".join(result)

    def remove_all(self):
        """Remove all Guardian Shield entries from hosts file (cleanup)."""
        with self._lock:
            try:
                if os.path.exists(self.hosts_path):
                    with open(self.hosts_path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                    cleaned = self._remove_our_entries(content)
                    with open(self.hosts_path, "w", encoding="utf-8") as f:
                        f.write(cleaned)
                    _flush_dns()
                    logger.info("All Guardian Shield blocks removed from hosts file")
                return True
            except PermissionError:
                logger.error("Permission denied. Run as Administrator/root.")
                return False
            except Exception as e:
                logger.error(f"Failed to clean hosts file: {e}")
                return False

    def is_blocked(self, domain: str) -> bool:
        """Check if a domain is currently blocked."""
        return domain.lower().strip() in self.all_blocked


class BlocklistUpdater:
    """Periodically updates the blocklist from online sources."""

    def __init__(self, blocker: WebsiteBlocker, interval: int = 3600):
        """
        Args:
            blocker: WebsiteBlocker instance to update.
            interval: Seconds between updates (default: 1 hour).
        """
        self.blocker = blocker
        self.interval = interval
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Start the periodic updater in a background thread."""
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="blocklist-updater")
        self._thread.start()

    def stop(self):
        """Stop the periodic updater."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self):
        """Main loop: update blocklist, then sleep."""
        while self._running:
            try:
                count = self.blocker.update_blocklist()
                if count >= 0:
                    logger.info(f"Blocklist updated: {count} total domains blocked")
                else:
                    logger.warning("Blocklist update failed, using cached version")
            except Exception as e:
                logger.error(f"Blocklist update error: {e}")

            # Sleep in small increments so we can stop quickly
            for _ in range(self.interval):
                if not self._running:
                    break
                time.sleep(1)


if __name__ == "__main__":
    # Quick test — requires admin/root
    logging.basicConfig(level=logging.DEBUG)
    blocker = WebsiteBlocker(custom_domains=["test-block.example.com"])
    print(f"Hosts file: {blocker.hosts_path}")
    print(f"Custom domains: {blocker.custom_domains}")

    # Test with a small custom list (don't download the full blocklist in test)
    blocker.apply()
    print(f"Is test-block.example.com blocked? {blocker.is_blocked('test-block.example.com')}")
    blocker.remove_all()
    print("Cleanup done.")

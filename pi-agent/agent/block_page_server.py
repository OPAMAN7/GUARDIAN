"""
Guardian Shield — Block Page Server
Runs on localhost:80 and intercepts requests to blocked domains.
(The hosts file already redirects all blocked domains to 127.0.0.1.)

Features:
- Tracks how many times each blocked site was visited
- Warning 1: soft beep + yellow warning page
- Warning 2: medium beep + orange warning page
- Warning 3+: alarm beep + red full-block page
- Reports all block events to the parent dashboard via callback
"""

import json
import logging
import os
import platform
import re
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

logger = logging.getLogger("guardian.block_server")

AGENT_DIR   = Path(__file__).parent
COUNTS_FILE = AGENT_DIR / "warning_counts.json"
PAGE_FILE   = AGENT_DIR / "warning_page.html"
PAGE_LVL3_FILE = AGENT_DIR / "warning_page_level3.html"
CERT_FILE   = AGENT_DIR / "block_page_cert.pem"
KEY_FILE    = AGENT_DIR / "block_page_key.pem"
CA_CERT_FILE = AGENT_DIR / "guardian_local_ca.pem"
CA_KEY_FILE = AGENT_DIR / "guardian_local_ca_key.pem"
CERTS_DIR = AGENT_DIR / "tls-certs"
SYSTEM_CA_INSTALL_PATH = Path("/usr/local/share/ca-certificates/guardian-shield-local-ca.crt")

_lock = threading.Lock()
_recent_attempt_at = {}
ATTEMPT_DEBOUNCE_SECONDS = float(os.environ.get("GUARDIAN_BLOCK_DEBOUNCE_SECONDS", "1.8"))
_tls_context_lock = threading.Lock()
_tls_context_cache = {}


# ── Warning count persistence ────────────────────────────────────────────────

def _load_counts() -> dict:
    try:
        return json.loads(COUNTS_FILE.read_text())
    except Exception:
        return {}


def _save_counts(counts: dict):
    try:
        COUNTS_FILE.write_text(json.dumps(counts, indent=2))
    except Exception:
        pass


def _register_attempt(host: str) -> tuple[int, bool]:
    """Register a host attempt once and ignore immediate duplicate reloads."""
    with _lock:
        counts = _load_counts()
        now = time.time()

        prior_count = int(counts.get(host, 0) or 0)
        last_at = float(_recent_attempt_at.get(host, 0.0) or 0.0)
        if prior_count > 0 and (now - last_at) < ATTEMPT_DEBOUNCE_SECONDS:
            return prior_count, False

        new_count = prior_count + 1
        counts[host] = new_count
        _save_counts(counts)
        _recent_attempt_at[host] = now
        return new_count, True


def _notify_attempt(host: str, alert_callback=None) -> tuple[int, int, bool]:
    """Register an attempt and trigger buzzer/alert callback for new attempts only."""
    count, is_new_attempt = _register_attempt(host)
    level = min(count, 3)

    if is_new_attempt:
        try:
            from buzzer import play_warning
            play_warning(level)
        except Exception as e:
            logger.debug(f"Buzzer error: {e}")

        if alert_callback:
            try:
                alert_callback(host, count, level)
            except Exception:
                pass

    return count, level, is_new_attempt


def _ensure_https_cert_files() -> bool:
    """Create a local self-signed certificate for HTTPS warning page if needed."""
    try:
        if CERT_FILE.exists() and KEY_FILE.exists():
            return True

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Guardian Shield Local"),
            x509.NameAttribute(NameOID.COMMON_NAME, "Guardian Shield Local Block Page"),
        ])

        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.utcnow() - timedelta(days=1))
            .not_valid_after(datetime.utcnow() + timedelta(days=3650))
            .add_extension(
                x509.SubjectAlternativeName([
                    x509.DNSName("localhost"),
                    x509.DNSName("127.0.0.1"),
                ]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )

        KEY_FILE.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        logger.info("Created local HTTPS certificate for block page server")
        return True
    except Exception as e:
        logger.warning(f"Could not prepare HTTPS cert: {e}")
        return False


def _normalize_tls_host(raw_host: Optional[str]) -> str:
    host = (raw_host or "").strip().lower()
    host = host.replace("http://", "").replace("https://", "")
    host = host.split("/", 1)[0].split(":", 1)[0].strip(".")
    if not host:
        return "localhost"
    if not re.fullmatch(r"[a-z0-9.-]+", host):
        return "localhost"
    return host


def _ensure_local_ca_files() -> bool:
    """Create a long-lived local CA used to sign per-host TLS certs."""
    try:
        if CA_CERT_FILE.exists() and CA_KEY_FILE.exists():
            return True

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Guardian Shield Local"),
            x509.NameAttribute(NameOID.COMMON_NAME, "Guardian Shield Local Root CA"),
        ])

        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.utcnow() - timedelta(days=1))
            .not_valid_after(datetime.utcnow() + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .sign(key, hashes.SHA256())
        )

        CA_KEY_FILE.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        CA_CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        logger.info("Created local Guardian Root CA for HTTPS block pages")
        return True
    except Exception as e:
        logger.warning(f"Could not create local Guardian CA: {e}")
        return False


def _install_local_ca_in_system_trust() -> bool:
    """Install Guardian local CA into Linux system trust store."""
    if platform.system() != "Linux":
        return True

    if not _ensure_local_ca_files():
        return False

    try:
        SYSTEM_CA_INSTALL_PATH.parent.mkdir(parents=True, exist_ok=True)
        src = CA_CERT_FILE.read_bytes()
        if not SYSTEM_CA_INSTALL_PATH.exists() or SYSTEM_CA_INSTALL_PATH.read_bytes() != src:
            SYSTEM_CA_INSTALL_PATH.write_bytes(src)

        commands = [
            ["update-ca-certificates"],
            ["update-ca-trust", "extract"],
        ]
        for cmd in commands:
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True)
                if proc.returncode == 0:
                    logger.info("Installed Guardian local CA into system trust")
                    return True
            except FileNotFoundError:
                continue

        logger.warning("Could not refresh CA trust store automatically")
        return False
    except PermissionError:
        logger.warning("Permission denied installing Guardian local CA into system trust")
        return False
    except Exception as e:
        logger.warning(f"Failed installing Guardian local CA: {e}")
        return False


def _ensure_host_tls_cert_files(hostname: str) -> Optional[tuple[Path, Path]]:
    """Create/get cert+key for a specific hostname signed by Guardian local CA."""
    host = _normalize_tls_host(hostname)
    try:
        if not _ensure_local_ca_files():
            return None

        CERTS_DIR.mkdir(parents=True, exist_ok=True)
        cert_path = CERTS_DIR / f"{host}.pem"
        key_path = CERTS_DIR / f"{host}.key.pem"
        if cert_path.exists() and key_path.exists():
            return cert_path, key_path

        ca_cert = x509.load_pem_x509_certificate(CA_CERT_FILE.read_bytes())
        ca_key = serialization.load_pem_private_key(CA_KEY_FILE.read_bytes(), password=None)

        leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Guardian Shield Local"),
            x509.NameAttribute(NameOID.COMMON_NAME, host),
        ])

        san_names = [x509.DNSName(host)]
        if host != "localhost":
            san_names.append(x509.DNSName("localhost"))

        leaf_cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(ca_cert.subject)
            .public_key(leaf_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.utcnow() - timedelta(days=1))
            .not_valid_after(datetime.utcnow() + timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName(san_names), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
                critical=False,
            )
            .sign(private_key=ca_key, algorithm=hashes.SHA256())
        )

        key_path.write_bytes(
            leaf_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        cert_path.write_bytes(leaf_cert.public_bytes(serialization.Encoding.PEM))
        logger.info(f"Prepared host TLS cert for blocked domain: {host}")
        return cert_path, key_path
    except Exception as e:
        logger.warning(f"Could not create TLS cert for host {host}: {e}")
        return None


def _get_host_tls_context(hostname: str) -> Optional[ssl.SSLContext]:
    """Return a cached TLS context for hostname, creating one if needed."""
    host = _normalize_tls_host(hostname)
    with _tls_context_lock:
        existing = _tls_context_cache.get(host)
        if existing:
            return existing

        cert_pair = _ensure_host_tls_cert_files(host)
        if not cert_pair:
            return None

        cert_path, key_path = cert_pair
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
            _tls_context_cache[host] = ctx
            return ctx
        except Exception as e:
            logger.warning(f"Could not load TLS context for {host}: {e}")
            return None


# ── HTTP Handler ─────────────────────────────────────────────────────────────

class BlockPageHandler(BaseHTTPRequestHandler):

    def _render_block_page(self, host: str):
        count, level, _is_new_attempt = _notify_attempt(
            host,
            alert_callback=getattr(self.server, "alert_callback", None),
        )
        safe_url = getattr(self.server, "safe_url", "https://www.bing.com")

        # Serve warning HTML
        try:
            page_file = PAGE_LVL3_FILE if level >= 3 and PAGE_LVL3_FILE.exists() else PAGE_FILE
            html = page_file.read_text(encoding="utf-8")
            html = html.replace("__HOST__", host)
            html = html.replace("__COUNT__", str(count))
            html = html.replace("__SAFE_URL_JSON__", json.dumps(safe_url))
        except Exception:
            html = f"<h1>BLOCKED</h1><p>{host} is not allowed. Warning #{count}</p>"

        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path or "/")

        # Stable local landing page after NUH UH timeout.
        if parsed.path == "/__guardian_safe":
            html = (
                "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                "<title>Guardian Safe Page</title>"
                "<style>body{font-family:Segoe UI,Arial,sans-serif;background:#0b1020;color:#d9e6ff;"
                "display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}"
                ".card{max-width:680px;width:92%;padding:28px;border-radius:16px;"
                "border:1px solid #2b3d73;background:linear-gradient(160deg,#111a33,#0e1530)}"
                "h1{margin:0 0 8px;color:#9ed0ff}p{margin:0 0 16px;line-height:1.5}"
                "a{display:inline-block;padding:10px 16px;border-radius:999px;background:#2f7cff;"
                "color:white;text-decoration:none;font-weight:700}</style></head>"
                "<body><div class='card'><h1>Guardian Active</h1>"
                "<p>This site is blocked. You are now on a safe local page.</p>"
                "<a href='http://127.0.0.1:3000'>Open Dashboard</a>"
                "</div></body></html>"
            )
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # Forced local trigger: /__guardian_block?host=example.com
        if parsed.path == "/__guardian_block":
            q = parse_qs(parsed.query or "")
            forced_host = (q.get("host", ["unknown-site"])[0] or "unknown-site").strip().lower()
            forced_host = forced_host.replace("http://", "").replace("https://", "").split("/")[0].split(":")[0]
            if forced_host in ("", "localhost", "127.0.0.1", "::1"):
                forced_host = "blocked-site"
            self._render_block_page(forced_host)
            return

        host = (self.headers.get("Host") or "unknown-site").split(":")[0]

        # Ignore browser favicon / internal requests
        if host in ("localhost", "127.0.0.1", "") or self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        self._render_block_page(host)

    def do_HEAD(self):
        self.do_GET()

    def log_message(self, fmt, *args):
        pass  # silence access logs


# ── Server launcher ──────────────────────────────────────────────────────────

def start_block_server(port: int = 80, alert_callback=None, safe_url: str = "https://www.bing.com"):
    """
    Start the block page server in a background daemon thread.
    Falls back to port 8080 if port 80 requires root/admin privileges.

    Args:
        port: Preferred port (80 captures HTTP without changing hosts file).
        alert_callback: Callable(host, count, level) for dashboard integration.
        safe_url: URL used by "Back to Safety" button on the warning page.

    Returns:
        The running HTTPServer instance, or None on failure.
    """
    primary_server = None
    for p in ([port] if port != 80 else [80, 8080, 9191]):
        try:
            server = HTTPServer(("", p), BlockPageHandler)
            server.alert_callback = alert_callback
            server.safe_url = safe_url
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            logger.info(f"Block page server listening on :{p}")
            primary_server = server
            break
        except OSError as e:
            logger.warning(f"Cannot bind :{p} — {e}")
        except Exception as e:
            logger.error(f"Block server error on :{p} — {e}")

    # Also try HTTPS for blocked HTTPS links (e.g. YouTube).
    if _ensure_https_cert_files():
        _install_local_ca_in_system_trust()
        for tls_port in [443, 8443, 9443]:
            try:
                tls_server = HTTPServer(("", tls_port), BlockPageHandler)
                tls_server.alert_callback = alert_callback
                tls_server.safe_url = safe_url

                context = _get_host_tls_context("localhost")
                if context is None:
                    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                    context.load_cert_chain(certfile=str(CERT_FILE), keyfile=str(KEY_FILE))

                def _sni_callback(ssl_sock, server_name, _initial_ctx):
                    attempted_host = _normalize_tls_host(server_name or "localhost")

                    # Count/notify at TLS handshake time so blocked HTTPS attempts
                    # are recorded even if browser halts on cert interstitial.
                    _notify_attempt(
                        attempted_host,
                        alert_callback=getattr(tls_server, "alert_callback", None),
                    )

                    host_ctx = _get_host_tls_context(attempted_host)
                    if host_ctx is not None:
                        ssl_sock.context = host_ctx

                context.set_servername_callback(_sni_callback)
                tls_server.socket = context.wrap_socket(tls_server.socket, server_side=True)

                t = threading.Thread(target=tls_server.serve_forever, daemon=True)
                t.start()
                logger.info(f"Block page HTTPS server listening on :{tls_port}")
                break
            except OSError as e:
                logger.warning(f"Cannot bind TLS :{tls_port} — {e}")
            except Exception as e:
                logger.warning(f"HTTPS block server error on :{tls_port} — {e}")

    if not primary_server:
        logger.error("Block page server could not start on any HTTP port.")
    return primary_server


def reset_warnings(host: str = None):
    """Reset warning count — for a specific host or all hosts."""
    with _lock:
        if host:
            c = _load_counts()
            c.pop(host, None)
            _save_counts(c)
        else:
            _save_counts({})


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Starting block page test server on :8080 ...")
    start_block_server(port=8080)
    import time
    time.sleep(60)

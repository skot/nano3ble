"""Bypass the Nano3s QR-code login and open the web dashboard.

The miner's web UI requires scanning a QR code with the Avalon Family
app.  This module reproduces the auth handshake directly:

1. Fetch the auth token from ``/get_auth.cgi``.
2. Open the login page in the default browser.
3. Trigger ``qr_auth`` via the cgminer API (TCP port 4028).
4. The login page's ``is_login.cgi`` poll detects the auth and
   redirects to the dashboard automatically.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import socket
import time
import webbrowser
from urllib.request import urlopen

log = logging.getLogger(__name__)

DEFAULT_PASSWORD = "admin"
CGMINER_API_PORT = 4028
AUTH_MAGIC = "ff0000ee"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def fetch_auth_token(ip: str, timeout: float = 5.0) -> tuple[str, str]:
    """Fetch the auth/code pair from the miner's ``get_auth.cgi``.

    Returns:
        ``(auth, code)`` — the 8-char random token and the DNA code.

    Raises:
        RuntimeError: If the endpoint is unreachable or the response is
                      malformed.
    """
    url = f"http://{ip}/get_auth.cgi"
    try:
        resp = urlopen(url, timeout=timeout).read().decode()
    except Exception as exc:
        raise RuntimeError(f"Cannot reach {url}: {exc}") from exc

    m_auth = re.search(r'"auth"\s*:\s*"([^"]+)"', resp)
    m_code = re.search(r'"code"\s*:\s*"([^"]+)"', resp)
    if not m_auth or not m_code:
        raise RuntimeError(f"Unexpected get_auth.cgi response: {resp[:200]}")

    return m_auth.group(1).strip(), m_code.group(1).strip()


def _build_verify(code: str, password: str) -> str:
    """Compute the QR-auth verify string.

    ``verify = "ff0000ee" + SHA256(code + SHA256(password)[:24])[:24]``
    """
    webpass_24 = _sha256(password)[:24]
    verify_hash = _sha256(code + webpass_24)[:24]
    return AUTH_MAGIC + verify_hash


def trigger_qr_auth(
    ip: str,
    auth: str,
    verify: str,
    port: int = CGMINER_API_PORT,
    timeout: float = 5.0,
) -> str:
    """Send ``ascset 0,qr_auth`` to the cgminer API.

    Returns:
        The raw API response string.

    Raises:
        RuntimeError: On connection or auth failure.
    """
    cmd = json.dumps({
        "command": "ascset",
        "parameter": f"0,qr_auth,{auth},{verify}",
    })
    log.debug("cgminer API → %s", cmd)

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((ip, port))
        s.sendall(cmd.encode())
        chunks: list[bytes] = []
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
        s.close()
    except Exception as exc:
        raise RuntimeError(
            f"Cannot reach cgminer API at {ip}:{port}: {exc}"
        ) from exc

    result = b"".join(chunks).decode(errors="replace")
    log.debug("cgminer API ← %s", result[:200])
    return result


def open_dashboard(
    ip: str,
    password: str = DEFAULT_PASSWORD,
    browser_delay: float = 4.0,
) -> bool:
    """Open the Nano3s dashboard in the default browser.

    Args:
        ip:            Miner IP address.
        password:      Web password (default ``"admin"``).
        browser_delay: Seconds to wait after opening the browser before
                       triggering auth (gives the login page time to
                       start its ``is_login.cgi`` polling loop).

    Returns:
        *True* if the auth was accepted, *False* otherwise.
    """
    # 1 — Fetch auth token
    print(f"Connecting to {ip}…")
    auth, code = fetch_auth_token(ip)
    log.debug("auth=%s  code=%s", auth, code)

    # 2 — Open the login page FIRST so it starts polling
    url = f"http://{ip}/"
    print(f"Opening {url} in your browser…")
    webbrowser.open(url)

    # 3 — Wait for the page to load and begin polling is_login.cgi
    print("Waiting for page to load", end="", flush=True)
    for _ in range(int(browser_delay)):
        time.sleep(1)
        print(".", end="", flush=True)
    time.sleep(browser_delay - int(browser_delay))
    print()

    # 4 — Trigger qr_auth via the cgminer JSON API
    verify = _build_verify(code, password)
    result = trigger_qr_auth(ip, auth, verify)

    if "set OK" in result:
        print("Auth accepted — the dashboard should appear in your browser.")
        return True

    # Auth failed — offer a manual fallback
    print(f"Auth failed: {result[:200]}")
    webpass_24 = _sha256(password)[:24]
    cookie = auth + webpass_24
    print()
    print("Fallback — paste this in your browser console:")
    print(f"  document.cookie='auth={cookie};path=/'; location='/dashboard.cgi';")
    return False

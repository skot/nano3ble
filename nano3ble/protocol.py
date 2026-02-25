"""
BLE GATT protocol constants and helpers for Avalon Nano3s.

The Nano3s exposes a custom GATT service (UUID 0xFFFF) with three
characteristics used for WiFi provisioning:

  FFE1 (read)   —  WiFi scan results
  FFE2 (write)  —  Command input
  FFE3 (read)   —  Device status (JSON)

Commands are ASCII strings terminated with the four-byte marker ``$$$$``.
SSID, password, and identity fields are hex-encoded UTF-8.

The firmware accumulates BLE writes in a 512-byte buffer and only
processes the command once the ``$$$$`` terminator is received.  Writes
must therefore be chunked to fit within the negotiated ATT MTU (typically
20 bytes on BLE 4.0 devices).
"""

from __future__ import annotations

import json

# ---------------------------------------------------------------------------
# GATT UUIDs (16-bit Bluetooth SIG base)
# ---------------------------------------------------------------------------
SERVICE_UUID = "0000ffff-0000-1000-8000-00805f9b34fb"
CHAR_WIFI_SCAN = "0000ffe1-0000-1000-8000-00805f9b34fb"   # read: scan results
CHAR_COMMAND = "0000ffe2-0000-1000-8000-00805f9b34fb"      # write: commands
CHAR_STATUS = "0000ffe3-0000-1000-8000-00805f9b34fb"       # read: JSON status

# ---------------------------------------------------------------------------
# Device discovery
# ---------------------------------------------------------------------------
DEVICE_NAME_PREFIX = "nan3s"  # advertised as "nan3s_XXXX"

# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------
TERMINATOR = b"$$$$"
MAX_WRITE_BUF = 512  # firmware-side accumulation buffer
DEFAULT_CHUNK = 20   # safe for BLE 4.0 (ATT MTU 23 − 3 = 20)

# ---------------------------------------------------------------------------
# WiFi auth types (as reported by the firmware scan)
# ---------------------------------------------------------------------------
AUTH_OPEN = 1
AUTH_WPA_PSK = 5
AUTH_WPA_EAP = 6
AUTH_WPA2_PSK = 7
AUTH_WPA2_EAP = 8

AUTH_NAMES: dict[int, str] = {
    1: "Open",
    2: "Open",
    3: "Open",
    4: "802.1X",
    AUTH_WPA_PSK: "WPA-PSK",
    AUTH_WPA_EAP: "WPA-EAP",
    AUTH_WPA2_PSK: "WPA2-PSK",
    AUTH_WPA2_EAP: "WPA2-EAP",
}

# ---------------------------------------------------------------------------
# Connection state (reported in the status JSON ``conn_state`` field)
# ---------------------------------------------------------------------------
CONN_DISCONNECTED = 0
CONN_CONNECTING = 1
CONN_CONNECTED = 2
CONN_FAILED = 3

CONN_STATE_NAMES: dict[int, str] = {
    CONN_DISCONNECTED: "Disconnected",
    CONN_CONNECTING: "Connecting",
    CONN_CONNECTED: "Connected",
    CONN_FAILED: "Failed",
}


# ---------------------------------------------------------------------------
# Encoding helpers
# ---------------------------------------------------------------------------
def _str_to_hex(s: str) -> str:
    """Hex-encode a UTF-8 string (as the firmware expects)."""
    return s.encode("utf-8").hex()


def _hex_to_str(h: str) -> str:
    """Decode a hex-encoded string back to UTF-8."""
    return bytes.fromhex(h).decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Command builders
# ---------------------------------------------------------------------------
def build_scan_command() -> bytes:
    """Return the ``scan$$$$`` command payload."""
    return b"scan" + TERMINATOR


def build_setssid_command(
    ssid: str,
    password: str,
    auth: int = AUTH_WPA2_PSK,
    identity: str = "none",
) -> bytes:
    """Build a ``setssid`` command to configure WiFi credentials.

    Wire format (all string fields are hex-encoded UTF-8)::

        setssid,<hex_ssid>,<hex_password>,<hex_identity>,<auth>$$$$

    The second field (password) is placed at struct offset 0x84 in the
    firmware, which ``wifi_connect()`` passes to ``wpa_cli set_network``
    as the WPA-PSK passphrase.  The third field (identity) goes to
    offset 0x104 and is only used for EAP authentication.

    Args:
        ssid:     WiFi network name.
        password: WiFi password / passphrase.
        auth:     Authentication type (default 7 = WPA2-PSK).
                  Use the value reported by ``scan-wifi``.
        identity: EAP identity.  Use ``"none"`` for WPA-PSK networks.
    """
    payload = (
        f"setssid,"
        f"{_str_to_hex(ssid)},"
        f"{_str_to_hex(password)},"
        f"{_str_to_hex(identity)},"
        f"{auth}"
    )
    return payload.encode("ascii") + TERMINATOR


# ---------------------------------------------------------------------------
# Response parsers
# ---------------------------------------------------------------------------
def parse_scan_results(raw: bytes) -> list[dict[str, object]]:
    """Parse WiFi scan results read from FFE1.

    The characteristic holds a comma-separated list of triplets::

        <hex_ssid>,<auth>,<rssi>,<hex_ssid>,<auth>,<rssi>,…

    Returns a list of dicts with keys ``ssid``, ``auth``, ``rssi``.
    """
    text = raw.decode("ascii", errors="replace").rstrip("\x00")
    if not text:
        return []

    parts = text.split(",")
    networks: list[dict[str, object]] = []
    i = 0
    while i + 2 < len(parts):
        try:
            ssid = _hex_to_str(parts[i].strip())
            auth = int(parts[i + 1].strip())
            rssi = int(parts[i + 2].strip())
            networks.append({"ssid": ssid, "auth": auth, "rssi": rssi})
        except (ValueError, IndexError):
            break
        i += 3
    return networks


def parse_status(raw: bytes) -> dict[str, object]:
    """Parse the JSON status blob read from FFE3.

    Expected shape::

        {"device":"nan3s_xxxx","ip":"…","ssid":"…","auth":N,"conn_state":N}
    """
    text = raw.decode("ascii", errors="replace").rstrip("\x00")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}

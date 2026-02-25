"""High-level BLE client for the Avalon Nano3s miner.

Typical usage::

    async with await Nano3s.connect() as miner:
        status = await miner.get_status()
        print(status)
        await miner.set_wifi("MySSID", "hunter2")
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from .protocol import (
    AUTH_NAMES,
    CHAR_COMMAND,
    CHAR_STATUS,
    CHAR_WIFI_SCAN,
    CONN_CONNECTED,
    CONN_FAILED,
    CONN_STATE_NAMES,
    DEFAULT_CHUNK,
    DEVICE_NAME_PREFIX,
    MAX_WRITE_BUF,
    build_scan_command,
    build_setssid_command,
    parse_scan_results,
    parse_status,
)

log = logging.getLogger(__name__)

# ── Data classes ──────────────────────────────────────────────────────────


@dataclass
class WifiNetwork:
    """A single WiFi network seen by the miner."""

    ssid: str
    auth: int
    rssi: int

    @property
    def auth_name(self) -> str:
        return AUTH_NAMES.get(self.auth, f"Unknown({self.auth})")

    def __str__(self) -> str:
        return f"{self.ssid:30s}  {self.auth_name:<10s}  {self.rssi}dBm"


@dataclass
class DeviceStatus:
    """Parsed status read from the FFE3 characteristic."""

    device: str
    ip: str
    ssid: str
    auth: int
    conn_state: int
    raw: dict

    @property
    def conn_state_name(self) -> str:
        return CONN_STATE_NAMES.get(
            self.conn_state, f"Unknown({self.conn_state})"
        )

    @property
    def is_connected(self) -> bool:
        return self.conn_state == CONN_CONNECTED

    def __str__(self) -> str:
        return (
            f"Device:  {self.device}\n"
            f"IP:      {self.ip or '(none)'}\n"
            f"SSID:    {self.ssid or '(none)'}\n"
            f"Auth:    {AUTH_NAMES.get(self.auth, self.auth)}\n"
            f"State:   {self.conn_state_name}"
        )


# ── Discovery ────────────────────────────────────────────────────────────


async def scan_devices(timeout: float = 10.0) -> list[BLEDevice]:
    """Return all nearby Avalon Nano3s BLE peripherals.

    The miner advertises as ``nan3s_XXXX`` where XXXX varies per unit.
    """
    log.info("Scanning for Nano3s devices (%.0fs timeout)…", timeout)
    all_devices = await BleakScanner.discover(timeout=timeout)

    matched: list[BLEDevice] = []
    for d in all_devices:
        if d.name and DEVICE_NAME_PREFIX in d.name.lower():
            matched.append(d)
    return matched


# ── Client ───────────────────────────────────────────────────────────────


class Nano3s:
    """BLE GATT client for an Avalon Nano3s miner.

    Use the async context manager returned by :meth:`connect`::

        async with await Nano3s.connect() as miner:
            …
    """

    def __init__(self, client: BleakClient, device: BLEDevice) -> None:
        self._client = client
        self._device = device

    # ── Construction / teardown ───────────────────────────────────────

    @classmethod
    async def connect(
        cls,
        device_name: str | None = None,
        timeout: float = 10.0,
    ) -> Nano3s:
        """Discover and connect to a Nano3s.

        Args:
            device_name: Optional name filter (e.g. ``"nan3s_aff2"``).
                         When *None*, the first matching device is used.
            timeout:     BLE scan and connection timeout in seconds.

        Raises:
            RuntimeError: If no device is found.
        """
        devices = await scan_devices(timeout=timeout)
        if not devices:
            raise RuntimeError(
                "No Nano3s found.  Is the miner powered on?  "
                "BLE advertising stops ~90 s after WiFi connects."
            )
        target = devices[0]
        if device_name:
            for d in devices:
                if d.name and device_name.lower() in d.name.lower():
                    target = d
                    break
            else:
                raise RuntimeError(
                    f"Device {device_name!r} not found among "
                    f"{[d.name for d in devices]}"
                )

        log.info("Connecting to %s (%s)…", target.name, target.address)
        client = BleakClient(target, timeout=timeout)
        await client.connect()
        log.info("Connected.")
        return cls(client, target)

    async def disconnect(self) -> None:
        if self._client.is_connected:
            await self._client.disconnect()
            log.info("Disconnected.")

    async def __aenter__(self) -> Nano3s:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.disconnect()

    # ── Properties ────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return self._device.name or "(unknown)"

    @property
    def address(self) -> str:
        return self._device.address

    # ── Low-level I/O ─────────────────────────────────────────────────

    async def _write_command(self, data: bytes) -> None:
        """Write *data* to FFE2, chunked to fit the ATT MTU.

        The firmware buffers incoming writes and only processes the
        accumulated payload once the ``$$$$`` terminator is received.
        Writes that exceed the ATT MTU are rejected by the Bluetooth
        stack, so we split into ≤20-byte chunks.
        """
        if len(data) > MAX_WRITE_BUF:
            raise ValueError(
                f"Payload too large ({len(data)} bytes, max {MAX_WRITE_BUF})"
            )

        chunk_size = DEFAULT_CHUNK
        mtu = getattr(self._client, "mtu_size", None)
        if mtu and mtu > 3:
            chunk_size = max(mtu - 3, DEFAULT_CHUNK)

        offset = 0
        while offset < len(data):
            chunk = data[offset : offset + chunk_size]
            try:
                await self._client.write_gatt_char(
                    CHAR_COMMAND, chunk, response=True
                )
            except Exception:
                # Fall back to Write Without Response
                await self._client.write_gatt_char(
                    CHAR_COMMAND, chunk, response=False
                )
            offset += chunk_size
            if offset < len(data):
                await asyncio.sleep(0.05)

        log.debug("Wrote %d bytes in %d-byte chunks.", len(data), chunk_size)

    async def _read_char(self, uuid: str) -> bytes:
        return await self._client.read_gatt_char(uuid)

    # ── High-level operations ─────────────────────────────────────────

    async def get_status(self) -> DeviceStatus:
        """Read the current device status from FFE3."""
        raw = await self._read_char(CHAR_STATUS)
        d = parse_status(raw)
        return DeviceStatus(
            device=d.get("device", ""),
            ip=d.get("ip", ""),
            ssid=d.get("ssid", ""),
            auth=d.get("auth", 0),
            conn_state=d.get("conn_state", 0),
            raw=d,
        )

    async def scan_wifi(self, wait: float = 8.0) -> list[WifiNetwork]:
        """Trigger a WiFi scan on the miner and return the results.

        The firmware needs several seconds to complete the scan.
        *wait* controls how long to pause before reading the results.
        """
        log.info("Triggering WiFi scan…")
        await self._write_command(build_scan_command())
        log.info("Waiting %.0fs for results…", wait)
        await asyncio.sleep(wait)
        return await self.get_wifi_list()

    async def get_wifi_list(self) -> list[WifiNetwork]:
        """Read the most recent WiFi scan results (no new scan)."""
        raw = await self._read_char(CHAR_WIFI_SCAN)
        return [WifiNetwork(**r) for r in parse_scan_results(raw)]

    async def set_wifi(
        self,
        ssid: str,
        password: str,
        auth: int = 7,
        identity: str = "none",
        poll_timeout: float = 60.0,
    ) -> DeviceStatus:
        """Send WiFi credentials and wait for the connection result.

        Args:
            ssid:         Network name.
            password:     WPA passphrase.
            auth:         Auth type (default 7 = WPA2-PSK).
            identity:     EAP identity (``"none"`` for WPA-PSK).
            poll_timeout: Max seconds to wait for a connection result.

        Returns:
            Final :class:`DeviceStatus`.
        """
        cmd = build_setssid_command(ssid, password, auth=auth, identity=identity)
        log.info("Sending WiFi credentials (SSID=%r)…", ssid)
        await self._write_command(cmd)

        log.info("Waiting for connection (up to %.0fs)…", poll_timeout)
        elapsed = 0.0
        interval = 2.0
        while elapsed < poll_timeout:
            await asyncio.sleep(interval)
            elapsed += interval
            status = await self.get_status()
            log.info(
                "  [%2.0fs] %s  ip=%s",
                elapsed, status.conn_state_name, status.ip or "—",
            )
            if status.conn_state in (CONN_CONNECTED, CONN_FAILED):
                return status

        return await self.get_status()

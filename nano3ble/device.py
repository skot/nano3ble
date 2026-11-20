"""High-level BLE client for the Avalon Nano3s miner.

Typical usage::

    async with await Nano3s.connect() as miner:
        status = await miner.get_status()
        print(status)
        await miner.set_wifi("MySSID", "hunter2")
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from .protocol import (
    AUTH_NAMES,
    AUTH_WPA2_PSK,
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
    decode_status_ssid,
    parse_scan_results,
    parse_status,
)

log = logging.getLogger(__name__)
DEFAULT_TIMEOUT = 30.0

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

    @property
    def has_ip(self) -> bool:
        try:
            return not ipaddress.ip_address(self.ip).is_unspecified
        except ValueError:
            return False

    def __str__(self) -> str:
        return (
            f"Device:  {self.device}\n"
            f"IP:      {self.ip or '(none)'}\n"
            f"SSID:    {self.ssid or '(none)'}\n"
            f"Auth:    {AUTH_NAMES.get(self.auth, self.auth)}\n"
            f"State:   {self.conn_state_name}"
        )


# ── Discovery ────────────────────────────────────────────────────────────


def _advertised_name(device: BLEDevice, advertisement: AdvertisementData) -> str:
    return (advertisement.local_name or device.name or "").lower()


async def scan_devices(timeout: float = DEFAULT_TIMEOUT) -> list[BLEDevice]:
    """Return all nearby Avalon Nano3s BLE peripherals.

    The miner advertises as ``nan3s_XXXX`` where XXXX varies per unit.
    """
    log.info("Scanning for Nano3s devices (%.0fs timeout)…", timeout)
    advertisements = await BleakScanner.discover(timeout=timeout, return_adv=True)
    return [
        device for device, advertisement in advertisements.values()
        if DEVICE_NAME_PREFIX in _advertised_name(device, advertisement)
    ]


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
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Nano3s:
        """Discover and connect to a Nano3s.

        Args:
            device_name: Optional name filter (e.g. ``"nan3s_aff2"``).
                         When *None*, the first matching device is used.
            timeout:     BLE scan and connection timeout in seconds.

        Raises:
            RuntimeError: If no device is found.
        """
        name_filter = (device_name or "").lower()

        def matches(device: BLEDevice, advertisement: AdvertisementData) -> bool:
            name = _advertised_name(device, advertisement)
            return DEVICE_NAME_PREFIX in name and name_filter in name

        log.info("Looking for %s (%.0fs timeout)…", device_name or "a Nano3s", timeout)
        target = await BleakScanner.find_device_by_filter(matches, timeout=timeout)
        if target is None:
            raise RuntimeError(
                f"No advertisement found for {device_name or 'a Nano3s'}. "
                "Make sure the miner is powered on and in Wi-Fi setup mode."
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
        Use the write mode advertised by the characteristic. Retrying a
        failed write in another mode could resend bytes already received.
        """
        if len(data) > MAX_WRITE_BUF:
            raise ValueError(
                f"Payload too large ({len(data)} bytes, max {MAX_WRITE_BUF})"
            )

        characteristic = self._client.services.get_characteristic(CHAR_COMMAND)
        if characteristic is None:
            raise RuntimeError("The miner does not expose the Wi-Fi command characteristic.")
        if "write" in characteristic.properties:
            response = True
            mtu = self._client.mtu_size
            chunk_size = max(mtu - 3, DEFAULT_CHUNK)
        elif "write-without-response" in characteristic.properties:
            response = False
            chunk_size = characteristic.max_write_without_response_size
        else:
            raise RuntimeError("The miner's Wi-Fi command characteristic is read-only.")

        for offset in range(0, len(data), chunk_size):
            chunk = data[offset : offset + chunk_size]
            await self._client.write_gatt_char(characteristic, chunk, response=response)
            if offset + chunk_size < len(data):
                await asyncio.sleep(0.05)

        log.debug("Wrote %d bytes in %d-byte chunks.", len(data), chunk_size)

    async def _read_char(self, uuid: str) -> bytes:
        return await self._client.read_gatt_char(uuid)

    # ── High-level operations ─────────────────────────────────────────

    async def get_status(self) -> DeviceStatus:
        """Read the current device status from FFE3."""
        raw = await self._read_char(CHAR_STATUS)
        d = parse_status(raw)
        device = d.get("device", "")
        ip = d.get("ip", "")
        ssid = d.get("ssid", "")
        auth = d.get("auth", 0)
        state = d.get("conn_state")
        if (
            not all(isinstance(value, str) for value in (device, ip, ssid))
            or type(auth) is not int
            or type(state) is not int
        ):
            raise RuntimeError("The miner returned an invalid Wi-Fi status response.")
        return DeviceStatus(
            device=device,
            ip=ip,
            ssid=decode_status_ssid(ssid),
            auth=auth,
            conn_state=state,
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
        auth: int = AUTH_WPA2_PSK,
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
            if status.conn_state == CONN_FAILED or (status.is_connected and status.has_ip):
                return status

        return await self.get_status()
